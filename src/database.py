import hashlib
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from job_urls import normalize_url


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "data" / "job_radar.db"

ATS_SOURCES = frozenset(
    {
        "ashby",
        "greenhouse",
        "lever",
        "smartrecruiters",
        "workable",
    }
)

POSITION_MATCH_MAX_DAYS = 21


def _ensure_telegram_collection_control_columns(
    connection: sqlite3.Connection,
) -> None:
    """Add non-destructive Telegram control fields to older databases."""

    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(telegram_collection_control)"
        ).fetchall()
    }

    if "last_collection_started_epoch" not in columns:
        connection.execute(
            """
            ALTER TABLE telegram_collection_control
            ADD COLUMN last_collection_started_epoch INTEGER
                NOT NULL DEFAULT 0
                CHECK (last_collection_started_epoch >= 0)
            """
        )


def _job_value(
    job: Any,
    name: str,
) -> Any:
    try:
        return job[name]
    except (
        IndexError,
        KeyError,
        TypeError,
    ):
        return getattr(job, name, None)


def connect_database() -> sqlite3.Connection:
    """Open the local SQLite database."""

    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")

    return connection


def initialize_database(
    connection: sqlite3.Connection,
) -> None:
    """Create the first database tables and indexes."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            dedupe_key TEXT NOT NULL UNIQUE,

            title TEXT,
            company TEXT,
            location TEXT,
            posted_on TEXT,
            job_url TEXT,

            first_seen_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            last_seen_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS job_postings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            job_id INTEGER NOT NULL,

            source TEXT NOT NULL,
            source_group TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            source_message_url TEXT,
            message_date TEXT,

            raw_text TEXT NOT NULL,
            parse_confidence REAL NOT NULL,

            created_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (job_id)
                REFERENCES jobs(id)
                ON DELETE CASCADE,

            UNIQUE (
                source,
                source_group,
                source_message_id
            )
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_company
            ON jobs(company);

        CREATE INDEX IF NOT EXISTS idx_jobs_posted_on
            ON jobs(posted_on);

        CREATE INDEX IF NOT EXISTS idx_jobs_job_url
            ON jobs(job_url);

        CREATE INDEX IF NOT EXISTS idx_postings_job_id
            ON job_postings(job_id);

        CREATE INDEX IF NOT EXISTS idx_postings_source
            ON job_postings(source, source_group);

        CREATE TABLE IF NOT EXISTS job_identity_keys (
            identity_key TEXT PRIMARY KEY,
            job_id INTEGER NOT NULL,
            identity_kind TEXT NOT NULL,

            created_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (job_id)
                REFERENCES jobs(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_job_identity_keys_job_id
            ON job_identity_keys(job_id);

        CREATE TABLE IF NOT EXISTS job_position_fingerprints (
            fingerprint TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            posted_on TEXT,

            created_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (job_id)
                REFERENCES jobs(id)
                ON DELETE CASCADE,

            UNIQUE (fingerprint, job_id)
        );

        CREATE INDEX IF NOT EXISTS
            idx_job_position_fingerprints_value
        ON job_position_fingerprints(fingerprint);

        CREATE TABLE IF NOT EXISTS telegram_collection_state (
            group_identifier TEXT PRIMARY KEY,
            last_message_id INTEGER NOT NULL
                CHECK (last_message_id >= 0),

            updated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS telegram_collection_control (
            id INTEGER PRIMARY KEY
                CHECK (id = 1),
            retry_after_epoch INTEGER NOT NULL
                DEFAULT 0
                CHECK (retry_after_epoch >= 0),
            last_collection_started_epoch INTEGER NOT NULL
                DEFAULT 0
                CHECK (last_collection_started_epoch >= 0),

            updated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    _ensure_telegram_collection_control_columns(connection)
    _backfill_job_identities(connection)

    connection.commit()


def get_telegram_collection_cursor(
    connection: sqlite3.Connection,
    group_identifier: str,
) -> int | None:
    """Return the last fully processed Telegram message ID."""

    normalized_group = (
        group_identifier
        .strip()
        .lstrip("@")
        .casefold()
    )

    row = connection.execute(
        """
        SELECT last_message_id
        FROM telegram_collection_state
        WHERE group_identifier = ?
        """,
        (normalized_group,),
    ).fetchone()

    if row is None:
        return None

    return int(row["last_message_id"])


def save_telegram_collection_cursor(
    connection: sqlite3.Connection,
    group_identifier: str,
    last_message_id: int,
) -> None:
    """Advance one Telegram cursor without allowing regressions."""

    if last_message_id < 0:
        raise ValueError(
            "Telegram message IDs cannot be negative."
        )

    normalized_group = (
        group_identifier
        .strip()
        .lstrip("@")
        .casefold()
    )

    if not normalized_group:
        raise ValueError(
            "Telegram group identifier must not be empty."
        )

    connection.execute(
        """
        INSERT INTO telegram_collection_state (
            group_identifier,
            last_message_id
        )
        VALUES (?, ?)

        ON CONFLICT(group_identifier)
        DO UPDATE SET
            last_message_id = MAX(
                telegram_collection_state.last_message_id,
                excluded.last_message_id
            ),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            normalized_group,
            last_message_id,
        ),
    )


