import argparse
import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from database import (
    claim_telegram_collection_start,
    connect_database,
    get_database_counts,
    get_telegram_collection_cursor,
    get_telegram_retry_after,
    initialize_database,
    save_parsed_job,
    save_telegram_collection_cursor,
    save_telegram_retry_after,
)
from parse_telegram_job_digest import (
    SESSION_PATH,
    load_settings,
    parse_telegram_job_digest_message,
)
from process_lock import (
    AlreadyRunningError,
    interprocess_lock,
)
from refresh_progress import write_refresh_progress
from source_parsing import (
    JobParserRegistry,
    SourceContext,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTION_LOCK_PATH = (
    PROJECT_ROOT
    / "data"
    / "telegram_collection.lock"
)
MAX_ALLOWED_GROUPS = 10
HISTORY_REQUEST_WAIT_SECONDS = 1.0
TELEGRAM_COLLECTION_MIN_INTERVAL_SECONDS = 5 * 60
MAX_INCREMENTAL_BATCHES_PER_RUN = 5


@dataclass(frozen=True, slots=True)
class TelegramCollectionSettings:
    api_id: int
    api_hash: str
    phone: str
    group_identifiers: tuple[str, ...]
    message_limit: int


@dataclass(frozen=True, slots=True)
class ChannelCollectionSummary:
    group_identifier: str
    group_name: str
    messages_checked: int
    messages_parsed: int
    new_jobs: int
    new_postings: int
    existing_postings: int
    previous_cursor: int | None
    current_cursor: int | None
    safe_limit_reached: bool = False


class TelegramBackoffActiveError(RuntimeError):
    """Raised before connecting while a flood-wait is active."""

    def __init__(self, seconds_remaining: int) -> None:
        super().__init__(
            "Telegram rate-limit backoff is active for "
            f"another {seconds_remaining} seconds."
        )
        self.seconds_remaining = seconds_remaining


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally collect allowlisted Telegram "
            "job messages."
        )
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Require an existing authorized session and never "
            "prompt for a login code. Intended for automation."
        ),
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        help=(
            "Write aggregate source counts to this local JSON "
            "file. Source identities and message contents are "
            "never included."
        ),
    )
    return parser.parse_args()


def _normalize_group_identifier(value: str) -> str:
    normalized = value.strip().lstrip("@")

    if not normalized:
        raise RuntimeError(
            "Telegram group identifiers must not be empty."
        )

    return normalized


def load_collection_settings() -> TelegramCollectionSettings:
    """
    Load credentials and the explicit Telegram group allowlist.

    TELEGRAM_CHANNEL_USERNAMES is optional. When present, it is a
    comma-separated allowlist; otherwise the existing single-channel
    TELEGRAM_CHANNEL_USERNAME setting remains supported.
    """

    (
        api_id,
        api_hash,
        phone,
        fallback_group,
        message_limit,
    ) = load_settings()

    configured_groups = os.getenv(
        "TELEGRAM_CHANNEL_USERNAMES",
        "",
    )

    raw_groups = (
        configured_groups.split(",")
        if configured_groups.strip()
        else [fallback_group]
    )

    group_identifiers: list[str] = []
    seen_groups: set[str] = set()

    for raw_group in raw_groups:
        group = _normalize_group_identifier(raw_group)
        group_key = group.casefold()

        if group_key in seen_groups:
            continue

        seen_groups.add(group_key)
        group_identifiers.append(group)

    if len(group_identifiers) > MAX_ALLOWED_GROUPS:
        raise RuntimeError(
            "TELEGRAM_CHANNEL_USERNAMES may contain at most "
            f"{MAX_ALLOWED_GROUPS} groups."
        )

    return TelegramCollectionSettings(
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        group_identifiers=tuple(group_identifiers),
        message_limit=message_limit,
    )


def create_telegram_client(
    settings: TelegramCollectionSettings,
) -> TelegramClient:
    """
    Create a bounded read-only client.

    Incoming update processing, catch-up, automatic reconnect loops,
    and automatic flood-wait sleeping are disabled. The collector
    makes only explicit entity and history requests.
    """

    return TelegramClient(
        str(SESSION_PATH),
        settings.api_id,
        settings.api_hash,
        request_retries=1,
        connection_retries=1,
        retry_delay=2,
        auto_reconnect=False,
        flood_sleep_threshold=0,
        raise_last_call_error=True,
        receive_updates=False,
        catch_up=False,
    )


