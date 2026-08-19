import argparse
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from process_lock import (
    AlreadyRunningError,
    interprocess_lock,
)
from refresh_progress import write_refresh_progress


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
REFRESH_LOCK_PATH = (
    PROJECT_ROOT
    / "data"
    / "job_refresh.lock"
)
STAGE_POLL_INTERVAL_SECONDS = 0.2
STAGE_GRACEFUL_STOP_SECONDS = 5
STAGE_FORCE_STOP_SECONDS = 5
CANCELLED_EXIT_CODE = 130
_cancellation_requested = threading.Event()


@dataclass(frozen=True, slots=True)
class RefreshStage:
    key: str
    name: str
    command: tuple[str, ...]


def _request_cancellation(
    _signal_number: int,
    _frame: object,
) -> None:
    """Record a dashboard or console cancellation signal."""

    _cancellation_requested.set()


def _install_cancellation_handlers() -> None:
    """Install handlers without doing subprocess work in signal context."""

    signal.signal(
        signal.SIGTERM,
        _request_cancellation,
    )
    signal.signal(
        signal.SIGINT,
        _request_cancellation,
    )

    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signal.signal(
            signal.SIGBREAK,
            _request_cancellation,
        )


def _stage_creation_options() -> dict[str, object]:
    """Give each stage a group that can be stopped independently."""

    if os.name == "nt":
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
            ),
        }

    return {
        "start_new_session": True,
    }


def _wait_for_stage(
    process: subprocess.Popen[bytes],
    timeout: float,
) -> bool:
    """Wait for a stage for a bounded time."""

    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _stop_stage_process_tree(
    process: subprocess.Popen[bytes],
) -> None:
    """Stop one active stage group, then force only that tree."""

    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        pass

    if _wait_for_stage(
        process,
        STAGE_GRACEFUL_STOP_SECONDS,
    ):
        return

    if os.name == "nt":
        try:
            subprocess.run(
                (
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=STAGE_FORCE_STOP_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    _wait_for_stage(
        process,
        STAGE_FORCE_STOP_SECONDS,
    )


def run_stage(stage: RefreshStage) -> int:
    """Run one stage while remaining responsive to cancellation."""

    stage_environment = os.environ.copy()
    stage_environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        stage.command,
        cwd=PROJECT_ROOT,
        env=stage_environment,
        **_stage_creation_options(),
    )

    while process.poll() is None:
        if _cancellation_requested.is_set():
            _stop_stage_process_tree(process)
            return CANCELLED_EXIT_CODE

        _wait_for_stage(
            process,
            STAGE_POLL_INTERVAL_SECONDS,
        )

    return int(process.returncode or 0)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect public and optional Telegram jobs, enrich "
            "them, and recalculate all local match scores."
        )
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Do not permit Telegram login prompts. Required "
            "for dashboard and scheduled automation."
        ),
    )
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help=(
            "Refresh enrichment and filtering from existing "
            "local data without contacting Telegram."
        ),
    )
    parser.add_argument(
        "--skip-public",
        action="store_true",
        help=(
            "Refresh existing local data without collecting "
            "configured public employer sources."
        ),
    )
    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=30,
        help=(
            "Maximum number of previously unfetched job pages. "
            "Default: 30."
        ),
    )
    parser.add_argument(
        "--fetch-delay",
        type=float,
        default=1.0,
        help=(
            "Seconds between job-page requests. Default: 1."
        ),
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        help=(
            "Write aggregate structured progress to this local "
            "JSON file. No process output or source identities "
            "are included."
        ),
    )
    return parser.parse_args()