def get_telegram_retry_after(
    connection: sqlite3.Connection,
) -> int:
    """Return the persisted Telegram rate-limit deadline."""

    row = connection.execute(
        """
        SELECT retry_after_epoch
        FROM telegram_collection_control
        WHERE id = 1
        """
    ).fetchone()

    if row is None:
        return 0

    return int(row["retry_after_epoch"])


def save_telegram_retry_after(
    connection: sqlite3.Connection,
    retry_after_epoch: int,
) -> None:
    """Persist a monotonic global Telegram retry deadline."""

    if retry_after_epoch < 0:
        raise ValueError(
            "Telegram retry deadline cannot be negative."
        )

    connection.execute(
        """
        INSERT INTO telegram_collection_control (
            id,
            retry_after_epoch
        )
        VALUES (1, ?)

        ON CONFLICT(id)
        DO UPDATE SET
            retry_after_epoch = MAX(
                telegram_collection_control.retry_after_epoch,
                excluded.retry_after_epoch
            ),
            updated_at = CURRENT_TIMESTAMP
        """,
        (retry_after_epoch,),
    )


def get_telegram_last_collection_started(
    connection: sqlite3.Connection,
) -> int:
    """Return when the most recent Telegram collection was claimed."""

    row = connection.execute(
        """
        SELECT last_collection_started_epoch
        FROM telegram_collection_control
        WHERE id = 1
        """
    ).fetchone()

    if row is None:
        return 0

    return int(row["last_collection_started_epoch"])


def claim_telegram_collection_start(
    connection: sqlite3.Connection,
    *,
    current_epoch: int,
    minimum_interval_seconds: int,
) -> int:
    """
    Claim one Telegram collection slot.

    Returns zero when the caller may connect. Otherwise returns the
    number of seconds remaining in the persistent cooldown.
    """

    if current_epoch < 0:
        raise ValueError(
            "Telegram collection time cannot be negative."
        )

    if minimum_interval_seconds < 1:
        raise ValueError(
            "Telegram collection interval must be positive."
        )

    last_started = get_telegram_last_collection_started(
        connection
    )
    seconds_remaining = max(
        0,
        (
            last_started
            + minimum_interval_seconds
            - current_epoch
        ),
    )

    if seconds_remaining > 0:
        return seconds_remaining

    connection.execute(
        """
        INSERT INTO telegram_collection_control (
            id,
            last_collection_started_epoch
        )
        VALUES (1, ?)

        ON CONFLICT(id)
        DO UPDATE SET
            last_collection_started_epoch =
                excluded.last_collection_started_epoch,
            updated_at = CURRENT_TIMESTAMP
        """,
        (current_epoch,),
    )

    return 0


def normalize_fingerprint_text(
    value: str | None,
) -> str:
    """
    Normalize text for fallback duplicate detection.
    """

    if not value:
        return ""

    normalized = unicodedata.normalize(
        "NFKC",
        value,
    )

    normalized = normalized.casefold()

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


def build_dedupe_key(job: Any) -> str:
    """
    Build a stable identifier for a job.

    Prefer the canonical job URL. If no URL exists,
    use title, company and location when all are available. Otherwise,
    use the stable source-posting identity to avoid merging unrelated
    title-only previews.
    """

    if job.job_url:
        fingerprint_source = (
            f"url:{job.job_url.strip().casefold()}"
        )
    else:
        title = normalize_fingerprint_text(job.title)
        company = normalize_fingerprint_text(job.company)
        location = normalize_fingerprint_text(job.location)

        if title and company and location:
            fingerprint_source = (
                f"text:{title}|{company}|{location}"
            )
        else:
            source = normalize_fingerprint_text(
                _job_value(job, "source")
            )
            source_group = normalize_fingerprint_text(
                _job_value(job, "source_group")
            )
            source_message_id = str(
                _job_value(job, "source_message_id")
                or ""
            ).strip().casefold()
            fingerprint_source = (
                "source:"
                f"{source}|{source_group}|"
                f"{source_message_id}"
            )

    return hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()


