import re
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from html import unescape
from typing import Iterable
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from database import initialize_database, save_parsed_job
from evaluate_jobs import evaluate_stored_jobs
from job_analysis import (
    analyze_job_description,
    ensure_job_analysis_table,
    save_job_analysis,
)
from job_details import (
    JobDetails,
    ensure_job_details_table,
    save_job_details,
)
from job_filter import ensure_evaluation_table, load_profile
from source_parsing import NormalizedJob


AMAZON_EMAIL_SENDER = "noreply@mail.amazon.jobs"
AMAZON_EMAIL_SUBJECT_PREFIX = "recommended amazon jobs for "
AMAZON_EMAIL_SOURCE = "amazon_email"
AMAZON_EMAIL_SOURCE_GROUP = "amazon_recommendation_email"
AMAZON_MANUAL_SOURCE = "amazon_manual"
AMAZON_MANUAL_SOURCE_GROUP = "amazon_recommendations_clipboard"
AMAZON_RECOMMENDATIONS_URL = (
    "https://www.amazon.jobs/user/recommendations"
)
MAX_EMAIL_BYTES = 5 * 1024 * 1024
MAX_CLIPBOARD_TEXT_LENGTH = 250_000
MAX_CLIPBOARD_HTML_LENGTH = 750_000
MAX_JOB_CARD_TEXT_LENGTH = 8_000

AMAZON_TARGET_PATTERN = re.compile(
    r"https?://(?:www\.)?amazon\.jobs/"
    r"(?:(?:[a-z]{2}(?:-[a-z]{2})?)/)?"
    r"jobs/(\d+)",
    flags=re.IGNORECASE,
)
JOB_ID_LINE_PATTERN = re.compile(
    r"\bjob\s*id\s*[:#-]?\s*(\d{5,12})\b",
    flags=re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    flags=re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://\S+", flags=re.IGNORECASE)
PHONE_PATTERN = re.compile(
    r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)"
)
LOCATION_PATTERN = re.compile(
    r"(?:\bEXL\b|\bExampleland\b|\bISR\b|\bIsrael\b|\bUSA\b|\bUnited States\b|"
    r"\bUnited Kingdom\b|\bUK\b|\bCanada\b|\bGermany\b|"
    r"\bIndia\b|\bIreland\b|\bSpain\b|\bFrance\b|"
    r"\bItaly\b|\bPoland\b|\bRomania\b|\bJapan\b|"
    r"\bAustralia\b|\+\d+\s+other locations?)",
    flags=re.IGNORECASE,
)
IGNORED_CARD_LINES = frozenset(
    {
        "|",
        "read more",
        "apply now",
        "view job",
        "view jobs",
        "view all",
        "view all recommendations",
        "basic qualifications",
        "preferred qualifications",
        "sort by",
        "most relevant",
        "most recent",
    }
)
FOOTER_PREFIXES = (
    "amazon is an equal opportunity employer",
    "equal opportunity",
    "join us on",
    "privacy and data",
    "career areas",
    "working at amazon",
)


class AmazonJobImportError(ValueError):
    """Raised when Amazon job content fails closed validation."""


@dataclass(frozen=True, slots=True)
class AmazonEmailJobs:
    message_date: str | None
    jobs: tuple[NormalizedJob, ...]
    skipped_cards: int


@dataclass(frozen=True, slots=True)
class AmazonImportSummary:
    jobs_parsed: int
    new_jobs: int
    new_postings: int
    existing_postings: int
    analyzed_jobs: int
    evaluated_jobs: int


def _clean_line(value: str) -> str:
    return " ".join(unescape(value).split())


def _sanitized_lines(value: str) -> list[str]:
    sanitized = EMAIL_PATTERN.sub("", value)
    sanitized = URL_PATTERN.sub("", sanitized)
    lines: list[str] = []

    for raw_line in sanitized.splitlines():
        line = _clean_line(raw_line).strip("•\u2022 ")

        if not JOB_ID_LINE_PATTERN.search(line):
            line = _clean_line(PHONE_PATTERN.sub("", line))

        if line and (not lines or line != lines[-1]):
            lines.append(line)

    return lines


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
        raise AmazonJobImportError(
            "Amazon recommendation email has no HTML body."
        )

    try:
        content = body.get_content()
    except (LookupError, UnicodeError) as error:
        raise AmazonJobImportError(
            "Amazon recommendation email HTML could not be decoded."
        ) from error

    if not isinstance(content, str) or not content.strip():
        raise AmazonJobImportError(
            "Amazon recommendation email HTML is empty."
        )

    return content


