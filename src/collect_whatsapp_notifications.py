import argparse
import ctypes
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, TextIO

from database import connect_database, initialize_database
from native_export_import import ImportSummary, import_source_messages
from process_lock import AlreadyRunningError, interprocess_lock
from source_parsing import (
    JobParserRegistry,
    SourceContext,
)
from whatsapp_notifications import (
    MAX_JSON_LINE_CHARS,
    DiagnosticSummary,
    WhatsAppNotificationConfig,
    WhatsAppNotificationError,
    load_notification_config,
    parse_diagnostic_record,
    parse_notification_record,
)
from whatsapp_sources import prepare_whatsapp_parser


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = (
    PROJECT_ROOT
    / "config"
    / "whatsapp_notifications.local.json"
)
DEFAULT_COMPANION = "JobRadarWhatsAppNativeListener.exe"
DEFAULT_ACCESS_COMPANION = "JobRadarWhatsAppListener.exe"
LOCK_PATH = (
    PROJECT_ROOT / "data" / "whatsapp_notifications.lock"
)


@dataclass(slots=True)
class CollectionTotals:
    source_messages_checked: int = 0
    jobs_parsed: int = 0
    new_jobs: int = 0
    new_postings: int = 0
    existing_postings: int = 0

    def add(self, summary: ImportSummary) -> None:
        self.source_messages_checked += (
            summary.source_messages_checked
        )
        self.jobs_parsed += summary.jobs_parsed
        self.new_jobs += summary.new_jobs
        self.new_postings += summary.new_postings
        self.existing_postings += (
            summary.existing_postings
        )


def _validate_local_configuration(
    config_path: Path,
) -> tuple[
    WhatsAppNotificationConfig,
    SourceContext,
    JobParserRegistry,
]:
    config = load_notification_config(config_path)
    context, parser_registry = prepare_whatsapp_parser(
        group_identifier=config.group_identifier,
        exact_group_name=config.group_name,
    )
    return config, context, parser_registry


def consume_notification_lines(
    lines: Iterable[str],
    *,
    config: WhatsAppNotificationConfig,
    context: SourceContext,
    parser_registry: JobParserRegistry,
    connection: sqlite3.Connection | None,
) -> CollectionTotals:
    """Validate, parse, and optionally save accepted records."""

    totals = CollectionTotals()

    for line in lines:
        if not line.strip():
            continue

        message = parse_notification_record(
            line,
            expected_group_identifier=(
                config.group_identifier
            ),
        )

        if connection is None:
            parsed_job = parser_registry.parse(
                message,
                context,
            )
            totals.source_messages_checked += 1

            if parsed_job is not None:
                totals.jobs_parsed += 1

            continue

        summary = import_source_messages(
            connection=connection,
            messages=(message,),
            context=context,
            parser_registry=parser_registry,
        )
        totals.add(summary)

    return totals


def _build_companion_command(
    companion_path: str,
    *,
    config_path: Path | None = None,
    once: bool = False,
    diagnostic: bool = False,
    request_access: bool = False,
    check_access: bool = False,
) -> list[str]:
    command = [str(companion_path)]

    if request_access:
        command.append("--request-access")
        return command

    if check_access:
        command.append("--check-access")
        return command

    command.extend(
        (
            "--listen",
            "--config",
            str(config_path),
        )
    )

    if once:
        command.append("--once")

    if diagnostic:
        command.append("--diagnostic")

    return command


def _require_companion(value: str) -> str:
    supplied_path = Path(value)

    if supplied_path.is_absolute() or supplied_path.parent != Path(
        "."
    ):
        resolved = supplied_path.resolve()

        if resolved.is_file():
            return str(resolved)

    discovered = shutil.which(value)

    if discovered is None:
        raise WhatsAppNotificationError(
            "The packaged notification companion alias was not "
            "found. Build, sign, and install the local package "
            "before notification access."
        )

    return discovered


def _run_simple_companion_command(
    command: list[str],
) -> int:
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=60,
    )

    if completed.stdout:
        print(completed.stdout.strip())

    if completed.returncode != 0:
        print(
            completed.stderr.strip()
            or "Notification companion failed.",
            file=sys.stderr,
        )

    return completed.returncode


