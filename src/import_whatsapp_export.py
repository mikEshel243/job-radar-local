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
    read_whatsapp_export,
)
from source_parsing import (
    JobParserRegistry,
    SourceContext,
    SourceMessage,
)
from whatsapp_sources import (
    SUPPORTED_WHATSAPP_GROUPS,
    prepare_whatsapp_parser,
)


def _parse_since_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--since must use YYYY-MM-DD."
        ) from error


def _prepare_whatsapp_file(
    export_path: Path,
    group_identifier: str,
    since: date | None,
) -> tuple[
    tuple[SourceMessage, ...],
    SourceContext,
    JobParserRegistry,
]:
    """Read, date-filter, and configure one WhatsApp export."""

    group_key = group_identifier.strip().casefold()

    messages = filter_source_messages_since(
        read_whatsapp_export(
            path=export_path,
            group_identifier=group_key,
        ),
        since=since,
    )
    context, parser_registry = prepare_whatsapp_parser(
        group_identifier=group_key,
    )

    return messages, context, parser_registry


def preview_whatsapp_file(
    export_path: Path,
    group_identifier: str,
    since: date | None = None,
) -> PreviewSummary:
    """Parse a WhatsApp export without opening SQLite."""

    messages, context, parser_registry = (
        _prepare_whatsapp_file(
            export_path=export_path,
            group_identifier=group_identifier,
            since=since,
        )
    )

    return preview_source_messages(
        messages=messages,
        context=context,
        parser_registry=parser_registry,
    )


def import_whatsapp_file(
    connection: sqlite3.Connection,
    export_path: Path,
    group_identifier: str,
    since: date | None = None,
) -> ImportSummary:
    """Import one supported local WhatsApp text export."""

    messages, context, parser_registry = (
        _prepare_whatsapp_file(
            export_path=export_path,
            group_identifier=group_identifier,
            since=since,
        )
    )

    return import_source_messages(
        connection=connection,
        messages=messages,
        context=context,
        parser_registry=parser_registry,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import a supported native WhatsApp text export "
            "into the local Job Radar database."
        )
    )
    parser.add_argument(
        "export_path",
        type=Path,
        help="Explicit path to the exported UTF-8 .txt file.",
    )
    parser.add_argument(
        "--group-id",
        required=True,
        choices=sorted(SUPPORTED_WHATSAPP_GROUPS),
        help="Stable local ID of the exported WhatsApp group.",
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


def _print_summary(summary: ImportSummary) -> None:
    print("WhatsApp export import complete.")
    print(
        "Source messages checked: "
        f"{summary.source_messages_checked}"
    )
    print(f"Messages parsed as jobs: {summary.jobs_parsed}")
    print(f"New unique jobs: {summary.new_jobs}")
    print(f"New source postings: {summary.new_postings}")
    print(
        "Previously saved postings: "
        f"{summary.existing_postings}"
    )


def _print_preview(summary: PreviewSummary) -> None:
    print("DRY RUN: SQLite was not opened or changed.")
    print(
        "Source messages checked: "
        f"{summary.source_messages_checked}"
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
            preview = preview_whatsapp_file(
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

        _print_preview(preview)
        return 0

    database = connect_database()

    try:
        initialize_database(database)
        summary = import_whatsapp_file(
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

    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
