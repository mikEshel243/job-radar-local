import asyncio
import ipaddress
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import uvicorn
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from amazon_jobs import (
    MAX_CLIPBOARD_HTML_LENGTH,
    MAX_CLIPBOARD_TEXT_LENGTH,
    AmazonJobImportError,
    parse_amazon_recommendations_clipboard,
    persist_amazon_jobs,
)
from database import (
    connect_database,
    initialize_database,
)
from evaluate_jobs import evaluate_stored_jobs
from job_analysis import ensure_job_analysis_table
from job_details import ensure_job_details_table
from job_filter import (
    classify_location_preference,
    deduplicate_terms,
    ensure_evaluation_table,
    get_preference_settings,
    load_profile,
    normalize_text,
    update_profile_preferences,
)
from public_job_sources import (
    ensure_source_collection_table,
    get_collection_statuses,
    load_source_registry,
)
from refresh_progress import (
    read_refresh_progress,
    safe_refresh_progress,
    write_refresh_progress,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
INDEX_PATH = (
    PROJECT_ROOT
    / "src"
    / "templates"
    / "index.html"
)
REFRESH_SCRIPT_PATH = (
    PROJECT_ROOT
    / "src"
    / "refresh_jobs.py"
)
REFRESH_PROGRESS_PATH = (
    PROJECT_ROOT
    / "data"
    / "job_refresh_progress.json"
)
WHATSAPP_COLLECTOR_SCRIPT_PATH = (
    PROJECT_ROOT
    / "src"
    / "collect_whatsapp_notifications.py"
)
WHATSAPP_AUTO_COLLECT_MARKER_PATH = (
    PROJECT_ROOT
    / "config"
    / "whatsapp_dashboard_auto_collect.local"
)
TELEGRAM_AUTO_REFRESH_MARKER_PATH = (
    PROJECT_ROOT
    / "config"
    / "telegram_dashboard_auto_refresh.local"
)
MINIMUM_AUTO_REFRESH_MINUTES = 15
DEFAULT_TELEGRAM_AUTO_REFRESH_MINUTES = 8 * 60
DEFAULT_PUBLIC_REFRESH_MINUTES = 0
MAX_DETECTED_LOCATION_OPTIONS = 200
MINIMUM_PUBLIC_REFRESH_MINUTES = 60
PUBLIC_REFRESH_START_DELAY_SECONDS = 5
PUBLIC_REFRESH_RETRY_SECONDS = 60
PUBLIC_REFRESH_FETCH_LIMIT = 200
REFRESH_TIMEOUT_SECONDS = 45 * 60
REFRESH_GRACEFUL_STOP_SECONDS = 5
REFRESH_FORCE_STOP_SECONDS = 5
LOCAL_EVALUATION_INTERVAL_SECONDS = 5
UI_JOB_POLL_INTERVAL_SECONDS = 10

load_dotenv(ENV_PATH)

_refresh_task: asyncio.Task[None] | None = None
_periodic_refresh_task: asyncio.Task[None] | None = None
_periodic_public_refresh_task: (
    asyncio.Task[None] | None
) = None
_refresh_process: asyncio.subprocess.Process | None = None
_whatsapp_collector_task: asyncio.Task[None] | None = None
_local_evaluation_task: asyncio.Task[None] | None = None
_whatsapp_collector_process: (
    asyncio.subprocess.Process | None
) = None
_uvicorn_server: uvicorn.Server | None = None
_refresh_state: dict[str, Any] = {
    "status": "idle",
    "trigger": None,
    "source_filter": "all",
    "telegram_collection_included": None,
    "public_collection_included": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "stage_key": None,
    "stage_index": 0,
    "stage_count": None,
    "completed_stages": 0,
    "progress_mode": "indeterminate",
    "progress_completed": None,
    "progress_total": None,
    "progress_unit": None,
    "telegram_collection_outcome": None,
    "telegram_cooldown_seconds_remaining": 0,
    "telegram_safe_limit_reached": False,
}
_whatsapp_automation_state: dict[str, Any] = {
    "status": "disabled",
    "last_evaluation_at": None,
    "last_evaluated_jobs": 0,
    "error": None,
    "evaluation_error": None,
}


class FeedbackRequest(BaseModel):
    status: Literal[
        "none",
        "interested",
        "not_interested",
        "applied",
    ]

    notes: str | None = Field(
        default=None,
        max_length=2000,
    )


class PreferenceCriterionRequest(BaseModel):
    id: Literal[
        "role_domain",
        "location",
        "technology",
        "experience",
        "seniority",
        "education",
        "work_model",
    ]
    weight: int = Field(ge=1, le=5)
    required_for_high_match: bool
    selection_summary: dict[str, Any] | None = None


class PreferenceUpdateRequest(BaseModel):
    criteria: list[PreferenceCriterionRequest]


class AmazonRecommendationsImportRequest(BaseModel):
    text: str = Field(max_length=MAX_CLIPBOARD_TEXT_LENGTH)
    html: str | None = Field(
        default=None,
        max_length=MAX_CLIPBOARD_HTML_LENGTH,
    )


def ensure_feedback_table(
    connection: sqlite3.Connection,
) -> None:
    """Create the table used for manual user decisions."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_feedback (
            job_id INTEGER PRIMARY KEY,

            status TEXT NOT NULL
                CHECK (
                    status IN (
                        'interested',
                        'not_interested',
                        'applied'
                    )
                ),

            notes TEXT,

            updated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (job_id)
                REFERENCES jobs(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS
            idx_job_feedback_status
        ON job_feedback(status);
        """
    )

    connection.commit()


def parse_json_list(
    value: Any,
) -> list[str]:
    """Safely convert a stored JSON array to a Python list."""

    if not value:
        return []

    if isinstance(value, list):
        return [
            str(item)
            for item in value
        ]

    try:
        parsed = json.loads(value)
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        return []

    if not isinstance(parsed, list):
        return []

    return [
        str(item)
        for item in parsed
    ]