def _run_diagnostic(
    command: list[str],
) -> DiagnosticSummary:
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=60,
    )

    if completed.returncode != 0:
        raise WhatsAppNotificationError(
            completed.stderr.strip()
            or "Notification diagnostic failed."
        )

    lines = [
        line
        for line in completed.stdout.splitlines()
        if line.strip()
    ]

    if len(lines) != 1:
        raise WhatsAppNotificationError(
            "Notification diagnostic returned an unexpected "
            "number of records."
        )

    return parse_diagnostic_record(lines[0])


def _bounded_stdout_lines(
    stream: TextIO,
) -> Iterable[str]:
    while True:
        line = stream.readline(MAX_JSON_LINE_CHARS + 2)

        if not line:
            return

        if len(line) > MAX_JSON_LINE_CHARS + 1:
            raise WhatsAppNotificationError(
                "Companion output line exceeds its size limit."
            )

        yield line


def _run_collection(
    command: list[str],
    *,
    config: WhatsAppNotificationConfig,
    context: SourceContext,
    parser_registry: JobParserRegistry,
    connection: sqlite3.Connection | None,
    parent_pid: int | None = None,
) -> tuple[CollectionTotals, int, str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        bufsize=1,
    )
    parent_watch_stop = threading.Event()
    parent_watch: threading.Thread | None = None

    if parent_pid is not None:
        parent_watch = threading.Thread(
            target=_watch_parent_process,
            args=(
                parent_pid,
                process,
                parent_watch_stop,
            ),
            daemon=True,
            name="whatsapp-parent-watch",
        )
        parent_watch.start()

    if process.stdout is None or process.stderr is None:
        parent_watch_stop.set()

        if parent_watch is not None:
            parent_watch.join(timeout=2)

        process.kill()
        raise WhatsAppNotificationError(
            "Could not open companion output streams."
        )

    try:
        try:
            totals = consume_notification_lines(
                _bounded_stdout_lines(process.stdout),
                config=config,
                context=context,
                parser_registry=parser_registry,
                connection=connection,
            )
        except BaseException:
            process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

            raise

        stderr_text = process.stderr.read()
        return_code = process.wait()
        return totals, return_code, stderr_text

    finally:
        parent_watch_stop.set()

        if parent_watch is not None:
            parent_watch.join(timeout=2)


def _wait_for_windows_process_exit(
    process_id: int,
    stop_event: threading.Event,
) -> bool:
    """Return true when a specific Windows process exits."""

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    )
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [
        ctypes.c_void_p,
    ]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(
        synchronize,
        False,
        process_id,
    )

    if not handle:
        return True

    try:
        while not stop_event.is_set():
            result = kernel32.WaitForSingleObject(
                handle,
                1000,
            )

            if result == wait_object_0:
                return True

            if result != wait_timeout:
                return True

        return False

    finally:
        kernel32.CloseHandle(handle)


def _watch_parent_process(
    parent_pid: int,
    process: subprocess.Popen[str],
    stop_event: threading.Event,
) -> None:
    """Stop the native child if its dashboard owner disappears."""

    if sys.platform != "win32":
        return

    parent_exited = _wait_for_windows_process_exit(
        parent_pid,
        stop_event,
    )

    if parent_exited and process.poll() is None:
        process.terminate()


