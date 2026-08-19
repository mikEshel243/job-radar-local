import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from source_parsing import SourceMessage


WHATSAPP_HEADER_PATTERN = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{2}), "
    r"(?P<time>\d{2}:\d{2}) - "
    r"(?P<payload>.*)$"
)

VISIBLE_URL_PATTERN = re.compile(
    r"https?://[^\s<>()]+",
    flags=re.IGNORECASE,
)


class NativeExportFormatError(ValueError):
    """Raised when a local export does not match a supported format."""


@dataclass(frozen=True, slots=True)
class TelegramNativeExport:
    """Validated metadata and messages from Telegram Desktop JSON."""

    group_name: str
    export_type: str
    native_group_id: int
    messages: tuple[SourceMessage, ...]
    skipped_service_records: int


def _read_utf8_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise NativeExportFormatError(
            f"{path} is not valid UTF-8."
        ) from error
    except OSError as error:
        raise NativeExportFormatError(
            f"Could not read {path}: {error}"
        ) from error


def extract_visible_urls(raw_text: str) -> tuple[str, ...]:
    return tuple(
        match.group(0)
        for match in VISIBLE_URL_PATTERN.finditer(raw_text)
    )


def _normalize_fingerprint_part(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().casefold()


def build_whatsapp_message_fingerprint(
    group_identifier: str,
    message_date: str,
    sender: str | None,
    raw_text: str,
    occurrence_index: int,
) -> str:
    """Build a deterministic local ID for one WhatsApp message."""

    if occurrence_index < 1:
        raise ValueError(
            "WhatsApp occurrence index must be at least 1."
        )

    fingerprint_source = "\n".join(
        (
            group_identifier.strip().casefold(),
            message_date,
            _normalize_fingerprint_part(sender or ""),
            _normalize_fingerprint_part(raw_text),
            f"occurrence:{occurrence_index}",
        )
    )

    digest = hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()

    return f"wa_{digest}"


def read_whatsapp_export(
    path: Path,
    group_identifier: str,
) -> tuple[SourceMessage, ...]:
    """Read the demonstrated WhatsApp Android text export format."""

    text = _read_utf8_text(path)
    records: list[
        tuple[str, str | None, str]
    ] = []
    current_date: str | None = None
    current_sender: str | None = None
    current_body_lines: list[str] = []

    def finish_current_record() -> None:
        if current_date is None:
            return

        records.append(
            (
                current_date,
                current_sender,
                "\n".join(current_body_lines),
            )
        )

    for line_number, line in enumerate(
        text.splitlines(),
        start=1,
    ):
        header_match = WHATSAPP_HEADER_PATTERN.fullmatch(
            line
        )

        if header_match is None:
            if current_date is None:
                if not line:
                    continue

                raise NativeExportFormatError(
                    "Unexpected content before the first "
                    f"WhatsApp header on line {line_number}."
                )

            current_body_lines.append(line)
            continue

        finish_current_record()

        timestamp_text = (
            f"{header_match.group('date')} "
            f"{header_match.group('time')}"
        )

        try:
            parsed_timestamp = datetime.strptime(
                timestamp_text,
                "%m/%d/%y %H:%M",
            )
        except ValueError as error:
            raise NativeExportFormatError(
                "Invalid WhatsApp timestamp on "
                f"line {line_number}: {timestamp_text!r}."
            ) from error

        current_date = parsed_timestamp.isoformat(
            timespec="seconds"
        )

        payload = header_match.group("payload")
        sender, separator, first_body_line = (
            payload.partition(": ")
        )

        if separator and sender:
            current_sender = sender
            current_body_lines = [first_body_line]
        else:
            current_sender = None
            current_body_lines = [payload]

    finish_current_record()

    if not records:
        raise NativeExportFormatError(
            "No WhatsApp messages matched the supported "
            "M/d/yy, HH:mm export format."
        )

    occurrence_counts: dict[str, int] = {}
    messages: list[SourceMessage] = []

    for message_date, sender, raw_text in records:
        occurrence_key = "\n".join(
            (
                group_identifier.strip().casefold(),
                message_date,
                _normalize_fingerprint_part(sender or ""),
                _normalize_fingerprint_part(raw_text),
            )
        )
        occurrence_index = (
            occurrence_counts.get(occurrence_key, 0) + 1
        )
        occurrence_counts[occurrence_key] = occurrence_index

        source_message_id = (
            build_whatsapp_message_fingerprint(
                group_identifier=group_identifier,
                message_date=message_date,
                sender=sender,
                raw_text=raw_text,
                occurrence_index=occurrence_index,
            )
        )

        messages.append(
            SourceMessage(
                source_message_id=source_message_id,
                message_date=message_date,
                sender=sender,
                raw_text=raw_text,
                urls=extract_visible_urls(raw_text),
                source_message_url=None,
            )
        )

    return tuple(messages)


def _flatten_telegram_text(value: Any) -> str:
    if isinstance(value, str):
        return value

    if not isinstance(value, list):
        raise NativeExportFormatError(
            "Telegram message text must be a string or list."
        )

    parts: list[str] = []

    for part in value:
        if isinstance(part, str):
            parts.append(part)
            continue

        if (
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
        ):
            parts.append(part["text"])
            continue

        raise NativeExportFormatError(
            "Telegram message text contains an unsupported "
            "entity shape."
        )

    return "".join(parts)


def _extract_telegram_urls(
    raw_text: str,
    entities: Any,
) -> tuple[str, ...]:
    found_urls: list[str] = []

    if not isinstance(entities, list):
        raise NativeExportFormatError(
            "Telegram text_entities must be a list."
        )

    for entity in entities:
        if not isinstance(entity, dict):
            raise NativeExportFormatError(
                "Telegram text_entities contains a "
                "non-object value."
            )

        entity_type = entity.get("type")

        if (
            entity_type == "text_link"
            and isinstance(entity.get("href"), str)
        ):
            found_urls.append(entity["href"])

        elif (
            entity_type == "url"
            and isinstance(entity.get("text"), str)
        ):
            found_urls.append(entity["text"])

    found_urls.extend(extract_visible_urls(raw_text))

    unique_urls: list[str] = []
    seen: set[str] = set()

    for url in found_urls:
        if url in seen:
            continue

        seen.add(url)
        unique_urls.append(url)

    return tuple(unique_urls)


def read_telegram_json_export(
    path: Path,
) -> TelegramNativeExport:
    """Read the demonstrated Telegram Desktop JSON export."""

    text = _read_utf8_text(path)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise NativeExportFormatError(
            f"Invalid Telegram JSON: {error}"
        ) from error

    if not isinstance(data, dict):
        raise NativeExportFormatError(
            "Telegram export root must be an object."
        )

    group_name = data.get("name")
    export_type = data.get("type")
    native_group_id = data.get("id")
    raw_messages = data.get("messages")

    if not isinstance(group_name, str) or not group_name:
        raise NativeExportFormatError(
            "Telegram export has no valid group name."
        )

    if export_type != "public_channel":
        raise NativeExportFormatError(
            "Only the demonstrated Telegram public_channel "
            "export type is supported."
        )

    if (
        not isinstance(native_group_id, int)
        or isinstance(native_group_id, bool)
    ):
        raise NativeExportFormatError(
            "Telegram export has no numeric native group ID."
        )

    if not isinstance(raw_messages, list):
        raise NativeExportFormatError(
            "Telegram export messages must be a list."
        )

    messages: list[SourceMessage] = []
    skipped_service_records = 0
    seen_message_ids: set[int] = set()

    for record_index, record in enumerate(raw_messages):
        if not isinstance(record, dict):
            raise NativeExportFormatError(
                "Telegram messages contains a non-object "
                f"record at index {record_index}."
            )

        record_type = record.get("type")

        if record_type == "service":
            skipped_service_records += 1
            continue

        if record_type != "message":
            raise NativeExportFormatError(
                "Unsupported Telegram record type "
                f"{record_type!r} at index {record_index}."
            )

        message_id = record.get("id")

        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
        ):
            raise NativeExportFormatError(
                "Telegram message has no numeric ID at "
                f"index {record_index}."
            )

        if message_id in seen_message_ids:
            raise NativeExportFormatError(
                f"Duplicate Telegram message ID {message_id}."
            )

        seen_message_ids.add(message_id)

        unix_timestamp = record.get("date_unixtime")

        if (
            not isinstance(unix_timestamp, str)
            or not unix_timestamp.isdigit()
        ):
            raise NativeExportFormatError(
                "Telegram message has no valid date_unixtime "
                f"at index {record_index}."
            )

        message_date = datetime.fromtimestamp(
            int(unix_timestamp),
            tz=timezone.utc,
        ).isoformat()
        raw_text = _flatten_telegram_text(
            record.get("text")
        )
        urls = _extract_telegram_urls(
            raw_text,
            record.get("text_entities"),
        )
        sender = record.get("from")

        if sender is not None and not isinstance(sender, str):
            raise NativeExportFormatError(
                "Telegram message sender must be text at "
                f"index {record_index}."
            )

        messages.append(
            SourceMessage(
                source_message_id=message_id,
                message_date=message_date,
                sender=sender,
                raw_text=raw_text,
                urls=urls,
                source_message_url=None,
            )
        )

    return TelegramNativeExport(
        group_name=group_name,
        export_type=export_type,
        native_group_id=native_group_id,
        messages=tuple(messages),
        skipped_service_records=skipped_service_records,
    )