def get_detected_location_options(
    connection: sqlite3.Connection,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Return bounded, deduplicated aggregate job locations."""

    empty_result = {
        "groups": {
            category: []
            for category in (
                "preferred",
                "acceptable",
                "neutral",
                "excluded",
                "other",
            )
        },
        "total_distinct": 0,
        "shown": 0,
        "truncated": False,
    }

    try:
        rows = connection.execute(
            """
            SELECT
                location,
                COUNT(*) AS job_count
            FROM jobs
            WHERE
                location IS NOT NULL
                AND TRIM(location) != ''
            GROUP BY location
            """
        ).fetchall()
    except sqlite3.Error:
        return empty_result

    aggregated: dict[str, dict[str, Any]] = {}

    for row in rows:
        raw_location = row["location"]
        display_values = deduplicate_terms(
            (raw_location,)
        )

        if not display_values:
            continue

        display_value = display_values[0]
        normalized_value = normalize_text(
            display_value
        )

        if not normalized_value:
            continue

        count = int(row["job_count"] or 0)
        existing = aggregated.get(normalized_value)

        if existing is None:
            aggregated[normalized_value] = {
                "label": display_value,
                "count": count,
            }
        else:
            existing["count"] += count

    entries = sorted(
        aggregated.values(),
        key=lambda item: (
            -int(item["count"]),
            str(item["label"]).casefold(),
        ),
    )
    visible_entries = entries[
        :MAX_DETECTED_LOCATION_OPTIONS
    ]
    groups = empty_result["groups"]
    location_settings = profile["criteria"]["location"]

    for entry in visible_entries:
        category = classify_location_preference(
            str(entry["label"]),
            location_settings,
        )
        groups[category].append(entry)

    return {
        "groups": groups,
        "total_distinct": len(entries),
        "shown": len(visible_entries),
        "truncated": (
            len(visible_entries) < len(entries)
        ),
    }


def get_preferences_payload(
    profile: dict[str, Any],
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Build the safe preference response with local aggregates."""

    return {
        **get_preference_settings(profile),
        "location_catalog":
            get_detected_location_options(
                connection,
                profile,
            ),
    }


def parse_json_object_list(
    value: Any,
) -> list[dict[str, Any]]:
    """Read a stored JSON list containing only objects."""

    if not isinstance(value, str):
        return []

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []

    return [
        item
        for item in parsed
        if isinstance(item, dict)
    ]


def serialize_job(
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Convert a SQLite row to a JSON-ready object."""

    sources = sorted(
        {
            source.strip()
            for source in (
                row["sources_csv"] or ""
            ).split(",")
            if source.strip()
        },
        key=str.casefold,
    )

    return {
        "id": int(row["id"]),
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "posted_on": row["posted_on"],
        "job_url": (
            row["final_url"]
            or row["job_url"]
        ),
        "first_seen_at": row["first_seen_at"],

        "match_score": row["match_score"],
        "match_bucket": row["match_bucket"],
        "seniority_label": row["seniority_label"],
        "role_category": row["role_category"],
        "location_label": row["location_label"],

        "reasons": parse_json_list(
            row["reasons_json"]
        ),
        "score_components": parse_json_object_list(
            row["score_components_json"]
        ),
        "profile_schema_version": int(
            row["profile_schema_version"]
        ),
        "scoring_model": row["scoring_model"],

        "experience_min": row["experience_min"],
        "experience_max": row["experience_max"],
        "experience_label": row["experience_label"],

        "technologies": parse_json_list(
            row["technologies_json"]
        ),

        "analysis_confidence": (
            float(row["analysis_confidence"])
            if row["analysis_confidence"] is not None
            else None
        ),

        "description_preview": (
            row["description_preview"]
            or ""
        ),

        "source_count": int(
            row["source_count"] or 0
        ),
        "sources": sources,

        "source_message_url": (
            row["source_message_url"]
        ),

        "user_status": (
            row["user_status"]
            or "none"
        ),

        "user_notes": row["user_notes"],
    }


def get_summary(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    """Return totals used in the dashboard header."""

    summary = {
        "high_match": 0,
        "review": 0,
        "rejected": 0,
        "interested": 0,
        "applied": 0,
        "not_interested": 0,
    }

    bucket_rows = connection.execute(
        """
        SELECT
            match_bucket,
            COUNT(*) AS amount
        FROM job_evaluations
        GROUP BY match_bucket
        """
    ).fetchall()

    for row in bucket_rows:
        bucket = row["match_bucket"]

        if bucket in summary:
            summary[bucket] = int(
                row["amount"]
            )

    feedback_rows = connection.execute(
        """
        SELECT
            status,
            COUNT(*) AS amount
        FROM job_feedback
        GROUP BY status
        """
    ).fetchall()

    for row in feedback_rows:
        status = row["status"]

        if status in summary:
            summary[status] = int(
                row["amount"]
            )

    return summary


def _utc_timestamp() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def _load_auto_refresh_minutes() -> int | None:
    raw_value = os.getenv(
        "JOB_RADAR_AUTO_REFRESH_MINUTES",
        "0",
    ).strip()

    try:
        minutes = int(raw_value)
    except ValueError as error:
        raise RuntimeError(
            "JOB_RADAR_AUTO_REFRESH_MINUTES must be "
            "a whole number."
        ) from error

    if minutes == 0:
        if TELEGRAM_AUTO_REFRESH_MARKER_PATH.is_file():
            return DEFAULT_TELEGRAM_AUTO_REFRESH_MINUTES

        return None

    if minutes < MINIMUM_AUTO_REFRESH_MINUTES:
        raise RuntimeError(
            "JOB_RADAR_AUTO_REFRESH_MINUTES must be 0 "
            f"or at least {MINIMUM_AUTO_REFRESH_MINUTES}."
        )

    return minutes


def _load_public_refresh_minutes() -> int | None:
    raw_value = os.getenv(
        "JOB_RADAR_PUBLIC_REFRESH_MINUTES",
        str(DEFAULT_PUBLIC_REFRESH_MINUTES),
    ).strip()

    try:
        minutes = int(raw_value)
    except ValueError as error:
        raise RuntimeError(
            "JOB_RADAR_PUBLIC_REFRESH_MINUTES must be "
            "a whole number."
        ) from error

    if minutes == 0:
        return None

    if minutes < MINIMUM_PUBLIC_REFRESH_MINUTES:
        raise RuntimeError(
            "JOB_RADAR_PUBLIC_REFRESH_MINUTES must be 0 "
            f"or at least {MINIMUM_PUBLIC_REFRESH_MINUTES}."
        )

    return minutes


def _public_source_refresh_due(minutes: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=minutes
    )
    registry = load_source_registry()
    connection = connect_database()

    try:
        statuses = get_collection_statuses(
            connection,
            registry,
        )
    finally:
        connection.close()

    enabled_statuses = tuple(
        item
        for item in statuses
        if item.enabled
    )

    if not enabled_statuses:
        return False

    for item in enabled_statuses:
        if not item.last_collection_at:
            return True

        try:
            collected_at = datetime.fromisoformat(
                item.last_collection_at
            )
        except ValueError:
            return True

        if collected_at.tzinfo is None:
            collected_at = collected_at.replace(
                tzinfo=timezone.utc
            )

        if collected_at <= cutoff:
            return True

    return False


def _refresh_snapshot() -> dict[str, Any]:
    auto_refresh_minutes = (
        _load_auto_refresh_minutes()
    )
    public_refresh_minutes = (
        _load_public_refresh_minutes()
    )

    snapshot = {
        **_refresh_state,
        "automatic_refresh_enabled": (
            auto_refresh_minutes is not None
        ),
        "automatic_refresh_minutes": (
            auto_refresh_minutes
        ),
        "automatic_public_refresh_enabled": (
            public_refresh_minutes is not None
        ),
        "automatic_public_refresh_minutes": (
            public_refresh_minutes
        ),
    }

    if snapshot["status"] in {
        "running",
        "cancelling",
    }:
        snapshot.update(
            safe_refresh_progress(
                read_refresh_progress(
                    REFRESH_PROGRESS_PATH
                )
            )
        )

    return snapshot


def _whatsapp_automation_snapshot() -> dict[str, Any]:
    """Return aggregate local automation status."""

    return {
        "enabled": (
            WHATSAPP_AUTO_COLLECT_MARKER_PATH.is_file()
        ),
        **_whatsapp_automation_state,
        "ui_poll_interval_seconds": (
            UI_JOB_POLL_INTERVAL_SECONDS
        ),
    }


def _evaluate_pending_jobs_once() -> int:
    """Evaluate only jobs that do not yet have a local score."""

    profile = load_profile()
    connection = connect_database()

    try:
        initialize_database(connection)
        ensure_job_analysis_table(connection)
        ensure_evaluation_table(connection)
        return evaluate_stored_jobs(
            connection,
            profile,
            only_missing=True,
            source="whatsapp",
        )

    finally:
        connection.close()


async def _local_evaluation_loop() -> None:
    """Keep newly collected jobs visible to the dashboard."""

    while True:
        try:
            evaluated_count = await asyncio.to_thread(
                _evaluate_pending_jobs_once
            )
            _whatsapp_automation_state.update(
                {
                    "last_evaluation_at": _utc_timestamp(),
                    "last_evaluated_jobs": evaluated_count,
                    "evaluation_error": None,
                }
            )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            _whatsapp_automation_state.update(
                {
                    "evaluation_error": (
                        "Local evaluation failed ("
                        f"{type(error).__name__})."
                    ),
                }
            )

        await asyncio.sleep(
            LOCAL_EVALUATION_INTERVAL_SECONDS
        )


async def _stop_whatsapp_collector_process() -> None:
    """Stop the managed collector and allow its child to close."""

    process = _whatsapp_collector_process

    if process is None or process.returncode is not None:
        return

    try:
        if os.name == "nt":
            process.send_signal(
                signal.CTRL_BREAK_EVENT
            )
        else:
            process.terminate()

    except (
        OSError,
        ValueError,
    ):
        if process.returncode is not None:
            return

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=5,
        )
        return

    except TimeoutError:
        process.terminate()

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=2,
        )

    except TimeoutError:
        process.kill()
        await process.wait()


async def _run_whatsapp_collector() -> None:
    """Run one dashboard-owned continuous collector."""

    global _whatsapp_collector_process

    creation_options: dict[str, Any] = {}

    if os.name == "nt":
        creation_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        creation_options["start_new_session"] = True

    _whatsapp_automation_state.update(
        {
            "status": "starting",
            "error": None,
        }
    )

    try:
        _whatsapp_collector_process = (
            await asyncio.create_subprocess_exec(
                sys.executable,
                str(WHATSAPP_COLLECTOR_SCRIPT_PATH),
                "--parent-pid",
                str(os.getpid()),
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **creation_options,
            )
        )
        _whatsapp_automation_state["status"] = "running"
        return_code = (
            await _whatsapp_collector_process.wait()
        )

        if return_code in (
            0,
            130,
        ):
            _whatsapp_automation_state["status"] = "stopped"
        else:
            _whatsapp_automation_state.update(
                {
                    "status": "error",
                    "error": (
                        "WhatsApp collector exited with code "
                        f"{return_code}."
                    ),
                }
            )

    except asyncio.CancelledError:
        _whatsapp_automation_state["status"] = "stopping"
        await _stop_whatsapp_collector_process()
        _whatsapp_automation_state["status"] = "stopped"
        raise

    except Exception as error:
        _whatsapp_automation_state.update(
            {
                "status": "error",
                "error": (
                    "WhatsApp collector could not start ("
                    f"{type(error).__name__})."
                ),
            }
        )

    finally:
        _whatsapp_collector_process = None


def _is_loopback_host(value: str | None) -> bool:
    if not value:
        return False

    if value.casefold() == "localhost":
        return True

    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_dashboard_host(value: str) -> str:
    """Reject configurations that could expose the local dashboard."""

    host = value.strip()

    if not _is_loopback_host(host):
        raise RuntimeError(
            "JOB_RADAR_HOST must be localhost or a loopback IP "
            "address. Job Radar is deliberately local-only."
        )

    return host


def _require_local_automation_request(
    request: Request,
) -> None:
    client_host = (
        request.client.host
        if request.client is not None
        else None
    )

    if not _is_loopback_host(client_host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Local automation is available only "
                "from this computer."
            ),
        )

    origin = request.headers.get("origin")

    if (
        origin
        and not _is_loopback_host(
            urlsplit(origin).hostname
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Refresh request origin is not local.",
        )


def _normalize_source_filters(
    value: str | list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Normalize repeated source filters while preserving OR semantics."""

    raw_values = (
        (value,)
        if isinstance(value, str)
        else tuple(value)
    )

    if len(raw_values) > 50:
        raise HTTPException(
            status_code=400,
            detail="Too many source filters.",
        )

    normalized: list[str] = []

    for raw_value in raw_values:
        source = raw_value.strip().casefold()

        if len(source) > 50:
            raise HTTPException(
                status_code=400,
                detail="Invalid source filter.",
            )

        if (
            not source
            or source == "all"
            or source in normalized
        ):
            continue

        normalized.append(source)

    return tuple(normalized)


def _refresh_source_filter_label(
    value: str | list[str] | tuple[str, ...],
) -> str:
    """Serialize selected sources for aggregate refresh state."""

    sources = _normalize_source_filters(value)
    return ",".join(sources) if sources else "all"


def _source_filter_includes_telegram(
    value: str | list[str] | tuple[str, ...],
) -> bool:
    """Return whether a manual refresh should collect Telegram."""

    sources = _normalize_source_filters(value)
    return not sources or "telegram" in sources


def _source_filter_includes_public(
    value: str | list[str] | tuple[str, ...],
) -> bool:
    """Return whether a manual refresh should collect public sources."""

    sources = _normalize_source_filters(value)

    return not sources or any(
        source not in {"telegram", "whatsapp"}
        for source in sources
    )


async def _stop_refresh_process() -> None:
    """Stop the full refresh process group without leaving a stage."""

    process = _refresh_process

    if process is None or process.returncode is not None:
        return

    graceful_signal_sent = False

    try:
        if os.name == "nt":
            process.send_signal(
                signal.CTRL_BREAK_EVENT
            )
        else:
            os.killpg(
                process.pid,
                signal.SIGTERM,
            )
        graceful_signal_sent = True

    except (
        OSError,
        ProcessLookupError,
        ValueError,
    ):
        pass

    if graceful_signal_sent:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=REFRESH_GRACEFUL_STOP_SECONDS,
            )
            return

        except TimeoutError:
            pass

    if os.name == "nt":
        try:
            tree_kill = (
                await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            await asyncio.wait_for(
                tree_kill.wait(),
                timeout=REFRESH_FORCE_STOP_SECONDS,
            )
        except (
            OSError,
            TimeoutError,
        ):
            pass
    else:
        try:
            os.killpg(
                process.pid,
                signal.SIGKILL,
            )
        except ProcessLookupError:
            pass

    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=REFRESH_FORCE_STOP_SECONDS,
        )
    except TimeoutError:
        pass


async def _run_refresh(
    trigger: str,
    *,
    include_telegram: bool,
    include_public: bool = True,
    fetch_limit: int = 30,
) -> None:
    global _refresh_process

    process_creation_task: (
        asyncio.Task[asyncio.subprocess.Process] | None
    ) = None
    command = [
        sys.executable,
        str(REFRESH_SCRIPT_PATH),
        "--non-interactive",
        "--progress-file",
        str(REFRESH_PROGRESS_PATH),
        "--fetch-limit",
        str(fetch_limit),
    ]

    if not include_telegram:
        command.append("--skip-telegram")

    if not include_public:
        command.append("--skip-public")

    creation_options: dict[str, Any] = {}

    if os.name == "nt":
        creation_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        creation_options["start_new_session"] = True

    refresh_environment = os.environ.copy()
    refresh_environment["PYTHONIOENCODING"] = "utf-8"

    try:
        process_creation_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=refresh_environment,
                **creation_options,
            )
        )
        _refresh_process = await asyncio.shield(
            process_creation_task
        )

        try:
            return_code = await asyncio.wait_for(
                _refresh_process.wait(),
                timeout=REFRESH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            await _stop_refresh_process()
            raise RuntimeError(
                "Refresh exceeded the 45-minute safety timeout."
            )

        if return_code != 0:
            raise RuntimeError(
                "The refresh workflow stopped before completion."
            )

        _refresh_state.update(
            safe_refresh_progress(
                read_refresh_progress(
                    REFRESH_PROGRESS_PATH
                )
            )
        )
        _refresh_state.update(
            {
                "status": "success",
                "trigger": trigger,
                "finished_at": _utc_timestamp(),
                "error": None,
            }
        )

    except asyncio.CancelledError:
        if (
            _refresh_process is None
            and process_creation_task is not None
        ):
            try:
                _refresh_process = await process_creation_task
            except (OSError, subprocess.SubprocessError):
                pass

        await _stop_refresh_process()
        _refresh_state.update(
            safe_refresh_progress(
                read_refresh_progress(
                    REFRESH_PROGRESS_PATH
                )
            )
        )
        user_cancelled = (
            _refresh_state.get("status") == "cancelling"
        )

        _refresh_state.update(
            {
                "status": (
                    "cancelled"
                    if user_cancelled
                    else "error"
                ),
                "trigger": trigger,
                "finished_at": _utc_timestamp(),
                "error": (
                    None
                    if user_cancelled
                    else (
                        "Refresh was stopped because the "
                        "dashboard server shut down."
                    )
                ),
            }
        )
        raise

    except Exception as error:
        _refresh_state.update(
            safe_refresh_progress(
                read_refresh_progress(
                    REFRESH_PROGRESS_PATH
                )
            )
        )
        _refresh_state.update(
            {
                "status": "error",
                "trigger": trigger,
                "finished_at": _utc_timestamp(),
                "error": (
                    str(error)
                    if isinstance(error, RuntimeError)
                    else (
                        "The refresh workflow could not "
                        "complete."
                    )
                ),
            }
        )

    finally:
        _refresh_process = None


async def _cancel_refresh() -> bool:
    """Cancel one active refresh and wait for its process group."""

    if (
        _refresh_task is None
        or _refresh_task.done()
    ):
        return False

    _refresh_state.update(
        {
            "status": "cancelling",
            "error": None,
        }
    )
    _refresh_task.cancel()
    await asyncio.gather(
        _refresh_task,
        return_exceptions=True,
    )

    if _refresh_state.get("status") == "cancelling":
        _refresh_state.update(
            {
                "status": "cancelled",
                "finished_at": _utc_timestamp(),
                "error": None,
            }
        )

    return True


def _start_refresh(
    trigger: str,
    *,
    include_telegram: bool,
    include_public: bool = True,
    source_filter: str = "all",
    fetch_limit: int = 30,
) -> bool:
    global _refresh_task

    if (
        _refresh_task is not None
        and not _refresh_task.done()
    ):
        return False

    write_refresh_progress(
        REFRESH_PROGRESS_PATH,
        stage_key="starting",
        stage_index=0,
        stage_count=None,
        completed_stages=0,
        progress_mode="indeterminate",
        progress_completed=None,
        progress_total=None,
        progress_unit=None,
        telegram_collection_outcome=None,
        telegram_cooldown_seconds_remaining=0,
        telegram_safe_limit_reached=False,
    )
    _refresh_state.update(
        {
            "status": "running",
            "trigger": trigger,
            "source_filter": source_filter,
            "telegram_collection_included": (
                include_telegram
            ),
            "public_collection_included": (
                include_public
            ),
            "started_at": _utc_timestamp(),
            "finished_at": None,
            "error": None,
            "stage_key": "starting",
            "stage_index": 0,
            "stage_count": None,
            "completed_stages": 0,
            "progress_mode": "indeterminate",
            "progress_completed": None,
            "progress_total": None,
            "progress_unit": None,
            "telegram_collection_outcome": None,
            "telegram_cooldown_seconds_remaining": 0,
            "telegram_safe_limit_reached": False,
        }
    )
    _refresh_task = asyncio.create_task(
        _run_refresh(
            trigger,
            include_telegram=include_telegram,
            include_public=include_public,
            fetch_limit=fetch_limit,
        )
    )
    return True


async def _periodic_refresh_loop(
    minutes: int,
) -> None:
    interval_seconds = minutes * 60

    while True:
        await asyncio.sleep(interval_seconds)
        _start_refresh(
            "scheduled",
            include_telegram=True,
            include_public=False,
            source_filter="scheduled",
        )


async def _periodic_public_refresh_loop(
    minutes: int,
) -> None:
    interval_seconds = minutes * 60
    await asyncio.sleep(
        PUBLIC_REFRESH_START_DELAY_SECONDS
    )

    while True:
        try:
            due = await asyncio.to_thread(
                _public_source_refresh_due,
                minutes,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(
                PUBLIC_REFRESH_RETRY_SECONDS
            )
            continue

        started = False

        if due:
            started = _start_refresh(
                "scheduled_public",
                include_telegram=False,
                include_public=True,
                source_filter="public",
                fetch_limit=PUBLIC_REFRESH_FETCH_LIMIT,
            )

        await asyncio.sleep(
            interval_seconds
            if started or not due
            else PUBLIC_REFRESH_RETRY_SECONDS
        )


@asynccontextmanager
async def lifespan(
    _: FastAPI,
):
    """Prepare database tables when the web server starts."""

    _validate_dashboard_host(
        os.getenv("JOB_RADAR_HOST", "127.0.0.1")
    )

    connection = connect_database()

    try:
        initialize_database(connection)
        ensure_job_details_table(connection)
        ensure_job_analysis_table(connection)
        ensure_evaluation_table(connection)
        ensure_feedback_table(connection)
        ensure_source_collection_table(connection)

    finally:
        connection.close()

    auto_refresh_minutes = (
        _load_auto_refresh_minutes()
    )
    public_refresh_minutes = (
        _load_public_refresh_minutes()
    )

    global _periodic_refresh_task
    global _periodic_public_refresh_task
    global _whatsapp_collector_task
    global _local_evaluation_task

    if auto_refresh_minutes is not None:
        _periodic_refresh_task = asyncio.create_task(
            _periodic_refresh_loop(
                auto_refresh_minutes
            )
        )

    if public_refresh_minutes is not None:
        _periodic_public_refresh_task = (
            asyncio.create_task(
                _periodic_public_refresh_loop(
                    public_refresh_minutes
                )
            )
        )

    if WHATSAPP_AUTO_COLLECT_MARKER_PATH.is_file():
        _local_evaluation_task = asyncio.create_task(
            _local_evaluation_loop()
        )
        _whatsapp_collector_task = asyncio.create_task(
            _run_whatsapp_collector()
        )
    else:
        _whatsapp_automation_state["status"] = "disabled"

    try:
        yield

    finally:
        if _local_evaluation_task is not None:
            _local_evaluation_task.cancel()
            await asyncio.gather(
                _local_evaluation_task,
                return_exceptions=True,
            )
            _local_evaluation_task = None

        if _whatsapp_collector_task is not None:
            _whatsapp_collector_task.cancel()
            await asyncio.gather(
                _whatsapp_collector_task,
                return_exceptions=True,
            )
            _whatsapp_collector_task = None

        if _periodic_refresh_task is not None:
            _periodic_refresh_task.cancel()
            await asyncio.gather(
                _periodic_refresh_task,
                return_exceptions=True,
            )
            _periodic_refresh_task = None

        if _periodic_public_refresh_task is not None:
            _periodic_public_refresh_task.cancel()
            await asyncio.gather(
                _periodic_public_refresh_task,
                return_exceptions=True,
            )
            _periodic_public_refresh_task = None

        if (
            _refresh_task is not None
            and not _refresh_task.done()
        ):
            _refresh_task.cancel()
            await asyncio.gather(
                _refresh_task,
                return_exceptions=True,
            )


app = FastAPI(
    title="Job Radar",
    lifespan=lifespan,
)


@app.middleware("http")
async def require_loopback_client(
    request: Request,
    call_next,
):
    """Protect every dashboard and API route from remote clients."""

    client_host = (
        request.client.host
        if request.client is not None
        else None
    )

    if not _is_loopback_host(client_host):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "detail": (
                    "Job Radar is available only from this computer."
                )
            },
        )

    return await call_next(request)