def amazon_job_id_from_url(value: str) -> str | None:
    """Extract one public Amazon job ID without retaining tracking data."""

    cleaned = unescape(value).strip()

    if not cleaned:
        return None

    hostname = (
        urlsplit(cleaned).hostname or ""
    ).rstrip(".").casefold()

    if not (
        hostname in {"amazon.jobs", "www.amazon.jobs"}
        or hostname.endswith(".awstrack.me")
    ):
        return None

    decoded = cleaned

    for _ in range(2):
        decoded = unquote(decoded)
        match = AMAZON_TARGET_PATTERN.search(decoded)

        if match is not None:
            return match.group(1)

    return None


def canonical_amazon_job_url(job_id: str) -> str:
    return f"https://www.amazon.jobs/jobs/{job_id}"


def _card_container(anchor: object, job_id: str) -> object:
    parents = getattr(anchor, "parents", ())
    fallback = getattr(anchor, "parent", anchor)

    for parent in parents:
        name = str(getattr(parent, "name", "") or "")

        if name not in {"article", "div", "li", "table", "td"}:
            continue

        text = _clean_line(
            parent.get_text("\n", strip=True)
        )
        parent_job_ids = {
            detected_id
            for child_anchor in parent.find_all("a", href=True)
            if (
                detected_id := amazon_job_id_from_url(
                    str(child_anchor.get("href") or "")
                )
            )
        }

        if (
            parent_job_ids == {job_id}
            and "basic qualifications" in text.casefold()
        ):
            return parent

        if (
            parent_job_ids == {job_id}
            and name in {"article", "li", "table"}
        ):
            fallback = parent

    return fallback


def _location_from_lines(
    lines: list[str],
    *,
    title: str,
) -> str | None:
    try:
        title_index = lines.index(title)
    except ValueError:
        title_index = -1

    candidates = lines[title_index + 1 : title_index + 8]

    for line in candidates:
        folded = line.casefold()

        if (
            line == "|"
            or folded in IGNORED_CARD_LINES
            or JOB_ID_LINE_PATTERN.search(line)
            or folded.startswith("posted ")
        ):
            continue

        if LOCATION_PATTERN.search(line):
            return line[:300]

    return None


def _company_from_lines(lines: list[str]) -> str:
    joined = "\n".join(lines)

    if re.search(r"\bAnnapurna Labs\b", joined, re.IGNORECASE):
        return "Annapurna Labs Ltd."

    for line in lines:
        if re.search(
            r"\bAmazon\b.*\b(?:Ltd\.?|LLC|Inc\.?)\b",
            line,
            flags=re.IGNORECASE,
        ):
            return line[:300]

    return "Amazon"


def _raw_card_text(
    lines: list[str],
    *,
    title: str,
    company: str,
    location: str | None,
) -> str:
    output = [title, company]

    if location:
        output.append(location)

    for line in lines:
        if line in output:
            continue

        if any(
            line.casefold().startswith(prefix)
            for prefix in FOOTER_PREFIXES
        ):
            break

        output.append(line)

    return "\n".join(output)[:MAX_JOB_CARD_TEXT_LENGTH]


def _jobs_from_html(
    html: str,
    *,
    source: str,
    source_group: str,
    message_date: str | None,
) -> tuple[tuple[NormalizedJob, ...], int]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[NormalizedJob] = []
    seen_ids: set[str] = set()
    skipped_cards = 0

    for anchor in soup.find_all("a", href=True):
        job_id = amazon_job_id_from_url(
            str(anchor.get("href") or "")
        )

        if job_id is None or job_id in seen_ids:
            continue

        title = _clean_line(anchor.get_text(" ", strip=True))

        if not title or title.casefold() in IGNORED_CARD_LINES:
            skipped_cards += 1
            continue

        container = _card_container(anchor, job_id)
        lines = _sanitized_lines(
            container.get_text("\n", strip=True)
        )
        location = _location_from_lines(lines, title=title)
        company = _company_from_lines(lines)
        clean_url = canonical_amazon_job_url(job_id)
        seen_ids.add(job_id)
        jobs.append(
            NormalizedJob(
                source=source,
                source_group=source_group,
                source_message_id=job_id,
                source_message_url=clean_url,
                message_date=message_date,
                title=title[:500],
                company=company,
                location=location,
                posted_on=None,
                job_url=clean_url,
                raw_text=_raw_card_text(
                    lines,
                    title=title,
                    company=company,
                    location=location,
                ),
                parse_confidence=0.9,
            )
        )

    return tuple(jobs), skipped_cards


