import re
from urllib.parse import urlsplit

from job_urls import normalize_url
from source_parsing import (
    NormalizedJob,
    SourceContext,
    SourceMessage,
)


STANDARD_TITLE_PATTERN = re.compile(
    r"^\*(?P<title>[^*\r\n]+)\*$"
)

ALTERNATE_HEADER = "New entry-level development role!"

LEVEL_PREFIX = "📈 Level: "

EXAMPLE_JOB_PATH = re.compile(
    r"^/postings/\d+/?$"
)

NOTIFICATION_SOURCE_ID_PREFIX = "wa_notification_"

NOTIFICATION_JOB_TITLE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:"
    r"developer|engineer|programmer|software|"
    r"backend|front[\s-]?end|full[\s-]?stack|"
    r"devops|qa|algorithm|embedded|data\s+analyst"
    r")\b"
    r"|מפתח(?:ת)?|מהנדס(?:ת)?|מתכנת(?:ת)?|"
    r"פיתוח|תוכנה|בדיקות"
    r")",
    re.IGNORECASE,
)

MAX_NOTIFICATION_TITLE_CHARS = 256


def _find_job_url(urls: tuple[str, ...]) -> str | None:
    for url in urls:
        normalized = normalize_url(url)

        if not normalized:
            continue

        parts = urlsplit(normalized)

        if (
            parts.netloc.casefold() == "jobs.example.com"
            and EXAMPLE_JOB_PATH.fullmatch(parts.path)
        ):
            return normalized

    return None


def _find_notification_preview_title(
    message: SourceMessage,
    nonempty_lines: list[str],
) -> str | None:
    """
    Recognize a bounded job-title preview from the native listener.

    Windows notifications can omit the link and extended WhatsApp
    message body. Keep this fallback notification-specific so native
    exports continue to require one of their demonstrated full formats.
    """

    if (
        not isinstance(message.source_message_id, str)
        or not message.source_message_id.startswith(
            NOTIFICATION_SOURCE_ID_PREFIX
        )
        or len(nonempty_lines) != 1
    ):
        return None

    return _normalize_notification_title(
        nonempty_lines[0]
    )


def _normalize_notification_title(
    value: str,
) -> str | None:
    """Validate one notification-rendered job title."""

    title = value.strip()

    if (
        len(title) >= 2
        and title.startswith("*")
        and title.endswith("*")
    ):
        title = title[1:-1].strip()

    if (
        not title
        or len(title) > MAX_NOTIFICATION_TITLE_CHARS
        or "://" in title
        or NOTIFICATION_JOB_TITLE_PATTERN.search(title) is None
    ):
        return None

    return title


def parse_whatsapp_job_digest_message(
    message: SourceMessage,
    context: SourceContext,
) -> NormalizedJob | None:
    """Parse demonstrated job formats from the example WhatsApp job digest."""

    if message.sender is None:
        return None

    raw_text = message.raw_text
    nonempty_lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    ]

    if not nonempty_lines:
        return None

    job_url = _find_job_url(message.urls)

    if job_url is None:
        notification_title = _find_notification_preview_title(
            message,
            nonempty_lines,
        )

        if notification_title is None:
            return None

        return NormalizedJob(
            source=context.source,
            source_group=context.group_name,
            source_message_id=message.source_message_id,
            source_message_url=message.source_message_url,
            message_date=message.message_date,
            title=notification_title,
            company=None,
            location=None,
            posted_on=None,
            job_url=None,
            raw_text=raw_text,
            parse_confidence=0.35,
        )

    title: str | None = None
    recognized_level = False

    title_match = STANDARD_TITLE_PATTERN.fullmatch(
        nonempty_lines[0]
    )

    if title_match is not None:
        title = title_match.group("title").strip() or None
        recognized_level = any(
            line.startswith(LEVEL_PREFIX)
            for line in nonempty_lines[1:]
        )

        if not recognized_level:
            return None

    elif (
        nonempty_lines[0] == ALTERNATE_HEADER
        and len(nonempty_lines) >= 2
    ):
        title = nonempty_lines[1].strip() or None

    elif (
        isinstance(message.source_message_id, str)
        and message.source_message_id.startswith(
            NOTIFICATION_SOURCE_ID_PREFIX
        )
    ):
        title = _normalize_notification_title(
            nonempty_lines[0]
        )
        recognized_level = any(
            line.startswith(LEVEL_PREFIX)
            for line in nonempty_lines[1:]
        )

        if not recognized_level:
            return None

    if title is None:
        return None

    posted_on: str | None = None

    if (
        "Posted today" in nonempty_lines
        and message.message_date
    ):
        posted_on = message.message_date[:10]

    confidence = 0.30 + 0.25

    if recognized_level:
        confidence += 0.10

    if posted_on:
        confidence += 0.10

    return NormalizedJob(
        source=context.source,
        source_group=context.group_name,
        source_message_id=message.source_message_id,
        source_message_url=message.source_message_url,
        message_date=message.message_date,
        title=title,
        company=None,
        location=None,
        posted_on=posted_on,
        job_url=job_url,
        raw_text=raw_text,
        parse_confidence=round(confidence, 2),
    )