@app.get("/")
def show_dashboard() -> FileResponse:
    """Serve the local dashboard."""

    if not INDEX_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=(
                "The dashboard file is missing: "
                f"{INDEX_PATH}"
            ),
        )

    return FileResponse(
        INDEX_PATH,
        headers={
            "Cache-Control": (
                "no-store, no-cache, must-revalidate"
            ),
            "Pragma": "no-cache",
        },
    )


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {
        "status": "ok",
    }


@app.post("/api/import/amazon-recommendations")
def import_amazon_recommendations(
    request: Request,
    payload: AmazonRecommendationsImportRequest,
) -> dict[str, int]:
    """Import a user-copied Amazon recommendation snapshot locally."""

    _require_local_automation_request(request)

    try:
        jobs = parse_amazon_recommendations_clipboard(
            text=payload.text,
            html=payload.html,
        )
    except AmazonJobImportError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error

    connection = connect_database()

    try:
        summary = persist_amazon_jobs(connection, jobs)
    except sqlite3.Error as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The local Amazon import could not be saved.",
        ) from error
    finally:
        connection.close()

    return {
        "jobs_parsed": summary.jobs_parsed,
        "new_jobs": summary.new_jobs,
        "new_postings": summary.new_postings,
        "existing_postings": summary.existing_postings,
        "analyzed_jobs": summary.analyzed_jobs,
        "evaluated_jobs": summary.evaluated_jobs,
    }


