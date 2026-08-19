import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from collect_telegram_jobs import (  # noqa: E402
    HISTORY_REQUEST_WAIT_SECONDS,
    MAX_INCREMENTAL_BATCHES_PER_RUN,
    ChannelCollectionSummary,
    TelegramCollectionSettings,
    collect_channel,
    create_telegram_client,
    load_collection_settings,
    run_collection,
)
from database import (  # noqa: E402
    get_database_counts,
    get_telegram_collection_cursor,
    initialize_database,
    save_telegram_collection_cursor,
)
from source_parsing import (  # noqa: E402
    JobParserRegistry,
    NormalizedJob,
)


class FakeTelegramClient:
    def __init__(
        self,
        message_ids: list[int],
    ) -> None:
        self.messages = [
            SimpleNamespace(id=message_id)
            for message_id in message_ids
        ]
        self.iterator_options: dict[str, Any] | None = None
        self.iterator_options_history: list[
            dict[str, Any]
        ] = []

    async def get_entity(
        self,
        group_identifier: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            title=f"Title for {group_identifier}"
        )

    def iter_messages(
        self,
        _channel: Any,
        **options: Any,
    ):
        self.iterator_options = dict(options)
        self.iterator_options_history.append(dict(options))

        async def iterator():
            messages = list(self.messages)
            min_id = options.get("min_id")

            if isinstance(min_id, int):
                messages = [
                    message
                    for message in messages
                    if int(message.id) > min_id
                ]

            messages.sort(
                key=lambda message: int(message.id),
                reverse=not bool(options.get("reverse")),
            )
            limit = int(options["limit"])

            for message in messages[:limit]:
                yield message

        return iterator()


def make_parser(
    message: Any,
    context: Any,
) -> NormalizedJob:
    return NormalizedJob(
        source="telegram",
        source_group=context.group_name,
        source_message_id=message.id,
        source_message_url=(
            f"https://t.me/test_group/{message.id}"
        ),
        message_date="2026-07-28T12:00:00+03:00",
        title=f"Job {message.id}",
        company="Example",
        location="Example City",
        posted_on="2026-07-28",
        job_url=(
            "https://jobs.example.com/"
            f"{message.id}"
        ),
        raw_text=f"Job {message.id}",
        parse_confidence=1.0,
    )


class TelegramIncrementalCollectionTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "PRAGMA foreign_keys = ON"
        )
        initialize_database(self.connection)

        self.registry = JobParserRegistry()
        self.registry.register(
            source="telegram",
            group_identifier="test_group",
            parser=make_parser,
        )

    def tearDown(self) -> None:
        self.connection.close()

    async def test_first_run_uses_bounded_recent_history(
        self,
    ) -> None:
        client = FakeTelegramClient([12, 11])

        summary = await collect_channel(
            client,
            self.connection,
            self.registry,
            group_identifier="test_group",
            message_limit=25,
        )

        self.assertEqual(
            client.iterator_options,
            {
                "limit": 25,
                "wait_time":
                    HISTORY_REQUEST_WAIT_SECONDS,
            },
        )
        self.assertEqual(summary.messages_checked, 2)
        self.assertEqual(summary.new_jobs, 2)
        self.assertEqual(
            get_telegram_collection_cursor(
                self.connection,
                "test_group",
            ),
            12,
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (2, 2),
        )

    async def test_later_run_reads_only_newer_messages(
        self,
    ) -> None:
        with self.connection:
            save_telegram_collection_cursor(
                self.connection,
                "test_group",
                12,
            )

        client = FakeTelegramClient([14, 13])

        summary = await collect_channel(
            client,
            self.connection,
            self.registry,
            group_identifier="test_group",
            message_limit=25,
        )

        self.assertEqual(
            client.iterator_options,
            {
                "limit": (
                    25 * MAX_INCREMENTAL_BATCHES_PER_RUN
                ),
                "wait_time":
                    HISTORY_REQUEST_WAIT_SECONDS,
                "min_id": 12,
                "reverse": True,
            },
        )
        self.assertEqual(summary.previous_cursor, 12)
        self.assertEqual(summary.current_cursor, 14)
        self.assertEqual(
            get_telegram_collection_cursor(
                self.connection,
                "test_group",
            ),
            14,
        )

    async def test_incremental_run_catches_up_normal_backlog(
        self,
    ) -> None:
        with self.connection:
            save_telegram_collection_cursor(
                self.connection,
                "test_group",
                100,
            )

        client = FakeTelegramClient(
            list(range(101, 244))
        )

        summary = await collect_channel(
            client,
            self.connection,
            self.registry,
            group_identifier="test_group",
            message_limit=100,
        )

        self.assertEqual(summary.messages_checked, 143)
        self.assertEqual(summary.current_cursor, 243)
        self.assertFalse(summary.safe_limit_reached)
        self.assertEqual(
            client.iterator_options["limit"],
            100 * MAX_INCREMENTAL_BATCHES_PER_RUN,
        )

    async def test_incremental_run_stops_at_safe_limit(
        self,
    ) -> None:
        with self.connection:
            save_telegram_collection_cursor(
                self.connection,
                "test_group",
                100,
            )

        summary = await collect_channel(
            FakeTelegramClient(list(range(101, 171))),
            self.connection,
            self.registry,
            group_identifier="test_group",
            message_limit=10,
        )

        self.assertEqual(summary.messages_checked, 50)
        self.assertEqual(summary.current_cursor, 150)
        self.assertTrue(summary.safe_limit_reached)

    async def test_reports_exact_message_processing_progress(
        self,
    ) -> None:
        progress_updates: list[tuple[int, int]] = []

        summary = await collect_channel(
            FakeTelegramClient([14, 13]),
            self.connection,
            self.registry,
            group_identifier="test_group",
            message_limit=25,
            progress_callback=lambda completed, total: (
                progress_updates.append(
                    (completed, total)
                )
            ),
        )

        self.assertEqual(summary.messages_checked, 2)
        self.assertEqual(
            progress_updates,
            [
                (0, 2),
                (1, 2),
                (2, 2),
            ],
        )

    async def test_reports_zero_when_no_new_messages(
        self,
    ) -> None:
        progress_updates: list[tuple[int, int]] = []

        summary = await collect_channel(
            FakeTelegramClient([]),
            self.connection,
            self.registry,
            group_identifier="test_group",
            message_limit=25,
            progress_callback=lambda completed, total: (
                progress_updates.append(
                    (completed, total)
                )
            ),
        )

        self.assertEqual(summary.messages_checked, 0)
        self.assertEqual(progress_updates, [(0, 0)])

    async def test_parser_failure_does_not_advance_cursor(
        self,
    ) -> None:
        with self.connection:
            save_telegram_collection_cursor(
                self.connection,
                "test_group",
                20,
            )

        failing_registry = JobParserRegistry()

        def failing_parser(
            message: Any,
            context: Any,
        ) -> NormalizedJob:
            if message.id == 22:
                raise ValueError("fixture failure")

            return make_parser(message, context)

        failing_registry.register(
            source="telegram",
            group_identifier="test_group",
            parser=failing_parser,
        )

        with self.assertRaisesRegex(
            ValueError,
            "fixture failure",
        ):
            await collect_channel(
                FakeTelegramClient([21, 22]),
                self.connection,
                failing_registry,
                group_identifier="test_group",
                message_limit=25,
            )

        self.assertEqual(
            get_telegram_collection_cursor(
                self.connection,
                "test_group",
            ),
            20,
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (0, 0),
        )


class TelegramAutomationSafetyTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_client_disables_background_network_activity(
        self,
    ) -> None:
        settings = TelegramCollectionSettings(
            api_id=1,
            api_hash="fixture-hash",
            phone="fixture-phone",
            group_identifiers=("fixture-group",),
            message_limit=10,
        )

        with patch(
            "collect_telegram_jobs.TelegramClient"
        ) as client_class:
            create_telegram_client(settings)

        _, args, kwargs = client_class.mock_calls[0]
        self.assertEqual(args[1:], (1, "fixture-hash"))
        self.assertEqual(kwargs["request_retries"], 1)
        self.assertEqual(kwargs["connection_retries"], 1)
        self.assertFalse(kwargs["auto_reconnect"])
        self.assertEqual(kwargs["flood_sleep_threshold"], 0)
        self.assertFalse(kwargs["receive_updates"])
        self.assertFalse(kwargs["catch_up"])

    def test_settings_use_explicit_deduplicated_allowlist(
        self,
    ) -> None:
        with (
            patch(
                "collect_telegram_jobs.load_settings",
                return_value=(
                    1,
                    "fixture-hash",
                    "fixture-phone",
                    "fallback-group",
                    10,
                ),
            ),
            patch.dict(
                os.environ,
                {
                    "TELEGRAM_CHANNEL_USERNAMES": (
                        "first-group, @second-group, "
                        "FIRST-GROUP"
                    ),
                },
            ),
        ):
            settings = load_collection_settings()

        self.assertEqual(
            settings.group_identifiers,
            (
                "first-group",
                "second-group",
            ),
        )
        self.assertEqual(settings.message_limit, 10)

    async def test_progress_reports_counts_not_identities(
        self,
    ) -> None:
        settings = TelegramCollectionSettings(
            api_id=1,
            api_hash="fixture-hash",
            phone="fixture-phone",
            group_identifiers=(
                "private-fixture-one",
                "private-fixture-two",
            ),
            message_limit=10,
        )
        database = MagicMock()
        client = AsyncMock()
        client.is_user_authorized.return_value = True
        summaries = [
            ChannelCollectionSummary(
                group_identifier=group,
                group_name=f"name-{index}",
                messages_checked=0,
                messages_parsed=0,
                new_jobs=0,
                new_postings=0,
                existing_postings=0,
                previous_cursor=None,
                current_cursor=None,
            )
            for index, group in enumerate(
                settings.group_identifiers,
                start=1,
            )
        ]

        with tempfile.TemporaryDirectory() as directory:
            progress_path = (
                Path(directory) / "progress.json"
            )

            with (
                patch(
                    "collect_telegram_jobs."
                    "load_collection_settings",
                    return_value=settings,
                ),
                patch(
                    "collect_telegram_jobs.connect_database",
                    return_value=database,
                ),
                patch(
                    "collect_telegram_jobs.initialize_database"
                ),
                patch(
                    "collect_telegram_jobs."
                    "get_telegram_retry_after",
                    return_value=0,
                ),
                patch(
                    "collect_telegram_jobs."
                    "claim_telegram_collection_start",
                    return_value=0,
                ),
                patch(
                    "collect_telegram_jobs."
                    "create_telegram_client",
                    return_value=client,
                ),
                patch(
                    "collect_telegram_jobs.collect_channel",
                    new=AsyncMock(side_effect=summaries),
                ),
                patch(
                    "collect_telegram_jobs.get_database_counts",
                    return_value=(0, 0),
                ),
                patch("builtins.print"),
            ):
                await run_collection(
                    non_interactive=True,
                    progress_file=progress_path,
                )

            progress_text = progress_path.read_text(
                encoding="utf-8"
            )

        self.assertIn(
            '"progress_completed":2',
            progress_text,
        )
        self.assertIn(
            '"progress_total":2',
            progress_text,
        )
        self.assertNotIn(
            "private-fixture",
            progress_text,
        )

    async def test_cooldown_skips_network_but_completes_stage(
        self,
    ) -> None:
        settings = TelegramCollectionSettings(
            api_id=1,
            api_hash="fixture-hash",
            phone="fixture-phone",
            group_identifiers=("fixture-group",),
            message_limit=10,
        )
        database = MagicMock()

        with tempfile.TemporaryDirectory() as directory:
            progress_path = Path(directory) / "progress.json"

            with (
                patch(
                    "collect_telegram_jobs."
                    "load_collection_settings",
                    return_value=settings,
                ),
                patch(
                    "collect_telegram_jobs.connect_database",
                    return_value=database,
                ),
                patch(
                    "collect_telegram_jobs.initialize_database"
                ),
                patch(
                    "collect_telegram_jobs."
                    "get_telegram_retry_after",
                    return_value=0,
                ),
                patch(
                    "collect_telegram_jobs."
                    "claim_telegram_collection_start",
                    return_value=180,
                ),
                patch(
                    "collect_telegram_jobs."
                    "create_telegram_client"
                ) as create_client,
                patch("builtins.print"),
            ):
                summaries = await run_collection(
                    non_interactive=True,
                    progress_file=progress_path,
                )

            payload = json.loads(
                progress_path.read_text(encoding="utf-8")
            )

        self.assertEqual(summaries, ())
        create_client.assert_not_called()
        self.assertEqual(
            payload["telegram_collection_outcome"],
            "cooldown",
        )
        self.assertEqual(
            payload["telegram_cooldown_seconds_remaining"],
            180,
        )
        self.assertEqual(payload["progress_completed"], 1)
        self.assertEqual(payload["progress_total"], 1)


if __name__ == "__main__":
    unittest.main()