def build_refresh_stages(
    *,
    non_interactive: bool,
    skip_telegram: bool,
    fetch_limit: int,
    fetch_delay: float,
    progress_file: Path | None = None,
    skip_public: bool = False,
) -> tuple[RefreshStage, ...]:
    python = sys.executable
    stages: list[RefreshStage] = []

    if not skip_public:
        public_command = [
            python,
            str(SRC_PATH / "collect_public_jobs.py"),
            "--continue-on-source-errors",
        ]

        if progress_file is not None:
            public_command.extend(
                (
                    "--progress-file",
                    str(progress_file),
                )
            )

        stages.append(
            RefreshStage(
                key="public_source_collection",
                name="Public employer collection",
                command=tuple(public_command),
            )
        )

    if not skip_telegram:
        telegram_command = [
            python,
            str(SRC_PATH / "collect_telegram_jobs.py"),
        ]

        if non_interactive:
            telegram_command.append("--non-interactive")

        if progress_file is not None:
            telegram_command.extend(
                (
                    "--progress-file",
                    str(progress_file),
                )
            )

        stages.append(
            RefreshStage(
                key="telegram_collection",
                name="Telegram collection",
                command=tuple(telegram_command),
            )
        )

    fetch_command = [
        python,
        str(SRC_PATH / "fetch_job_details.py"),
        "--limit",
        str(fetch_limit),
        "--delay",
        str(fetch_delay),
    ]

    if progress_file is not None:
        fetch_command.extend(
            (
                "--progress-file",
                str(progress_file),
            )
        )

    stages.extend(
        (
            RefreshStage(
                key="job_page_enrichment",
                name="Job-page enrichment",
                command=tuple(fetch_command),
            ),
            RefreshStage(
                key="description_analysis",
                name="Local description analysis",
                command=(
                    python,
                    str(SRC_PATH / "analyze_job_details.py"),
                    "--limit",
                    str(fetch_limit),
                ),
            ),
            RefreshStage(
                key="relevance_filtering",
                name="Local relevance filtering",
                command=(
                    python,
                    str(SRC_PATH / "evaluate_jobs.py"),
                    "--quiet",
                ),
            ),
        )
    )

    return tuple(stages)


def run_refresh(
    stages: tuple[RefreshStage, ...],
    *,
    progress_file: Path | None = None,
) -> int:
    stage_count = len(stages)

    write_refresh_progress(
        progress_file,
        stage_key="starting",
        stage_index=0,
        stage_count=stage_count,
        completed_stages=0,
        progress_mode="indeterminate",
        progress_completed=None,
        progress_total=None,
        progress_unit=None,
    )

    for stage_index, stage in enumerate(
        stages,
        start=1,
    ):
        if _cancellation_requested.is_set():
            return CANCELLED_EXIT_CODE

        write_refresh_progress(
            progress_file,
            stage_key=stage.key,
            stage_index=stage_index,
            stage_count=stage_count,
            completed_stages=stage_index - 1,
            progress_mode="indeterminate",
            progress_completed=None,
            progress_total=None,
            progress_unit=None,
        )
        print(f"Starting: {stage.name}")

        return_code = run_stage(stage)

        if return_code != 0:
            write_refresh_progress(
                progress_file,
                stage_key=stage.key,
                stage_index=stage_index,
                stage_count=stage_count,
                completed_stages=stage_index - 1,
                progress_mode="indeterminate",
                progress_completed=None,
                progress_total=None,
                progress_unit=None,
            )
            print(
                f"Stopped: {stage.name} failed with "
                f"exit code {return_code}."
            )
            return return_code

        write_refresh_progress(
            progress_file,
            stage_key=stage.key,
            stage_index=stage_index,
            stage_count=stage_count,
            completed_stages=stage_index,
            progress_mode="indeterminate",
            progress_completed=None,
            progress_total=None,
            progress_unit=None,
        )
        print(f"Completed: {stage.name}")

    write_refresh_progress(
        progress_file,
        stage_key="complete",
        stage_index=stage_count,
        stage_count=stage_count,
        completed_stages=stage_count,
        progress_mode="determinate",
        progress_completed=stage_count,
        progress_total=stage_count,
        progress_unit="stages",
    )
    print("Job Radar refresh completed successfully.")
    return 0


def main() -> int:
    arguments = parse_arguments()
    _cancellation_requested.clear()
    _install_cancellation_handlers()

    if arguments.fetch_limit < 1:
        raise RuntimeError(
            "--fetch-limit must be at least 1."
        )

    if arguments.fetch_delay < 0:
        raise RuntimeError(
            "--fetch-delay cannot be negative."
        )

    stages = build_refresh_stages(
        non_interactive=arguments.non_interactive,
        skip_telegram=arguments.skip_telegram,
        fetch_limit=arguments.fetch_limit,
        fetch_delay=arguments.fetch_delay,
        progress_file=arguments.progress_file,
        skip_public=arguments.skip_public,
    )

    try:
        with interprocess_lock(
            REFRESH_LOCK_PATH,
            description="Job Radar refresh",
        ):
            return run_refresh(
                stages,
                progress_file=arguments.progress_file,
            )

    except AlreadyRunningError as error:
        print(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
