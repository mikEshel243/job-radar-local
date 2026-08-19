import re
from dataclasses import dataclass
from datetime import timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from html import unescape
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from source_parsing import NormalizedJob


LINKEDIN_JOB_SENDERS = frozenset(
    {
        "jobalerts-noreply@linkedin.com",
        "jobs-noreply@linkedin.com",
    }
)
LINKEDIN_TITLE_CLASS_MARKERS = frozenset(
    {
        "text-color-brand",
        "text-system-blue-50",
    }
)
LINKEDIN_SOURCE = "linkedin_email"
LINKEDIN_SOURCE_GROUP = "linkedin_jobs_email"
MAX_EMAIL_BYTES = 5 * 1024 * 1024
JOB_PATH = re.compile(
    r"^/(?:comm/)?jobs/view/(\d+)/?$",
    flags=re.IGNORECASE,
)


class LinkedInJobEmailError(ValueError):
    """Raised when a message is not a supported LinkedIn job email."""


@dataclass(frozen=True, slots=True)
class LinkedInEmailJobs:
    message_date: str | None
    jobs: tuple[NormalizedJob, ...]
    skipped_cards: int


def _message_date(message: EmailMessage) -> str | None:
    raw_date = message.get("Date")

    if not raw_date:
        return None

    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.isoformat()


def _html_body(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("html",))

    if body is None:
        raise LinkedInJobEmailError(
            "LinkedIn job email does not contain an HTML body."
        )

    try:
        content = body.get_content()
    except (LookupError, UnicodeError) as error:
        raise LinkedInJobEmailError(
            "LinkedIn job email HTML could not be decoded."
        ) from error

    if not isinstance(content, str) or not content.strip():
        raise LinkedInJobEmailError(
            "LinkedIn job email HTML is empty."
        )

    return content


def _linkedin_job_id(href: str) -> str | None:
    cleaned = unescape(href).strip()

    if not cleaned:
        return None

    parts = urlsplit(cleaned)
    hostname = (parts.hostname or "").rstrip(".").casefold()

    if hostname not in {"linkedin.com", "www.linkedin.com"}:
        return None

    match = JOB_PATH.fullmatch(parts.path)

    if match is None:
        return None

    return match.group(1)


def _class_contains(value: object, required: str) -> bool:
    if isinstance(value, str):
        classes = value.split()
    elif isinstance(value, list):
        classes = [str(item) for item in value]
    else:
        return False

    return required in classes


def _class_contains_any(
    value: object,
    required: frozenset[str],
) -> bool:
    if isinstance(value, str):
        classes = set(value.split())
    elif isinstance(value, list):
        classes = {str(item) for item in value}
    else:
        return False

    return bool(classes & required)


def _clean_text(value: str) -> str:
    return " ".join(unescape(value).split())


def _card_metadata(title_anchor: object) -> tuple[str, str] | None:
    find_parent = getattr(title_anchor, "find_parent", None)

    if not callable(find_parent):
        return None

    card_table = find_parent("table")

    if card_table is None:
        return None

    metadata = card_table.find(
        "p",
        class_=lambda value: _class_contains(
            value,
            "text-system-gray-100",
        ),
    )

    if metadata is None:
        return None

    metadata_text = _clean_text(
        metadata.get_text(" ", strip=True)
    )
    company, separator, location = metadata_text.partition("·")

    if not separator:
        return None

    company = _clean_text(company)
    location = _clean_text(location)

    if not company or not location:
        return None

    return company, location


def parse_linkedin_job_email(
    raw_message: bytes,
) -> LinkedInEmailJobs:
    """Parse job cards from one exact LinkedIn jobs email."""

    if not raw_message:
        raise LinkedInJobEmailError("Email message is empty.")

    if len(raw_message) > MAX_EMAIL_BYTES:
        raise LinkedInJobEmailError(
            "Email message exceeds the supported size limit."
        )

    parsed_message = BytesParser(
        policy=policy.default
    ).parsebytes(raw_message)

    sender = parseaddr(parsed_message.get("From", ""))[1].casefold()

    if sender not in LINKEDIN_JOB_SENDERS:
        raise LinkedInJobEmailError(
            "Email sender is not the supported LinkedIn jobs sender."
        )

    message_date = _message_date(parsed_message)
    soup = BeautifulSoup(
        _html_body(parsed_message),
        "html.parser",
    )
    jobs: list[NormalizedJob] = []
    seen_job_ids: set[str] = set()
    skipped_cards = 0

    title_anchors = soup.find_all(
        "a",
        class_=lambda value: _class_contains_any(
            value,
            LINKEDIN_TITLE_CLASS_MARKERS,
        ),
    )

    for title_anchor in title_anchors:
        job_id = _linkedin_job_id(
            str(title_anchor.get("href") or "")
        )

        if job_id is None or job_id in seen_job_ids:
            continue

        title = _clean_text(
            title_anchor.get_text(" ", strip=True)
        )
        metadata = _card_metadata(title_anchor)

        if not title or metadata is None:
            skipped_cards += 1
            continue

        company, location = metadata
        clean_url = (
            "https://www.linkedin.com/jobs/view/"
            f"{job_id}"
        )
        seen_job_ids.add(job_id)
        jobs.append(
            NormalizedJob(
                source=LINKEDIN_SOURCE,
                source_group=LINKEDIN_SOURCE_GROUP,
                source_message_id=job_id,
                source_message_url=clean_url,
                message_date=message_date,
                title=title,
                company=company,
                location=location,
                posted_on=None,
                job_url=clean_url,
                raw_text="\n".join(
                    (title, company, location)
                ),
                parse_confidence=0.9,
            )
        )

    if not jobs:
        raise LinkedInJobEmailError(
            "LinkedIn email contains no supported job cards."
        )

    return LinkedInEmailJobs(
        message_date=message_date,
        jobs=tuple(jobs),
        skipped_cards=skipped_cards,
    )
