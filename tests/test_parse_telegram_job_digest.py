import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from parse_telegram_job_digest import (  # noqa: E402
    parse_telegram_job_digest_message,
    parse_posted_date,
    split_title_and_company,
)
from source_parsing import SourceContext  # noqa: E402


class FakeTelegramMessage:
    id = 42
    raw_text = (
        "Backend Developer @ Example\n"
        "Posted on: 27-07-2026\n"
        "Location: Example City\n"
        "https://jobs.example.com/42"
    )
    date = datetime(
        2026,
        7,
        27,
        9,
        0,
        tzinfo=timezone.utc,
    )
    buttons: list[object] = []
    media = None

    @staticmethod
    def get_entities_text() -> list[object]:
        return []


class ParserHelperTests(unittest.TestCase):
    def test_parses_supported_dates(self) -> None:
        cases = {
            "27-07-2026": "2026-07-27",
            "27/07/2026": "2026-07-27",
            "2026-07-27": "2026-07-27",
        }

        for raw_value, expected in cases.items():
            with self.subTest(raw_value=raw_value):
                self.assertEqual(
                    parse_posted_date(raw_value),
                    expected,
                )

    def test_rejects_invalid_date(self) -> None:
        self.assertIsNone(
            parse_posted_date("not a date")
        )

    def test_splits_title_and_company_from_right(
        self,
    ) -> None:
        self.assertEqual(
            split_title_and_company(
                "R&D @ Platform @ Example"
            ),
            (
                "R&D @ Platform",
                "Example",
            ),
        )

    def test_parser_accepts_source_context(self) -> None:
        parsed_job = parse_telegram_job_digest_message(
            message=FakeTelegramMessage(),
            context=SourceContext(
                source="telegram",
                group_name="Tech Jobs",
                group_identifier="tech_jobs",
            ),
        )

        self.assertIsNotNone(parsed_job)
        self.assertEqual(
            parsed_job.source_group,
            "Tech Jobs",
        )
        self.assertEqual(
            parsed_job.source_message_url,
            "https://t.me/tech_jobs/42",
        )

    def test_parser_preserves_legacy_arguments(self) -> None:
        parsed_job = parse_telegram_job_digest_message(
            message=FakeTelegramMessage(),
            channel_title="Tech Jobs",
            channel_username="tech_jobs",
        )

        self.assertIsNotNone(parsed_job)
        self.assertEqual(
            parsed_job.title,
            "Backend Developer",
        )


if __name__ == "__main__":
    unittest.main()
