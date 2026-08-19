import argparse
import json
import sqlite3
from typing import Any

from database import (
    connect_database,
    initialize_database,
)
from job_analysis import (
    ensure_job_analysis_table,
)
from job_details import ensure_job_details_table
from job_filter import (
    ensure_evaluation_table,
    evaluate_job,
    load_profile,
    save_evaluation,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate locally stored jobs."
    )

    parser.add_argument(
        "--show-rejected",
        action="store_true",
        help="Also print rejected jobs.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Evaluate and save all jobs without printing the "
            "human-readable report."
        ),
    )

    return parser.parse_args()


def parse_json_list(
    value: Any,
) -> list[str]:
    """Read a JSON list stored in SQLite."""

    if not value:
        return []

    try:
        parsed = json.loads(value)
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        return []

    if not isinstance(parsed, list):
        return []

    return [
        str(item)
        for item in parsed
    ]


def format_experience(
    minimum: int | None,
    maximum: int | None,
) -> str:
    """Return a readable experience requirement."""

    if minimum is None:
        return "not detected"

    minimum = int(minimum)

    if maximum is None:
        return f"{minimum}+ years"

    maximum = int(maximum)

    if minimum == maximum:
        return f"{minimum} years"

    return f"{minimum}-{maximum} years"


def print_job(
    row,
) -> None:
    """Print one evaluated job."""

    bucket_labels = {
        "high_match": "HIGH MATCH",
        "review": "REVIEW",
        "rejected": "REJECTED",
    }

    label = bucket_labels.get(
        row["match_bucket"],
        row["match_bucket"].upper(),
    )

    print("=" * 90)

    print(
        f"[{label}] "
        f"Score: {row['match_score']}"
    )

    print(
        f"{row['title']} @ "
        f"{row['company']}"
    )

    print(
        f"Location: {row['location']}"
    )

    print(
        f"Seniority: "
        f"{row['seniority_label']}"
    )

    print(
        f"Category: "
        f"{row['role_category']}"
    )

    print(
        "Experience requirement: "
        + format_experience(
            row["experience_min"],
            row["experience_max"],
        )
    )

    technologies = parse_json_list(
        row["technologies_json"]
    )

    print(
        "Technologies: "
        + (
            ", ".join(technologies)
            if technologies
            else "not analyzed"
        )
    )

    if row["analysis_confidence"] is not None:
        print(
            "Description-analysis confidence: "
            f"{float(row['analysis_confidence']):.0%}"
        )

    print(f"URL: {row['job_url']}")

    reasons = json.loads(
        row["reasons_json"]
    )

    print("Reasons:")

    for reason in reasons:
        print(f"  - {reason}")

    print()


def evaluation_query(
    bucket_condition: str,
) -> str:
    """Build the query used to display evaluated jobs."""

    return f"""
        SELECT
            jobs.id,
            jobs.title,
            jobs.company,
            jobs.location,
            jobs.posted_on,
            jobs.job_url,

            job_evaluations.seniority_label,
            job_evaluations.role_category,
            job_evaluations.location_label,
            job_evaluations.match_score,
            job_evaluations.match_bucket,
            job_evaluations.reasons_json,

            job_analysis.experience_min,
            job_analysis.experience_max,
            job_analysis.experience_label,
            job_analysis.technologies_json,
            job_analysis.analysis_confidence

        FROM jobs

        INNER JOIN job_evaluations
            ON job_evaluations.job_id = jobs.id

        LEFT JOIN job_analysis
            ON job_analysis.job_id = jobs.id

        WHERE {bucket_condition}

        ORDER BY
            CASE job_evaluations.match_bucket
                WHEN 'high_match' THEN 1
                WHEN 'review' THEN 2
                ELSE 3
            END,
            job_evaluations.match_score DESC,
            jobs.posted_on DESC,
            jobs.id DESC
    """


def evaluate_stored_jobs(
    connection: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    only_missing: bool = False,
    source: str | None = None,
) -> int:
    """Evaluate stored jobs, optionally limiting work and source."""

    jobs = connection.execute(
        """
        SELECT
            jobs.id,
            jobs.title,
            jobs.company,
            jobs.location,
            jobs.posted_on,
            jobs.job_url,

            job_analysis.experience_min,
            job_analysis.experience_max,
            job_analysis.experience_label,
            job_analysis.technologies_json,
            job_analysis.seniority_signals_json,
            job_analysis.education_signals_json,
            job_analysis.analysis_confidence,
            job_details.description_text

        FROM jobs

        LEFT JOIN job_analysis
            ON job_analysis.job_id = jobs.id

        LEFT JOIN job_details
            ON job_details.job_id = jobs.id

        LEFT JOIN job_evaluations AS existing_evaluation
            ON existing_evaluation.job_id = jobs.id

        WHERE
            (
                ? = 0
                OR existing_evaluation.job_id IS NULL
            )
            AND (
                ? IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM job_postings
                    WHERE
                        job_postings.job_id = jobs.id
                        AND LOWER(job_postings.source)
                            = LOWER(?)
                )
            )

        ORDER BY jobs.id
        """,
        (
            int(only_missing),
            source,
            source,
        ),
    ).fetchall()

    evaluated_count = 0

    for job in jobs:
        evaluation = evaluate_job(
            job,
            profile,
        )

        with connection:
            save_evaluation(
                connection,
                evaluation,
            )

        evaluated_count += 1

    return evaluated_count


def main() -> None:
    arguments = parse_arguments()
    profile = load_profile()

    connection = connect_database()

    initialize_database(
        connection
    )

    ensure_job_analysis_table(
        connection
    )
    ensure_job_details_table(
        connection
    )

    ensure_evaluation_table(
        connection
    )

    try:
        evaluated_count = evaluate_stored_jobs(
            connection,
            profile,
        )

        if arguments.quiet:
            return

        summary_rows = connection.execute(
            """
            SELECT
                match_bucket,
                COUNT(*) AS amount
            FROM job_evaluations
            GROUP BY match_bucket
            ORDER BY match_bucket
            """
        ).fetchall()

        analyzed_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM job_analysis
            """
        ).fetchone()[0]

        print()
        print("EVALUATION SUMMARY")
        print("=" * 90)

        print(
            f"Profile: {profile['profile_name']}"
        )

        print(
            f"Jobs evaluated: {evaluated_count}"
        )

        print(
            f"Jobs with analyzed descriptions: "
            f"{analyzed_count}"
        )

        for row in summary_rows:
            print(
                f"{row['match_bucket']}: "
                f"{row['amount']}"
            )

        print()
        print("RELEVANT JOBS")
        print("=" * 90)

        relevant_jobs = connection.execute(
            evaluation_query(
                """
                job_evaluations.match_bucket
                IN ('high_match', 'review')
                """
            )
        ).fetchall()

        if not relevant_jobs:
            print(
                "No relevant jobs were found."
            )
            print()

        for row in relevant_jobs:
            print_job(row)

        if arguments.show_rejected:
            print()
            print("REJECTED JOBS")
            print("=" * 90)

            rejected_jobs = connection.execute(
                evaluation_query(
                    """
                    job_evaluations.match_bucket
                    = 'rejected'
                    """
                )
            ).fetchall()

            for row in rejected_jobs:
                print_job(row)

    finally:
        connection.close()


if __name__ == "__main__":
    main()
