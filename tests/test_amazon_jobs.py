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
    / "amazon_jobs_email.eml"
)

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from amazon_jobs import (  # noqa: E402
    AmazonJobImportError,
    parse_amazon_job_email,
    parse_amazon_recommendations_clipboard,
)
from collect_amazon_job_emails import (  # noqa: E402
    process_amazon_gmail_messages,
)
from gmail_jobs import fetch_amazon_job_email_mime  # noqa: E402


class AmazonJobParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_message = FIXTURE_PATH.read_bytes()

    def test_parses_email_cards_and_discards_tracking(self) -> None:
        parsed = parse_amazon_job_email(self.raw_message)

        self.assertEqual(len(parsed.jobs), 2)
        self.assertEqual(parsed.skipped_cards, 0)
        self.assertEqual(
            parsed.message_date,
            "2026-08-01T08:00:00+00:00",
        )
        first = parsed.jobs[0]
        self.assertEqual(
            first.title,
            "Software Development Engineer, Systems",
        )
        self.assertEqual(first.company, "Amazon")
        self.assertEqual(first.location, "Example City, EXL")
        self.assertEqual(
            first.job_url,
            "https://www.amazon.jobs/jobs/12345001",
        )
        self.assertIn("3+ years", first.raw_text)
        self.assertNotIn("awstrack", first.raw_text)
        self.assertEqual(
            parsed.jobs[1].company,
            "Amazon",
        )

    def test_rejects_another_sender(self) -> None:
        changed = self.raw_message.replace(
            b"noreply@mail.amazon.jobs",
            b"someone@example.invalid",
            1,
        )

        with self.assertRaisesRegex(AmazonJobImportError, "sender"):
            parse_amazon_job_email(changed)

    def test_clipboard_plain_text_parses_complete_loaded_list(self) -> None:
        jobs = parse_amazon_recommendations_clipboard(
            text=(
                "Software Engineer\n"
                "Example City, EXL\n"
                "Job ID: 22345001\n"
                "Basic Qualifications\n"
                "2+ years of Java experience.\n"
                "Contact candidate@example.com through the demo form.\n"
                "Backend Developer\n"
                "Demo Metro, Exampleland\n"
                "Job ID: 22345002\n"
                "Basic Qualifications\n"
                "Experience with Linux and Docker.\n"
                "Amazon is an equal opportunity employer."
            )
        )

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].source, "amazon_manual")
        self.assertEqual(jobs[0].title, "Software Engineer")
        self.assertEqual(jobs[0].location, "Example City, EXL")
        self.assertNotIn("candidate@example.com", jobs[0].raw_text)
        self.assertNotIn("+972", jobs[0].raw_text)
        self.assertNotIn("equal opportunity", jobs[1].raw_text.casefold())

    def test_clipboard_prefers_html_links(self) -> None:
        html = """
            <article>
                <a href="https://www.amazon.jobs/en/jobs/32345001/test">
                    Systems Developer
                </a>
                <span>Example City, Exampleland</span>
                <h3>Basic Qualifications</h3>
                <p>Java and networking experience.</p>
            </article>
        """
        jobs = parse_amazon_recommendations_clipboard(
            text="Rendered fallback without a Job ID line",
            html=html,
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Systems Developer")
        self.assertEqual(
            jobs[0].job_url,
            "https://www.amazon.jobs/jobs/32345001",
        )


class AmazonGmailCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_message = FIXTURE_PATH.read_bytes()

    def test_fetches_only_exact_bounded_amazon_messages(self) -> None:
        service = MagicMock()
        messages_api = service.users.return_value.messages.return_value
        messages_api.list.return_value.execute.return_value = {
            "messages": [{"id": "synthetic-amazon-message"}]
        }
        encoded = base64.urlsafe_b64encode(
            self.raw_message
        ).decode("ascii").rstrip("=")
        messages_api.get.return_value.execute.return_value = {
            "raw": encoded
        }

        messages = fetch_amazon_job_email_mime(
            service,
            newer_than_days=7,
            max_messages=3,
        )

        self.assertEqual(messages, (self.raw_message,))
        list_kwargs = messages_api.list.call_args.kwargs
        self.assertEqual(list_kwargs["maxResults"], 3)
        self.assertIn(
            "from:noreply@mail.amazon.jobs",
            list_kwargs["q"],
        )
        self.assertIn(
            'subject:"Recommended Amazon jobs for"',
            list_kwargs["q"],
        )
        self.assertIn("newer_than:7d", list_kwargs["q"])

    def test_dry_run_deduplicates_without_database(self) -> None:
        summary = process_amazon_gmail_messages(
            (self.raw_message, self.raw_message),
            connection=None,
        )

        self.assertEqual(summary.emails_checked, 2)
        self.assertEqual(summary.jobs_parsed, 2)
        self.assertEqual(summary.duplicate_cards, 2)
        self.assertEqual(summary.new_jobs, 0)

    def test_import_is_idempotent_and_analyzes_summaries(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        try:
            first = process_amazon_gmail_messages(
                (self.raw_message,),
                connection=connection,
            )
            second = process_amazon_gmail_messages(
                (self.raw_message,),
                connection=connection,
            )

            self.assertEqual(first.new_jobs, 2)
            self.assertEqual(first.new_postings, 2)
            self.assertEqual(first.analyzed_jobs, 2)
            self.assertEqual(first.evaluated_jobs, 2)
            self.assertEqual(second.new_jobs, 0)
            self.assertEqual(second.new_postings, 0)
            self.assertEqual(second.existing_postings, 2)
            detail_rows = connection.execute(
                """
                SELECT fetch_status, extractor
                FROM job_details
                ORDER BY job_id
                """
            ).fetchall()
            self.assertEqual(len(detail_rows), 2)
            self.assertTrue(
                all(
                    row["fetch_status"]
                    == "source_automation_prohibited"
                    for row in detail_rows
                )
            )
            self.assertTrue(
                all(
                    row["extractor"] == "amazon_email"
                    for row in detail_rows
                )
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
