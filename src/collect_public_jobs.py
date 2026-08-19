import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from database import (
    connect_database,
    initialize_database,
    save_parsed_job,
)
from public_job_sources import (
    REQUEST_HEADERS,
    PublicSource,
    SourceRegistry,
    collect_public_source,
    ensure_source_collection_table,
    finish_collection_run,
    load_source_registry,
    start_collection_run,
)
from refresh_progress import write_refresh_progress


@dataclass(frozen=True, slots=True)
class SourceRunSummary:
    source_id: str
    company: str
    status: str
    postings_seen: int
    postings_relevant: int
    new_jobs: int
    new_postings: int
    error: str | None = None


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect published Israeli jobs from configured "
            "public ATS feeds."
        )
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "Collect only this registry source id. "
            "May be supplied more than once."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Fetch and filter feeds without opening or "
            "changing SQLite."
        ),
    )
    parser.add_argument(
        "--limit-per-source",
        type=int,
        default=None,
        help=(
            "Optional maximum relevant jobs saved per source. "
            "Useful for a first controlled validation."
        ),
    )
    parser.add_argument(
        "--continue-on-source-errors",
        action="store_true",
        help=(
            "Return success when at least one configured source "
            "succeeds. Failed sources still retain an error status."
        ),
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        help=(
            "Write aggregate source counts to this local JSON "
            "file. Employer identities and output are omitted."
        ),
    )

    arguments = parser.parse_args()

    if (
        arguments.limit_per_source is not None
        and not 1 <= arguments.limit_per_source <= 500
    ):
        parser.error(
            "--limit-per-source must be between 1 and 500."
        )

    return arguments


def select_sources(
    registry: SourceRegistry,
    requested_ids: list[str],
) -> tuple[PublicSource, ...]:
    enabled_sources = tuple(
        source
        for source in registry.sources
        if source.enabled
    )

    if not requested_ids:
        return enabled_sources

    requested = {
        value.strip().casefold()
        for value in requested_ids
        if value.strip()
    }
    selected = tuple(
        source
        for source in enabled_sources
        if source.id.casefold() in requested
    )
    selected_keys = {
        source.id.casefold()
        for source in selected
    }
    missing = sorted(
        requested - selected_keys
    )

    if missing:
        raise ValueError(
            "Unknown or disabled source id(s): "
            + ", ".join(missing)
        )

    return selected


def _safe_error_text(error: Exception) -> str:
    return (
        f"{type(error).__name__}: {error}"
    )[:2000]


def run_collection(
    *,
    registry: SourceRegistry,
    sources: tuple[PublicSource, ...],
    dry_run: bool,
    limit_per_source: int | None,
    progress_file: Path | None = None,
) -> tuple[SourceRunSummary, ...]:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    connection = None

    if not dry_run:
        connection = connect_database()
        initialize_database(connection)
        ensure_source_collection_table(connection)

    summaries: list[SourceRunSummary] = []
    source_count = len(sources)

    write_refresh_progress(
        progress_file,
        stage_key="public_source_collection",
        progress_mode="determinate",
        progress_completed=0,
        progress_total=source_count,
        progress_unit="sources",
    )

    try:
        for source_index, source in enumerate(
            sources,
            start=1,
        ):
            run_id = (
                start_collection_run(
                    connection,
                    source,
                )
                if connection is not None
                else None
            )

            try:
                collection = collect_public_source(
                    session,
                    source,
                    registry,
                )
                jobs = collection.relevant_jobs

                if limit_per_source is not None:
                    jobs = jobs[:limit_per_source]

                new_jobs = 0
                new_postings = 0

                if connection is not None:
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

                    finish_collection_run(
                        connection,
                        run_id=int(run_id),
                        status="success",
                        postings_seen=(
                            collection.postings_seen
                        ),
                        postings_relevant=len(jobs),
                        new_jobs=new_jobs,
                        new_postings=new_postings,
                    )

                summaries.append(
                    SourceRunSummary(
                        source_id=source.id,
                        company=source.company,
                        status="success",
                        postings_seen=(
                            collection.postings_seen
                        ),
                        postings_relevant=len(jobs),
                        new_jobs=new_jobs,
                        new_postings=new_postings,
                    )
                )

            except (
                requests.RequestException,
                sqlite3.Error,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                error_text = _safe_error_text(error)

                if connection is not None:
                    connection.rollback()
                    finish_collection_run(
                        connection,
                        run_id=int(run_id),
                        status="error",
                        error=error_text,
                    )

                summaries.append(
                    SourceRunSummary(
                        source_id=source.id,
                        company=source.company,
                        status="error",
                        postings_seen=0,
                        postings_relevant=0,
                        new_jobs=0,
                        new_postings=0,
                        error=error_text,
                    )
                )

            write_refresh_progress(
                progress_file,
                stage_key="public_source_collection",
                progress_mode="determinate",
                progress_completed=source_index,
                progress_total=source_count,
                progress_unit="sources",
            )

    finally:
        session.close()

        if connection is not None:
            connection.close()

    return tuple(summaries)


def print_summaries(
    summaries: tuple[SourceRunSummary, ...],
    *,
    dry_run: bool,
) -> None:
    mode = "DRY RUN" if dry_run else "IMPORT"
    print(f"Public ATS collection: {mode}")

    for summary in summaries:
        if summary.status == "success":
            print(
                f"[OK] {summary.company} "
                f"({summary.source_id}): "
                f"{summary.postings_relevant} relevant / "
                f"{summary.postings_seen} published, "
                f"{summary.new_jobs} new jobs, "
                f"{summary.new_postings} new postings"
            )
        else:
            print(
                f"[ERROR] {summary.company} "
                f"({summary.source_id}): "
                f"{summary.error}"
            )

    if dry_run:
        print("SQLite was not opened or changed.")


def main() -> int:
    arguments = parse_arguments()

    try:
        registry = load_source_registry()
        sources = select_sources(
            registry,
            arguments.source,
        )
    except ValueError as error:
        print(
            f"Configuration error: {error}",
            file=sys.stderr,
        )

        return 2

    summaries = run_collection(
        registry=registry,
        sources=sources,
        dry_run=arguments.dry_run,
        limit_per_source=arguments.limit_per_source,
        progress_file=arguments.progress_file,
    )
    print_summaries(
        summaries,
        dry_run=arguments.dry_run,
    )

    failed = any(
        item.status == "error"
        for item in summaries
    )
    succeeded = any(
        item.status == "success"
        for item in summaries
    )

    if not failed:
        return 0

    if arguments.continue_on_source_errors and succeeded:
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