def parse_amazon_job_email(raw_message: bytes) -> AmazonEmailJobs:
    """Parse exact Amazon recommendation cards from one email."""

    if not raw_message:
        raise AmazonJobImportError("Email message is empty.")

    if len(raw_message) > MAX_EMAIL_BYTES:
        raise AmazonJobImportError(
            "Email message exceeds the supported size limit."
        )

    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = parseaddr(message.get("From", ""))[1].casefold()
    subject = _clean_line(str(message.get("Subject", ""))).casefold()

    if sender != AMAZON_EMAIL_SENDER:
        raise AmazonJobImportError(
            "Email sender is not the supported Amazon jobs sender."
        )

    if (
        not subject.startswith(AMAZON_EMAIL_SUBJECT_PREFIX)
        or len(subject) == len(AMAZON_EMAIL_SUBJECT_PREFIX)
    ):
        raise AmazonJobImportError(
            "Email subject is not a supported Amazon recommendation."
        )

    jobs, skipped_cards = _jobs_from_html(
        _html_body(message),
        source=AMAZON_EMAIL_SOURCE,
        source_group=AMAZON_EMAIL_SOURCE_GROUP,
        message_date=_message_date(message),
    )

    if not jobs:
        raise AmazonJobImportError(
            "Amazon recommendation email contains no supported job cards."
        )

    return AmazonEmailJobs(
        message_date=_message_date(message),
        jobs=jobs,
        skipped_cards=skipped_cards,
    )


def _is_title_candidate(line: str) -> bool:
    folded = line.casefold()

    return bool(
        3 <= len(line) <= 500
        and folded not in IGNORED_CARD_LINES
        and not JOB_ID_LINE_PATTERN.search(line)
        and not folded.startswith("posted ")
        and not folded.startswith("updated ")
        and not LOCATION_PATTERN.fullmatch(line)
        and not all(character in "|•-–—" for character in line)
    )


def _plain_text_jobs(text: str) -> tuple[NormalizedJob, ...]:
    lines = _sanitized_lines(text)
    markers: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        match = JOB_ID_LINE_PATTERN.search(line)

        if match is not None:
            markers.append((index, match.group(1)))

    if not markers:
        raise AmazonJobImportError(
            "No Amazon Job ID lines were found in the pasted text."
        )

    parsed_rows: list[tuple[int, str, str, str | None]] = []

    for marker_index, job_id in markers:
        lower_bound = max(0, marker_index - 12)
        location_index: int | None = None

        for index in range(marker_index - 1, lower_bound - 1, -1):
            if LOCATION_PATTERN.search(lines[index]):
                location_index = index
                break

        title_search_end = (
            location_index
            if location_index is not None
            else marker_index
        )
        title_index: int | None = None

        for index in range(title_search_end - 1, lower_bound - 1, -1):
            if _is_title_candidate(lines[index]):
                title_index = index
                break

        if title_index is None:
            continue

        parsed_rows.append(
            (
                title_index,
                job_id,
                lines[title_index],
                (
                    lines[location_index]
                    if location_index is not None
                    else None
                ),
            )
        )

    jobs: list[NormalizedJob] = []
    seen_ids: set[str] = set()

    for row_index, (title_index, job_id, title, location) in enumerate(
        parsed_rows
    ):
        if job_id in seen_ids:
            continue

        end_index = (
            parsed_rows[row_index + 1][0]
            if row_index + 1 < len(parsed_rows)
            else len(lines)
        )
        card_lines = lines[title_index:end_index]
        company = _company_from_lines(card_lines)
        raw_text = _raw_card_text(
            card_lines,
            title=title,
            company=company,
            location=location,
        )
        clean_url = canonical_amazon_job_url(job_id)
        seen_ids.add(job_id)
        jobs.append(
            NormalizedJob(
                source=AMAZON_MANUAL_SOURCE,
                source_group=AMAZON_MANUAL_SOURCE_GROUP,
                source_message_id=job_id,
                source_message_url=clean_url,
                message_date=None,
                title=title,
                company=company,
                location=location,
                posted_on=None,
                job_url=clean_url,
                raw_text=raw_text,
                parse_confidence=0.75,
            )
        )

    if not jobs:
        raise AmazonJobImportError(
            "Amazon job cards could not be identified in the pasted text."
        )

    return tuple(jobs)


