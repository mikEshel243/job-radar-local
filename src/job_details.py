import sqlite3
from dataclasses import dataclass


@dataclass
class JobDetails:
    job_id: int
    final_url: str | None
    page_title: str | None
    description_text: str | None
    extractor: str | None
    fetch_status: str
    fetch_error: str | None
    http_status: int | None
    resolved_company: str | None = None
    resolved_location: str | None = None


def ensure_job_details_table(
    connection: sqlite3.Connection,
) -> None:
    """Create the table that stores fetched job-page content."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_details (
            job_id INTEGER PRIMARY KEY,

            final_url TEXT,
            page_title TEXT,
            description_text TEXT,
            extractor TEXT,

            fetch_status TEXT NOT NULL,
            fetch_error TEXT,
            http_status INTEGER,

            fetched_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (job_id)
                REFERENCES jobs(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS
            idx_job_details_fetch_status
        ON job_details(fetch_status);
        """
    )

    connection.commit()


def save_job_details(
    connection: sqlite3.Connection,
    details: JobDetails,
) -> None:
    """Insert or update fetched details for one job."""

    connection.execute(
        """
        INSERT INTO job_details (
            job_id,
            final_url,
            page_title,
            description_text,
            extractor,
            fetch_status,
            fetch_error,
            http_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(job_id)
        DO UPDATE SET
            final_url = excluded.final_url,
            page_title = excluded.page_title,
            description_text = excluded.description_text,
            extractor = excluded.extractor,
            fetch_status = excluded.fetch_status,
            fetch_error = excluded.fetch_error,
            http_status = excluded.http_status,
            fetched_at = CURRENT_TIMESTAMP
        """,
        (
            details.job_id,
            details.final_url,
            details.page_title,
            details.description_text,
            details.extractor,
            details.fetch_status,
            details.fetch_error,
            details.http_status,
        ),
    )


def get_fetch_status_counts(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return the number of records for each fetch status."""

    return connection.execute(
        """
        SELECT
            fetch_status,
            COUNT(*) AS amount
        FROM job_details
        GROUP BY fetch_status
        ORDER BY fetch_status
        """
    ).fetchall()
