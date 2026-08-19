import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from database import (  # noqa: E402
    claim_telegram_collection_start,
    get_database_counts,
    get_telegram_collection_cursor,
    get_telegram_last_collection_started,
    get_telegram_retry_after,
    initialize_database,
    save_parsed_job,
    save_telegram_collection_cursor,
    save_telegram_retry_after,
)


def make_job(
    **overrides: Any,
) -> SimpleNamespace:
    values: dict[str, Any] = {
        "source": "telegram",
        "source_group": "Tech Jobs",
        "source_message_id": 101,
        "source_message_url": (
            "https://t.me/tech_jobs/101"
        ),
        "message_date": "2026-07-27T10:00:00+03:00",
        "title": "Backend Developer",
        "company": "Example",
        "location": "Example City",
        "posted_on": "2026-07-27",
        "job_url": "https://jobs.example.com/42",
        "raw_text": "Backend Developer @ Example",
        "parse_confidence": 1.0,
    }
    values.update(overrides)

    return SimpleNamespace(**values)


class DatabaseDeduplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "PRAGMA foreign_keys = ON"
        )
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_repeated_collection_preserves_existing_rows(
        self,
    ) -> None:
        first_result = save_parsed_job(
            self.connection,
            make_job(),
        )
        repeated_result = save_parsed_job(
            self.connection,
            make_job(
                title="Backend Developer - Java",
            ),
        )

        self.assertEqual(
            first_result,
            (1, True, True),
        )
        self.assertEqual(
            repeated_result,
            (1, False, False),
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (1, 1),
        )

        stored_title = self.connection.execute(
            """
            SELECT title
            FROM jobs
            WHERE id = 1
            """
        ).fetchone()["title"]

        self.assertEqual(
            stored_title,
            "Backend Developer - Java",
        )

    def test_new_source_posting_reuses_unique_job(
        self,
    ) -> None:
        save_parsed_job(
            self.connection,
            make_job(),
        )

        result = save_parsed_job(
            self.connection,
            make_job(
                source_message_id=102,
                source_message_url=(
                    "https://t.me/tech_jobs/102"
                ),
            ),
        )

        self.assertEqual(
            result,
            (1, False, True),
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (1, 2),
        )

    def test_fallback_dedupe_normalizes_text(
        self,
    ) -> None:
        first_job_id, _, _ = save_parsed_job(
            self.connection,
            make_job(
                job_url=None,
                title="Platform  Developer",
                company="EXAMPLE",
                location=" Example City ",
            ),
        )

        second_job_id, was_new_job, _ = (
            save_parsed_job(
                self.connection,
                make_job(
                    source_message_id=102,
                    job_url=None,
                    title="platform developer",
                    company="example",
                    location="example city",
                ),
            )
        )

        self.assertEqual(
            second_job_id,
            first_job_id,
        )
        self.assertFalse(was_new_job)
        self.assertEqual(
            get_database_counts(self.connection),
            (1, 2),
        )

    def test_incomplete_text_identity_uses_source_posting(
        self,
    ) -> None:
        first_result = save_parsed_job(
            self.connection,
            make_job(
                source_message_id="notification-1",
                job_url=None,
                title="Backend Developer",
                company=None,
                location=None,
            ),
        )
        second_result = save_parsed_job(
            self.connection,
            make_job(
                source_message_id="notification-2",
                job_url=None,
                title="Backend Developer",
                company=None,
                location=None,
            ),
        )

        self.assertEqual(first_result, (1, True, True))
        self.assertEqual(second_result, (2, True, True))
        self.assertEqual(
            get_database_counts(self.connection),
            (2, 2),
        )

    def test_nearby_position_merges_across_sources(
        self,
    ) -> None:
        save_parsed_job(
            self.connection,
            make_job(
                job_url=(
                    "https://hiremetech.com/job/42"
                ),
            ),
        )

        result = save_parsed_job(
            self.connection,
            make_job(
                source="greenhouse",
                source_group="example_greenhouse",
                source_message_id="gh-42",
                source_message_url=(
                    "https://jobs.example.com/gh-42"
                ),
                posted_on="2026-07-28",
                job_url=(
                    "https://jobs.example.com/gh-42"
                ),
            ),
        )

        self.assertEqual(
            result,
            (1, False, True),
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (1, 2),
        )

        stored_url = self.connection.execute(
            """
            SELECT job_url
            FROM jobs
            WHERE id = 1
            """
        ).fetchone()["job_url"]
        self.assertEqual(
            stored_url,
            "https://jobs.example.com/gh-42",
        )

    def test_distinct_same_feed_requisitions_stay_separate(
        self,
    ) -> None:
        first_result = save_parsed_job(
            self.connection,
            make_job(
                source="lever",
                source_group="example_lever",
                source_message_id="lever-1",
                job_url=(
                    "https://jobs.lever.co/example/1"
                ),
            ),
        )
        second_result = save_parsed_job(
            self.connection,
            make_job(
                source="lever",
                source_group="example_lever",
                source_message_id="lever-2",
                job_url=(
                    "https://jobs.lever.co/example/2"
                ),
            ),
        )

        self.assertEqual(
            first_result,
            (1, True, True),
        )
        self.assertEqual(
            second_result,
            (2, True, True),
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (2, 2),
        )

    def test_changed_url_for_same_posting_has_no_orphan(
        self,
    ) -> None:
        save_parsed_job(
            self.connection,
            make_job(
                source="ashby",
                source_group="example_ashby",
                source_message_id="ashby-42",
                job_url=(
                    "https://jobs.ashbyhq.com/example/old"
                ),
            ),
        )

        result = save_parsed_job(
            self.connection,
            make_job(
                source="ashby",
                source_group="example_ashby",
                source_message_id="ashby-42",
                job_url=(
                    "https://jobs.ashbyhq.com/example/new"
                ),
            ),
        )

        self.assertEqual(
            result,
            (1, False, False),
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (1, 1),
        )

    def test_telegram_cursor_is_additive_and_monotonic(
        self,
    ) -> None:
        self.assertIsNone(
            get_telegram_collection_cursor(
                self.connection,
                "@Tech_Jobs",
            )
        )

        with self.connection:
            save_telegram_collection_cursor(
                self.connection,
                "Tech_Jobs",
                120,
            )
            save_telegram_collection_cursor(
                self.connection,
                "@tech_jobs",
                110,
            )

        self.assertEqual(
            get_telegram_collection_cursor(
                self.connection,
                "TECH_JOBS",
            ),
            120,
        )

    def test_telegram_retry_deadline_is_monotonic(
        self,
    ) -> None:
        self.assertEqual(
            get_telegram_retry_after(self.connection),
            0,
        )

        with self.connection:
            save_telegram_retry_after(
                self.connection,
                2000,
            )
            save_telegram_retry_after(
                self.connection,
                1500,
            )

        self.assertEqual(
            get_telegram_retry_after(self.connection),
            2000,
        )

    def test_telegram_collection_start_has_persistent_cooldown(
        self,
    ) -> None:
        self.assertEqual(
            get_telegram_last_collection_started(
                self.connection
            ),
            0,
        )

        with self.connection:
            first_remaining = claim_telegram_collection_start(
                self.connection,
                current_epoch=1000,
                minimum_interval_seconds=300,
            )
            repeated_remaining = claim_telegram_collection_start(
                self.connection,
                current_epoch=1120,
                minimum_interval_seconds=300,
            )

        self.assertEqual(first_remaining, 0)
        self.assertEqual(repeated_remaining, 180)
        self.assertEqual(
            get_telegram_last_collection_started(
                self.connection
            ),
            1000,
        )

        with self.connection:
            next_remaining = claim_telegram_collection_start(
                self.connection,
                current_epoch=1300,
                minimum_interval_seconds=300,
            )

        self.assertEqual(next_remaining, 0)
        self.assertEqual(
            get_telegram_last_collection_started(
                self.connection
            ),
            1300,
        )

    def test_telegram_control_migration_preserves_retry_state(
        self,
    ) -> None:
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.execute(
            """
            CREATE TABLE telegram_collection_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                retry_after_epoch INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO telegram_collection_control (
                id,
                retry_after_epoch
            )
            VALUES (1, 2468)
            """
        )

        try:
            initialize_database(legacy)
            columns = {
                str(row["name"])
                for row in legacy.execute(
                    "PRAGMA table_info(telegram_collection_control)"
                ).fetchall()
            }

            self.assertIn(
                "last_collection_started_epoch",
                columns,
            )
            self.assertEqual(
                get_telegram_retry_after(legacy),
                2468,
            )
            self.assertEqual(
                get_telegram_last_collection_started(legacy),
                0,
            )
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