async def _read_messages(
    telegram_client: Any,
    channel: Any,
    *,
    previous_cursor: int | None,
    message_limit: int,
) -> list[Any]:
    request_limit = message_limit

    if previous_cursor is not None:
        request_limit *= MAX_INCREMENTAL_BATCHES_PER_RUN

    iterator_options: dict[str, Any] = {
        "limit": request_limit,
        "wait_time": HISTORY_REQUEST_WAIT_SECONDS,
    }

    if previous_cursor is not None:
        iterator_options.update(
            {
                "min_id": previous_cursor,
                "reverse": True,
            }
        )

    messages = [
        message
        async for message in telegram_client.iter_messages(
            channel,
            **iterator_options,
        )
    ]

    return sorted(
        messages,
        key=lambda message: int(message.id),
    )


async def collect_channel(
    telegram_client: Any,
    database: sqlite3.Connection,
    parser_registry: JobParserRegistry,
    *,
    group_identifier: str,
    message_limit: int,
    progress_callback: (
        Callable[[int, int], None] | None
    ) = None,
) -> ChannelCollectionSummary:
    """Collect one allowlisted group and atomically advance its cursor."""

    channel = await telegram_client.get_entity(
        group_identifier
    )
    group_name = str(
        getattr(
            channel,
            "title",
            group_identifier,
        )
    )
    previous_cursor = get_telegram_collection_cursor(
        database,
        group_identifier,
    )
    messages = await _read_messages(
        telegram_client,
        channel,
        previous_cursor=previous_cursor,
        message_limit=message_limit,
    )

    source_context = SourceContext(
        source="telegram",
        group_name=group_name,
        group_identifier=group_identifier,
    )

    parsed_count = 0
    new_jobs_count = 0
    new_postings_count = 0
    existing_postings_count = 0
    message_count = len(messages)
    incremental_limit = (
        message_limit * MAX_INCREMENTAL_BATCHES_PER_RUN
    )
    safe_limit_reached = (
        previous_cursor is not None
        and message_count >= incremental_limit
    )

    if progress_callback is not None:
        progress_callback(0, message_count)

    with database:
        for message_index, message in enumerate(
            messages,
            start=1,
        ):
            parsed_job = parser_registry.parse(
                message=message,
                context=source_context,
            )

            if parsed_job is not None:
                parsed_count += 1

                (
                    _,
                    was_new_job,
                    was_new_posting,
                ) = save_parsed_job(
                    database,
                    parsed_job,
                )

                if was_new_job:
                    new_jobs_count += 1

                if was_new_posting:
                    new_postings_count += 1
                else:
                    existing_postings_count += 1

            if progress_callback is not None:
                progress_callback(
                    message_index,
                    message_count,
                )

        if messages:
            save_telegram_collection_cursor(
                database,
                group_identifier,
                max(int(message.id) for message in messages),
            )

    current_cursor = (
        max(int(message.id) for message in messages)
        if messages
        else previous_cursor
    )

    return ChannelCollectionSummary(
        group_identifier=group_identifier,
        group_name=group_name,
        messages_checked=len(messages),
        messages_parsed=parsed_count,
        new_jobs=new_jobs_count,
        new_postings=new_postings_count,
        existing_postings=existing_postings_count,
        previous_cursor=previous_cursor,
        current_cursor=current_cursor,
        safe_limit_reached=safe_limit_reached,
    )


