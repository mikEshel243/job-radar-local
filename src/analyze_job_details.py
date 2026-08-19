import argparse

from database import (
    connect_database,
    initialize_database,
)
from job_analysis import (
    analyze_job_description,
    ensure_job_analysis_table,
    save_job_analysis,
)
from job_details import (
    ensure_job_details_table,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze fetched job descriptions locally."
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help=(
            "Maximum number of descriptions to analyze."
        ),
    )

    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help=(
            "Analyze jobs again even if analysis exists."
        ),
    )

    return parser.parse_args()


def format_experience(
    minimum: int | None,
    maximum: int | None,
) -> str:
    """Return a readable experience range."""

    if minimum is None:
        return "not specified"

    if maximum is None:
        return f"{minimum}+ years"

    if minimum == maximum:
        return f"{minimum} years"

    return f"{minimum}-{maximum} years"


def main() -> None:
    arguments = parse_arguments()

    if arguments.limit < 1:
        raise RuntimeError(
            "--limit must be at least 1."
        )

    connection = connect_database()

    initialize_database(
        connection
    )

    ensure_job_details_table(
        connection
    )

    ensure_job_analysis_table(
        connection
    )

    try:
        if arguments.reanalyze:
            analysis_condition = "1 = 1"
        else:
            analysis_condition = (
                "job_analysis.job_id IS NULL"
            )

        jobs = connection.execute(
            f"""
            SELECT
                jobs.id,
                jobs.title,
                jobs.company,
                jobs.location,
                job_details.description_text

            FROM jobs

            INNER JOIN job_details
                ON job_details.job_id = jobs.id

            LEFT JOIN job_analysis
                ON job_analysis.job_id = jobs.id

            WHERE
                job_details.fetch_status = 'success'
                AND job_details.description_text IS NOT NULL
                AND job_details.description_text != ''
                AND ({analysis_condition})

            ORDER BY
                jobs.posted_on DESC,
                jobs.id DESC

            LIMIT ?
            """,
            (
                arguments.limit,
            ),
        ).fetchall()

        if not jobs:
            print(
                "No fetched descriptions currently "
                "require analysis."
            )
            return

        print(
            f"Analyzing {len(jobs)} "
            f"fetched descriptions..."
        )
        print()

        for job in jobs:
            analysis = analyze_job_description(
                job_id=int(job["id"]),
                title=job["title"],
                description_text=(
                    job["description_text"]
                ),
            )

            with connection:
                save_job_analysis(
                    connection,
                    analysis,
                )

            print("=" * 90)

            print(
                f"Job #{job['id']}: "
                f"{job['title']} @ "
                f"{job['company']}"
            )

            print(
                f"Location: {job['location']}"
            )

            print(
                "Experience: "
                + format_experience(
                    analysis.experience_min,
                    analysis.experience_max,
                )
            )

            print(
                "Experience label: "
                f"{analysis.experience_label}"
            )

            print(
                "Technologies: "
                + (
                    ", ".join(
                        analysis.technologies
                    )
                    if analysis.technologies
                    else "none detected"
                )
            )

            print(
                "Seniority signals: "
                + (
                    ", ".join(
                        analysis.seniority_signals
                    )
                    if analysis.seniority_signals
                    else "none"
                )
            )

            print(
                "Education signals: "
                + (
                    ", ".join(
                        analysis.education_signals
                    )
                    if analysis.education_signals
                    else "none"
                )
            )

            print(
                "Confidence: "
                f"{analysis.analysis_confidence:.0%}"
            )

        summary = connection.execute(
            """
            SELECT
                experience_label,
                COUNT(*) AS amount
            FROM job_analysis
            GROUP BY experience_label
            ORDER BY amount DESC
            """
        ).fetchall()

        print()
        print("=" * 90)
        print("ANALYSIS SUMMARY")
        print("=" * 90)

        for row in summary:
            print(
                f"{row['experience_label']}: "
                f"{row['amount']}"
            )

        print()
        print(
            "Analysis is stored in "
            "the job_analysis table."
        )

    finally:
        connection.close()


if __name__ == "__main__":
    main()
