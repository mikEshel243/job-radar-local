import base64
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
FIXTURE_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "email"
    / "linkedin_jobs_email.eml"
)

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from collect_linkedin_job_emails import (  # noqa: E402
    process_linkedin_gmail_messages,
)
from gmail_jobs import (  # noqa: E402
    fetch_linkedin_job_email_mime,
)
from linkedin_job_emails import (  # noqa: E402
    LinkedInJobEmailError,
    parse_linkedin_job_email,
)


class LinkedInJobEmailParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_message = FIXTURE_PATH.read_bytes()

    def test_parses_cards_and_removes_tracking(self) -> None:
        parsed = parse_linkedin_job_email(
            self.raw_message
        )

        self.assertEqual(len(parsed.jobs), 2)
        self.assertEqual(parsed.skipped_cards, 0)
        self.assertEqual(
            parsed.message_date,
            "2026-08-01T08:30:00+03:00",
        )

        first = parsed.jobs[0]
        self.assertEqual(first.title, "Backend Developer")
        self.assertEqual(first.company, "Example Systems")
        self.assertEqual(
            first.location,
            "Example City, Exampleland (Hybrid)",
        )
        self.assertEqual(
            first.job_url,
            "https://www.linkedin.com/jobs/view/123456789",
        )
        self.assertNotIn("trackingId", first.raw_text)
        self.assertNotIn("trkEmail", first.raw_text)

    def test_rejects_another_sender_before_parsing(self) -> None:
        changed = self.raw_message.replace(
            b"jobs-noreply@linkedin.com",
            b"someone@example.invalid",
            1,
        )

        with self.assertRaisesRegex(
            LinkedInJobEmailError,
            "sender",
        ):
            parse_linkedin_job_email(changed)

    def test_parses_verified_job_alert_sender_template(self) -> None:
        changed = self.raw_message.replace(
            b"jobs-noreply@linkedin.com",
            b"jobalerts-noreply@linkedin.com",
            1,
        ).replace(
            b"text-color-brand",
            b"text-system-blue-50",
        )

        parsed = parse_linkedin_job_email(changed)

        self.assertEqual(len(parsed.jobs), 2)
        self.assertEqual(parsed.jobs[0].company, "Example Systems")

    def test_rejects_message_without_supported_cards(self) -> None:
        changed = self.raw_message.replace(
            b"text-color-brand",
            b"unrelated-link",
        )

        with self.assertRaisesRegex(
            LinkedInJobEmailError,
            "no supported job cards",
        ):
            parse_linkedin_job_email(changed)


class GmailJobCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_message = FIXTURE_PATH.read_bytes()

    def test_fetches_only_bounded_raw_messages(self) -> None:
        service = MagicMock()
        messages_api = (
            service.users.return_value.messages.return_value
        )
        messages_api.list.return_value.execute.return_value = {
            "messages": [{"id": "synthetic-message"}]
        }
        encoded = base64.urlsafe_b64encode(
            self.raw_message
        ).decode("ascii").rstrip("=")
        messages_api.get.return_value.execute.return_value = {
            "raw": encoded
        }

        messages = fetch_linkedin_job_email_mime(
            service,
            newer_than_days=7,
            max_messages=3,
        )

        self.assertEqual(messages, (self.raw_message,))
        list_kwargs = messages_api.list.call_args.kwargs
        self.assertEqual(list_kwargs["maxResults"], 3)
        self.assertIn(
            "from:jobs-noreply@linkedin.com",
            list_kwargs["q"],
        )
        self.assertIn(
            "from:jobalerts-noreply@linkedin.com",
            list_kwargs["q"],
        )
        self.assertIn("newer_than:7d", list_kwargs["q"])
        self.assertEqual(
            messages_api.get.call_args.kwargs["format"],
            "raw",
        )

    def test_rejects_unbounded_gmail_requests(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "max_messages",
        ):
            fetch_linkedin_job_email_mime(
                MagicMock(),
                max_messages=51,
            )

    def test_dry_run_deduplicates_without_database(self) -> None:
        summary = process_linkedin_gmail_messages(
            (self.raw_message, self.raw_message),
            connection=None,
        )

        self.assertEqual(summary.emails_checked, 2)
        self.assertEqual(summary.jobs_parsed, 2)
        self.assertEqual(summary.duplicate_cards, 2)
        self.assertEqual(summary.new_jobs, 0)

    def test_import_is_idempotent_and_evaluates_jobs(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        try:
            first = process_linkedin_gmail_messages(
                (self.raw_message,),
                connection=connection,
            )
            second = process_linkedin_gmail_messages(
                (self.raw_message,),
                connection=connection,
            )

            self.assertEqual(first.new_jobs, 2)
            self.assertEqual(first.new_postings, 2)
            self.assertEqual(first.evaluated_jobs, 2)
            self.assertEqual(second.new_jobs, 0)
            self.assertEqual(second.new_postings, 0)
            self.assertEqual(second.existing_postings, 2)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM job_evaluations"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