@app.get("/api/runtime")
def get_runtime_status(
    request: Request,
) -> dict[str, Any]:
    """Identify this local server without exposing paths or settings."""

    _require_local_automation_request(request)
    return {
        "application": "job-radar",
        "process_id": os.getpid(),
    }


@app.post(
    "/api/shutdown",
    status_code=status.HTTP_202_ACCEPTED,
)
def shutdown_dashboard(
    request: Request,
) -> dict[str, Any]:
    """Request a graceful shutdown of the local dashboard server."""

    _require_local_automation_request(request)

    if _uvicorn_server is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The dashboard server is not managed by the "
                "Job Radar command."
            ),
        )

    _uvicorn_server.should_exit = True
    return {
        "status": "stopping",
        "process_id": os.getpid(),
    }


@app.get("/api/whatsapp-automation")
def get_whatsapp_automation_status(
    request: Request,
) -> dict[str, Any]:
    """Return aggregate automatic WhatsApp update status."""

    _require_local_automation_request(request)
    return _whatsapp_automation_snapshot()


@app.get("/api/refresh")
def get_refresh_status(
    request: Request,
) -> dict[str, Any]:
    """Return local refresh progress without exposing process output."""

    _require_local_automation_request(request)
    return _refresh_snapshot()


@app.post(
    "/api/refresh",
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_refresh(
    request: Request,
    source: list[str] = Query(default=[]),
) -> dict[str, Any]:
    """Start a filter-aware, non-interactive local refresh."""

    _require_local_automation_request(request)
    normalized_sources = _normalize_source_filters(source)

    if not _start_refresh(
        "manual",
        include_telegram=(
            _source_filter_includes_telegram(
                normalized_sources
            )
        ),
        include_public=(
            _source_filter_includes_public(
                normalized_sources
            )
        ),
        source_filter=_refresh_source_filter_label(
            normalized_sources
        ),
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Job Radar refresh is already running.",
        )

    return _refresh_snapshot()


@app.delete("/api/refresh")
async def cancel_refresh(
    request: Request,
) -> dict[str, Any]:
    """Cancel one active local refresh and its current stage."""

    _require_local_automation_request(request)

    if not await _cancel_refresh():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No Job Radar refresh is currently running.",
        )

    return _refresh_snapshot()


