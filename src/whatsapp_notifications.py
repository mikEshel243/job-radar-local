import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from native_exports import extract_visible_urls
from source_parsing import SourceMessage


PROTOCOL_VERSION = 1
MAX_CONFIG_BYTES = 16_384
MAX_APP_ID_CHARS = 512
MAX_GROUP_NAME_CHARS = 256
MAX_GROUP_ID_CHARS = 100
MAX_JSON_LINE_CHARS = 65_536
MAX_BODY_LINES = 16
MAX_BODY_LINE_CHARS = 4_096
MAX_BODY_CHARS = 32_768
MAX_DIAGNOSTIC_ERROR_CATEGORIES = 16
SOURCE_MESSAGE_ID_PATTERN = re.compile(
    r"^wa_notification_[0-9a-f]{64}$"
)
DIAGNOSTIC_ERROR_CATEGORY_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9]{0,127}, HRESULT 0x[0-9A-F]{8}$"
)
OFFICIAL_WHATSAPP_AUMID_PREFIXES = (
    "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!",
)


class WhatsAppNotificationError(ValueError):
    """Raised for invalid local configuration or companion data."""


@dataclass(frozen=True, slots=True)
class WhatsAppNotificationConfig:
    version: int
    app_user_model_id: str
    group_name: str
    group_identifier: str
    poll_interval_seconds: int
    max_notifications_per_poll: int


@dataclass(frozen=True, slots=True)
class DiagnosticSummary:
    total_notifications: int
    application_identity_errors: int
    allowed_app_notifications: int
    exact_group_notifications: int
    accepted_notifications: int
    oversized_notifications: int
    application_info_errors: int | None = None
    application_info_error_categories: dict[str, int] | None = None
    official_package_family_matches: int | None = None
    app_user_model_id_errors: int | None = None
    reconstructed_application_identities: int | None = None
    visual_inspection_errors: int | None = None


def _require_exact_keys(
    value: dict[str, Any],
    expected: set[str],
    *,
    description: str,
) -> None:
    extra = set(value) - expected
    missing = expected - set(value)

    if extra or missing:
        raise WhatsAppNotificationError(
            f"{description} has unexpected or missing fields."
        )


def _require_bounded_text(
    value: Any,
    *,
    field_name: str,
    max_chars: int,
) -> str:
    if not isinstance(value, str) or not value:
        raise WhatsAppNotificationError(
            f"{field_name} must be non-empty text."
        )

    if value != value.strip():
        raise WhatsAppNotificationError(
            f"{field_name} must not have outer whitespace."
        )

    if len(value) > max_chars:
        raise WhatsAppNotificationError(
            f"{field_name} exceeds its size limit."
        )

    if any(ord(character) < 32 for character in value):
        raise WhatsAppNotificationError(
            f"{field_name} contains a control character."
        )

    return value


def _require_bounded_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise WhatsAppNotificationError(
            f"{field_name} must be between "
            f"{minimum} and {maximum}."
        )

    return value


def load_notification_config(
    path: Path,
) -> WhatsAppNotificationConfig:
    """Load a small local allowlist without exposing its values."""

    try:
        size = path.stat().st_size
    except OSError as error:
        raise WhatsAppNotificationError(
            "Could not inspect the notification config file."
        ) from error

    if size > MAX_CONFIG_BYTES:
        raise WhatsAppNotificationError(
            "Notification config exceeds its size limit."
        )

    try:
        raw_config = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError) as error:
        raise WhatsAppNotificationError(
            "Could not read the notification config as UTF-8."
        ) from error
    except json.JSONDecodeError as error:
        raise WhatsAppNotificationError(
            "Notification config is not valid JSON."
        ) from error

    if not isinstance(raw_config, dict):
        raise WhatsAppNotificationError(
            "Notification config root must be an object."
        )

    expected_keys = {
        "version",
        "app_user_model_id",
        "group_name",
        "group_identifier",
        "poll_interval_seconds",
        "max_notifications_per_poll",
    }
    _require_exact_keys(
        raw_config,
        expected_keys,
        description="Notification config",
    )

    version = raw_config["version"]

    if version != PROTOCOL_VERSION:
        raise WhatsAppNotificationError(
            "Unsupported notification config version."
        )

    app_user_model_id = _require_bounded_text(
        raw_config["app_user_model_id"],
        field_name="app_user_model_id",
        max_chars=MAX_APP_ID_CHARS,
    )

    if not app_user_model_id.startswith(
        OFFICIAL_WHATSAPP_AUMID_PREFIXES
    ):
        raise WhatsAppNotificationError(
            "app_user_model_id is not an allowlisted official "
            "WhatsApp Desktop package identity."
        )

    return WhatsAppNotificationConfig(
        version=version,
        app_user_model_id=app_user_model_id,
        group_name=_require_bounded_text(
            raw_config["group_name"],
            field_name="group_name",
            max_chars=MAX_GROUP_NAME_CHARS,
        ),
        group_identifier=_require_bounded_text(
            raw_config["group_identifier"],
            field_name="group_identifier",
            max_chars=MAX_GROUP_ID_CHARS,
        ),
        poll_interval_seconds=_require_bounded_int(
            raw_config["poll_interval_seconds"],
            field_name="poll_interval_seconds",
            minimum=2,
            maximum=300,
        ),
        max_notifications_per_poll=_require_bounded_int(
            raw_config["max_notifications_per_poll"],
            field_name="max_notifications_per_poll",
            minimum=1,
            maximum=500,
        ),
    )