def build_url_identity_key(
    job_url: str | None,
) -> str | None:
    """Build a strong identity from one normalized job URL."""

    if not job_url:
        return None

    normalized = normalize_url(job_url)

    if not normalized:
        return None

    return hashlib.sha256(
        (
            "url:"
            + normalized.casefold()
        ).encode("utf-8")
    ).hexdigest()


def build_position_fingerprint(
    job: Any,
) -> str | None:
    """Build a conservative cross-source position fingerprint."""

    title = normalize_fingerprint_text(
        _job_value(job, "title")
    )
    company = normalize_fingerprint_text(
        _job_value(job, "company")
    )
    location = normalize_fingerprint_text(
        _job_value(job, "location")
    )

    if not title or not company or not location:
        return None

    return hashlib.sha256(
        (
            f"position:{title}|{company}|{location}"
        ).encode("utf-8")
    ).hexdigest()


def _parse_posted_date(
    value: str | None,
) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _dates_are_nearby(
    first_value: str | None,
    second_value: str | None,
) -> bool:
    first_date = _parse_posted_date(first_value)
    second_date = _parse_posted_date(second_value)

    if first_date is None or second_date is None:
        return False

    return (
        abs((first_date - second_date).days)
        <= POSITION_MATCH_MAX_DAYS
    )


def _register_job_identities(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    job: Any,
) -> None:
    url_identity = build_url_identity_key(
        _job_value(job, "job_url")
    )

    if url_identity:
        connection.execute(
            """
            INSERT OR IGNORE INTO job_identity_keys (
                identity_key,
                job_id,
                identity_kind
            )
            VALUES (?, ?, 'url')
            """,
            (
                url_identity,
                job_id,
            ),
        )

    position_fingerprint = (
        build_position_fingerprint(job)
    )

    if position_fingerprint:
        connection.execute(
            """
            INSERT OR IGNORE INTO
                job_position_fingerprints (
                    fingerprint,
                    job_id,
                    posted_on
                )
            VALUES (?, ?, ?)
            """,
            (
                position_fingerprint,
                job_id,
                _job_value(job, "posted_on"),
            ),
        )


def _backfill_job_identities(
    connection: sqlite3.Connection,
) -> None:
    """Register additive identity rows for existing jobs."""

    rows = connection.execute(
        """
        SELECT
            id,
            title,
            company,
            location,
            posted_on,
            job_url
        FROM jobs
        """
    ).fetchall()

    for row in rows:
        _register_job_identities(
            connection,
            job_id=int(row["id"]),
            job=row,
        )


def _find_job_by_url(
    connection: sqlite3.Connection,
    job_url: str | None,
) -> int | None:
    identity_key = build_url_identity_key(job_url)

    if not identity_key:
        return None

    row = connection.execute(
        """
        SELECT job_id
        FROM job_identity_keys
        WHERE identity_key = ?
        """,
        (identity_key,),
    ).fetchone()

    if row is None:
        return None

    return int(row["job_id"])


def _find_job_by_position(
    connection: sqlite3.Connection,
    job: Any,
) -> int | None:
    fingerprint = build_position_fingerprint(job)
    posted_on = _job_value(job, "posted_on")
    incoming_url_identity = build_url_identity_key(
        _job_value(job, "job_url")
    )

    if not fingerprint:
        return None

    rows = connection.execute(
        """
        SELECT
            jobs.id,
            jobs.posted_on,
            jobs.job_url,
            EXISTS (
                SELECT 1
                FROM job_postings
                WHERE
                    job_postings.job_id = jobs.id
                    AND LOWER(job_postings.source)
                        = LOWER(?)
                    AND LOWER(job_postings.source_group)
                        = LOWER(?)
            ) AS same_source_group
        FROM job_position_fingerprints
        INNER JOIN jobs
            ON jobs.id
                = job_position_fingerprints.job_id
        WHERE
            job_position_fingerprints.fingerprint = ?
        ORDER BY jobs.id
        """,
        (
            str(_job_value(job, "source") or ""),
            str(
                _job_value(job, "source_group")
                or ""
            ),
            fingerprint,
        ),
    ).fetchall()

    for row in rows:
        if not incoming_url_identity:
            return int(row["id"])

        if not _dates_are_nearby(
            posted_on,
            row["posted_on"],
        ):
            continue

        existing_url_identity = build_url_identity_key(
            row["job_url"]
        )

        if (
            bool(row["same_source_group"])
            and incoming_url_identity
            and existing_url_identity
            and incoming_url_identity
                != existing_url_identity
        ):
            continue

        return int(row["id"])

    return None