@app.get("/api/preferences")
def get_preferences(
    request: Request,
) -> dict[str, Any]:
    """Return safe editable matching preferences locally."""

    _require_local_automation_request(request)

    connection: sqlite3.Connection | None = None

    try:
        profile = load_profile()
        connection = connect_database()

        return get_preferences_payload(
            profile,
            connection,
        )
    except RuntimeError as error:
        raise HTTPException(
            status_code=500,
            detail="The local preference profile is invalid.",
        ) from error
    finally:
        if connection is not None:
            connection.close()


@app.put("/api/preferences")
def save_preferences(
    request: Request,
    preference_update: PreferenceUpdateRequest,
) -> dict[str, Any]:
    """Save preferences atomically and re-evaluate local jobs."""

    _require_local_automation_request(request)

    if _refresh_state["status"] in {
        "starting",
        "running",
        "cancelling",
    }:
        raise HTTPException(
            status_code=409,
            detail=(
                "Wait for the active refresh to finish before "
                "saving preferences."
            ),
        )

    payload = (
        preference_update.model_dump()
        if hasattr(preference_update, "model_dump")
        else preference_update.dict()
    )

    try:
        updated_profile = update_profile_preferences(
            payload
        )
    except RuntimeError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    connection = connect_database()

    try:
        initialize_database(connection)
        ensure_job_details_table(connection)
        ensure_job_analysis_table(connection)
        ensure_evaluation_table(connection)
        evaluated_jobs = evaluate_stored_jobs(
            connection,
            updated_profile,
        )
        response_payload = get_preferences_payload(
            updated_profile,
            connection,
        )
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "Preferences were saved, but local job "
                "re-evaluation failed."
            ),
        ) from error
    finally:
        connection.close()

    return {
        **response_payload,
        "evaluated_jobs": evaluated_jobs,
    }


