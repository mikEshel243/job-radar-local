import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
FIXTURE_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "native_exports"
)

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from native_exports import (  # noqa: E402
    NativeExportFormatError,
    read_telegram_json_export,
    read_whatsapp_export,
)


class WhatsAppNativeExportTests(unittest.TestCase):
    def test_preserves_multiline_messages_and_system_records(
        self,
    ) -> None:
        messages = read_whatsapp_export(
            FIXTURE_PATH / "whatsapp_example_digest.txt",
            "whatsapp_example_digest",
        )

        self.assertEqual(len(messages), 7)
        self.assertIsNone(messages[0].sender)
        self.assertEqual(
            messages[2].message_date,
            "2026-07-23T19:11:00",
        )
        self.assertIn(
            "\n\nhttps://jobs.example.com/postings/101",
            messages[2].raw_text,
        )
        self.assertEqual(
            len(messages[2].urls),
            2,
        )

    def test_repeated_messages_get_stable_distinct_ids(
        self,
    ) -> None:
        fixture = (
            FIXTURE_PATH / "whatsapp_example_digest.txt"
        )
        first_read = read_whatsapp_export(
            fixture,
            "whatsapp_example_digest",
        )
        second_read = read_whatsapp_export(
            fixture,
            "whatsapp_example_digest",
        )

        self.assertEqual(
            first_read[-2].raw_text,
            first_read[-1].raw_text,
        )
        self.assertNotEqual(
            first_read[-2].source_message_id,
            first_read[-1].source_message_id,
        )
        self.assertEqual(
            [message.source_message_id for message in first_read],
            [message.source_message_id for message in second_read],
        )

    def test_rejects_unknown_text_before_first_header(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            export_path = (
                Path(temp_directory) / "invalid.txt"
            )
            export_path.write_text(
                "not a native header\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                NativeExportFormatError,
                "before the first WhatsApp header",
            ):
                read_whatsapp_export(
                    export_path,
                    "whatsapp_example_digest",
                )


class TelegramNativeExportTests(unittest.TestCase):
    def test_reads_native_metadata_entities_and_service_records(
        self,
    ) -> None:
        native_export = read_telegram_json_export(
            FIXTURE_PATH / "telegram_example_digest.json"
        )

        self.assertEqual(
            native_export.group_name,
            "Example Telegram Job Digest",
        )
        self.assertEqual(
            native_export.export_type,
            "public_channel",
        )
        self.assertEqual(
            native_export.skipped_service_records,
            1,
        )
        self.assertEqual(len(native_export.messages), 4)

        first_job = native_export.messages[0]
        self.assertEqual(first_job.source_message_id, 2)
        self.assertIn(
            "Backend Developer @ Example",
            first_job.raw_text,
        )
        self.assertEqual(
            first_job.urls,
            (
                "https://jobs.example.com/101"
                "?utm_source=fixture",
            ),
        )
        self.assertEqual(
            first_job.message_date,
            "2025-01-22T10:00:00+00:00",
        )

    def test_rejects_an_unseen_export_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            export_path = (
                Path(temp_directory) / "invalid.json"
            )
            export_path.write_text(
                (
                    '{"name":"Example","type":"private_group",'
                    '"id":1,"messages":[]}'
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                NativeExportFormatError,
                "public_channel",
            ):
                read_telegram_json_export(export_path)


if __name__ == "__main__":
    unittest.main()