def _preferred_job_url(
    *,
    existing_url: str | None,
    incoming_url: str | None,
    incoming_source: str,
) -> str | None:
    if not incoming_url:
        return existing_url

    if not existing_url:
        return incoming_url

    if incoming_source.strip().casefold() in ATS_SOURCES:
        return incoming_url

    return existing_url


def _update_existing_job(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    job: Any,
) -> None:
    existing_row = connection.execute(
        """
        SELECT job_url
        FROM jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()

    existing_url = (
        existing_row["job_url"]
        if existing_row is not None
        else None
    )

    selected_url = _preferred_job_url(
        existing_url=existing_url,
        incoming_url=_job_value(job, "job_url"),
        incoming_source=str(
            _job_value(job, "source") or ""
        ),
    )

    connection.execute(
        """
        UPDATE jobs
        SET
            title = COALESCE(?, title),
            company = COALESCE(?, company),
            location = COALESCE(?, location),
            posted_on = COALESCE(?, posted_on),
            job_url = ?,
            last_seen_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            _job_value(job, "title"),
            _job_value(job, "company"),
            _job_value(job, "location"),
            _job_value(job, "posted_on"),
            selected_url,
            job_id,
        ),
    )

    _register_job_identities(
        connection,
        job_id=job_id,
        job=job,
    )


def save_parsed_job(
    connection: sqlite3.Connection,
    job: Any,
) -> tuple[int, bool, bool]:
    """
    Save one parsed job and its source posting.

    Returns:
        job_id
        was_new_job
        was_new_posting
    """

    existing_posting = connection.execute(
        """
        SELECT job_id
        FROM job_postings
        WHERE
            source = ?
            AND source_group = ?
            AND source_message_id = ?
        """,
        (
            job.source,
            job.source_group,
            job.source_message_id,
        ),
    ).fetchone()

    if existing_posting is not None:
        job_id = int(existing_posting["job_id"])
        _update_existing_job(
            connection,
            job_id=job_id,
            job=job,
        )

        return (
            job_id,
            False,
            False,
        )

    job_id = _find_job_by_url(
        connection,
        _job_value(job, "job_url"),
    )

    if job_id is None:
        job_id = _find_job_by_position(
            connection,
            job,
        )

    if job_id is None:
        dedupe_key = build_dedupe_key(job)

        cursor = connection.execute(
            """
            INSERT INTO jobs (
                dedupe_key,
                title,
                company,
                location,
                posted_on,
                job_url
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                dedupe_key,
                job.title,
                job.company,
                job.location,
                job.posted_on,
                job.job_url,
            ),
        )

        job_id = int(cursor.lastrowid)
        was_new_job = True

    else:
        was_new_job = False
        _update_existing_job(
            connection,
            job_id=job_id,
            job=job,
        )

    _register_job_identities(
        connection,
        job_id=job_id,
        job=job,
    )

    posting_cursor = connection.execute(
        """
        INSERT OR IGNORE INTO job_postings (
            job_id,
            source,
            source_group,
            source_message_id,
            source_message_url,
            message_date,
            raw_text,
            parse_confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            job.source,
            job.source_group,
            job.source_message_id,
            job.source_message_url,
            job.message_date,
            job.raw_text,
            job.parse_confidence,
        ),
    )

    was_new_posting = posting_cursor.rowcount == 1

    return (
        job_id,
        was_new_job,
        was_new_posting,
    )


def get_database_counts(
    connection: sqlite3.Connection,
) -> tuple[int, int]:
    """Return total unique jobs and source postings."""

    jobs_count = connection.execute(
        "SELECT COUNT(*) FROM jobs"
    ).fetchone()[0]

    postings_count = connection.execute(
        "SELECT COUNT(*) FROM job_postings"
    ).fetchone()[0]

    return int(jobs_count), int(postings_count)
