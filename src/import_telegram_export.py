import argparse
import sqlite3
from datetime import date
from pathlib import Path
from typing import Sequence

from database import (
    connect_database,
    initialize_database,
)
from native_export_import import (
    ImportSummary,
    PreviewSummary,
    filter_source_messages_since,
    import_source_messages,
    preview_source_messages,
)
from native_exports import (
    NativeExportFormatError,
    TelegramNativeExport,
    read_telegram_json_export,
)
from parse_telegram_job_digest import (
    parse_telegram_job_digest_export_message,
)
from source_parsing import (
    JobParserRegistry,
    SourceContext,
    SourceMessage,
)


SUPPORTED_GROUPS = {
    "telegram_example_digest": (
        "Example Telegram Job Digest",
        parse_telegram_job_digest_export_message,
    ),
}


def _parse_since_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--since must use YYYY-MM-DD."
        ) from error


def _validate_export_group(
    native_export: TelegramNativeExport,
    expected_group_name: str,
) -> None:
    if native_export.group_name != expected_group_name:
        raise NativeExportFormatError(
            "Telegram export group name does not match "
            f"{expected_group_name!r}."
        )


def _prepare_telegram_file(
    export_path: Path,
    group_identifier: str,
    since: date | None,
) -> tuple[
    tuple[SourceMessage, ...],
    SourceContext,
    JobParserRegistry,
    int,
]:
    """Read, date-filter, and configure one Telegram export."""

    group_key = group_identifier.strip().casefold()

    try:
        group_name, message_parser = (
            SUPPORTED_GROUPS[group_key]
        )
    except KeyError as error:
        supported = ", ".join(
            sorted(SUPPORTED_GROUPS)
        )
        raise ValueError(
            "Unsupported Telegram group ID "
            f"{group_identifier!r}. Supported IDs: {supported}."
        ) from error

    native_export = read_telegram_json_export(export_path)
    _validate_export_group(
        native_export,
        expected_group_name=group_name,
    )

    context = SourceContext(
        source="telegram",
        group_name=group_name,
        group_identifier=group_key,
    )
    parser_registry = JobParserRegistry()
    parser_registry.register(
        source=context.source,
        group_identifier=context.group_identifier,
        parser=message_parser,
    )

    messages = filter_source_messages_since(
        native_export.messages,
        since=since,
    )

    return (
        messages,
        context,
        parser_registry,
        native_export.skipped_service_records,
    )


def preview_telegram_file(
    export_path: Path,
    group_identifier: str,
    since: date | None = None,
) -> tuple[PreviewSummary, int]:
    """Parse a Telegram export without opening SQLite."""

    (
        messages,
        context,
        parser_registry,
        skipped_service_records,
    ) = _prepare_telegram_file(
        export_path=export_path,
        group_identifier=group_identifier,
        since=since,
    )
    summary = preview_source_messages(
        messages=messages,
        context=context,
        parser_registry=parser_registry,
    )

    return summary, skipped_service_records


def import_telegram_file(
    connection: sqlite3.Connection,
    export_path: Path,
    group_identifier: str,
    since: date | None = None,
) -> tuple[ImportSummary, int]:
    """Import one supported local Telegram Desktop JSON export."""

    (
        messages,
        context,
        parser_registry,
        skipped_service_records,
    ) = _prepare_telegram_file(
        export_path=export_path,
        group_identifier=group_identifier,
        since=since,
    )

    summary = import_source_messages(
        connection=connection,
        messages=messages,
        context=context,
        parser_registry=parser_registry,
    )

    return (
        summary,
        skipped_service_records,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import a supported native Telegram Desktop JSON "
            "export into the local Job Radar database."
        )
    )
    parser.add_argument(
        "export_path",
        type=Path,
        help="Explicit path to the exported UTF-8 JSON file.",
    )
    parser.add_argument(
        "--group-id",
        required=True,
        choices=sorted(SUPPORTED_GROUPS),
        help="Stable local ID of the exported Telegram group.",
    )
    parser.add_argument(
        "--since",
        type=_parse_since_date,
        help=(
            "Only process messages dated YYYY-MM-DD or later."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate and preview parsed jobs without opening "
            "or changing SQLite."
        ),
    )
    return parser


def _print_summary(
    summary: ImportSummary,
    skipped_service_records: int,
) -> None:
    print("Telegram export import complete.")
    print(
        "Source messages checked: "
        f"{summary.source_messages_checked}"
    )
    print(
        "Service records skipped: "
        f"{skipped_service_records}"
    )
    print(f"Messages parsed as jobs: {summary.jobs_parsed}")
    print(f"New unique jobs: {summary.new_jobs}")
    print(f"New source postings: {summary.new_postings}")
    print(
        "Previously saved postings: "
        f"{summary.existing_postings}"
    )


def _print_preview(
    summary: PreviewSummary,
    skipped_service_records: int,
) -> None:
    print("DRY RUN: SQLite was not opened or changed.")
    print(
        "Source messages checked: "
        f"{summary.source_messages_checked}"
    )
    print(
        "Service records skipped: "
        f"{skipped_service_records}"
    )
    print(f"Messages parsed as jobs: {summary.jobs_parsed}")

    if not summary.jobs:
        print("No parsed jobs available for preview.")
        return

    print("Preview:")

    for index, job in enumerate(summary.jobs, start=1):
        print(
            f"{index}. {job.title or 'Unknown title'}"
            f" | {job.company or 'Unknown company'}"
            f" | {job.location or 'Unknown location'}"
            f" | {job.posted_on or 'Unknown date'}"
            f" | URL: {'yes' if job.job_url else 'no'}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)

    if args.dry_run:
        try:
            (
                preview,
                skipped_service_records,
            ) = preview_telegram_file(
                export_path=args.export_path,
                group_identifier=args.group_id,
                since=args.since,
            )
        except (
            NativeExportFormatError,
            ValueError,
        ) as error:
            print(f"Dry run failed: {error}")
            return 1

        _print_preview(
            preview,
            skipped_service_records,
        )
        return 0

    database = connect_database()

    try:
        initialize_database(database)
        (
            summary,
            skipped_service_records,
        ) = import_telegram_file(
            connection=database,
            export_path=args.export_path,
            group_identifier=args.group_id,
            since=args.since,
        )
    except (NativeExportFormatError, ValueError) as error:
        print(f"Import failed: {error}")
        return 1
    finally:
        database.close()

    _print_summary(
        summary,
        skipped_service_records,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
