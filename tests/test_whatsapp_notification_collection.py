import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from collect_whatsapp_notifications import (  # noqa: E402
    _watch_parent_process,
    consume_notification_lines,
)
from database import (  # noqa: E402
    get_database_counts,
    initialize_database,
)
from whatsapp_notifications import (  # noqa: E402
    WhatsAppNotificationConfig,
    WhatsAppNotificationError,
    build_notification_source_message_id,
    load_notification_config,
    parse_diagnostic_record,
    parse_notification_record,
)
from whatsapp_sources import (  # noqa: E402
    SUPPORTED_WHATSAPP_GROUPS,
    prepare_whatsapp_parser,
)


APP_ID = (
    "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!Fixture"
)
GROUP_ID = "whatsapp_example_digest"
GROUP_NAME = SUPPORTED_WHATSAPP_GROUPS[GROUP_ID].group_name
CREATION_TIME = "2026-01-02T03:04:05.0000000Z"


def make_config() -> WhatsAppNotificationConfig:
    return WhatsAppNotificationConfig(
        version=1,
        app_user_model_id=APP_ID,
        group_name=GROUP_NAME,
        group_identifier=GROUP_ID,
        poll_interval_seconds=5,
        max_notifications_per_poll=200,
    )


def make_notification_record(
    *,
    notification_id: int = 42,
    group_identifier: str = GROUP_ID,
    body_lines: list[str] | None = None,
) -> str:
    source_message_id = build_notification_source_message_id(
        app_user_model_id=APP_ID,
        group_identifier=group_identifier,
        notification_id=notification_id,
        creation_time=CREATION_TIME,
    )
    return json.dumps(
        {
            "type": "notification",
            "protocol_version": 1,
            "group_identifier": group_identifier,
            "source_message_id": source_message_id,
            "message_date": CREATION_TIME,
            "body_lines": body_lines
            or [
                "Synthetic Sender: *Junior Backend Developer*",
                "📈 Level: Junior",
                "Posted today",
                "https://jobs.example.com/postings/999",
            ],
        },
        ensure_ascii=False,
    )


