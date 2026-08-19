import asyncio
import ctypes
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
)

from fastapi import HTTPException


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from database import (  # noqa: E402
    initialize_database,
    save_parsed_job,
)
from evaluate_jobs import (  # noqa: E402
    evaluate_stored_jobs,
)
from job_analysis import (  # noqa: E402
    ensure_job_analysis_table,
)
from job_details import (  # noqa: E402
    ensure_job_details_table,
)
from job_filter import (  # noqa: E402
    PREFERENCE_CRITERIA,
    ensure_evaluation_table,
    get_preference_settings,
    load_profile,
    normalize_text,
)
from web_app import (  # noqa: E402
    app,
    ensure_feedback_table,
    get_detected_location_options,
    get_jobs,
)
import web_app  # noqa: E402


async def request_app(
    path: str,
    *,
    method: str = "GET",
    client_host: str = "127.0.0.1",
    query_string: str = "",
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    response_headers: dict[bytes, bytes] | None = None,
) -> tuple[int, bytes]:
    """Issue one local GET request directly to the ASGI app."""

    response_messages: list[
        dict[str, Any]
    ] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent

        if not request_sent:
            request_sent = True

            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        return {
            "type": "http.disconnect",
        }

    async def send(
        message: dict[str, Any],
    ) -> None:
        response_messages.append(message)

        if (
            response_headers is not None
            and message["type"]
            == "http.response.start"
        ):
            response_headers.update(
                {
                    bytes(name).lower(): bytes(value)
                    for name, value in message.get(
                        "headers",
                        [],
                    )
                }
            )

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {
            "version": "3.0",
            "spec_version": "2.3",
        },
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string.encode("ascii"),
        "root_path": "",
        "headers": headers or [],
        "client": (
            client_host,
            50000,
        ),
        "server": (
            "127.0.0.1",
            8000,
        ),
    }

    await app(
        scope,
        receive,
        send,
    )

    status = next(
        int(message["status"])
        for message in response_messages
        if message["type"] == "http.response.start"
    )

    body = b"".join(
        message.get("body", b"")
        for message in response_messages
        if message["type"] == "http.response.body"
    )

    return status, body


def process_exists(process_id: int) -> bool:
    """Return whether a synthetic validation process still exists."""

    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )

        if not handle:
            return False

        exit_code = ctypes.c_ulong()

        try:
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False

            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(process_id, 0)
        return True
    except OSError:
        return False


class WebAppTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_dashboard_host_accepts_only_loopback_values(
        self,
    ) -> None:
        for host in (
            "127.0.0.1",
            "127.12.34.56",
            "::1",
            "localhost",
        ):
            with self.subTest(host=host):
                self.assertEqual(
                    web_app._validate_dashboard_host(host),
                    host,
                )

        for host in (
            "0.0.0.0",
            "::",
            "192.168.1.20",
            "job-radar.example.com",
        ):
            with (
                self.subTest(host=host),
                self.assertRaises(RuntimeError),
            ):
                web_app._validate_dashboard_host(host)

    async def test_every_route_rejects_a_remote_client(
        self,
    ) -> None:
        for path in (
            "/",
            "/api/health",
            "/api/runtime",
            "/api/preferences",
            "/api/options",
            "/api/jobs",
        ):
            with self.subTest(path=path):
                response_status, body = await request_app(
                    path,
                    client_host="192.168.1.50",
                )
                self.assertEqual(response_status, 403)
                self.assertIn(
                    b"only from this computer",
                    body,
                )

    async def test_health_endpoint(self) -> None:
        status, body = await request_app(
            "/api/health"
        )

        self.assertEqual(status, 200)
        self.assertIn(
            b'"status":"ok"',
            body,
        )

    async def test_dashboard_is_served(self) -> None:
        response_headers: dict[bytes, bytes] = {}
        status, body = await request_app(
            "/",
            response_headers=response_headers,
        )

        self.assertEqual(status, 200)
        self.assertIn(
            b"<title>",
            body,
        )
        self.assertIn(
            b"safeExternalUrl",
            body,
        )
        self.assertIn(
            b'sourceFilter',
            body,
        )
        self.assertIn(
            b'sortFilter',
            body,
        )
        self.assertIn(
            b'parameters.set(\n'
            b'                "sort"',
            body,
        )
        self.assertIn(
            b'job.sources',
            body,
        )
        self.assertIn(
            b'collectionStatus',
            body,
        )
        self.assertIn(
            b'liveUpdateStatus',
            body,
        )
        self.assertIn(
            b'LIVE_UPDATE_INTERVAL_MS = 10000',
            body,
        )
        self.assertIn(
            b'loadJobs(false)',
            body,
        )
        self.assertIn(
            b'lastJobsSignature',
            body,
        )
        self.assertIn(
            b'&& !showLoading',
            body,
        )
        self.assertIn(
            b'"/api/whatsapp-automation"',
            body,
        )
        self.assertIn(
            b'"/api/refresh"',
            body,
        )
        self.assertIn(
            b'id="refreshProgressBar"',
            body,
        )
        self.assertIn(
            b"public_source_collection",
            body,
        )
        self.assertIn(
            b'id="clearSearchButton"',
            body,
        )
        self.assertIn(
            b'id="preferencesButton"',
            body,
        )
        self.assertNotIn(
            b"position: fixed",
            body,
        )
        self.assertIn(
            b'"preference-toggle"',
            body,
        )
        self.assertNotIn(
            "לחץ להסבר ולבחירות".encode("utf-8"),
            body,
        )
        self.assertIn(
            b"flex: 1 1 auto",
            body,
        )
        self.assertIn(
            b'"aria-expanded"',
            body,
        )
        self.assertIn(
            b"body.hidden = isExpanded",
            body,
        )
        self.assertIn(
            b"primaryRow.append(\n"
            b"                    toggle,\n"
            b"                    weightControl,\n"
            b"                    requiredControl",
            body,
        )
        self.assertIn(
            b"selection_summary",
            body,
        )
        self.assertIn(
            b"location_catalog",
            body,
        )
        self.assertIn(
            b'"preference-board"',
            body,
        )
        self.assertIn(
            b'"preference-drag-chip"',
            body,
        )
        self.assertIn(
            b"chip.draggable = true",
            body,
        )
        self.assertIn(
            b"appendExperienceEditor",
            body,
        )
        self.assertIn(
            b"appendCriterionExtraControls",
            body,
        )
        self.assertIn(
            b"criterion.selection_summary",
            body,
        )
        self.assertIn(
            b'id="preferencesDialog"',
            body,
        )
        self.assertIn(
            b'fetch(\n'
            b'                    "/api/preferences"',
            body,
        )
        self.assertIn(
            b'required_for_high_match',
            body,
        )
        self.assertIn(
            b"function conciseJobReasons(job)",
            body,
        )
        self.assertIn(
            b"Relevant title keywords:",
            body,
        )
        self.assertNotIn(
            b"Role and professional domain: preferred",
            body,
        )
        self.assertIn(
            'aria-label="חיפוש חופשי"'.encode("utf-8"),
            body,
        )
        self.assertNotIn(
            b'<label for="searchInput">',
            body,
        )
        self.assertIn(
            "תיאור משרה".encode("utf-8"),
            body,
        )
        self.assertIn(
            b'id="sourceFilterOptions"',
            body,
        )
        self.assertIn(
            b'https://www.amazon.jobs/user/recommendations',
            body,
        )
        self.assertIn(
            b'"/api/import/amazon-recommendations"',
            body,
        )
        self.assertIn(b'amazon_email', body)
        self.assertIn(b'amazon_manual', body)
        self.assertNotIn(b'awstrack.me', body)
        self.assertNotIn(b'passport.amazon.jobs', body)

        self.assertIn(
            b'parameters.append("source", source)',
            body,
        )
        self.assertIn(
            b'telegram_collection_outcome',
            body,
        )
        self.assertIn(
            b'telegram_cooldown_seconds_remaining',
            body,
        )
        self.assertIn(
            b'class="description"\n'
            b'                            dir="ltr"',
            body,
        )
        self.assertIn(
            b"no-store",
            response_headers[b"cache-control"],
        )
        self.assertIn(
            b'id="manualRefreshHeading"',
            body,
        )
        self.assertIn(
            b'id="whatsappAutomationHeading"',
            body,
        )
        self.assertIn(
            b"renderRefreshProgress",
            body,
        )
        self.assertIn(
            b'removeAttribute(\n'
            b'                    "value"',
            body,
        )
        self.assertIn(
            b'data.progress_unit === "sources"',
            body,
        )
        self.assertIn(
            b'data.progress_unit === "messages"',
            body,
        )
        self.assertNotIn(
            " הודעות".encode("utf-8"),
            body,
        )
        self.assertNotIn(
            "התקדמות מדויקת אינה זמינה".encode("utf-8"),
            body,
        )
        self.assertIn(
            b'"source"',
            body,
        )
        self.assertIn(
            b'method: "DELETE"',
            body,
        )
        self.assertIn(
            b"handleRefreshButton",
            body,
        )
        self.assertNotIn(
            b"sourceFilterIncludesTelegram",
            body,
        )
        self.assertIn(
            b"window.confirm",
            body,
        )
        live_refresh_start = body.index(
            b"async function refreshLiveData"
        )
        live_refresh_end = body.index(
            b"function scheduleLiveUpdates",
            live_refresh_start,
        )
        live_refresh_source = body[
            live_refresh_start:live_refresh_end
        ]
        self.assertIn(
            b"loadJobs(false)",
            live_refresh_source,
        )
        self.assertIn(
            b"loadLiveUpdateStatus()",
            live_refresh_source,
        )
        self.assertNotIn(
            b"requestRefresh",
            live_refresh_source,
        )
        self.assertNotIn(
            b"pollRefresh",
            live_refresh_source,
        )

    async def test_amazon_clipboard_import_is_local_and_bounded(
        self,
    ) -> None:
        request_body = json.dumps(
            {
                "text": (
                    "Software Engineer\n"
                    "Example City, EXL\n"
                    "Job ID: 42345001\n"
                    "Basic Qualifications\n"
                    "3+ years of Java and Linux experience."
                ),
                "html": None,
            }
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = (
                Path(temporary_directory) / "amazon-import.db"
            )

            def open_database() -> sqlite3.Connection:
                connection = sqlite3.connect(database_path)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                return connection

            with patch(
                "web_app.connect_database",
                side_effect=open_database,
            ):
                local_status, local_body = await request_app(
                    "/api/import/amazon-recommendations",
                    method="POST",
                    body=request_body,
                    headers=[
                        (b"content-type", b"application/json")
                    ],
                )
                remote_status, _ = await request_app(
                    "/api/import/amazon-recommendations",
                    method="POST",
                    client_host="192.168.1.50",
                    body=request_body,
                    headers=[
                        (b"content-type", b"application/json")
                    ],
                )

            payload = json.loads(local_body)
            self.assertEqual(local_status, 200)
            self.assertEqual(remote_status, 403)
            self.assertEqual(payload["jobs_parsed"], 1)
            self.assertEqual(payload["new_jobs"], 1)
            self.assertEqual(payload["analyzed_jobs"], 1)

            connection = open_database()

            try:
                row = connection.execute(
                    """
                    SELECT
                        jobs.job_url,
                        job_postings.source,
                        job_postings.raw_text
                    FROM jobs
                    INNER JOIN job_postings
                        ON job_postings.job_id = jobs.id
                    """
                ).fetchone()
            finally:
                connection.close()

            self.assertEqual(
                row["job_url"],
                "https://www.amazon.jobs/jobs/42345001",
            )
            self.assertEqual(row["source"], "amazon_manual")
            self.assertNotIn("@", row["raw_text"])

    async def test_whatsapp_automation_status_is_local_only(
        self,
    ) -> None:
        local_status, local_body = await request_app(
            "/api/whatsapp-automation"
        )
        remote_status, remote_body = await request_app(
            "/api/whatsapp-automation",
            client_host="192.168.1.50",
        )

        self.assertEqual(local_status, 200)
        self.assertIn(
            b'"ui_poll_interval_seconds":10',
            local_body,
        )
        self.assertIn(
            b'"status"',
            local_body,
        )
        self.assertEqual(remote_status, 403)
        self.assertIn(
            b"only from this computer",
            remote_body,
        )

    async def test_refresh_status_is_local_only(
        self,
    ) -> None:
        local_status, local_body = await request_app(
            "/api/refresh"
        )
        remote_status, remote_body = await request_app(
            "/api/refresh",
            client_host="192.168.1.50",
        )

        self.assertEqual(local_status, 200)
        self.assertIn(
            b'"automatic_refresh_enabled"',
            local_body,
        )
        self.assertEqual(remote_status, 403)
        self.assertIn(
            b"only from this computer",
            remote_body,
        )

    async def test_refresh_start_is_nonblocking(
        self,
    ) -> None:
        with patch(
            "web_app._start_refresh",
            return_value=True,
        ) as start_mock:
            status, body = await request_app(
                "/api/refresh",
                method="POST",
            )

        self.assertEqual(status, 202)
        self.assertIn(
            b'"status"',
            body,
        )
        start_mock.assert_called_once_with(
            "manual",
            include_telegram=True,
            include_public=True,
            source_filter="all",
        )

    async def test_telegram_filter_includes_telegram(
        self,
    ) -> None:
        with patch(
            "web_app._start_refresh",
            return_value=True,
        ) as start_mock:
            status, _ = await request_app(
                "/api/refresh",
                method="POST",
                query_string="source=telegram",
            )

        self.assertEqual(status, 202)
        start_mock.assert_called_once_with(
            "manual",
            include_telegram=True,
            include_public=False,
            source_filter="telegram",
        )

    async def test_multiple_source_families_are_combined_for_refresh(
        self,
    ) -> None:
        with patch(
            "web_app._start_refresh",
            return_value=True,
        ) as start_mock:
            status, _ = await request_app(
                "/api/refresh",
                method="POST",
                query_string=(
                    "source=telegram&source=greenhouse"
                ),
            )

        self.assertEqual(status, 202)
        start_mock.assert_called_once_with(
            "manual",
            include_telegram=True,
            include_public=True,
            source_filter="telegram,greenhouse",
        )

    async def test_non_telegram_filters_skip_telegram(
        self,
    ) -> None:
        for source in ("whatsapp", "greenhouse"):
            with (
                self.subTest(source=source),
                patch(
                    "web_app._start_refresh",
                    return_value=True,
                ) as start_mock,
            ):
                status, _ = await request_app(
                    "/api/refresh",
                    method="POST",
                    query_string=f"source={source}",
                )

                self.assertEqual(status, 202)
                start_mock.assert_called_once_with(
                    "manual",
                    include_telegram=False,
                    include_public=(
                        source == "greenhouse"
                    ),
                    source_filter=source,
                )

    async def test_refresh_subprocess_uses_private_progress_channel(
        self,
    ) -> None:
        original_state = web_app._refresh_state.copy()
        process = SimpleNamespace(
            wait=AsyncMock(return_value=0),
            returncode=0,
        )

        try:
            with (
                patch(
                    "web_app.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ) as create_mock,
                patch(
                    "web_app.read_refresh_progress",
                    return_value={
                        "stage_key": "complete",
                        "stage_index": 4,
                        "stage_count": 4,
                        "completed_stages": 4,
                        "progress_mode": "determinate",
                        "progress_completed": 4,
                        "progress_total": 4,
                        "progress_unit": "stages",
                    },
                ),
            ):
                await web_app._run_refresh(
                    "manual",
                    include_telegram=True,
                )
        finally:
            web_app._refresh_state.clear()
            web_app._refresh_state.update(original_state)
            web_app._refresh_process = None

        args, kwargs = create_mock.call_args
        self.assertIn("--non-interactive", args)
        self.assertIn("--progress-file", args)
        self.assertNotIn("--skip-telegram", args)
        self.assertEqual(
            kwargs["stdin"],
            subprocess.DEVNULL,
        )
        self.assertEqual(
            kwargs["stdout"],
            subprocess.DEVNULL,
        )
        self.assertEqual(
            kwargs["stderr"],
            subprocess.DEVNULL,
        )
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
            self.assertTrue(
                kwargs["start_new_session"]
            )

    async def test_refresh_stop_signals_process_group(
        self,
    ) -> None:
        original_process = web_app._refresh_process
        process = SimpleNamespace(
            returncode=None,
            send_signal=MagicMock(),
            wait=AsyncMock(return_value=0),
            pid=12345,
        )

        try:
            web_app._refresh_process = process
            await web_app._stop_refresh_process()
        finally:
            web_app._refresh_process = original_process

        if os.name == "nt":
            process.send_signal.assert_called_once_with(
                signal.CTRL_BREAK_EVENT
            )
        process.wait.assert_awaited_once()

    async def test_filtered_refresh_skips_telegram_stage(
        self,
    ) -> None:
        original_state = web_app._refresh_state.copy()
        process = SimpleNamespace(
            wait=AsyncMock(return_value=0),
            returncode=0,
        )

        try:
            with (
                patch(
                    "web_app.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ) as create_mock,
                patch(
                    "web_app.read_refresh_progress",
                    return_value={},
                ),
            ):
                await web_app._run_refresh(
                    "manual",
                    include_telegram=False,
                )
        finally:
            web_app._refresh_state.clear()
            web_app._refresh_state.update(original_state)
            web_app._refresh_process = None

        args, _ = create_mock.call_args
        self.assertIn("--skip-telegram", args)

    async def test_active_refresh_can_be_cancelled(
        self,
    ) -> None:
        original_state = web_app._refresh_state.copy()
        original_task = web_app._refresh_task
        wait_started = asyncio.Event()

        async def wait_forever() -> int:
            wait_started.set()
            await asyncio.Future()
            return 0

        process = SimpleNamespace(
            wait=wait_forever,
            returncode=None,
        )
        stop_mock = AsyncMock()

        try:
            web_app._refresh_state.update(
                {
                    "status": "running",
                    "trigger": "manual",
                }
            )

            with (
                patch(
                    "web_app.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ),
                patch(
                    "web_app._stop_refresh_process",
                    new=stop_mock,
                ),
                patch(
                    "web_app.read_refresh_progress",
                    return_value={},
                ),
            ):
                web_app._refresh_task = (
                    asyncio.create_task(
                        web_app._run_refresh(
                            "manual",
                            include_telegram=True,
                        )
                    )
                )
                await wait_started.wait()
                was_cancelled = (
                    await web_app._cancel_refresh()
                )
        finally:
            if (
                web_app._refresh_task is not None
                and not web_app._refresh_task.done()
            ):
                web_app._refresh_task.cancel()
                await asyncio.gather(
                    web_app._refresh_task,
                    return_exceptions=True,
                )

            web_app._refresh_task = original_task
            web_app._refresh_process = None
            final_status = web_app._refresh_state["status"]
            web_app._refresh_state.clear()
            web_app._refresh_state.update(original_state)

        self.assertTrue(was_cancelled)
        self.assertEqual(final_status, "cancelled")
        stop_mock.assert_awaited_once()

    async def test_refresh_cancel_during_process_creation(
        self,
    ) -> None:
        original_state = web_app._refresh_state.copy()
        original_task = web_app._refresh_task
        original_process = web_app._refresh_process
        creation_started = asyncio.Event()
        allow_creation = asyncio.Event()
        process = SimpleNamespace(
            returncode=None,
        )
        stop_mock = AsyncMock()

        async def delayed_process_creation(
            *_args,
            **_kwargs,
        ):
            creation_started.set()
            await allow_creation.wait()
            return process

        try:
            web_app._refresh_state.update(
                {
                    "status": "running",
                    "trigger": "manual",
                }
            )

            with (
                patch(
                    "web_app.asyncio.create_subprocess_exec",
                    side_effect=delayed_process_creation,
                ),
                patch(
                    "web_app._stop_refresh_process",
                    new=stop_mock,
                ),
                patch(
                    "web_app.read_refresh_progress",
                    return_value={},
                ),
            ):
                web_app._refresh_task = (
                    asyncio.create_task(
                        web_app._run_refresh(
                            "manual",
                            include_telegram=True,
                        )
                    )
                )
                await creation_started.wait()
                cancel_task = asyncio.create_task(
                    web_app._cancel_refresh()
                )
                await asyncio.sleep(0)
                allow_creation.set()
                was_cancelled = await cancel_task
        finally:
            if (
                web_app._refresh_task is not None
                and not web_app._refresh_task.done()
            ):
                web_app._refresh_task.cancel()
                await asyncio.gather(
                    web_app._refresh_task,
                    return_exceptions=True,
                )

            final_status = web_app._refresh_state["status"]
            web_app._refresh_task = original_task
            web_app._refresh_process = original_process
            web_app._refresh_state.clear()
            web_app._refresh_state.update(original_state)

        self.assertTrue(was_cancelled)
        self.assertEqual(final_status, "cancelled")
        stop_mock.assert_awaited_once()

    async def test_completed_refresh_cannot_be_cancelled(
        self,
    ) -> None:
        original_state = web_app._refresh_state.copy()
        original_task = web_app._refresh_task
        completed_task = asyncio.create_task(
            asyncio.sleep(0)
        )
        await completed_task

        try:
            web_app._refresh_task = completed_task
            web_app._refresh_state["status"] = "success"
            was_cancelled = await web_app._cancel_refresh()
            final_status = web_app._refresh_state["status"]
        finally:
            web_app._refresh_task = original_task
            web_app._refresh_state.clear()
            web_app._refresh_state.update(original_state)

        self.assertFalse(was_cancelled)
        self.assertEqual(final_status, "success")

    async def test_force_stop_cleans_synthetic_process_tree(
        self,
    ) -> None:
        original_process = web_app._refresh_process
        child_process_id: int | None = None

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            child_path = directory_path / "child.py"
            parent_path = directory_path / "parent.py"
            child_pid_path = directory_path / "child.pid"
            ignored_signal = (
                "signal.SIGBREAK"
                if os.name == "nt"
                else "signal.SIGTERM"
            )
            child_path.write_text(
                "import signal\n"
                "import time\n"
                f"signal.signal({ignored_signal}, "
                "signal.SIG_IGN)\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            parent_path.write_text(
                "import signal\n"
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                "from pathlib import Path\n"
                f"signal.signal({ignored_signal}, "
                "signal.SIG_IGN)\n"
                "child = subprocess.Popen(["
                "sys.executable, sys.argv[1]])\n"
                "Path(sys.argv[2]).write_text("
                "str(child.pid), encoding='utf-8')\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            creation_options: dict[str, Any] = {}

            if os.name == "nt":
                creation_options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                creation_options["start_new_session"] = True

            parent_process = (
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(parent_path),
                    str(child_path),
                    str(child_pid_path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **creation_options,
                )
            )

            try:
                for _ in range(40):
                    if child_pid_path.is_file():
                        child_process_id = int(
                            child_pid_path.read_text(
                                encoding="utf-8"
                            )
                        )
                        break

                    await asyncio.sleep(0.05)

                self.assertIsNotNone(child_process_id)
                web_app._refresh_process = parent_process

                with (
                    patch.object(
                        web_app,
                        "REFRESH_GRACEFUL_STOP_SECONDS",
                        0.25,
                    ),
                    patch.object(
                        web_app,
                        "REFRESH_FORCE_STOP_SECONDS",
                        2,
                    ),
                ):
                    await web_app._stop_refresh_process()

                for _ in range(40):
                    if not process_exists(
                        int(child_process_id)
                    ):
                        break

                    await asyncio.sleep(0.05)

                self.assertIsNotNone(
                    parent_process.returncode
                )
                self.assertFalse(
                    process_exists(int(child_process_id))
                )
            finally:
                web_app._refresh_process = original_process

                if parent_process.returncode is None:
                    parent_process.kill()
                    await parent_process.wait()

                if (
                    child_process_id is not None
                    and process_exists(child_process_id)
                ):
                    if os.name == "nt":
                        subprocess.run(
                            (
                                "taskkill",
                                "/PID",
                                str(child_process_id),
                                "/T",
                                "/F",
                            ),
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        os.kill(
                            child_process_id,
                            signal.SIGKILL,
                        )

    async def test_refresh_cancel_endpoint_is_local_only(
        self,
    ) -> None:
        with patch(
            "web_app._cancel_refresh",
            new=AsyncMock(return_value=True),
        ):
            local_status, local_body = await request_app(
                "/api/refresh",
                method="DELETE",
            )
            remote_status, remote_body = await request_app(
                "/api/refresh",
                method="DELETE",
                client_host="192.168.1.50",
            )

        self.assertEqual(local_status, 200)
        self.assertIn(b'"status"', local_body)
        self.assertEqual(remote_status, 403)
        self.assertIn(
            b"only from this computer",
            remote_body,
        )

    async def test_refresh_cancel_rejects_when_idle(
        self,
    ) -> None:
        with patch(
            "web_app._cancel_refresh",
            new=AsyncMock(return_value=False),
        ):
            status, body = await request_app(
                "/api/refresh",
                method="DELETE",
            )

        self.assertEqual(status, 409)
        self.assertIn(
            b"No Job Radar refresh",
            body,
        )

    async def test_runtime_api_identifies_only_local_server(
        self,
    ) -> None:
        local_status, local_body = await request_app(
            "/api/runtime",
        )
        remote_status, remote_body = await request_app(
            "/api/runtime",
            client_host="192.168.1.50",
        )
        runtime = json.loads(local_body)

        self.assertEqual(local_status, 200)
        self.assertEqual(runtime["application"], "job-radar")
        self.assertEqual(runtime["process_id"], os.getpid())
        self.assertEqual(remote_status, 403)
        self.assertIn(
            b"only from this computer",
            remote_body,
        )

    async def test_shutdown_api_requests_managed_server_exit(
        self,
    ) -> None:
        original_server = web_app._uvicorn_server
        fake_server = SimpleNamespace(should_exit=False)

        try:
            web_app._uvicorn_server = fake_server
            status, body = await request_app(
                "/api/shutdown",
                method="POST",
            )
            response = json.loads(body)

            self.assertEqual(status, 202)
            self.assertEqual(response["status"], "stopping")
            self.assertEqual(response["process_id"], os.getpid())
            self.assertTrue(fake_server.should_exit)

        finally:
            web_app._uvicorn_server = original_server

    async def test_shutdown_api_is_local_only(
        self,
    ) -> None:
        original_server = web_app._uvicorn_server
        fake_server = SimpleNamespace(should_exit=False)

        try:
            web_app._uvicorn_server = fake_server
            status, body = await request_app(
                "/api/shutdown",
                method="POST",
                client_host="192.168.1.50",
            )

            self.assertEqual(status, 403)
            self.assertIn(
                b"only from this computer",
                body,
            )
            self.assertFalse(fake_server.should_exit)

        finally:
            web_app._uvicorn_server = original_server

    async def test_shutdown_api_rejects_unmanaged_server(
        self,
    ) -> None:
        original_server = web_app._uvicorn_server

        try:
            web_app._uvicorn_server = None
            status, body = await request_app(
                "/api/shutdown",
                method="POST",
            )

            self.assertEqual(status, 409)
            self.assertIn(
                b"not managed",
                body,
            )

        finally:
            web_app._uvicorn_server = original_server

    async def test_refresh_api_returns_only_aggregate_progress(
        self,
    ) -> None:
        original_state = web_app._refresh_state.copy()

        with tempfile.TemporaryDirectory() as directory:
            progress_path = (
                Path(directory) / "refresh.json"
            )
            progress_path.write_text(
                json.dumps(
                    {
                        "stage_key": "telegram_collection",
                        "stage_index": 1,
                        "stage_count": 4,
                        "completed_stages": 0,
                        "progress_mode": "determinate",
                        "progress_completed": 1,
                        "progress_total": 2,
                        "progress_unit": "sources",
                        "source_name": "private fixture",
                        "process_output": "private fixture",
                        "path": "private fixture",
                    }
                ),
                encoding="utf-8",
            )

            try:
                web_app._refresh_state.update(
                    {
                        "status": "running",
                        "stage_key": "starting",
                    }
                )

                with (
                    patch.object(
                        web_app,
                        "REFRESH_PROGRESS_PATH",
                        progress_path,
                    ),
                    patch.dict(
                        os.environ,
                        {
                            "JOB_RADAR_AUTO_REFRESH_MINUTES":
                                "0",
                        },
                    ),
                ):
                    status, body = await request_app(
                        "/api/refresh"
                    )
            finally:
                web_app._refresh_state.clear()
                web_app._refresh_state.update(
                    original_state
                )

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["stage_key"],
            "telegram_collection",
        )
        self.assertEqual(
            payload["progress_completed"],
            1,
        )
        self.assertEqual(payload["progress_total"], 2)
        self.assertNotIn("source_name", payload)
        self.assertNotIn("process_output", payload)
        self.assertNotIn("path", payload)

    async def test_periodic_refresh_waits_between_attempts(
        self,
    ) -> None:
        sleep_mock = AsyncMock(
            side_effect=[
                None,
                asyncio.CancelledError(),
            ]
        )

        with (
            patch(
                "web_app.asyncio.sleep",
                new=sleep_mock,
            ),
            patch(
                "web_app._start_refresh"
            ) as start_mock,
        ):
            with self.assertRaises(
                asyncio.CancelledError
            ):
                await web_app._periodic_refresh_loop(15)

        self.assertEqual(
            [call.args for call in sleep_mock.call_args_list],
            [
                (15 * 60,),
                (15 * 60,),
            ],
        )
        start_mock.assert_called_once_with(
            "scheduled",
            include_telegram=True,
            include_public=False,
            source_filter="scheduled",
        )


class WebPreferenceTests(
    unittest.IsolatedAsyncioTestCase
):
    @staticmethod
    def preference_payload() -> dict[str, Any]:
        settings = get_preference_settings(
            load_profile()
        )
        return {
            "criteria": [
                {
                    "id": item["id"],
                    "weight": item["weight"],
                    "required_for_high_match": item[
                        "required_for_high_match"
                    ],
                    "selection_summary": item[
                        "selection_summary"
                    ],
                }
                for item in settings["criteria"]
            ]
        }

    async def test_preferences_api_is_local_and_safe(
        self,
    ) -> None:
        local_status, local_body = await request_app(
            "/api/preferences"
        )
        remote_status, _ = await request_app(
            "/api/preferences",
            client_host="192.168.1.50",
        )
        payload = json.loads(local_body)

        self.assertEqual(local_status, 200)
        self.assertEqual(remote_status, 403)
        self.assertEqual(
            len(payload["criteria"]),
            len(PREFERENCE_CRITERIA),
        )
        self.assertIn(
            "selection_summary",
            payload["criteria"][0],
        )
        self.assertIn(
            "location_catalog",
            payload,
        )
        self.assertNotIn(
            "preferred_keywords",
            local_body.decode("utf-8"),
        )
        self.assertNotIn(
            "positive_title_keywords",
            local_body.decode("utf-8"),
        )

    def test_detected_locations_are_deduplicated_and_grouped(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        profile = deepcopy(load_profile())
        profile["criteria"]["location"].update(
            {
                "preferred_keywords": [
                    "example city",
                ],
                "acceptable_keywords": [
                    "demo metro",
                ],
                "neutral_keywords": [],
                "excluded_keywords": [],
            }
        )
        connection.executemany(
            """
            INSERT INTO jobs (
                dedupe_key,
                location
            )
            VALUES (?, ?)
            """,
            [
                ("location-1", "Example City"),
                ("location-2", " example city "),
                ("location-3", "Demo-Metro"),
                ("location-4", "demo metro"),
                ("location-5", "Sample Harbor"),
            ],
        )

        try:
            result = get_detected_location_options(
                connection,
                profile,
            )
        finally:
            connection.close()

        self.assertEqual(
            result["total_distinct"],
            3,
        )
        self.assertEqual(
            [
                {
                    "label": normalize_text(
                        item["label"]
                    ),
                    "count": item["count"],
                }
                for item in result[
                    "groups"
                ]["preferred"]
            ],
            [
                {
                    "label": "example city",
                    "count": 2,
                }
            ],
        )
        self.assertEqual(
            [
                {
                    "label": normalize_text(
                        item["label"]
                    ),
                    "count": item["count"],
                }
                for item in result[
                    "groups"
                ]["acceptable"]
            ],
            [
                {
                    "label": "demo metro",
                    "count": 2,
                }
            ],
        )
        self.assertEqual(
            result["groups"]["other"],
            [
                {
                    "label": "Sample Harbor",
                    "count": 1,
                }
            ],
        )

    async def test_saving_preferences_re_evaluates_jobs(
        self,
    ) -> None:
        profile = load_profile()
        connection = MagicMock()
        request_body = json.dumps(
            self.preference_payload()
        ).encode("utf-8")

        with (
            patch(
                "web_app.update_profile_preferences",
                return_value=profile,
            ) as update_mock,
            patch(
                "web_app.connect_database",
                return_value=connection,
            ),
            patch("web_app.initialize_database"),
            patch("web_app.ensure_job_details_table"),
            patch("web_app.ensure_job_analysis_table"),
            patch("web_app.ensure_evaluation_table"),
            patch(
                "web_app.evaluate_stored_jobs",
                return_value=558,
            ) as evaluate_mock,
        ):
            status, body = await request_app(
                "/api/preferences",
                method="PUT",
                body=request_body,
                headers=[
                    (
                        b"content-type",
                        b"application/json",
                    )
                ],
            )

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["evaluated_jobs"], 558)
        update_mock.assert_called_once()
        evaluate_mock.assert_called_once_with(
            connection,
            profile,
        )
        connection.close.assert_called_once()

    async def test_active_refresh_blocks_preference_save(
        self,
    ) -> None:
        original_status = web_app._refresh_state["status"]
        request_body = json.dumps(
            self.preference_payload()
        ).encode("utf-8")

        try:
            web_app._refresh_state["status"] = "running"

            with patch(
                "web_app.update_profile_preferences"
            ) as update_mock:
                status, body = await request_app(
                    "/api/preferences",
                    method="PUT",
                    body=request_body,
                    headers=[
                        (
                            b"content-type",
                            b"application/json",
                        )
                    ],
                )
        finally:
            web_app._refresh_state["status"] = (
                original_status
            )

        self.assertEqual(status, 409)
        self.assertIn(
            b"active refresh",
            body,
        )
        update_mock.assert_not_called()


class TelegramScheduleConfigurationTests(
    unittest.TestCase
):
    def test_periodic_refresh_is_disabled_by_default(
        self,
    ) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "JOB_RADAR_AUTO_REFRESH_MINUTES": "0",
                },
            ),
            patch(
                "web_app.TELEGRAM_AUTO_REFRESH_MARKER_PATH"
            ) as marker_path,
        ):
            marker_path.is_file.return_value = False
            self.assertIsNone(
                web_app._load_auto_refresh_minutes()
            )

    def test_local_opt_in_enables_eight_hour_fallback(
        self,
    ) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "JOB_RADAR_AUTO_REFRESH_MINUTES": "0",
                },
            ),
            patch(
                "web_app.TELEGRAM_AUTO_REFRESH_MARKER_PATH"
            ) as marker_path,
        ):
            marker_path.is_file.return_value = True
            self.assertEqual(
                web_app._load_auto_refresh_minutes(),
                8 * 60,
            )

    def test_periodic_refresh_requires_conservative_interval(
        self,
    ) -> None:
        for value in (
            "1",
            "14",
            "-1",
        ):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {
                        "JOB_RADAR_AUTO_REFRESH_MINUTES":
                            value,
                    },
                ),
                self.assertRaises(RuntimeError),
            ):
                web_app._load_auto_refresh_minutes()

        with patch.dict(
            os.environ,
            {
                "JOB_RADAR_AUTO_REFRESH_MINUTES": "15",
            },
        ):
            self.assertEqual(
                web_app._load_auto_refresh_minutes(),
                15,
            )


class PublicScheduleConfigurationTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_public_refresh_is_disabled_by_default(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            self.assertEqual(
                web_app._load_public_refresh_minutes(),
                None,
            )

    def test_public_refresh_requires_hourly_or_slower(
        self,
    ) -> None:
        for value in ("1", "59", "-1"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {
                        "JOB_RADAR_PUBLIC_REFRESH_MINUTES":
                            value,
                    },
                ),
                self.assertRaises(RuntimeError),
            ):
                web_app._load_public_refresh_minutes()

        with patch.dict(
            os.environ,
            {
                "JOB_RADAR_PUBLIC_REFRESH_MINUTES": "60",
            },
        ):
            self.assertEqual(
                web_app._load_public_refresh_minutes(),
                60,
            )

    async def test_public_refresh_starts_when_due(
        self,
    ) -> None:
        sleep_mock = AsyncMock(
            side_effect=[
                None,
                asyncio.CancelledError(),
            ]
        )

        with (
            patch(
                "web_app.asyncio.sleep",
                new=sleep_mock,
            ),
            patch(
                "web_app.asyncio.to_thread",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "web_app._start_refresh",
                return_value=True,
            ) as start_mock,
        ):
            with self.assertRaises(
                asyncio.CancelledError
            ):
                await web_app._periodic_public_refresh_loop(
                    360
                )

        self.assertEqual(
            [call.args for call in sleep_mock.call_args_list],
            [
                (
                    web_app
                    .PUBLIC_REFRESH_START_DELAY_SECONDS,
                ),
                (360 * 60,),
            ],
        )
        start_mock.assert_called_once_with(
            "scheduled_public",
            include_telegram=False,
            include_public=True,
            source_filter="public",
            fetch_limit=web_app.PUBLIC_REFRESH_FETCH_LIMIT,
        )


