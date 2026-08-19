import argparse
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import (
    urljoin,
    urlsplit,
)

import requests
from bs4 import BeautifulSoup

from database import (
    connect_database,
    initialize_database,
)
from job_details import (
    JobDetails,
    ensure_job_details_table,
    get_fetch_status_counts,
    save_job_details,
)
from job_urls import (
    is_public_web_url,
    normalize_url,
)
from refresh_progress import write_refresh_progress


DEFAULT_TIMEOUT_SECONDS = 20
MAX_DESCRIPTION_LENGTH = 60000
MAX_REDIRECTS = 5

REDIRECT_STATUSES = {
    301,
    302,
    303,
    307,
    308,
}

HIREME_HOSTS = {
    "hiremetech.com",
    "www.hiremetech.com",
}

INTERMEDIARY_HOST_SUFFIXES = (
    "glassdoor.com",
    "indeed.com",
    "linkedin.com",
)

AUTOMATION_PROHIBITED_HOST_SUFFIXES = (
    "amazon.jobs",
    "jobs.amazon.co.uk",
    "linkedin.com",
)

HIREME_JOB_PATH = re.compile(
    r"^/jobs?/(\d+)/?$",
    flags=re.IGNORECASE,
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}


class UnsafeJobUrlError(ValueError):
    """Raised when a fetch target is not a public web URL."""