class WhatsAppNotificationProtocolTests(unittest.TestCase):
    def test_parent_watch_stops_native_child(
        self,
    ) -> None:
        process = Mock()
        process.poll.return_value = None

        with (
            patch(
                "collect_whatsapp_notifications.sys.platform",
                "win32",
            ),
            patch(
                "collect_whatsapp_notifications."
                "_wait_for_windows_process_exit",
                return_value=True,
            ),
        ):
            _watch_parent_process(
                1234,
                process,
                threading.Event(),
            )

        process.terminate.assert_called_once_with()

    def test_accepted_record_becomes_source_message(self) -> None:
        message = parse_notification_record(
            make_notification_record(),
            expected_group_identifier=GROUP_ID,
        )

        self.assertEqual(message.sender, "Synthetic Sender")
        self.assertEqual(
            message.raw_text.splitlines()[0],
            "*Junior Backend Developer*",
        )
        self.assertEqual(
            message.urls,
            ("https://jobs.example.com/postings/999",),
        )
        self.assertTrue(
            str(message.source_message_id).startswith(
                "wa_notification_"
            )
        )

    def test_separate_sender_element_is_not_stored_in_body(
        self,
    ) -> None:
        message = parse_notification_record(
            make_notification_record(
                body_lines=[
                    "Synthetic Sender",
                    "*Junior Backend Developer*",
                    "📈 Level: Junior",
                    "https://jobs.example.com/postings/999",
                ]
            ),
            expected_group_identifier=GROUP_ID,
        )

        self.assertEqual(message.sender, "Synthetic Sender")
        self.assertTrue(
            message.raw_text.startswith(
                "*Junior Backend Developer*"
            )
        )

    def test_wrong_group_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            WhatsAppNotificationError,
            "wrong group identifier",
        ):
            parse_notification_record(
                make_notification_record(
                    group_identifier="wa_other_fixture"
                ),
                expected_group_identifier=GROUP_ID,
            )

    def test_oversized_body_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            WhatsAppNotificationError,
            "size limit",
        ):
            parse_notification_record(
                make_notification_record(
                    body_lines=["x" * 4097]
                ),
                expected_group_identifier=GROUP_ID,
            )

    def test_diagnostic_contains_only_aggregate_fields(
        self,
    ) -> None:
        line = json.dumps(
            {
                "type": "diagnostic",
                "protocol_version": 1,
                "total_notifications": 8,
                "application_identity_errors": 2,
                "allowed_app_notifications": 3,
                "exact_group_notifications": 2,
                "accepted_notifications": 1,
                "oversized_notifications": 1,
            }
        )

        summary = parse_diagnostic_record(line)

        self.assertEqual(summary.total_notifications, 8)
        self.assertEqual(summary.application_identity_errors, 2)
        self.assertEqual(summary.accepted_notifications, 1)
        self.assertIsNone(summary.application_info_errors)
        self.assertIsNone(summary.app_user_model_id_errors)
        self.assertIsNone(
            summary.reconstructed_application_identities
        )

    def test_diagnostic_accepts_stage_specific_identity_counts(
        self,
    ) -> None:
        line = json.dumps(
            {
                "type": "diagnostic",
                "protocol_version": 1,
                "total_notifications": 8,
                "application_identity_errors": 1,
                "application_info_errors": 1,
                "app_user_model_id_errors": 2,
                "reconstructed_application_identities": 2,
                "allowed_app_notifications": 3,
                "exact_group_notifications": 2,
                "accepted_notifications": 1,
                "oversized_notifications": 1,
            }
        )

        summary = parse_diagnostic_record(line)

        self.assertEqual(summary.application_info_errors, 1)
        self.assertEqual(summary.app_user_model_id_errors, 2)
        self.assertEqual(
            summary.reconstructed_application_identities,
            2,
        )
        self.assertIsNone(
            summary.application_info_error_categories
        )

    def test_diagnostic_accepts_safe_error_categories(
        self,
    ) -> None:
        line = json.dumps(
            {
                "type": "diagnostic",
                "protocol_version": 1,
                "total_notifications": 8,
                "application_identity_errors": 1,
                "application_info_errors": 1,
                "application_info_error_categories": {
                    "COMException, HRESULT 0x80004001": 1,
                },
                "official_package_family_matches": 3,
                "app_user_model_id_errors": 0,
                "reconstructed_application_identities": 0,
                "allowed_app_notifications": 0,
                "exact_group_notifications": 0,
                "accepted_notifications": 0,
                "oversized_notifications": 0,
                "visual_inspection_errors": 1,
            }
        )

        summary = parse_diagnostic_record(line)

        self.assertEqual(
            summary.application_info_error_categories,
            {"COMException, HRESULT 0x80004001": 1},
        )
        self.assertEqual(
            summary.official_package_family_matches,
            3,
        )
        self.assertEqual(
            summary.visual_inspection_errors,
            1,
        )

    def test_diagnostic_rejects_private_error_category_text(
        self,
    ) -> None:
        line = json.dumps(
            {
                "type": "diagnostic",
                "protocol_version": 1,
                "total_notifications": 8,
                "application_identity_errors": 1,
                "application_info_errors": 1,
                "application_info_error_categories": {
                    "private notification text": 1,
                },
                "app_user_model_id_errors": 0,
                "reconstructed_application_identities": 0,
                "allowed_app_notifications": 0,
                "exact_group_notifications": 0,
                "accepted_notifications": 0,
                "oversized_notifications": 0,
            }
        )

        with self.assertRaisesRegex(
            WhatsAppNotificationError,
            "category is invalid",
        ):
            parse_diagnostic_record(line)

    def test_diagnostic_rejects_partial_identity_counts(
        self,
    ) -> None:
        line = json.dumps(
            {
                "type": "diagnostic",
                "protocol_version": 1,
                "total_notifications": 8,
                "application_identity_errors": 1,
                "application_info_errors": 1,
                "allowed_app_notifications": 3,
                "exact_group_notifications": 2,
                "accepted_notifications": 1,
                "oversized_notifications": 1,
            }
        )

        with self.assertRaisesRegex(
            WhatsAppNotificationError,
            "unexpected fields",
        ):
            parse_diagnostic_record(line)

    def test_source_id_is_stable_for_notification_updates(
        self,
    ) -> None:
        first = build_notification_source_message_id(
            app_user_model_id=APP_ID,
            group_identifier=GROUP_ID,
            notification_id=42,
            creation_time=CREATION_TIME,
        )
        repeated = build_notification_source_message_id(
            app_user_model_id=APP_ID,
            group_identifier=GROUP_ID,
            notification_id=42,
            creation_time=CREATION_TIME,
        )

        self.assertEqual(first, repeated)