def parse_amazon_recommendations_clipboard(
    *,
    text: str,
    html: str | None = None,
) -> tuple[NormalizedJob, ...]:
    """Parse a user-copied Amazon recommendation snapshot locally."""

    if len(text) > MAX_CLIPBOARD_TEXT_LENGTH:
        raise AmazonJobImportError(
            "Pasted Amazon text exceeds the supported size limit."
        )

    if html is not None and len(html) > MAX_CLIPBOARD_HTML_LENGTH:
        raise AmazonJobImportError(
            "Pasted Amazon HTML exceeds the supported size limit."
        )

    if html and html.strip():
        jobs, _ = _jobs_from_html(
            html,
            source=AMAZON_MANUAL_SOURCE,
            source_group=AMAZON_MANUAL_SOURCE_GROUP,
            message_date=None,
        )

        if jobs:
            return jobs

    if not text.strip():
        raise AmazonJobImportError(
            "Paste the loaded Amazon recommendations before importing."
        )

    return _plain_text_jobs(text)


def _save_summary_details(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    job: NormalizedJob,
) -> bool:
    existing = connection.execute(
        """
        SELECT
            fetch_status,
            description_text
        FROM job_details
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()

    if existing is not None:
        existing_status = str(existing["fetch_status"] or "")
        existing_text = str(existing["description_text"] or "")

        if existing_status != "source_automation_prohibited":
            return False

        if len(existing_text) >= len(job.raw_text):
            return False

    save_job_details(
        connection,
        JobDetails(
            job_id=job_id,
            final_url=job.job_url,
            page_title=job.title,
            description_text=job.raw_text,
            extractor=job.source,
            fetch_status="source_automation_prohibited",
            fetch_error=(
                "Full Amazon page was not fetched automatically."
            ),
            http_status=None,
        ),
    )
    return True


def persist_amazon_jobs(
    connection: sqlite3.Connection,
    jobs: Iterable[NormalizedJob],
) -> AmazonImportSummary:
    """Save sanitized Amazon cards and analyze their local summaries."""

    jobs = tuple(jobs)
    initialize_database(connection)
    ensure_job_details_table(connection)
    ensure_job_analysis_table(connection)
    ensure_evaluation_table(connection)
    new_jobs = 0
    new_postings = 0
    existing_postings = 0
    analyzed_jobs = 0
    sources: set[str] = set()

    with connection:
        for job in jobs:
            sources.add(job.source)
            job_id, was_new_job, was_new_posting = save_parsed_job(
                connection,
                job,
            )
            new_jobs += int(was_new_job)
            new_postings += int(was_new_posting)
            existing_postings += int(not was_new_posting)
            details_updated = _save_summary_details(
                connection,
                job_id=job_id,
                job=job,
            )
            existing_analysis = connection.execute(
                "SELECT 1 FROM job_analysis WHERE job_id = ?",
                (job_id,),
            ).fetchone()

            if details_updated and existing_analysis is None:
                save_job_analysis(
                    connection,
                    analyze_job_description(
                        job_id=job_id,
                        title=job.title,
                        description_text=job.raw_text,
                    ),
                )
                analyzed_jobs += 1

    evaluated_jobs = 0
    profile = load_profile()

    for source in sorted(sources):
        evaluated_jobs += evaluate_stored_jobs(
            connection,
            profile,
            source=source,
        )

    return AmazonImportSummary(
        jobs_parsed=len(jobs),
        new_jobs=new_jobs,
        new_postings=new_postings,
        existing_postings=existing_postings,
        analyzed_jobs=analyzed_jobs,
        evaluated_jobs=evaluated_jobs,
    )