async def run_collection(
    *,
    non_interactive: bool,
    progress_file: Path | None = None,
) -> tuple[ChannelCollectionSummary, ...]:
    settings = load_collection_settings()
    source_count = len(settings.group_identifiers)
    write_refresh_progress(
        progress_file,
        stage_key="telegram_collection",
        progress_mode="determinate",
        progress_completed=0,
        progress_total=source_count,
        progress_unit="sources",
        telegram_collection_outcome=None,
        telegram_cooldown_seconds_remaining=0,
        telegram_safe_limit_reached=False,
    )
    database = connect_database()
    initialize_database(database)
    retry_after = get_telegram_retry_after(database)
    current_epoch = int(time.time())

    if retry_after > current_epoch:
        database.close()
        raise TelegramBackoffActiveError(
            retry_after - current_epoch
        )

    with database:
        cooldown_seconds_remaining = (
            claim_telegram_collection_start(
                database,
                current_epoch=current_epoch,
                minimum_interval_seconds=(
                    TELEGRAM_COLLECTION_MIN_INTERVAL_SECONDS
                ),
            )
        )

    if cooldown_seconds_remaining > 0:
        write_refresh_progress(
            progress_file,
            stage_key="telegram_collection",
            progress_mode="determinate",
            progress_completed=source_count,
            progress_total=source_count,
            progress_unit="sources",
            telegram_collection_outcome="cooldown",
            telegram_cooldown_seconds_remaining=(
                cooldown_seconds_remaining
            ),
            telegram_safe_limit_reached=False,
        )
        database.close()
        print(
            "Telegram collection skipped: the five-minute "
            "safety interval has "
            f"{cooldown_seconds_remaining} seconds remaining."
        )
        return ()

    parser_registry = JobParserRegistry()

    for group_identifier in settings.group_identifiers:
        parser_registry.register(
            source="telegram",
            group_identifier=group_identifier,
            parser=parse_telegram_job_digest_message,
        )

    telegram_client: TelegramClient | None = None

    try:
        telegram_client = create_telegram_client(
            settings
        )
        print("Connecting to Telegram...")

        if non_interactive:
            await telegram_client.connect()

            if not await telegram_client.is_user_authorized():
                raise RuntimeError(
                    "The existing Telegram session is not "
                    "authorized. Run the collector manually once."
                )
        else:
            await telegram_client.start(
                phone=settings.phone
            )

        summaries: list[ChannelCollectionSummary] = []

        for source_index, group_identifier in enumerate(
            settings.group_identifiers,
            start=1,
        ):
            def report_message_progress(
                completed: int,
                total: int,
            ) -> None:
                write_refresh_progress(
                    progress_file,
                    stage_key="telegram_collection",
                    progress_mode="determinate",
                    progress_completed=completed,
                    progress_total=total,
                    progress_unit="messages",
                )

            summary = await collect_channel(
                telegram_client,
                database,
                parser_registry,
                group_identifier=group_identifier,
                message_limit=settings.message_limit,
                progress_callback=report_message_progress,
            )
            summaries.append(summary)
            write_refresh_progress(
                progress_file,
                stage_key="telegram_collection",
                progress_mode="determinate",
                progress_completed=source_index,
                progress_total=source_count,
                progress_unit="sources",
            )

            print(
                f"{summary.group_name}: "
                f"checked {summary.messages_checked}, "
                f"parsed {summary.messages_parsed}, "
                f"new jobs {summary.new_jobs}, "
                f"new postings {summary.new_postings}."
            )

        safe_limit_reached = any(
            summary.safe_limit_reached
            for summary in summaries
        )
        write_refresh_progress(
            progress_file,
            stage_key="telegram_collection",
            progress_mode="determinate",
            progress_completed=source_count,
            progress_total=source_count,
            progress_unit="sources",
            telegram_collection_outcome="performed",
            telegram_cooldown_seconds_remaining=0,
            telegram_safe_limit_reached=(
                safe_limit_reached
            ),
        )

        total_jobs, total_postings = get_database_counts(
            database
        )
        print(
            "Telegram collection complete. "
            f"Database totals: {total_jobs} jobs, "
            f"{total_postings} source postings."
        )

        return tuple(summaries)

    finally:
        if telegram_client is not None:
            await telegram_client.disconnect()

        database.close()
        print("Telegram disconnected safely.")
        print("Database closed safely.")


def _persist_flood_wait(seconds: int) -> None:
    database = connect_database()

    try:
        initialize_database(database)

        with database:
            save_telegram_retry_after(
                database,
                int(time.time()) + max(seconds, 1),
            )

    finally:
        database.close()


async def async_main() -> int:
    arguments = parse_arguments()

    try:
        with interprocess_lock(
            COLLECTION_LOCK_PATH,
            description="Telegram collection",
        ):
            await run_collection(
                non_interactive=arguments.non_interactive,
                progress_file=arguments.progress_file,
            )

    except AlreadyRunningError as error:
        print(error)
        return 2

    except FloodWaitError as error:
        _persist_flood_wait(error.seconds)
        print(
            "Telegram requested a rate-limit pause of "
            f"{error.seconds} seconds. Collection stopped; "
            "the next scheduled run may try again."
        )
        return 75

    except TelegramBackoffActiveError as error:
        print(error)
        return 75

    except Exception as error:
        print(
            "Telegram collection failed: "
            f"{type(error).__name__}: {error}"
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(async_main())
    )