@app.get("/api/options")
def get_filter_options() -> dict[str, Any]:
    """Return available locations and technologies."""

    connection = connect_database()

    try:
        ensure_feedback_table(connection)
        ensure_source_collection_table(connection)

        location_rows = connection.execute(
            """
            SELECT DISTINCT location
            FROM jobs
            WHERE
                location IS NOT NULL
                AND TRIM(location) != ''
            ORDER BY location COLLATE NOCASE
            """
        ).fetchall()

        technology_rows = connection.execute(
            """
            SELECT technologies_json
            FROM job_analysis
            WHERE
                technologies_json IS NOT NULL
                AND technologies_json != ''
            """
        ).fetchall()

        source_rows = connection.execute(
            """
            SELECT DISTINCT source
            FROM job_postings
            WHERE
                source IS NOT NULL
                AND TRIM(source) != ''
            ORDER BY source COLLATE NOCASE
            """
        ).fetchall()

        technologies: set[str] = set()

        for row in technology_rows:
            technologies.update(
                parse_json_list(
                    row["technologies_json"]
                )
            )

        collection_statuses = (
            get_collection_statuses(
                connection,
                load_source_registry(),
            )
        )

        return {
            "locations": [
                row["location"]
                for row in location_rows
            ],
            "technologies": sorted(
                technologies,
                key=str.casefold,
            ),
            "sources": [
                row["source"]
                for row in source_rows
            ],
            "collection_sources": [
                {
                    "source_id": status.source_id,
                    "company": status.company,
                    "adapter": status.adapter,
                    "enabled": status.enabled,
                    "status": status.status,
                    "last_collection_at": (
                        status.last_collection_at
                    ),
                    "postings_seen": (
                        status.postings_seen
                    ),
                    "postings_relevant": (
                        status.postings_relevant
                    ),
                    "new_jobs": status.new_jobs,
                    "new_postings": (
                        status.new_postings
                    ),
                    "error": status.error,
                }
                for status in collection_statuses
            ],
            "summary": get_summary(
                connection
            ),
        }

    finally:
        connection.close()


