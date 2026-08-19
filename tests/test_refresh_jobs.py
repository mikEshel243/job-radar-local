import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from refresh_jobs import (  # noqa: E402
    CANCELLED_EXIT_CODE,
    _cancellation_requested,
    build_refresh_stages,
    run_refresh,
    run_stage,
)
from process_lock import (  # noqa: E402
    AlreadyRunningError,
    interprocess_lock,
)


class RefreshWorkflowTests(unittest.TestCase):
    def test_builds_safe_non_interactive_stage_order(
        self,
    ) -> None:
        stages = build_refresh_stages(
            non_interactive=True,
            skip_telegram=False,
            fetch_limit=30,
            fetch_delay=1.0,
        )

        self.assertEqual(
            [stage.name for stage in stages],
            [
                "Public employer collection",
                "Telegram collection",
                "Job-page enrichment",
                "Local description analysis",
                "Local relevance filtering",
            ],
        )
        self.assertIn(
            "--non-interactive",
            stages[1].command,
        )
        self.assertIn(
            "--continue-on-source-errors",
            stages[0].command,
        )
        self.assertNotIn(
            "--retry-failed",
            stages[2].command,
        )
        self.assertNotIn(
            "--reanalyze",
            stages[3].command,
        )
        self.assertIn(
            "--quiet",
            stages[4].command,
        )

    def test_passes_aggregate_progress_file_to_collection(
        self,
    ) -> None:
        progress_path = Path("data") / "fixture_progress.json"
        stages = build_refresh_stages(
            non_interactive=True,
            skip_telegram=False,
            fetch_limit=30,
            fetch_delay=1.0,
            progress_file=progress_path,
        )

        self.assertEqual(
            [stage.key for stage in stages],
            [
                "public_source_collection",
                "telegram_collection",
                "job_page_enrichment",
                "description_analysis",
                "relevance_filtering",
            ],
        )
        self.assertIn(
            "--progress-file",
            stages[0].command,
        )
        self.assertIn(
            str(progress_path),
            stages[0].command,
        )
        self.assertIn(
            "--progress-file",
            stages[1].command,
        )
        self.assertIn(
            str(progress_path),
            stages[1].command,
        )
        self.assertIn(
            "--progress-file",
            stages[2].command,
        )
        self.assertIn(
            str(progress_path),
            stages[2].command,
        )

    def test_skip_telegram_removes_only_collection_stage(
        self,
    ) -> None:
        stages = build_refresh_stages(
            non_interactive=True,
            skip_telegram=True,
            fetch_limit=30,
            fetch_delay=1.0,
        )

        self.assertEqual(
            [stage.key for stage in stages],
            [
                "public_source_collection",
                "job_page_enrichment",
                "description_analysis",
                "relevance_filtering",
            ],
        )
        self.assertFalse(
            any(
                "collect_telegram_jobs.py"
                in " ".join(stage.command)
                for stage in stages
            )
        )

    @patch("refresh_jobs.run_stage")
    def test_writes_structured_stage_progress(
        self,
        run_stage_mock,
    ) -> None:
        run_stage_mock.return_value = 0

        with tempfile.TemporaryDirectory() as directory:
            progress_path = (
                Path(directory) / "refresh.json"
            )
            stages = build_refresh_stages(
                non_interactive=True,
                skip_telegram=False,
                fetch_limit=30,
                fetch_delay=1.0,
                progress_file=progress_path,
            )

            result = run_refresh(
                stages,
                progress_file=progress_path,
            )
            payload = json.loads(
                progress_path.read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            payload,
            {
                "stage_key": "complete",
                "stage_index": 5,
                "stage_count": 5,
                "completed_stages": 5,
                "progress_mode": "determinate",
                "progress_completed": 5,
                "progress_total": 5,
                "progress_unit": "stages",
            },
        )

    def test_skip_public_preserves_local_pipeline(
        self,
    ) -> None:
        stages = build_refresh_stages(
            non_interactive=True,
            skip_telegram=True,
            skip_public=True,
            fetch_limit=30,
            fetch_delay=1.0,
        )

        self.assertEqual(
            [stage.key for stage in stages],
            [
                "job_page_enrichment",
                "description_analysis",
                "relevance_filtering",
            ],
        )

    @patch("refresh_jobs.run_stage")
    def test_stops_after_first_failed_stage(
        self,
        run_stage_mock,
    ) -> None:
        run_stage_mock.side_effect = [0, 75]
        stages = build_refresh_stages(
            non_interactive=True,
            skip_telegram=False,
            fetch_limit=30,
            fetch_delay=1.0,
        )

        result = run_refresh(stages)

        self.assertEqual(result, 75)
        self.assertEqual(run_stage_mock.call_count, 2)

    @patch("refresh_jobs.subprocess.Popen")
    def test_stage_uses_an_independent_process_group(
        self,
        popen_mock,
    ) -> None:
        process = SimpleNamespace(
            pid=12345,
            returncode=0,
            poll=MagicMock(side_effect=[None, 0]),
            wait=MagicMock(return_value=0),
        )
        popen_mock.return_value = process
        stage = build_refresh_stages(
            non_interactive=True,
            skip_telegram=False,
            fetch_limit=30,
            fetch_delay=1.0,
        )[0]
        _cancellation_requested.clear()

        result = run_stage(stage)

        self.assertEqual(result, 0)
        _, kwargs = popen_mock.call_args
        self.assertEqual(
            kwargs["env"]["PYTHONIOENCODING"],
            "utf-8",
        )
        if os.name == "nt":
            self.assertEqual(
                kwargs["creationflags"],
                subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertTrue(kwargs["start_new_session"])

    @patch("refresh_jobs.run_stage")
    def test_cancellation_before_stage_creation(
        self,
        run_stage_mock,
    ) -> None:
        stages = build_refresh_stages(
            non_interactive=True,
            skip_telegram=False,
            fetch_limit=30,
            fetch_delay=1.0,
        )
        _cancellation_requested.set()

        try:
            result = run_refresh(stages)
        finally:
            _cancellation_requested.clear()

        self.assertEqual(result, CANCELLED_EXIT_CODE)
        run_stage_mock.assert_not_called()

    def test_process_lock_rejects_overlap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "refresh.lock"

            with interprocess_lock(
                lock_path,
                description="fixture refresh",
            ):
                with self.assertRaises(
                    AlreadyRunningError
                ):
                    with interprocess_lock(
                        lock_path,
                        description="fixture refresh",
                    ):
                        self.fail(
                            "Overlapping lock was acquired."
                        )


if __name__ == "__main__":
    unittest.main()