class WebSourceFilterTests(unittest.TestCase):
    def test_filters_jobs_by_any_source_and_serializes_badge(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_database(connection)
        ensure_job_details_table(connection)
        ensure_job_analysis_table(connection)
        ensure_evaluation_table(connection)
        ensure_feedback_table(connection)

        job_ids: list[int] = []

        for source, message_id, job_number in (
            ("telegram", 1, 101),
            ("whatsapp", "wa_fixture", 102),
        ):
            job_id, _, _ = save_parsed_job(
                connection,
                SimpleNamespace(
                    source=source,
                    source_group="Fixture Group",
                    source_message_id=message_id,
                    source_message_url=None,
                    message_date="2026-07-28T10:00:00",
                    title=f"Job {job_number}",
                    company="Example",
                    location="Example City",
                    posted_on="2026-07-28",
                    job_url=(
                        "https://jobs.example.com/"
                        f"{job_number}"
                    ),
                    raw_text=f"Job {job_number}",
                    parse_confidence=1.0,
                ),
            )
            job_ids.append(job_id)

        for job_id in job_ids:
            connection.execute(
                """
                INSERT INTO job_evaluations (
                    job_id,
                    profile_name,
                    seniority_label,
                    role_category,
                    location_label,
                    match_score,
                    match_bucket,
                    reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "fixture",
                    "junior",
                    "backend",
                    "preferred",
                    80,
                    "high_match",
                    "[]",
                ),
            )

        connection.execute(
            """
            INSERT INTO job_details (
                job_id,
                final_url,
                description_text,
                extractor,
                fetch_status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_ids[1],
                (
                    "https://company.example/"
                    "careers/job-102"
                ),
                "Original employer description.",
                "html-fallback",
                "success",
            ),
        )

        connection.commit()

        single_source_connection = sqlite3.connect(":memory:")
        single_source_connection.row_factory = sqlite3.Row
        connection.backup(single_source_connection)

        with patch(
            "web_app.connect_database",
            return_value=connection,
        ):
            combined_result = get_jobs(
                bucket="all",
                sort_by="match_score",
                q="",
                location="",
                technology="",
                source=["telegram", "whatsapp"],
                user_status="all",
                min_score=-500,
                limit=200,
            )

        with patch(
            "web_app.connect_database",
            return_value=single_source_connection,
        ):
            single_result = get_jobs(
                bucket="all",
                sort_by="match_score",
                q="",
                location="",
                technology="",
                source="whatsapp",
                user_status="all",
                min_score=-500,
                limit=200,
            )

        self.assertEqual(combined_result["total"], 2)
        self.assertEqual(
            {
                item["title"]
                for item in combined_result["items"]
            },
            {"Job 101", "Job 102"},
        )
        self.assertEqual(single_result["total"], 1)
        self.assertEqual(
            single_result["items"][0]["title"],
            "Job 102",
        )
        self.assertEqual(
            single_result["items"][0]["sources"],
            ["whatsapp"],
        )
        self.assertEqual(
            single_result["items"][0]["job_url"],
            (
                "https://company.example/"
                "careers/job-102"
            ),
        )


class WebJobSortingTests(unittest.TestCase):
    @staticmethod
    def make_connection() -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_database(connection)
        ensure_job_details_table(connection)
        ensure_job_analysis_table(connection)
        ensure_evaluation_table(connection)
        ensure_feedback_table(connection)

        fixtures = (
            (
                201,
                "Older High Score",
                "2026-07-28 10:00:00",
                95,
            ),
            (
                202,
                "Newest Lower Score",
                "2026-07-30 10:00:00",
                40,
            ),
        )

        for message_id, title, first_seen_at, score in fixtures:
            job_id, _, _ = save_parsed_job(
                connection,
                SimpleNamespace(
                    source="telegram",
                    source_group="Fixture Group",
                    source_message_id=message_id,
                    source_message_url=None,
                    message_date="2026-07-30T10:00:00",
                    title=title,
                    company="Example",
                    location="Example City",
                    posted_on="2026-07-30",
                    job_url=(
                        "https://jobs.example.com/"
                        f"{message_id}"
                    ),
                    raw_text=title,
                    parse_confidence=1.0,
                ),
            )
            connection.execute(
                """
                UPDATE jobs
                SET first_seen_at = ?
                WHERE id = ?
                """,
                (
                    first_seen_at,
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_evaluations (
                    job_id,
                    profile_name,
                    seniority_label,
                    role_category,
                    location_label,
                    match_score,
                    match_bucket,
                    reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "fixture",
                    "junior",
                    "backend",
                    "preferred",
                    score,
                    "high_match",
                    "[]",
                ),
            )

        connection.commit()
        return connection

    @staticmethod
    def get_sorted_titles(
        sort_by: str,
    ) -> list[str]:
        connection = (
            WebJobSortingTests.make_connection()
        )

        with patch(
            "web_app.connect_database",
            return_value=connection,
        ):
            result = get_jobs(
                bucket="all",
                sort_by=sort_by,
                q="",
                location="",
                technology="",
                source="",
                user_status="all",
                min_score=-500,
                limit=200,
            )

        return [
            item["title"]
            for item in result["items"]
        ]

    def test_sorts_by_match_score_or_newest_added(
        self,
    ) -> None:
        self.assertEqual(
            self.get_sorted_titles("match_score"),
            [
                "Older High Score",
                "Newest Lower Score",
            ],
        )
        self.assertEqual(
            self.get_sorted_titles("newest"),
            [
                "Newest Lower Score",
                "Older High Score",
            ],
        )

    def test_rejects_invalid_sort_order(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            get_jobs(
                bucket="all",
                sort_by="not-a-sort",
                q="",
                location="",
                technology="",
                source="",
                user_status="all",
                min_score=-500,
                limit=200,
            )

        self.assertEqual(
            raised.exception.status_code,
            400,
        )

    def test_pending_evaluation_preserves_existing_scores(
        self,
    ) -> None:
        connection = self.make_connection()

        try:
            newest_job_id = connection.execute(
                """
                SELECT id
                FROM jobs
                WHERE title = ?
                """,
                (
                    "Newest Lower Score",
                ),
            ).fetchone()["id"]
            connection.execute(
                """
                DELETE FROM job_evaluations
                WHERE job_id = ?
                """,
                (
                    newest_job_id,
                ),
            )
            connection.commit()

            restricted_count = evaluate_stored_jobs(
                connection,
                load_profile(),
                only_missing=True,
                source="whatsapp",
            )
            evaluated_count = evaluate_stored_jobs(
                connection,
                load_profile(),
                only_missing=True,
            )
            existing_score = connection.execute(
                """
                SELECT match_score
                FROM job_evaluations
                WHERE job_id != ?
                """,
                (
                    newest_job_id,
                ),
            ).fetchone()["match_score"]
            evaluation_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM job_evaluations
                """
            ).fetchone()[0]

            self.assertEqual(restricted_count, 0)
            self.assertEqual(evaluated_count, 1)
            self.assertEqual(existing_score, 95)
            self.assertEqual(evaluation_count, 2)

        finally:
            connection.close()


class WebFreeTextSearchTests(unittest.TestCase):
    def test_searches_description_with_existing_filters(
        self,
    ) -> None:
        connection = WebJobSortingTests.make_connection()

        try:
            job_id = connection.execute(
                """
                SELECT id
                FROM jobs
                WHERE title = ?
                """,
                ("Older High Score",),
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO job_details (
                    job_id,
                    description_text,
                    fetch_status
                )
                VALUES (?, ?, ?)
                """,
                (
                    job_id,
                    (
                        "Build a Kubernetes platform with "
                        "Spring services."
                    ),
                    "success",
                ),
            )
            connection.execute(
                """
                INSERT INTO job_feedback (
                    job_id,
                    status
                )
                VALUES (?, ?)
                """,
                (
                    job_id,
                    "interested",
                ),
            )
            connection.commit()

            with patch(
                "web_app.connect_database",
                return_value=connection,
            ):
                result = get_jobs(
                    bucket="high_match",
                    sort_by="newest",
                    q="kubernetes",
                    location="Example City",
                    technology="",
                    source="telegram",
                    user_status="interested",
                    min_score=90,
                    limit=200,
                )
        finally:
            connection.close()

        self.assertEqual(result["total"], 1)
        self.assertEqual(
            result["items"][0]["title"],
            "Older High Score",
        )


if __name__ == "__main__":
    unittest.main()