def _parse_iso_datetime(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise WhatsAppNotificationError(
            "message_date must be a bounded ISO timestamp."
        )

    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WhatsAppNotificationError(
            "message_date must be an ISO timestamp."
        ) from error

    return value


def _parse_body_lines(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_BODY_LINES
    ):
        raise WhatsAppNotificationError(
            "body_lines must be a bounded non-empty list."
        )

    body_lines: list[str] = []
    total_chars = 0

    for line in value:
        if not isinstance(line, str):
            raise WhatsAppNotificationError(
                "body_lines must contain only text."
            )

        if len(line) > MAX_BODY_LINE_CHARS:
            raise WhatsAppNotificationError(
                "A notification body line exceeds its size limit."
            )

        total_chars += len(line)

        if total_chars > MAX_BODY_CHARS:
            raise WhatsAppNotificationError(
                "Notification body exceeds its size limit."
            )

        body_lines.append(line)

    if not any(line.strip() for line in body_lines):
        raise WhatsAppNotificationError(
            "Notification body must not be empty."
        )

    return tuple(body_lines)


def _split_sender_and_body(
    body_lines: tuple[str, ...],
) -> tuple[str, str]:
    combined = "\n".join(body_lines)
    first_line, separator, remainder = combined.partition("\n")
    sender, sender_separator, first_message_line = (
        first_line.partition(": ")
    )

    if (
        sender_separator
        and sender.strip()
        and len(sender.strip()) <= 256
    ):
        raw_text = first_message_line

        if separator:
            raw_text += "\n" + remainder

        return sender.strip(), raw_text

    if (
        len(body_lines) >= 2
        and "\n" not in body_lines[0]
        and 0 < len(body_lines[0].strip()) <= 256
        and not body_lines[0].lstrip().startswith(
            ("*", "http://", "https://")
        )
    ):
        remaining_body = "\n".join(body_lines[1:])

        if remaining_body.lstrip().startswith("*"):
            return body_lines[0].strip(), remaining_body

    return "notification", combined


def parse_notification_record(
    line: str,
    *,
    expected_group_identifier: str,
) -> SourceMessage:
    """Validate one accepted companion record as SourceMessage."""

    if len(line) > MAX_JSON_LINE_CHARS:
        raise WhatsAppNotificationError(
            "Companion record exceeds its size limit."
        )

    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise WhatsAppNotificationError(
            "Companion emitted invalid JSON."
        ) from error

    if not isinstance(record, dict):
        raise WhatsAppNotificationError(
            "Companion record root must be an object."
        )

    expected_keys = {
        "type",
        "protocol_version",
        "group_identifier",
        "source_message_id",
        "message_date",
        "body_lines",
    }
    _require_exact_keys(
        record,
        expected_keys,
        description="Companion notification record",
    )

    if (
        record["type"] != "notification"
        or record["protocol_version"] != PROTOCOL_VERSION
    ):
        raise WhatsAppNotificationError(
            "Companion protocol version or record type is invalid."
        )

    if record["group_identifier"] != expected_group_identifier:
        raise WhatsAppNotificationError(
            "Companion record has the wrong group identifier."
        )

    source_message_id = record["source_message_id"]

    if (
        not isinstance(source_message_id, str)
        or SOURCE_MESSAGE_ID_PATTERN.fullmatch(
            source_message_id
        )
        is None
    ):
        raise WhatsAppNotificationError(
            "Companion source message ID is invalid."
        )

    message_date = _parse_iso_datetime(
        record["message_date"]
    )
    body_lines = _parse_body_lines(record["body_lines"])
    sender, raw_text = _split_sender_and_body(body_lines)

    return SourceMessage(
        source_message_id=source_message_id,
        message_date=message_date,
        sender=sender,
        raw_text=raw_text,
        urls=extract_visible_urls(raw_text),
        source_message_url=None,
    )


def parse_diagnostic_record(line: str) -> DiagnosticSummary:
    """Validate aggregate-only diagnostic output."""

    if len(line) > MAX_JSON_LINE_CHARS:
        raise WhatsAppNotificationError(
            "Diagnostic record exceeds its size limit."
        )

    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise WhatsAppNotificationError(
            "Companion emitted invalid diagnostic JSON."
        ) from error

    if not isinstance(record, dict):
        raise WhatsAppNotificationError(
            "Diagnostic record root must be an object."
        )

    legacy_keys = {
        "type",
        "protocol_version",
        "total_notifications",
        "application_identity_errors",
        "allowed_app_notifications",
        "exact_group_notifications",
        "accepted_notifications",
        "oversized_notifications",
    }
    extended_keys = {
        "application_info_errors",
        "app_user_model_id_errors",
        "reconstructed_application_identities",
    }
    category_keys = {
        "application_info_error_categories",
    }
    native_package_keys = {
        "official_package_family_matches",
    }
    native_visual_keys = {
        "visual_inspection_errors",
    }
    record_keys = set(record)

    if record_keys not in (
        legacy_keys,
        legacy_keys | extended_keys,
        legacy_keys | extended_keys | category_keys,
        (
            legacy_keys
            | extended_keys
            | category_keys
            | native_package_keys
        ),
        (
            legacy_keys
            | extended_keys
            | category_keys
            | native_package_keys
            | native_visual_keys
        ),
    ):
        raise WhatsAppNotificationError(
            "Companion diagnostic record has unexpected fields."
        )

    if (
        record["type"] != "diagnostic"
        or record["protocol_version"] != PROTOCOL_VERSION
    ):
        raise WhatsAppNotificationError(
            "Companion diagnostic protocol is invalid."
        )

    values: dict[str, Any] = {}

    for field_name in legacy_keys - {
        "type",
        "protocol_version",
    }:
        values[field_name] = _require_bounded_int(
            record[field_name],
            field_name=field_name,
            minimum=0,
            maximum=1_000_000,
        )

    if extended_keys <= record_keys:
        for field_name in extended_keys:
            values[field_name] = _require_bounded_int(
                record[field_name],
                field_name=field_name,
                minimum=0,
                maximum=1_000_000,
            )

    if category_keys <= record_keys:
        categories = record["application_info_error_categories"]

        if (
            not isinstance(categories, dict)
            or len(categories) > MAX_DIAGNOSTIC_ERROR_CATEGORIES
        ):
            raise WhatsAppNotificationError(
                "Application-info error categories are invalid."
            )

        parsed_categories: dict[str, int] = {}

        for category, count in categories.items():
            if (
                not isinstance(category, str)
                or DIAGNOSTIC_ERROR_CATEGORY_PATTERN.fullmatch(
                    category
                )
                is None
            ):
                raise WhatsAppNotificationError(
                    "Application-info error category is invalid."
                )

            parsed_categories[category] = _require_bounded_int(
                count,
                field_name="application_info_error_category_count",
                minimum=1,
                maximum=1_000_000,
            )

        values["application_info_error_categories"] = (
            parsed_categories
        )

    if native_package_keys <= record_keys:
        values["official_package_family_matches"] = (
            _require_bounded_int(
                record["official_package_family_matches"],
                field_name="official_package_family_matches",
                minimum=0,
                maximum=1_000_000,
            )
        )

    if native_visual_keys <= record_keys:
        values["visual_inspection_errors"] = (
            _require_bounded_int(
                record["visual_inspection_errors"],
                field_name="visual_inspection_errors",
                minimum=0,
                maximum=1_000_000,
            )
        )

    return DiagnosticSummary(**values)


def build_notification_source_message_id(
    *,
    app_user_model_id: str,
    group_identifier: str,
    notification_id: int,
    creation_time: str,
) -> str:
    """Reference implementation used by synthetic Python tests."""

    fingerprint = "\n".join(
        (
            app_user_model_id,
            group_identifier,
            str(notification_id),
            creation_time,
        )
    )
    digest = hashlib.sha256(
        fingerprint.encode("utf-8")
    ).hexdigest()
    return f"wa_notification_{digest}"


def parse_notification_records(
    lines: Iterable[str],
    *,
    expected_group_identifier: str,
) -> tuple[SourceMessage, ...]:
    """Parse a bounded synthetic or companion record stream."""

    return tuple(
        parse_notification_record(
            line,
            expected_group_identifier=(
                expected_group_identifier
            ),
        )
        for line in lines
        if line.strip()
    )