class JobSourceResolutionError(ValueError):
    """Raised when an intermediary cannot name an original page."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status


def get_public_response(
    session: requests.Session,
    url: str,
    *,
    accept: str | None = None,
) -> requests.Response:
    """GET one public URL while validating every redirect target."""

    current_url = normalize_url(url)

    for redirect_index in range(
        MAX_REDIRECTS + 1
    ):
        if not is_public_web_url(current_url):
            raise UnsafeJobUrlError(
                "Fetch target is not a public HTTP(S) URL."
            )

        headers = (
            {"Accept": accept}
            if accept
            else None
        )
        response = session.get(
            current_url,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            allow_redirects=False,
            headers=headers,
        )

        if response.status_code not in REDIRECT_STATUSES:
            return response

        location = response.headers.get("Location")

        if not location:
            raise requests.TooManyRedirects(
                "Redirect response did not include Location."
            )

        if redirect_index >= MAX_REDIRECTS:
            raise requests.TooManyRedirects(
                f"More than {MAX_REDIRECTS} redirects."
            )

        next_url = urljoin(
            response.url or current_url,
            location,
        )
        response.close()
        current_url = next_url

    raise requests.TooManyRedirects(
        f"More than {MAX_REDIRECTS} redirects."
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and store job-page descriptions."
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of jobs to fetch. Default: 10.",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between requests in seconds. Default: 1.",
    )

    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry jobs that previously failed.",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        help=(
            "Write aggregate fetch counts to this local JSON "
            "file. Job identities and URLs are never included."
        ),
    )

    return parser.parse_args()


def normalize_text(value: str | None) -> str:
    """Normalize extracted page text."""

    if not value:
        return ""

    value = value.replace("\u00a0", " ")
    value = value.replace("\r\n", "\n")
    value = value.replace("\r", "\n")

    lines: list[str] = []

    for raw_line in value.splitlines():
        line = re.sub(
            r"[ \t]+",
            " ",
            raw_line,
        ).strip()

        if not line:
            continue

        if lines and lines[-1] == line:
            continue

        lines.append(line)

    normalized = "\n".join(lines)

    return normalized[:MAX_DESCRIPTION_LENGTH]


def html_fragment_to_text(value: Any) -> str:
    """Convert a JSON-LD value or HTML fragment to plain text."""

    if value is None:
        return ""

    if isinstance(value, list):
        parts = [
            html_fragment_to_text(item)
            for item in value
        ]

        return normalize_text(
            "\n".join(
                part
                for part in parts
                if part
            )
        )

    if isinstance(value, dict):
        preferred_keys = [
            "name",
            "description",
            "value",
        ]

        parts: list[str] = []

        for key in preferred_keys:
            if key in value:
                parsed = html_fragment_to_text(
                    value[key]
                )

                if parsed:
                    parts.append(parsed)

        if parts:
            return normalize_text("\n".join(parts))

        return ""

    soup = BeautifulSoup(
        str(value),
        "html.parser",
    )

    return normalize_text(
        soup.get_text("\n")
    )


def is_job_posting(node: dict[str, Any]) -> bool:
    """Check whether a JSON-LD object represents a JobPosting."""

    type_value = node.get("@type")

    if isinstance(type_value, str):
        return type_value.casefold() == "jobposting"

    if isinstance(type_value, list):
        return any(
            str(item).casefold() == "jobposting"
            for item in type_value
        )

    return False


def find_job_postings(
    value: Any,
) -> list[dict[str, Any]]:
    """Recursively find JobPosting objects in JSON-LD."""

    results: list[dict[str, Any]] = []

    if isinstance(value, dict):
        if is_job_posting(value):
            results.append(value)

        for child_value in value.values():
            results.extend(
                find_job_postings(child_value)
            )

    elif isinstance(value, list):
        for item in value:
            results.extend(
                find_job_postings(item)
            )

    return results


def parse_json_ld_values(
    soup: BeautifulSoup,
) -> list[Any]:
    """Read valid JSON-LD values from the page."""

    values: list[Any] = []

    scripts = soup.find_all(
        "script",
        attrs={"type": "application/ld+json"},
    )

    for script in scripts:
        raw_json = script.string or script.get_text()

        if not raw_json or not raw_json.strip():
            continue

        try:
            parsed_json = json.loads(
                raw_json.strip()
            )
        except json.JSONDecodeError:
            continue

        values.append(parsed_json)

    return values


def parse_json_ld_scripts(
    soup: BeautifulSoup,
) -> list[dict[str, Any]]:
    """Read valid JSON-LD JobPosting objects from the page."""

    job_postings: list[dict[str, Any]] = []

    for parsed_json in parse_json_ld_values(
        soup
    ):
        job_postings.extend(
            find_job_postings(parsed_json)
        )

    return job_postings


def iter_json_objects(
    value: Any,
) -> Iterator[dict[str, Any]]:
    """Yield all nested JSON objects."""

    if isinstance(value, dict):
        yield value

        for child_value in value.values():
            yield from iter_json_objects(
                child_value
            )

    elif isinstance(value, list):
        for item in value:
            yield from iter_json_objects(item)


def schema_type_matches(
    node: dict[str, Any],
    expected_type: str,
) -> bool:
    """Return whether one schema.org node has a given type."""

    type_value = node.get("@type")

    if isinstance(type_value, str):
        return (
            type_value.casefold()
            == expected_type.casefold()
        )

    if isinstance(type_value, list):
        return any(
            str(item).casefold()
            == expected_type.casefold()
            for item in type_value
        )

    return False


def extract_location_value(
    value: Any,
) -> str:
    """Convert common JobPosting location shapes to text."""

    if isinstance(value, list):
        return normalize_text(
            ", ".join(
                location
                for location in (
                    extract_location_value(item)
                    for item in value
                )
                if location
            )
        )

    if isinstance(value, str):
        return normalize_text(value)

    if not isinstance(value, dict):
        return ""

    address = value.get("address")

    if isinstance(address, dict):
        parts = [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]

        return normalize_text(
            ", ".join(
                str(part)
                for part in parts
                if part
            )
        )

    for key in (
        "name",
        "addressLocality",
        "addressRegion",
        "addressCountry",
    ):
        if value.get(key):
            return normalize_text(
                str(value[key])
            )

    return ""


def extract_original_page_metadata(
    soup: BeautifulSoup,
    page_url: str,
) -> tuple[str | None, str | None]:
    """Extract company and location from an original job page."""

    values = parse_json_ld_values(soup)
    objects = [
        node
        for value in values
        for node in iter_json_objects(value)
    ]
    company: str | None = None
    location: str | None = None

    for posting in (
        node
        for node in objects
        if schema_type_matches(
            node,
            "JobPosting",
        )
    ):
        organization = posting.get(
            "hiringOrganization"
        )

        if isinstance(organization, dict):
            company_value = normalize_text(
                str(
                    organization.get("name")
                    or ""
                )
            )

            if company_value:
                company = company_value

        elif isinstance(organization, str):
            company_value = normalize_text(
                organization
            )

            if company_value:
                company = company_value

        location_value = extract_location_value(
            posting.get("jobLocation")
            or posting.get(
                "applicantLocationRequirements"
            )
        )

        if location_value:
            location = location_value

        job_location_type = normalize_text(
            str(
                posting.get("jobLocationType")
                or ""
            )
        ).casefold()

        if (
            not location
            and "telecommute"
            in job_location_type
        ):
            location = "Remote"

        if company and location:
            break

    page_hostname = (
        urlsplit(page_url).hostname
        or ""
    ).casefold()

    if not company and page_hostname:
        for organization in (
            node
            for node in objects
            if schema_type_matches(
                node,
                "Organization",
            )
        ):
            organization_url = normalize_url(
                str(
                    organization.get("url")
                    or ""
                )
            )
            organization_hostname = (
                urlsplit(
                    organization_url
                ).hostname
                or ""
            ).casefold()

            if (
                organization_hostname
                != page_hostname
            ):
                continue

            company_value = normalize_text(
                str(
                    organization.get("name")
                    or ""
                )
            )

            if company_value:
                company = company_value
                break

    if not location:
        for selector in (
            "[itemprop='jobLocation']",
            "[class*='job-location']",
            "[class*='job_location']",
            "main .location",
            "article .location",
        ):
            try:
                element = soup.select_one(
                    selector
                )
            except Exception:
                continue

            if not element:
                continue

            location_value = normalize_text(
                element.get_text(" ")
            )

            if (
                location_value
                and len(location_value) <= 200
            ):
                location = location_value
                break

    return company, location


def extract_from_json_ld(
    soup: BeautifulSoup,
) -> tuple[str | None, str | None]:
    """
    Extract title and description from structured JobPosting data.
    """

    job_postings = parse_json_ld_scripts(soup)

    if not job_postings:
        return None, None

    candidates: list[tuple[str, str]] = []

    for posting in job_postings:
        title = html_fragment_to_text(
            posting.get("title")
            or posting.get("name")
        )

        fields = [
            posting.get("description"),
            posting.get("responsibilities"),
            posting.get("qualifications"),
            posting.get("skills"),
            posting.get("experienceRequirements"),
            posting.get("educationRequirements"),
        ]

        description_parts = [
            html_fragment_to_text(field)
            for field in fields
        ]

        description = normalize_text(
            "\n\n".join(
                part
                for part in description_parts
                if part
            )
        )

        candidates.append(
            (title, description)
        )

    best_title, best_description = max(
        candidates,
        key=lambda item: len(item[1]),
    )

    return (
        best_title or None,
        best_description or None,
    )


def extract_page_title(
    soup: BeautifulSoup,
) -> str | None:
    """Extract a readable title from the HTML document."""

    if soup.title and soup.title.string:
        title = normalize_text(
            soup.title.string
        )

        if title:
            return title

    heading = soup.find("h1")

    if heading:
        title = normalize_text(
            heading.get_text(" ")
        )

        if title:
            return title

    return None


def clean_page_for_text(
    soup: BeautifulSoup,
) -> None:
    """Remove HTML elements that are not part of the job description."""

    for tag_name in [
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "iframe",
    ]:
        for tag in soup.find_all(tag_name):
            tag.decompose()


def extract_from_html(
    soup: BeautifulSoup,
) -> str | None:
    """Fallback extraction for pages without JobPosting JSON-LD."""

    selectors = [
        "[itemtype*='JobPosting']",
        "[class*='job-description']",
        "[class*='job_description']",
        "[id*='job-description']",
        "[id*='job_description']",
        ".job-details",
        ".job__description",
        ".description",
        "#content",
        "main",
        "article",
    ]

    candidates: list[str] = []

    for selector in selectors:
        try:
            elements = soup.select(selector)
        except Exception:
            continue

        for element in elements:
            text = normalize_text(
                element.get_text("\n")
            )

            if len(text) >= 200:
                candidates.append(text)

    if candidates:
        return max(
            candidates,
            key=len,
        )[:MAX_DESCRIPTION_LENGTH]

    clean_page_for_text(soup)

    body = soup.body

    if not body:
        return None

    body_text = normalize_text(
        body.get_text("\n")
    )

    if len(body_text) < 200:
        return None

    return body_text[:MAX_DESCRIPTION_LENGTH]


def extract_hireme_job_id(
    job_url: str,
) -> str | None:
    """Return the numeric HireMe ID for an exact listing URL."""

    normalized = normalize_url(job_url)

    if not normalized:
        return None

    parts = urlsplit(normalized)
    hostname = (
        parts.hostname or ""
    ).casefold()

    if hostname not in HIREME_HOSTS:
        return None

    match = HIREME_JOB_PATH.fullmatch(
        parts.path
    )

    if not match:
        return None

    return match.group(1)


def is_intermediary_host(
    hostname: str,
) -> bool:
    """Return whether a host is another job-listing intermediary."""

    normalized = hostname.rstrip(".").casefold()

    return (
        normalized in HIREME_HOSTS
        or any(
            normalized == suffix
            or normalized.endswith(
                f".{suffix}"
            )
            for suffix in INTERMEDIARY_HOST_SUFFIXES
        )
    )


def is_automation_prohibited_url(job_url: str) -> bool:
    """Return whether Job Radar must not fetch this source page."""

    normalized = normalize_url(job_url)

    if not normalized:
        return False

    hostname = (
        urlsplit(normalized).hostname or ""
    ).rstrip(".").casefold()

    return any(
        hostname == suffix
        or hostname.endswith(f".{suffix}")
        for suffix in AUTOMATION_PROHIBITED_HOST_SUFFIXES
    )


def resolve_original_job_url(
    session: requests.Session,
    job_url: str,
) -> tuple[str, bool]:
    """Resolve supported intermediary URLs to employer/ATS pages."""

    hireme_job_id = extract_hireme_job_id(
        job_url
    )

    if hireme_job_id is None:
        return job_url, False

    api_url = (
        "https://hiremetech.com/api/jobs/"
        f"{hireme_job_id}"
    )

    try:
        response = get_public_response(
            session,
            api_url,
            accept="application/json",
        )
    except (
        requests.RequestException,
        UnsafeJobUrlError,
    ) as error:
        raise JobSourceResolutionError(
            "Could not resolve the original employer page: "
            f"{error}"
        ) from error

    if not response.ok:
        raise JobSourceResolutionError(
            "HireMe resolver returned "
            f"HTTP {response.status_code}.",
            http_status=response.status_code,
        )

    try:
        payload = response.json()
    except (
        requests.JSONDecodeError,
        ValueError,
    ) as error:
        raise JobSourceResolutionError(
            "HireMe resolver returned invalid JSON.",
            http_status=response.status_code,
        ) from error

    if not isinstance(payload, dict):
        raise JobSourceResolutionError(
            "HireMe resolver returned an invalid job record.",
            http_status=response.status_code,
        )

    job_data = payload.get("job")

    if not isinstance(job_data, dict):
        raise JobSourceResolutionError(
            "HireMe resolver did not return a job record.",
            http_status=response.status_code,
        )

    original_url = normalize_url(
        str(
            job_data.get("job_url")
            or ""
        )
    )

    if not original_url:
        raise JobSourceResolutionError(
            "HireMe did not provide an original employer URL.",
            http_status=response.status_code,
        )

    original_hostname = (
        urlsplit(original_url).hostname
        or ""
    )

    if is_intermediary_host(
        original_hostname
    ):
        raise JobSourceResolutionError(
            "HireMe points to another listing intermediary "
            "instead of an employer or ATS page.",
            http_status=response.status_code,
        )

    return original_url, True


def fetch_job_page(
    session: requests.Session,
    job_id: int,
    job_url: str,
) -> JobDetails:
    """Fetch and extract one employer or ATS job page."""

    try:
        response = get_public_response(
            session,
            job_url,
        )

    except UnsafeJobUrlError as error:
        return JobDetails(
            job_id=job_id,
            final_url=None,
            page_title=None,
            description_text=None,
            extractor=None,
            fetch_status="unsafe_url",
            fetch_error=str(error),
            http_status=None,
        )

    except requests.RequestException as error:
        return JobDetails(
            job_id=job_id,
            final_url=None,
            page_title=None,
            description_text=None,
            extractor=None,
            fetch_status="request_error",
            fetch_error=str(error),
            http_status=None,
        )

    final_url = response.url
    http_status = response.status_code

    if http_status in {401, 403, 429}:
        return JobDetails(
            job_id=job_id,
            final_url=final_url,
            page_title=None,
            description_text=None,
            extractor=None,
            fetch_status="blocked",
            fetch_error=(
                f"Website returned HTTP {http_status}"
            ),
            http_status=http_status,
        )

    if not response.ok:
        return JobDetails(
            job_id=job_id,
            final_url=final_url,
            page_title=None,
            description_text=None,
            extractor=None,
            fetch_status="http_error",
            fetch_error=(
                f"Website returned HTTP {http_status}"
            ),
            http_status=http_status,
        )

    content_type = response.headers.get(
        "Content-Type",
        "",
    ).casefold()

    if (
        "html" not in content_type
        and "text" not in content_type
    ):
        return JobDetails(
            job_id=job_id,
            final_url=final_url,
            page_title=None,
            description_text=None,
            extractor=None,
            fetch_status="unsupported_content",
            fetch_error=(
                f"Unsupported Content-Type: {content_type}"
            ),
            http_status=http_status,
        )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    page_title = extract_page_title(soup)
    (
        resolved_company,
        resolved_location,
    ) = extract_original_page_metadata(
        soup,
        final_url,
    )

    structured_title, description = (
        extract_from_json_ld(soup)
    )

    extractor = "json-ld"

    if structured_title:
        page_title = structured_title

    if not description or len(description) < 200:
        description = extract_from_html(soup)
        extractor = "html-fallback"

    if not description or len(description) < 200:
        return JobDetails(
            job_id=job_id,
            final_url=final_url,
            page_title=page_title,
            description_text=description,
            extractor=extractor,
            fetch_status="insufficient_content",
            fetch_error=(
                "Could not find enough job-description text."
            ),
            http_status=http_status,
            resolved_company=resolved_company,
            resolved_location=resolved_location,
        )

    return JobDetails(
        job_id=job_id,
        final_url=final_url,
        page_title=page_title,
        description_text=description,
        extractor=extractor,
        fetch_status="success",
        fetch_error=None,
        http_status=http_status,
        resolved_company=resolved_company,
        resolved_location=resolved_location,
    )


def fetch_job(
    session: requests.Session,
    job_id: int,
    job_url: str,
) -> JobDetails:
    """Resolve intermediaries, then fetch the original job page."""

    if is_automation_prohibited_url(job_url):
        return JobDetails(
            job_id=job_id,
            final_url=None,
            page_title=None,
            description_text=None,
            extractor=None,
            fetch_status="source_automation_prohibited",
            fetch_error=(
                "Automated fetching is disabled for this source."
            ),
            http_status=None,
        )

    try:
        target_url, was_resolved = (
            resolve_original_job_url(
                session,
                job_url,
            )
        )
    except JobSourceResolutionError as error:
        return JobDetails(
            job_id=job_id,
            final_url=None,
            page_title=None,
            description_text=None,
            extractor="source-resolver",
            fetch_status="source_resolution_error",
            fetch_error=str(error),
            http_status=error.http_status,
        )

    result = fetch_job_page(
        session=session,
        job_id=job_id,
        job_url=target_url,
    )

    if was_resolved:
        result.extractor = (
            "original-source/"
            f"{result.extractor}"
            if result.extractor
            else "original-source"
        )

    return result


def fill_missing_job_metadata(
    connection: sqlite3.Connection,
    result: JobDetails,
) -> None:
    """Fill blank company/location from the original job page."""

    if (
        not result.resolved_company
        and not result.resolved_location
    ):
        return

    connection.execute(
        """
        UPDATE jobs
        SET
            company = CASE
                WHEN
                    company IS NULL
                    OR TRIM(company) = ''
                THEN ?
                ELSE company
            END,
            location = CASE
                WHEN
                    location IS NULL
                    OR TRIM(location) = ''
                THEN ?
                ELSE location
            END
        WHERE id = ?
        """,
        (
            result.resolved_company,
            result.resolved_location,
            result.job_id,
        ),
    )


def print_result(
    title: str,
    company: str,
    result: JobDetails,
) -> None:
    """Print a concise fetch result."""

    description_length = len(
        result.description_text or ""
    )

    print(
        f"[{result.fetch_status.upper()}] "
        f"Job #{result.job_id}: "
        f"{title} @ {company}"
    )

    print(
        f"  HTTP: {result.http_status} | "
        f"Extractor: {result.extractor} | "
        f"Characters: {description_length}"
    )

    if result.fetch_error:
        print(f"  Error: {result.fetch_error}")


def main() -> None:
    arguments = parse_arguments()

    if arguments.limit < 1:
        raise RuntimeError(
            "--limit must be at least 1."
        )

    if arguments.delay < 0:
        raise RuntimeError(
            "--delay cannot be negative."
        )

    connection = connect_database()
    initialize_database(connection)
    ensure_job_details_table(connection)

    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    try:
        if arguments.retry_failed:
            status_condition = """
                job_details.job_id IS NULL
                OR job_details.fetch_status != 'success'
            """
        else:
            status_condition = """
                job_details.job_id IS NULL
            """

        jobs = connection.execute(
            f"""
            SELECT
                jobs.id,
                jobs.title,
                jobs.company,
                jobs.job_url
            FROM jobs

            LEFT JOIN job_details
                ON job_details.job_id = jobs.id

            WHERE
                jobs.job_url IS NOT NULL
                AND jobs.job_url != ''
                AND COALESCE(
                    job_details.fetch_status,
                    ''
                ) != 'source_automation_prohibited'
                AND ({status_condition})

            ORDER BY
                jobs.posted_on DESC,
                jobs.id DESC

            LIMIT ?
            """,
            (arguments.limit,),
        ).fetchall()
        total_jobs = len(jobs)
        write_refresh_progress(
            arguments.progress_file,
            stage_key="job_page_enrichment",
            progress_mode="determinate",
            progress_completed=0,
            progress_total=total_jobs,
            progress_unit="jobs",
        )

        if not jobs:
            print(
                "No jobs currently require description fetching."
            )
            return

        print(
            f"Fetching details for {len(jobs)} jobs..."
        )
        print()

        for index, job in enumerate(
            jobs,
            start=1,
        ):
            result = fetch_job(
                session=session,
                job_id=int(job["id"]),
                job_url=job["job_url"],
            )

            with connection:
                save_job_details(
                    connection,
                    result,
                )
                fill_missing_job_metadata(
                    connection,
                    result,
                )

            write_refresh_progress(
                arguments.progress_file,
                stage_key="job_page_enrichment",
                progress_mode="determinate",
                progress_completed=index,
                progress_total=total_jobs,
                progress_unit="jobs",
            )

            print_result(
                title=job["title"] or "Unknown title",
                company=job["company"] or "Unknown company",
                result=result,
            )

            if (
                index < len(jobs)
                and arguments.delay > 0
            ):
                time.sleep(arguments.delay)

        print()
        print("=" * 90)
        print("FETCH STATUS SUMMARY")
        print("=" * 90)

        status_rows = get_fetch_status_counts(
            connection
        )

        for row in status_rows:
            print(
                f"{row['fetch_status']}: "
                f"{row['amount']}"
            )

    finally:
        session.close()
        connection.close()


if __name__ == "__main__":
    main()