class WhatsAppNotificationConfigTests(unittest.TestCase):
    def test_loads_exact_local_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "app_user_model_id": APP_ID,
                        "group_name": GROUP_NAME,
                        "group_identifier": GROUP_ID,
                        "poll_interval_seconds": 5,
                        "max_notifications_per_poll": 200,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            config = load_notification_config(path)

        self.assertEqual(config.app_user_model_id, APP_ID)
        self.assertEqual(config.group_name, GROUP_NAME)

    def test_rejects_non_official_application_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "app_user_model_id": "fixture.other!App",
                        "group_name": GROUP_NAME,
                        "group_identifier": GROUP_ID,
                        "poll_interval_seconds": 5,
                        "max_notifications_per_poll": 200,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                WhatsAppNotificationError,
                "official WhatsApp",
            ):
                load_notification_config(path)


class WhatsAppNotificationImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        initialize_database(self.connection)
        self.context, self.registry = (
            prepare_whatsapp_parser(
                GROUP_ID,
                exact_group_name=GROUP_NAME,
            )
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_import_uses_shared_transactional_deduplication(
        self,
    ) -> None:
        line = make_notification_record()

        first = consume_notification_lines(
            (line,),
            config=make_config(),
            context=self.context,
            parser_registry=self.registry,
            connection=self.connection,
        )
        repeated = consume_notification_lines(
            (line,),
            config=make_config(),
            context=self.context,
            parser_registry=self.registry,
            connection=self.connection,
        )

        self.assertEqual(
            (
                first.source_messages_checked,
                first.jobs_parsed,
                first.new_jobs,
                first.new_postings,
            ),
            (1, 1, 1, 1),
        )
        self.assertEqual(
            (
                repeated.new_jobs,
                repeated.new_postings,
                repeated.existing_postings,
            ),
            (0, 0, 1),
        )
        self.assertEqual(get_database_counts(self.connection), (1, 1))

    def test_dry_run_does_not_require_a_database(
        self,
    ) -> None:
        totals = consume_notification_lines(
            (make_notification_record(),),
            config=make_config(),
            context=self.context,
            parser_registry=self.registry,
            connection=None,
        )

        self.assertEqual(
            (
                totals.source_messages_checked,
                totals.jobs_parsed,
                totals.new_jobs,
            ),
            (1, 1, 0),
        )

    def test_title_only_job_notification_is_parsed_without_url(
        self,
    ) -> None:
        totals = consume_notification_lines(
            (
                make_notification_record(
                    body_lines=[
                        (
                            "Synthetic Sender: "
                            "Junior Algorithm Engineer"
                        )
                    ]
                ),
            ),
            config=make_config(),
            context=self.context,
            parser_registry=self.registry,
            connection=self.connection,
        )

        self.assertEqual(
            (
                totals.source_messages_checked,
                totals.jobs_parsed,
                totals.new_jobs,
                totals.new_postings,
            ),
            (1, 1, 1, 1),
        )

        stored_job = self.connection.execute(
            """
            SELECT title, job_url
            FROM jobs
            """
        ).fetchone()
        self.assertEqual(
            stored_job["title"],
            "Junior Algorithm Engineer",
        )
        self.assertIsNone(stored_job["job_url"])

    def test_notification_title_without_markdown_is_parsed(
        self,
    ) -> None:
        totals = consume_notification_lines(
            (
                make_notification_record(
                    body_lines=[
                        (
                            "Synthetic Sender: "
                            "Junior Algorithm Engineer"
                        ),
                        "📈 Level: Junior",
                        "https://jobs.example.com/postings/999",
                    ]
                ),
            ),
            config=make_config(),
            context=self.context,
            parser_registry=self.registry,
            connection=self.connection,
        )

        self.assertEqual(
            (
                totals.source_messages_checked,
                totals.jobs_parsed,
                totals.new_jobs,
                totals.new_postings,
            ),
            (1, 1, 1, 1),
        )

        stored_title = self.connection.execute(
            "SELECT title FROM jobs"
        ).fetchone()["title"]
        self.assertEqual(
            stored_title,
            "Junior Algorithm Engineer",
        )

    def test_non_job_notification_summary_is_not_parsed(
        self,
    ) -> None:
        totals = consume_notification_lines(
            (
                make_notification_record(
                    body_lines=[
                        "Synthetic Sender: Thanks for sharing"
                    ]
                ),
            ),
            config=make_config(),
            context=self.context,
            parser_registry=self.registry,
            connection=None,
        )

        self.assertEqual(
            (
                totals.source_messages_checked,
                totals.jobs_parsed,
            ),
            (1, 0),
        )


if __name__ == "__main__":
    unittest.main()
