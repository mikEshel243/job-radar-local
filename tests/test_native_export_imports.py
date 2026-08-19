import io
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
FIXTURE_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "native_exports"
)

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from database import (  # noqa: E402
    get_database_counts,
    initialize_database,
)
from import_telegram_export import (  # noqa: E402
    import_telegram_file,
    main as telegram_main,
    preview_telegram_file,
)
from import_whatsapp_export import (  # noqa: E402
    import_whatsapp_file,
    main as whatsapp_main,
    preview_whatsapp_file,
)


class NativeExportImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "PRAGMA foreign_keys = ON"
        )
        initialize_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_whatsapp_import_is_local_and_idempotent(
        self,
    ) -> None:
        export_path = (
            FIXTURE_PATH / "whatsapp_example_digest.txt"
        )

        first_summary = import_whatsapp_file(
            connection=self.connection,
            export_path=export_path,
            group_identifier="whatsapp_example_digest",
        )
        second_summary = import_whatsapp_file(
            connection=self.connection,
            export_path=export_path,
            group_identifier="whatsapp_example_digest",
        )

        self.assertEqual(
            (
                first_summary.source_messages_checked,
                first_summary.jobs_parsed,
                first_summary.new_jobs,
                first_summary.new_postings,
            ),
            (7, 4, 3, 4),
        )
        self.assertEqual(
            (
                second_summary.new_jobs,
                second_summary.new_postings,
                second_summary.existing_postings,
            ),
            (0, 0, 4),
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (3, 4),
        )

        postings = self.connection.execute(
            """
            SELECT source_message_id
            FROM job_postings
            WHERE source = 'whatsapp'
            ORDER BY id
            """
        ).fetchall()

        self.assertTrue(
            all(
                row["source_message_id"].startswith("wa_")
                for row in postings
            )
        )
        self.assertEqual(
            len(
                {
                    row["source_message_id"]
                    for row in postings
                }
            ),
            4,
        )

        first_job_url = self.connection.execute(
            """
            SELECT job_url
            FROM jobs
            WHERE title = 'Junior Backend Developer'
            """
        ).fetchone()["job_url"]

        self.assertEqual(
            first_job_url,
            "https://jobs.example.com/postings/101",
        )

    def test_telegram_import_skips_non_job_records(
        self,
    ) -> None:
        summary, skipped_service_records = (
            import_telegram_file(
                connection=self.connection,
                export_path=(
                    FIXTURE_PATH
                    / "telegram_example_digest.json"
                ),
                group_identifier=(
                    "telegram_example_digest"
                ),
            )
        )

        self.assertEqual(skipped_service_records, 1)
        self.assertEqual(
            (
                summary.source_messages_checked,
                summary.jobs_parsed,
                summary.new_jobs,
                summary.new_postings,
            ),
            (4, 2, 2, 2),
        )
        self.assertEqual(
            get_database_counts(self.connection),
            (2, 2),
        )

        stored_jobs = self.connection.execute(
            """
            SELECT title, company, location, posted_on, job_url
            FROM jobs
            ORDER BY title
            """
        ).fetchall()

        self.assertEqual(
            [row["title"] for row in stored_jobs],
            [
                "Backend Developer",
                "Full-Stack Developer",
            ],
        )
        self.assertEqual(
            stored_jobs[0]["job_url"],
            "https://jobs.example.com/101",
        )
        self.assertEqual(
            stored_jobs[1]["job_url"],
            "https://jobs.example.com/102",
        )

    def test_rejects_a_group_id_not_registered_for_source(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported WhatsApp group ID",
        ):
            import_whatsapp_file(
                connection=self.connection,
                export_path=(
                    FIXTURE_PATH
                    / "whatsapp_example_digest.txt"
                ),
                group_identifier="unknown",
            )

    def test_since_filter_applies_before_preview(
        self,
    ) -> None:
        whatsapp_preview = preview_whatsapp_file(
            export_path=(
                FIXTURE_PATH
                / "whatsapp_example_digest.txt"
            ),
            group_identifier="whatsapp_example_digest",
            since=date(2026, 7, 24),
        )
        telegram_preview, _ = preview_telegram_file(
            export_path=(
                FIXTURE_PATH
                / "telegram_example_digest.json"
            ),
            group_identifier="telegram_example_digest",
            since=date(2025, 1, 23),
        )

        self.assertEqual(
            (
                whatsapp_preview.source_messages_checked,
                whatsapp_preview.jobs_parsed,
            ),
            (0, 0),
        )
        self.assertEqual(
            (
                telegram_preview.source_messages_checked,
                telegram_preview.jobs_parsed,
            ),
            (0, 0),
        )

    def test_whatsapp_dry_run_never_opens_sqlite(
        self,
    ) -> None:
        with (
            patch(
                "import_whatsapp_export.connect_database"
            ) as connect_database,
            patch(
                "sys.stdout",
                new_callable=io.StringIO,
            ) as output,
        ):
            exit_code = whatsapp_main(
                [
                    str(
                        FIXTURE_PATH
                        / "whatsapp_example_digest.txt"
                    ),
                    "--group-id",
                    "whatsapp_example_digest",
                    "--since",
                    "2026-07-23",
                    "--dry-run",
                ]
            )

        self.assertEqual(exit_code, 0)
        connect_database.assert_not_called()
        self.assertIn(
            "SQLite was not opened or changed",
            output.getvalue(),
        )
        self.assertNotIn(
            "https://",
            output.getvalue(),
        )

    def test_telegram_dry_run_never_opens_sqlite(
        self,
    ) -> None:
        with (
            patch(
                "import_telegram_export.connect_database"
            ) as connect_database,
            patch(
                "sys.stdout",
                new_callable=io.StringIO,
            ) as output,
        ):
            exit_code = telegram_main(
                [
                    str(
                        FIXTURE_PATH
                        / "telegram_example_digest.json"
                    ),
                    "--group-id",
                    "telegram_example_digest",
                    "--dry-run",
                ]
            )

        self.assertEqual(exit_code, 0)
        connect_database.assert_not_called()
        self.assertIn(
            "SQLite was not opened or changed",
            output.getvalue(),
        )
        self.assertNotIn(
            "https://",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