@app.get("/api/jobs")
def get_jobs(
    bucket: str = Query(
        default="high_match,review",
    ),
    sort_by: str = Query(
        default="match_score",
        alias="sort",
    ),
    q: str = Query(
        default="",
        max_length=200,
    ),
    location: str = Query(
        default="",
        max_length=200,
    ),
    technology: str = Query(
        default="",
        max_length=100,
    ),
    source: list[str] = Query(default=[]),
    user_status: str = Query(
        default="all",
    ),
    min_score: int = Query(
        default=-500,
        ge=-500,
        le=500,
    ),
    limit: int = Query(
        default=200,
        ge=1,
        le=500,
    ),
) -> dict[str, Any]:
    """Return filtered and evaluated jobs."""

    allowed_buckets = {
        "high_match",
        "review",
        "rejected",
    }

    allowed_statuses = {
        "all",
        "none",
        "interested",
        "not_interested",
        "applied",
    }
    sort_clauses = {
        "match_score": """
            job_evaluations.match_score DESC,
            jobs.first_seen_at DESC,
            jobs.id DESC
        """,
        "newest": """
            jobs.first_seen_at DESC,
            jobs.id DESC
        """,
    }

    if user_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail="Invalid user status.",
        )

    normalized_sort = sort_by.strip()

    if normalized_sort not in sort_clauses:
        raise HTTPException(
            status_code=400,
            detail="Invalid job sort order.",
        )

    order_by_clause = sort_clauses[
        normalized_sort
    ]

    conditions = [
        "job_evaluations.job_id IS NOT NULL",
        "job_evaluations.match_score >= ?",
    ]

    parameters: list[Any] = [
        min_score,
    ]

    normalized_bucket = bucket.strip()

    if (
        normalized_bucket
        and normalized_bucket != "all"
    ):
        requested_buckets = [
            item.strip()
            for item in normalized_bucket.split(",")
            if item.strip()
        ]

        invalid_buckets = [
            item
            for item in requested_buckets
            if item not in allowed_buckets
        ]

        if invalid_buckets:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid match bucket: "
                    + ", ".join(invalid_buckets)
                ),
            )

        if requested_buckets:
            placeholders = ", ".join(
                "?"
                for _ in requested_buckets
            )

            conditions.append(
                "job_evaluations.match_bucket "
                f"IN ({placeholders})"
            )

            parameters.extend(
                requested_buckets
            )

    search_text = q.strip().casefold()

    if search_text:
        search_value = (
            f"%{search_text}%"
        )

        conditions.append(
            """
            (
                LOWER(
                    COALESCE(jobs.title, '')
                ) LIKE ?
                OR LOWER(
                    COALESCE(jobs.company, '')
                ) LIKE ?
                OR LOWER(
                    COALESCE(jobs.location, '')
                ) LIKE ?
                OR LOWER(
                    COALESCE(
                        job_details.description_text,
                        ''
                    )
                ) LIKE ?
            )
            """
        )

        parameters.extend(
            [
                search_value,
                search_value,
                search_value,
                search_value,
            ]
        )

    location_text = location.strip()

    if location_text:
        conditions.append(
            """
            LOWER(
                COALESCE(jobs.location, '')
            ) LIKE ?
            """
        )

        parameters.append(
            f"%{location_text.casefold()}%"
        )

    technology_text = technology.strip()

    if technology_text:
        conditions.append(
            """
            LOWER(
                COALESCE(
                    job_analysis.technologies_json,
                    ''
                )
            ) LIKE ?
            """
        )

        parameters.append(
            (
                '%"'
                + technology_text.casefold()
                + '"%'
            )
        )

    requested_sources = _normalize_source_filters(source)

    if requested_sources:
        source_placeholders = ", ".join(
            "?"
            for _ in requested_sources
        )
        conditions.append(
            f"""
            EXISTS (
                SELECT 1
                FROM job_postings AS source_postings
                WHERE
                    source_postings.job_id = jobs.id
                    AND LOWER(source_postings.source)
                        IN ({source_placeholders})
            )
            """
        )
        parameters.extend(requested_sources)

    if user_status == "none":
        conditions.append(
            "job_feedback.job_id IS NULL"
        )

    elif user_status != "all":
        conditions.append(
            "job_feedback.status = ?"
        )

        parameters.append(
            user_status
        )

    where_clause = (
        " AND ".join(conditions)
    )

    joins = """
        FROM jobs

        INNER JOIN job_evaluations
            ON job_evaluations.job_id = jobs.id

        LEFT JOIN job_analysis
            ON job_analysis.job_id = jobs.id

        LEFT JOIN job_details
            ON job_details.job_id = jobs.id

        LEFT JOIN job_feedback
            ON job_feedback.job_id = jobs.id
    """

    connection = connect_database()

    try:
        ensure_feedback_table(connection)

        total_row = connection.execute(
            f"""
            SELECT COUNT(*) AS amount
            {joins}
            WHERE {where_clause}
            """,
            tuple(parameters),
        ).fetchone()

        total = int(
            total_row["amount"]
        )

        rows = connection.execute(
            f"""
            SELECT
                jobs.id,
                jobs.title,
                jobs.company,
                jobs.location,
                jobs.posted_on,
                jobs.job_url,
                jobs.first_seen_at,
                job_details.final_url,

                job_evaluations.match_score,
                job_evaluations.match_bucket,
                job_evaluations.seniority_label,
                job_evaluations.role_category,
                job_evaluations.location_label,
                job_evaluations.reasons_json,
                job_evaluations.score_components_json,
                job_evaluations.profile_schema_version,
                job_evaluations.scoring_model,

                job_analysis.experience_min,
                job_analysis.experience_max,
                job_analysis.experience_label,
                job_analysis.technologies_json,
                job_analysis.analysis_confidence,

                SUBSTR(
                    job_details.description_text,
                    1,
                    1500
                ) AS description_preview,

                COALESCE(
                    job_feedback.status,
                    'none'
                ) AS user_status,

                job_feedback.notes AS user_notes,

                (
                    SELECT COUNT(*)
                    FROM job_postings
                    WHERE
                        job_postings.job_id
                        = jobs.id
                ) AS source_count,

                (
                    SELECT GROUP_CONCAT(
                        DISTINCT job_postings.source
                    )
                    FROM job_postings
                    WHERE
                        job_postings.job_id
                        = jobs.id
                ) AS sources_csv,

                (
                    SELECT source_message_url
                    FROM job_postings
                    WHERE
                        job_postings.job_id
                        = jobs.id
                    ORDER BY job_postings.id
                    LIMIT 1
                ) AS source_message_url

            {joins}

            WHERE {where_clause}

            ORDER BY {order_by_clause}

            LIMIT ?
            """,
            tuple(
                [
                    *parameters,
                    limit,
                ]
            ),
        ).fetchall()

        return {
            "total": total,
            "items": [
                serialize_job(row)
                for row in rows
            ],
            "summary": get_summary(
                connection
            ),
        }

    finally:
        connection.close()


