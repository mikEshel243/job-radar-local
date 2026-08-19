"""Create a local Job Radar database containing synthetic portfolio data."""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from database import initialize_database, save_parsed_job  # noqa: E402
from evaluate_jobs import evaluate_stored_jobs  # noqa: E402
from job_analysis import (  # noqa: E402
    analyze_job_description,
    ensure_job_analysis_table,
    save_job_analysis,
)
from job_details import (  # noqa: E402
    JobDetails,
    ensure_job_details_table,
    save_job_details,
)
from job_filter import (  # noqa: E402
    ensure_evaluation_table,
    load_profile,
)
from source_parsing import NormalizedJob  # noqa: E402
from web_app import ensure_feedback_table  # noqa: E402


@dataclass(frozen=True, slots=True)
class DemoJob:
    title: str
    company: str
    location: str
    description: str
    status: str
    note: str | None = None


DEMO_JOBS = (
    DemoJob(
        title="Associate Platform Developer",
        company="Northstar Byteworks",
        location="Example City (Hybrid)",
        description=(
            "Build Python and FastAPI services backed by PostgreSQL. "
            "The role asks for 1-2 years of experience and a degree "
            "or equivalent practical experience."
        ),
        status="interested",
        note="Synthetic example: review the platform responsibilities.",
    ),
    DemoJob(
        title="Data Engineer",
        company="Blue Orchard Labs",
        location="Remote",
        description=(
            "Develop Python data pipelines and PostgreSQL models. "
            "Two years of software experience are preferred."
        ),
        status="applied",
        note="Synthetic example application status.",
    ),
    DemoJob(
        title="Quality Automation Engineer",
        company="Copper Kite Systems",
        location="Sample Harbor",
        description=(
            "Create automated API tests using Python, Git, and CI. "
            "This entry-level position welcomes up to 2 years of experience."
        ),
        status="none",
    ),
    DemoJob(
        title="Software Developer",
        company="Mosaic River Software",
        location="Demo Metro (On-site)",
        description=(
            "Maintain local services and SQLite tools. Three to four "
            "years of development experience are requested."
        ),
        status="not_interested",
        note="Synthetic example: work model is not preferred.",
    ),
    DemoJob(
        title="Sales Representative",
        company="Paper Lantern Works",
        location="Outside Region",
        description=(
            "Manage fictional customer accounts. Seven years of sales "
            "experience are required for this example posting."
        ),
        status="not_interested",
        note="Synthetic example outside the configured role domain.",
    ),
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a synthetic local Job Radar database."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "job_radar.db",
        help="Output SQLite path (must not already exist).",
    )
    return parser.parse_args()


def create_demo_database(database_path: Path) -> int:
    database_path = database_path.resolve()

    if database_path.exists():
        raise RuntimeError(
            f"Refusing to overwrite an existing database: {database_path}"
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        initialize_database(connection)
        ensure_job_details_table(connection)
        ensure_job_analysis_table(connection)
        ensure_evaluation_table(connection)
        ensure_feedback_table(connection)

        job_ids: list[int] = []

        for index, demo in enumerate(DEMO_JOBS, start=1):
            job_url = f"https://jobs.example.com/postings/demo-{index}"
            parsed = NormalizedJob(
                source="synthetic_demo",
                source_group="Example Portfolio Feed",
                source_message_id=index,
                source_message_url=None,
                message_date=f"2026-08-{index:02d}T09:00:00+00:00",
                title=demo.title,
                company=demo.company,
                location=demo.location,
                posted_on=f"2026-08-{index:02d}",
                job_url=job_url,
                raw_text=(
                    f"{demo.title} at {demo.company}\n{demo.description}"
                ),
                parse_confidence=1.0,
            )
            job_id, _, _ = save_parsed_job(connection, parsed)
            job_ids.append(job_id)
            save_job_details(
                connection,
                JobDetails(
                    job_id=job_id,
                    final_url=job_url,
                    page_title=f"{demo.title} | Synthetic demo",
                    description_text=demo.description,
                    extractor="synthetic_demo",
                    fetch_status="success",
                    fetch_error=None,
                    http_status=200,
                    resolved_company=demo.company,
                    resolved_location=demo.location,
                ),
            )
            save_job_analysis(
                connection,
                analyze_job_description(
                    job_id,
                    demo.title,
                    demo.description,
                ),
            )

        connection.commit()
        evaluate_stored_jobs(connection, load_profile())

        for job_id, demo in zip(job_ids, DEMO_JOBS, strict=True):
            if demo.status == "none":
                continue

            connection.execute(
                """
                INSERT INTO job_feedback (job_id, status, notes)
                VALUES (?, ?, ?)
                """,
                (job_id, demo.status, demo.note),
            )

        connection.commit()
        return len(job_ids)
    finally:
        connection.close()


def main() -> None:
    arguments = parse_arguments()
    count = create_demo_database(arguments.database)
    print(f"Created synthetic demo database with {count} jobs.")


if __name__ == "__main__":
    main()
