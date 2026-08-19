import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from evaluate_jobs import main  # noqa: E402


class EvaluateJobsTests(unittest.TestCase):
    def test_quiet_mode_skips_human_readable_report(
        self,
    ) -> None:
        connection = MagicMock()

        with (
            patch(
                "evaluate_jobs.parse_arguments",
                return_value=SimpleNamespace(
                    quiet=True,
                    show_rejected=False,
                ),
            ),
            patch(
                "evaluate_jobs.load_profile",
                return_value={"profile_name": "fixture"},
            ),
            patch(
                "evaluate_jobs.connect_database",
                return_value=connection,
            ),
            patch("evaluate_jobs.initialize_database"),
            patch("evaluate_jobs.ensure_job_analysis_table"),
            patch("evaluate_jobs.ensure_evaluation_table"),
            patch(
                "evaluate_jobs.evaluate_stored_jobs",
                return_value=3,
            ),
            patch("builtins.print") as print_mock,
        ):
            main()

        connection.execute.assert_not_called()
        print_mock.assert_not_called()
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
