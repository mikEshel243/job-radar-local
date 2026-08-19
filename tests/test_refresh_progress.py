import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from refresh_progress import (  # noqa: E402
    read_refresh_progress,
    safe_refresh_progress,
    write_refresh_progress,
)


class RefreshProgressTests(unittest.TestCase):
    def test_round_trip_contains_only_aggregate_updates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            progress_path = (
                Path(directory) / "refresh.json"
            )
            write_refresh_progress(
                progress_path,
                stage_key="telegram_collection",
                progress_mode="determinate",
                progress_completed=1,
                progress_total=3,
                progress_unit="sources",
            )

            payload = read_refresh_progress(progress_path)

        self.assertEqual(
            payload,
            {
                "stage_key": "telegram_collection",
                "progress_mode": "determinate",
                "progress_completed": 1,
                "progress_total": 3,
                "progress_unit": "sources",
            },
        )

    def test_api_filter_rejects_identity_and_output_fields(
        self,
    ) -> None:
        payload = {
            "stage_key": "telegram_collection",
            "stage_index": 1,
            "stage_count": 4,
            "completed_stages": 0,
            "progress_mode": "determinate",
            "progress_completed": 2,
            "progress_total": 3,
            "progress_unit": "sources",
            "source_name": "private fixture",
            "process_output": "private fixture output",
            "path": "private fixture path",
        }

        safe = safe_refresh_progress(payload)

        self.assertEqual(
            safe,
            {
                "stage_key": "telegram_collection",
                "stage_index": 1,
                "stage_count": 4,
                "completed_stages": 0,
                "progress_mode": "determinate",
                "progress_completed": 2,
                "progress_total": 3,
                "progress_unit": "sources",
            },
        )
        serialized = json.dumps(safe)
        self.assertNotIn("private fixture", serialized)

    def test_api_filter_rejects_unknown_stage_key(
        self,
    ) -> None:
        safe = safe_refresh_progress(
            {
                "stage_key": "private_source_identity",
                "progress_mode": "indeterminate",
            }
        )

        self.assertNotIn("stage_key", safe)

    def test_api_filter_accepts_message_progress(
        self,
    ) -> None:
        safe = safe_refresh_progress(
            {
                "stage_key": "telegram_collection",
                "progress_mode": "determinate",
                "progress_completed": 1,
                "progress_total": 3,
                "progress_unit": "messages",
            }
        )

        self.assertEqual(
            safe,
            {
                "stage_key": "telegram_collection",
                "progress_mode": "determinate",
                "progress_completed": 1,
                "progress_total": 3,
                "progress_unit": "messages",
            },
        )

    def test_api_filter_accepts_job_progress(
        self,
    ) -> None:
        safe = safe_refresh_progress(
            {
                "stage_key": "job_page_enrichment",
                "progress_mode": "determinate",
                "progress_completed": 2,
                "progress_total": 5,
                "progress_unit": "jobs",
            }
        )

        self.assertEqual(
            safe,
            {
                "stage_key": "job_page_enrichment",
                "progress_mode": "determinate",
                "progress_completed": 2,
                "progress_total": 5,
                "progress_unit": "jobs",
            },
        )

    def test_api_filter_accepts_public_source_progress(
        self,
    ) -> None:
        safe = safe_refresh_progress(
            {
                "stage_key": "public_source_collection",
                "progress_mode": "determinate",
                "progress_completed": 4,
                "progress_total": 23,
                "progress_unit": "sources",
            }
        )

        self.assertEqual(
            safe,
            {
                "stage_key": "public_source_collection",
                "progress_mode": "determinate",
                "progress_completed": 4,
                "progress_total": 23,
                "progress_unit": "sources",
            },
        )

    def test_api_filter_accepts_aggregate_telegram_control_state(
        self,
    ) -> None:
        safe = safe_refresh_progress(
            {
                "stage_key": "telegram_collection",
                "telegram_collection_outcome": "cooldown",
                "telegram_cooldown_seconds_remaining": 180,
                "telegram_safe_limit_reached": False,
                "telegram_group": "private fixture",
            }
        )

        self.assertEqual(
            safe,
            {
                "stage_key": "telegram_collection",
                "telegram_collection_outcome": "cooldown",
                "telegram_cooldown_seconds_remaining": 180,
                "telegram_safe_limit_reached": False,
            },
        )
        self.assertNotIn("telegram_group", safe)

        rejected = safe_refresh_progress(
            {
                "telegram_collection_outcome": "private fixture",
                "telegram_cooldown_seconds_remaining": True,
                "telegram_safe_limit_reached": 1,
            }
        )
        self.assertEqual(rejected, {})


if __name__ == "__main__":
    unittest.main()