def _print_totals(
    totals: CollectionTotals,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        print("DRY RUN: SQLite was not opened or changed.")
    else:
        print("WhatsApp notification collection complete.")

    print(
        "Accepted source messages checked: "
        f"{totals.source_messages_checked}"
    )
    print(f"Messages parsed as jobs: {totals.jobs_parsed}")

    if not dry_run:
        print(f"New unique jobs: {totals.new_jobs}")
        print(f"New source postings: {totals.new_postings}")
        print(
            "Previously saved postings: "
            f"{totals.existing_postings}"
        )


def _print_diagnostic(
    summary: DiagnosticSummary,
) -> None:
    print(
        "DIAGNOSTIC: aggregate counts only; no notification "
        "titles or bodies were printed or stored."
    )
    print(
        f"Toast notifications observed: "
        f"{summary.total_notifications}"
    )
    print(
        "Unrecoverable application identity errors: "
        f"{summary.application_identity_errors}"
    )
    if summary.application_info_errors is not None:
        print(
            "Application-info retrieval errors: "
            f"{summary.application_info_errors}"
        )
        if summary.application_info_error_categories is not None:
            for category, count in sorted(
                summary.application_info_error_categories.items()
            ):
                print(
                    "Application-info error category: "
                    f"{category}; count: {count}"
                )
        if summary.official_package_family_matches is not None:
            print(
                "Official package-family matches: "
                f"{summary.official_package_family_matches}"
            )
        print(
            "Combined application-ID getter errors: "
            f"{summary.app_user_model_id_errors}"
        )
        print(
            "Exact application identities reconstructed locally: "
            f"{summary.reconstructed_application_identities}"
        )
    print(
        "Official application-package matches: "
        f"{summary.allowed_app_notifications}"
    )
    print(
        "Exact group-title matches: "
        f"{summary.exact_group_notifications}"
    )
    print(
        "Accepted bounded notifications: "
        f"{summary.accepted_notifications}"
    )
    print(
        "Rejected oversized notifications: "
        f"{summary.oversized_notifications}"
    )
    if summary.visual_inspection_errors is not None:
        print(
            "Notification visual-inspection errors: "
            f"{summary.visual_inspection_errors}"
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect one allowlisted WhatsApp group's new "
            "Windows notifications through the local companion."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the ignored local notification allowlist.",
    )
    parser.add_argument(
        "--companion",
        default=DEFAULT_COMPANION,
        help=(
            "Installed packaged app alias, or an explicit "
            "companion path for synthetic self-tests."
        ),
    )
    parser.add_argument(
        "--access-companion",
        default=DEFAULT_ACCESS_COMPANION,
        help=(
            "Installed packaged alias used only for permission "
            "request and access-status checks."
        ),
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--request-access",
        action="store_true",
        help=(
            "Explicitly request one-time Windows notification "
            "listener permission, then exit."
        ),
    )
    mode.add_argument(
        "--check-access",
        action="store_true",
        help="Report notification-listener permission state.",
    )
    mode.add_argument(
        "--diagnostic",
        action="store_true",
        help=(
            "Read current notifications once and report only "
            "aggregate allowlist counts."
        ),
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read current notifications once, parse accepted "
            "records, and report aggregate counts without SQLite."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Poll once instead of listening until interrupted."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)

    try:
        if (
            args.parent_pid is not None
            and args.parent_pid <= 0
        ):
            raise WhatsAppNotificationError(
                "Parent process ID must be positive."
            )

        if (
            args.parent_pid is not None
            and (
                args.request_access
                or args.check_access
                or args.diagnostic
                or args.once
                or args.dry_run
            )
        ):
            raise WhatsAppNotificationError(
                "Parent monitoring requires continuous mode."
            )

        if args.request_access or args.check_access:
            companion_path = _require_companion(
                args.access_companion
            )
            command = _build_companion_command(
                companion_path,
                request_access=args.request_access,
                check_access=args.check_access,
            )
            return _run_simple_companion_command(command)

        companion_path = _require_companion(args.companion)
        config, context, parser_registry = (
            _validate_local_configuration(args.config)
        )

        if args.diagnostic:
            command = _build_companion_command(
                companion_path,
                config_path=args.config.resolve(),
                once=True,
                diagnostic=True,
            )

            with interprocess_lock(
                LOCK_PATH,
                description=(
                    "WhatsApp notification collector"
                ),
            ):
                summary = _run_diagnostic(command)

            _print_diagnostic(summary)
            return 0

        once = args.once or args.dry_run
        command = _build_companion_command(
            companion_path,
            config_path=args.config.resolve(),
            once=once,
        )
        database: sqlite3.Connection | None = None

        if not args.dry_run:
            database = connect_database()
            initialize_database(database)

        try:
            with interprocess_lock(
                LOCK_PATH,
                description=(
                    "WhatsApp notification collector"
                ),
            ):
                totals, return_code, stderr_text = (
                    _run_collection(
                        command,
                        config=config,
                        context=context,
                        parser_registry=parser_registry,
                        connection=database,
                        parent_pid=args.parent_pid,
                    )
                )
        finally:
            if database is not None:
                database.close()

        if return_code != 0:
            raise WhatsAppNotificationError(
                stderr_text.strip()
                or "Notification companion failed."
            )

        _print_totals(totals, dry_run=args.dry_run)
        return 0
    except (
        AlreadyRunningError,
        OSError,
        subprocess.SubprocessError,
        WhatsAppNotificationError,
        ValueError,
    ) as error:
        print(f"WhatsApp notification collection failed: {error}")
        return 1
    except KeyboardInterrupt:
        print(
            "WhatsApp notification collection stopped by user."
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
