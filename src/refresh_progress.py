import json
import os
from pathlib import Path
from typing import Any


ALLOWED_PROGRESS_MODES = frozenset(
    {
        "determinate",
        "indeterminate",
    }
)
ALLOWED_PROGRESS_UNITS = frozenset(
    {
        "jobs",
        "messages",
        "sources",
        "stages",
    }
)
ALLOWED_STAGE_KEYS = frozenset(
    {
        "complete",
        "description_analysis",
        "job_page_enrichment",
        "public_source_collection",
        "relevance_filtering",
        "starting",
        "telegram_collection",
    }
)
ALLOWED_TELEGRAM_COLLECTION_OUTCOMES = frozenset(
    {
        "cooldown",
        "performed",
    }
)


def read_refresh_progress(
    path: Path,
) -> dict[str, Any]:
    """Read structured refresh progress, returning no data on failure."""

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        return {}

    if not isinstance(payload, dict):
        return {}

    return payload


def write_refresh_progress(
    path: Path | None,
    **updates: Any,
) -> None:
    """Atomically merge aggregate progress into a local JSON file."""

    if path is None:
        return

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    payload = read_refresh_progress(path)
    payload.update(updates)
    temporary_path = path.with_name(
        f"{path.name}.{os.getpid()}.tmp"
    )
    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.replace(
        temporary_path,
        path,
    )


def safe_refresh_progress(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return only validated aggregate fields suitable for the API."""

    safe: dict[str, Any] = {}
    stage_key = payload.get("stage_key")

    if stage_key in ALLOWED_STAGE_KEYS:
        safe["stage_key"] = stage_key

    for key in (
        "stage_index",
        "stage_count",
        "completed_stages",
        "progress_completed",
        "progress_total",
    ):
        value = payload.get(key)

        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            safe[key] = value

    progress_mode = payload.get("progress_mode")

    if progress_mode in ALLOWED_PROGRESS_MODES:
        safe["progress_mode"] = progress_mode

    progress_unit = payload.get("progress_unit")

    if progress_unit in ALLOWED_PROGRESS_UNITS:
        safe["progress_unit"] = progress_unit

    telegram_outcome = payload.get(
        "telegram_collection_outcome"
    )

    if telegram_outcome in ALLOWED_TELEGRAM_COLLECTION_OUTCOMES:
        safe["telegram_collection_outcome"] = (
            telegram_outcome
        )

    cooldown_seconds = payload.get(
        "telegram_cooldown_seconds_remaining"
    )

    if (
        isinstance(cooldown_seconds, int)
        and not isinstance(cooldown_seconds, bool)
        and cooldown_seconds >= 0
    ):
        safe["telegram_cooldown_seconds_remaining"] = (
            cooldown_seconds
        )

    safe_limit_reached = payload.get(
        "telegram_safe_limit_reached"
    )

    if isinstance(safe_limit_reached, bool):
        safe["telegram_safe_limit_reached"] = (
            safe_limit_reached
        )

    return safe
