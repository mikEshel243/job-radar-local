import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from database import (
    connect_database,
    initialize_database,
    save_parsed_job,
)
from evaluate_jobs import evaluate_stored_jobs
from gmail_jobs import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_TOKEN_PATH,
    GmailJobsError,
    build_readonly_gmail_service,
    fetch_linkedin_job_email_mime,
)
from job_analysis import ensure_job_analysis_table
from job_details import ensure_job_details_table
from job_filter import (
    ensure_evaluation_table,
    load_profile,
)
from linkedin_job_emails import (
    LINKEDIN_SOURCE,
    LinkedInJobEmailError,
    parse_linkedin_job_email,
)
from source_parsing import NormalizedJob


@dataclass(frozen=True, slots=True)
class LinkedInGmailSummary:
    emails_checked: int
    emails_parsed: int
    emails_skipped: int
    jobs_parsed: int
    duplicate_cards: int
    malformed_cards: int
    new_jobs: int
    new_postings: int
    existing_postings: int
    evaluated_jobs: int


def _parse_messages(
    raw_messages: Iterable[bytes],
) -> tuple[
    tuple[NormalizedJob, ...],
    int,
    int,
    int,
    int,
    int,
]:
    emails_checked = 0
    emails_parsed = 0
    emails_skipped = 0
    malformed_cards = 0
    total_cards = 0
    jobs_by_id: dict[str, NormalizedJob] = {}

    for raw_message in raw_messages:
        emails_checked += 1

        try:
            parsed = parse_linkedin_job_email(raw_message)
        except LinkedInJobEmailError:
            emails_skipped += 1
            continue

        emails_parsed += 1
        malformed_cards += parsed.skipped_cards
        total_cards += len(parsed.jobs)

        for job in parsed.jobs:
            jobs_by_id.setdefault(
                str(job.source_message_id),
                job,
            )

    return (
        tuple(jobs_by_id.values()),
        emails_checked,
        emails_parsed,
        emails_skipped,
        malformed_cards,
        total_cards,
    )


def process_linkedin_gmail_messages(
    raw_messages: Iterable[bytes],
    *,
    connection: sqlite3.Connection | None,
) -> LinkedInGmailSummary:
    """Parse a Gmail batch and optionally save it transactionally."""

    raw_messages = tuple(raw_messages)
    (
        jobs,
        emails_checked,
        emails_parsed,
        emails_skipped,
        malformed_cards,
        total_cards,
    ) = _parse_messages(raw_messages)
    duplicate_cards = total_cards - len(jobs)
    new_jobs = 0
    new_postings = 0
    existing_postings = 0
    evaluated_jobs = 0

    if connection is not None:
        initialize_database(connection)

        with connection:
            for job in jobs:
                (
                    _,
                    was_new_job,
                    was_new_posting,
                ) = save_parsed_job(
                    connection,
                    job,
                )

                if was_new_job:
                    new_jobs += 1

                if was_new_posting:
                    new_postings += 1
                else:
                    existing_postings += 1

        ensure_job_details_table(connection)
        ensure_job_analysis_table(connection)
        ensure_evaluation_table(connection)
        evaluated_jobs = evaluate_stored_jobs(
            connection,
            load_profile(),
            only_missing=True,
            source=LINKEDIN_SOURCE,
        )

    return LinkedInGmailSummary(
        emails_checked=emails_checked,
        emails_parsed=emails_parsed,
        emails_skipped=emails_skipped,
        jobs_parsed=len(jobs),
        duplicate_cards=duplicate_cards,
        malformed_cards=malformed_cards,
        new_jobs=new_jobs,
        new_postings=new_postings,
        existing_postings=existing_postings,
        evaluated_jobs=evaluated_jobs,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect LinkedIn job recommendation and alert "
            "emails through the official Gmail read-only API."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse matching Gmail messages without opening or "
            "changing SQLite."
        ),
    )
    parser.add_argument(
        "--newer-than-days",
        type=int,
        default=14,
        help="Search the most recent 1-90 days (default: 14).",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=20,
        help="Read at most 1-50 matching messages (default: 20).",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS_PATH,
        help=(
            "Local OAuth desktop-client JSON path. Its contents "
            "are never printed."
        ),
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=DEFAULT_TOKEN_PATH,
        help=(
            "Ignored local OAuth token path. Its contents are "
            "never printed."
        ),
    )
    return parser


def _print_summary(
    summary: LinkedInGmailSummary,
    *,
    dry_run: bool,
) -> None:
    mode = "DRY RUN" if dry_run else "IMPORT"
    print(f"LinkedIn Gmail collection: {mode}")
    print(f"Matching emails checked: {summary.emails_checked}")
    print(f"Supported emails parsed: {summary.emails_parsed}")
    print(f"Unsupported emails skipped: {summary.emails_skipped}")
    print(f"Unique job cards parsed: {summary.jobs_parsed}")
    print(f"Duplicate cards skipped: {summary.duplicate_cards}")
    print(f"Malformed cards skipped: {summary.malformed_cards}")

    if dry_run:
        print("SQLite was not opened or changed.")
        return

    print(f"New unique jobs: {summary.new_jobs}")
    print(f"New source postings: {summary.new_postings}")
    print(
        "Previously saved postings: "
        f"{summary.existing_postings}"
    )
    print(f"Newly evaluated jobs: {summary.evaluated_jobs}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)

    try:
        service = build_readonly_gmail_service(
            credentials_path=arguments.credentials,
            token_path=arguments.token,
        )
        messages = fetch_linkedin_job_email_mime(
            service,
            newer_than_days=arguments.newer_than_days,
            max_messages=arguments.max_messages,
        )
    except (GmailJobsError, ValueError) as error:
        print(
            f"LinkedIn Gmail collection failed: {error}",
            file=sys.stderr,
        )
        return 1

    connection = None

    if not arguments.dry_run:
        connection = connect_database()

    try:
        summary = process_linkedin_gmail_messages(
            messages,
            connection=connection,
        )
    except sqlite3.Error:
        print(
            "LinkedIn Gmail import failed while updating SQLite.",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            connection.close()

    _print_summary(
        summary,
        dry_run=arguments.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