@app.post("/api/jobs/{job_id}/feedback")
def update_feedback(
    job_id: int,
    feedback: FeedbackRequest,
) -> dict[str, Any]:
    """Save a manual decision for one job."""

    connection = connect_database()

    try:
        ensure_feedback_table(connection)

        job_exists = connection.execute(
            """
            SELECT id
            FROM jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()

        if job_exists is None:
            raise HTTPException(
                status_code=404,
                detail="Job not found.",
            )

        if feedback.status == "none":
            with connection:
                connection.execute(
                    """
                    DELETE FROM job_feedback
                    WHERE job_id = ?
                    """,
                    (job_id,),
                )

        else:
            notes = (
                feedback.notes.strip()
                if feedback.notes
                else None
            )

            with connection:
                connection.execute(
                    """
                    INSERT INTO job_feedback (
                        job_id,
                        status,
                        notes
                    )
                    VALUES (?, ?, ?)

                    ON CONFLICT(job_id)
                    DO UPDATE SET
                        status = excluded.status,
                        notes = excluded.notes,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        job_id,
                        feedback.status,
                        notes,
                    ),
                )

        return {
            "job_id": job_id,
            "status": feedback.status,
            "summary": get_summary(
                connection
            ),
        }

    finally:
        connection.close()


if __name__ == "__main__":
    host = _validate_dashboard_host(
        os.getenv(
            "JOB_RADAR_HOST",
            "127.0.0.1",
        )
    )

    port_raw = os.getenv(
        "JOB_RADAR_PORT",
        "8000",
    )

    try:
        port = int(port_raw)
    except ValueError as error:
        raise RuntimeError(
            "JOB_RADAR_PORT must be a number."
        ) from error

    server_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
    )
    _uvicorn_server = uvicorn.Server(server_config)

    try:
        _uvicorn_server.run()
    finally:
        _uvicorn_server = None
