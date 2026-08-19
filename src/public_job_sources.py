import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import requests
from bs4 import BeautifulSoup

from fetch_job_details import get_public_response
from job_urls import normalize_url
from source_parsing import NormalizedJob


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_REGISTRY_PATH = (
    PROJECT_ROOT / "config" / "job_sources.local.json"
)
EXAMPLE_REGISTRY_PATH = (
    PROJECT_ROOT / "config" / "job_sources.example.json"
)

SUPPORTED_ADAPTERS = frozenset(
    {
        "ashby",
        "greenhouse",
        "lever",
        "microsoft_careers",
        "nvidia_workday",
        "smartrecruiters",
        "workable",
    }
)

FIXED_ADAPTER_IDENTIFIERS = {
    "microsoft_careers": frozenset({"israel"}),
    "nvidia_workday": frozenset(
        {"NVIDIAExternalCareerSite"}
    ),
}

SOURCE_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)

DEFAULT_TIMEOUT_SECONDS = 20
MAX_FEED_BYTES = 12 * 1024 * 1024
MAX_FEED_PAGES = 20
MAX_OFFICIAL_DETAIL_PAGES = 50
OFFICIAL_DETAIL_DELAY_SECONDS = 0.5

REQUEST_HEADERS = {
    "User-Agent": (
        "JobRadar/1.0 "
        "(local read-only public job feed collector)"
    ),
    "Accept": "application/json",
}


@dataclass(frozen=True, slots=True)
class PublicSource:
    id: str
    company: str
    adapter: str
    identifier: str
    enabled: bool = True
    region: str = "global"


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    country_codes: frozenset[str]
    location_terms: tuple[str, ...]
    sources: tuple[PublicSource, ...]


@dataclass(frozen=True, slots=True)
class PublicPosting:
    source_job_id: str
    title: str
    location: str | None
    posted_on: str | None
    job_url: str
    description_text: str
    country_codes: tuple[str, ...] = ()
    location_search_text: str = ""


@dataclass(frozen=True, slots=True)
class SourceCollection:
    postings_seen: int
    relevant_jobs: tuple[NormalizedJob, ...]


@dataclass(frozen=True, slots=True)
class CollectionStatus:
    source_id: str
    company: str
    adapter: str
    enabled: bool
    status: str
    last_collection_at: str | None
    postings_seen: int
    postings_relevant: int
    new_jobs: int
    new_postings: int
    error: str | None


def _required_string(
    value: Any,
    *,
    field_name: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field_name} must be a non-empty string."
        )

    return value.strip()


def parse_source_registry(
    payload: Any,
) -> SourceRegistry:
    """Validate source-registry JSON without touching matching rules."""

    if not isinstance(payload, Mapping):
        raise ValueError(
            "Job source registry must be a JSON object."
        )

    if payload.get("version") != 1:
        raise ValueError(
            "Job source registry version must be 1."
        )

    location_policy = payload.get("location_policy")

    if not isinstance(location_policy, Mapping):
        raise ValueError(
            "location_policy must be a JSON object."
        )

    raw_country_codes = location_policy.get(
        "country_codes",
        [],
    )
    raw_location_terms = location_policy.get(
        "terms",
        [],
    )

    if not isinstance(raw_country_codes, list):
        raise ValueError(
            "location_policy.country_codes must be a list."
        )

    if not isinstance(raw_location_terms, list):
        raise ValueError(
            "location_policy.terms must be a list."
        )

    country_codes = frozenset(
        _required_string(
            item,
            field_name=(
                "location_policy.country_codes item"
            ),
        ).upper()
        for item in raw_country_codes
    )

    location_terms = tuple(
        _required_string(
            item,
            field_name="location_policy.terms item",
        ).casefold()
        for item in raw_location_terms
    )

    if not country_codes and not location_terms:
        raise ValueError(
            "Location policy must contain a country code "
            "or location term."
        )

    raw_sources = payload.get("sources")

    if not isinstance(raw_sources, list):
        raise ValueError(
            "sources must be a JSON list."
        )

    sources: list[PublicSource] = []
    source_ids: set[str] = set()

    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, Mapping):
            raise ValueError(
                f"sources[{index}] must be a JSON object."
            )

        source_id = _required_string(
            raw_source.get("id"),
            field_name=f"sources[{index}].id",
        )
        source_key = source_id.casefold()

        if source_key in source_ids:
            raise ValueError(
                f"Duplicate source id: {source_id!r}."
            )

        source_ids.add(source_key)

        adapter = _required_string(
            raw_source.get("adapter"),
            field_name=f"sources[{index}].adapter",
        ).casefold()

        if adapter not in SUPPORTED_ADAPTERS:
            raise ValueError(
                f"Unsupported adapter: {adapter!r}."
            )

        identifier = _required_string(
            raw_source.get("identifier"),
            field_name=f"sources[{index}].identifier",
        )

        if not SOURCE_IDENTIFIER.fullmatch(identifier):
            raise ValueError(
                "Source identifiers may contain only letters, "
                "numbers, dots, underscores, and hyphens."
            )

        fixed_identifiers = FIXED_ADAPTER_IDENTIFIERS.get(
            adapter
        )

        if (
            fixed_identifiers is not None
            and identifier not in fixed_identifiers
        ):
            raise ValueError(
                f"{adapter} identifier must be one of the "
                "fixed official source identifiers."
            )

        enabled = raw_source.get("enabled", True)

        if not isinstance(enabled, bool):
            raise ValueError(
                f"sources[{index}].enabled must be boolean."
            )

        region = str(
            raw_source.get("region", "global")
        ).strip().casefold()

        if adapter == "lever":
            if region not in {"global", "eu"}:
                raise ValueError(
                    "Lever region must be 'global' or 'eu'."
                )
        else:
            region = "global"

        sources.append(
            PublicSource(
                id=source_id,
                company=_required_string(
                    raw_source.get("company"),
                    field_name=(
                        f"sources[{index}].company"
                    ),
                ),
                adapter=adapter,
                identifier=identifier,
                enabled=enabled,
                region=region,
            )
        )

    return SourceRegistry(
        country_codes=country_codes,
        location_terms=location_terms,
        sources=tuple(sources),
    )


def load_source_registry(
    path: Path | None = None,
) -> SourceRegistry:
    if path is None:
        path = (
            LOCAL_REGISTRY_PATH
            if LOCAL_REGISTRY_PATH.exists()
            else EXAMPLE_REGISTRY_PATH
        )

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )
    except FileNotFoundError as error:
        raise ValueError(
            f"Job source registry is missing: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Job source registry is invalid JSON: {error}"
        ) from error

    return parse_source_registry(payload)


def _plain_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value)

    if "<" in text and ">" in text:
        text = BeautifulSoup(
            text,
            "html.parser",
        ).get_text(" ", strip=True)

    return " ".join(text.split())


def _date_part(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()

    if len(text) < 10:
        return None

    candidate = text[:10]

    try:
        datetime.strptime(
            candidate,
            "%Y-%m-%d",
        )
    except ValueError:
        return None

    return candidate


def _lever_date(value: Any) -> str | None:
    try:
        timestamp = float(value) / 1000
        parsed = datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        )
    except (
        OSError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        return None

    return parsed.date().isoformat()


def _normalized_job_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    return normalize_url(value)


def parse_greenhouse_jobs(
    payload: Any,
) -> tuple[PublicPosting, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError(
            "Greenhouse feed must be a JSON object."
        )

    raw_jobs = payload.get("jobs")

    if not isinstance(raw_jobs, list):
        raise ValueError(
            "Greenhouse feed has no jobs list."
        )

    jobs: list[PublicPosting] = []

    for item in raw_jobs:
        if not isinstance(item, Mapping):
            continue

        job_url = _normalized_job_url(
            item.get("absolute_url")
        )
        title = _plain_text(item.get("title"))
        source_job_id = str(
            item.get("id", "")
        ).strip()
        location_value = item.get("location")
        location = (
            _plain_text(location_value.get("name"))
            if isinstance(location_value, Mapping)
            else ""
        )

        if not job_url or not title or not source_job_id:
            continue

        jobs.append(
            PublicPosting(
                source_job_id=source_job_id,
                title=title,
                location=location or None,
                posted_on=(
                    _date_part(
                        item.get("first_published")
                    )
                    or _date_part(
                        item.get("updated_at")
                    )
                ),
                job_url=job_url,
                description_text=_plain_text(
                    item.get("content")
                ),
                location_search_text=location,
            )
        )

    return tuple(jobs)


def parse_lever_jobs(
    payload: Any,
) -> tuple[PublicPosting, ...]:
    if not isinstance(payload, list):
        raise ValueError(
            "Lever feed must be a JSON list."
        )

    jobs: list[PublicPosting] = []

    for item in payload:
        if not isinstance(item, Mapping):
            continue

        categories = item.get("categories")
        categories = (
            categories
            if isinstance(categories, Mapping)
            else {}
        )
        all_locations = categories.get(
            "allLocations",
            [],
        )

        if not isinstance(all_locations, list):
            all_locations = []

        location = _plain_text(
            categories.get("location")
        )
        search_locations = [
            location,
            *(
                _plain_text(value)
                for value in all_locations
            ),
        ]
        job_url = _normalized_job_url(
            item.get("hostedUrl")
        )
        title = _plain_text(item.get("text"))
        source_job_id = str(
            item.get("id", "")
        ).strip()

        if not job_url or not title or not source_job_id:
            continue

        description = (
            _plain_text(
                item.get("descriptionPlain")
            )
            or _plain_text(
                item.get("description")
            )
        )

        jobs.append(
            PublicPosting(
                source_job_id=source_job_id,
                title=title,
                location=location or None,
                posted_on=_lever_date(
                    item.get("createdAt")
                ),
                job_url=job_url,
                description_text=description,
                country_codes=(
                    str(item.get("country")).upper(),
                )
                if item.get("country")
                else (),
                location_search_text=" ".join(
                    search_locations
                ),
            )
        )

    return tuple(jobs)


def parse_ashby_jobs(
    payload: Any,
) -> tuple[PublicPosting, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError(
            "Ashby feed must be a JSON object."
        )

    raw_jobs = payload.get("jobs")

    if not isinstance(raw_jobs, list):
        raise ValueError(
            "Ashby feed has no jobs list."
        )

    jobs: list[PublicPosting] = []

    for item in raw_jobs:
        if (
            not isinstance(item, Mapping)
            or item.get("isListed") is False
        ):
            continue

        job_url = _normalized_job_url(
            item.get("jobUrl")
        )
        title = _plain_text(item.get("title"))
        source_job_id = str(
            item.get("id", "")
        ).strip()
        location = _plain_text(
            item.get("location")
        )
        address = item.get("address")
        postal_address: Mapping[str, Any] = {}

        if isinstance(address, Mapping):
            raw_postal = address.get(
                "postalAddress"
            )

            if isinstance(raw_postal, Mapping):
                postal_address = raw_postal

        secondary_locations = item.get(
            "secondaryLocations",
            [],
        )

        if not isinstance(secondary_locations, list):
            secondary_locations = []

        secondary_text = " ".join(
            _plain_text(entry.get("location"))
            for entry in secondary_locations
            if isinstance(entry, Mapping)
        )
        country = _plain_text(
            postal_address.get("addressCountry")
        )

        if not job_url or not title or not source_job_id:
            continue

        jobs.append(
            PublicPosting(
                source_job_id=source_job_id,
                title=title,
                location=location or None,
                posted_on=_date_part(
                    item.get("publishedAt")
                ),
                job_url=job_url,
                description_text=(
                    _plain_text(
                        item.get("descriptionPlain")
                    )
                    or _plain_text(
                        item.get("descriptionHtml")
                    )
                ),
                country_codes=("IL",)
                if country.casefold() == "israel"
                else (),
                location_search_text=" ".join(
                    (
                        location,
                        secondary_text,
                        country,
                        _plain_text(
                            postal_address.get(
                                "addressLocality"
                            )
                        ),
                    )
                ),
            )
        )

    return tuple(jobs)


def parse_smartrecruiters_jobs(
    payloads: list[Any],
    *,
    company_identifier: str,
) -> tuple[PublicPosting, ...]:
    jobs: list[PublicPosting] = []

    for payload in payloads:
        if not isinstance(payload, Mapping):
            raise ValueError(
                "SmartRecruiters feed must be a JSON object."
            )

        raw_jobs = payload.get("content")

        if not isinstance(raw_jobs, list):
            raise ValueError(
                "SmartRecruiters feed has no content list."
            )

        for item in raw_jobs:
            if not isinstance(item, Mapping):
                continue

            source_job_id = str(
                item.get("id")
                or item.get("uuid")
                or ""
            ).strip()
            title = _plain_text(item.get("name"))
            location_value = item.get("location")
            location_value = (
                location_value
                if isinstance(
                    location_value,
                    Mapping,
                )
                else {}
            )
            location = (
                _plain_text(
                    location_value.get("fullLocation")
                )
                or ", ".join(
                    part
                    for part in (
                        _plain_text(
                            location_value.get("city")
                        ),
                        _plain_text(
                            location_value.get("region")
                        ),
                        _plain_text(
                            location_value.get("country")
                        ),
                    )
                    if part
                )
            )

            if not source_job_id or not title:
                continue

            public_url = normalize_url(
                "https://jobs.smartrecruiters.com/"
                + quote(
                    company_identifier,
                    safe="",
                )
                + "/"
                + quote(source_job_id, safe="")
            )

            jobs.append(
                PublicPosting(
                    source_job_id=source_job_id,
                    title=title,
                    location=location or None,
                    posted_on=_date_part(
                        item.get("releasedDate")
                    ),
                    job_url=public_url,
                    description_text=title,
                    country_codes=(
                        str(
                            location_value.get(
                                "country"
                            )
                        ).upper(),
                    )
                    if location_value.get("country")
                    else (),
                    location_search_text=location,
                )
            )

    return tuple(jobs)


def parse_workable_jobs(
    payload: Any,
) -> tuple[PublicPosting, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError(
            "Workable feed must be a JSON object."
        )

    raw_jobs = payload.get("jobs")

    if not isinstance(raw_jobs, list):
        raise ValueError(
            "Workable feed has no jobs list."
        )

    jobs: list[PublicPosting] = []

    for item in raw_jobs:
        if not isinstance(item, Mapping):
            continue

        title = _plain_text(item.get("title"))
        source_job_id = str(
            item.get("shortcode")
            or item.get("code")
            or ""
        ).strip()
        job_url = _normalized_job_url(
            item.get("shortlink")
            or item.get("url")
        )
        location = ", ".join(
            part
            for part in (
                _plain_text(item.get("city")),
                _plain_text(item.get("state")),
                _plain_text(item.get("country")),
            )
            if part
        )
        raw_locations = item.get("locations", [])

        if not isinstance(raw_locations, list):
            raw_locations = []

        location_search = " ".join(
            (
                location,
                *(
                    " ".join(
                        _plain_text(entry.get(key))
                        for key in (
                            "city",
                            "state",
                            "country_name",
                            "country_code",
                        )
                    )
                    for entry in raw_locations
                    if isinstance(entry, Mapping)
                ),
            )
        )

        if not job_url or not title or not source_job_id:
            continue

        country = _plain_text(item.get("country"))
        country_codes = tuple(
            {
                str(entry.get("country_code")).upper()
                for entry in raw_locations
                if (
                    isinstance(entry, Mapping)
                    and entry.get("country_code")
                )
            }
        )

        if country.casefold() == "israel":
            country_codes = tuple(
                {
                    *country_codes,
                    "IL",
                }
            )

        jobs.append(
            PublicPosting(
                source_job_id=source_job_id,
                title=title,
                location=location or None,
                posted_on=(
                    _date_part(
                        item.get("published_on")
                    )
                    or _date_part(
                        item.get("created_at")
                    )
                ),
                job_url=job_url,
                description_text=(
                    _plain_text(
                        item.get("description")
                    )
                    or title
                ),
                country_codes=country_codes,
                location_search_text=location_search,
            )
        )

    return tuple(jobs)


def _expected_https_url(
    value: Any,
    *,
    hostname: str,
    path_prefix: str,
) -> str:
    if not isinstance(value, str):
        return ""

    normalized = normalize_url(value)
    parts = urlsplit(normalized)

    if (
        parts.scheme != "https"
        or parts.hostname != hostname
        or parts.port is not None
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith(path_prefix)
    ):
        return ""

    return normalized


def parse_nvidia_sitemap(
    payload: str,
) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ValueError(
            "NVIDIA sitemap is invalid XML."
        ) from error

    namespace = (
        "{http://www.sitemaps.org/schemas/"
        "sitemap/0.9}"
    )
    urls: list[str] = []

    for element in root.findall(
        f"{namespace}url/{namespace}loc"
    ):
        job_url = _expected_https_url(
            element.text,
            hostname=(
                "nvidia.wd5.myworkdayjobs.com"
            ),
            path_prefix=(
                "/NVIDIAExternalCareerSite/job/"
            ),
        )

        if job_url:
            urls.append(job_url)

    if not urls:
        raise ValueError(
            "NVIDIA sitemap contains no public job URLs."
        )

    return tuple(dict.fromkeys(urls))


def parse_nvidia_job_page(
    payload: str,
    *,
    job_url: str,
) -> PublicPosting:
    expected_url = _expected_https_url(
        job_url,
        hostname="nvidia.wd5.myworkdayjobs.com",
        path_prefix="/NVIDIAExternalCareerSite/job/",
    )

    if not expected_url:
        raise ValueError(
            "NVIDIA job URL is outside the official site."
        )

    soup = BeautifulSoup(payload, "html.parser")
    structured_data: Mapping[str, Any] | None = None

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"},
    ):
        try:
            candidate = json.loads(
                script.string or script.get_text()
            )
        except (json.JSONDecodeError, TypeError):
            continue

        if (
            isinstance(candidate, Mapping)
            and candidate.get("@type") == "JobPosting"
        ):
            structured_data = candidate
            break

    if structured_data is None:
        raise ValueError(
            "NVIDIA job page has no JobPosting data."
        )

    identifier = structured_data.get("identifier")
    source_job_id = (
        str(identifier.get("value", "")).strip()
        if isinstance(identifier, Mapping)
        else str(identifier or "").strip()
    )
    title = _plain_text(structured_data.get("title"))
    raw_locations = structured_data.get(
        "jobLocation",
        [],
    )

    if isinstance(raw_locations, Mapping):
        raw_locations = [raw_locations]
    elif not isinstance(raw_locations, list):
        raw_locations = []

    locations: list[str] = []
    country_codes: set[str] = set()

    for entry in raw_locations:
        if not isinstance(entry, Mapping):
            continue

        address = entry.get("address")

        if not isinstance(address, Mapping):
            continue

        country = _plain_text(
            address.get("addressCountry")
        )
        location = ", ".join(
            part
            for part in (
                _plain_text(
                    address.get("addressLocality")
                ),
                _plain_text(
                    address.get("addressRegion")
                ),
                country,
            )
            if part
        )

        if location:
            locations.append(location)

        if country.casefold() in {
            "il",
            "israel",
            "isr",
        }:
            country_codes.add("IL")

    location_text = "; ".join(
        dict.fromkeys(locations)
    )

    if not source_job_id or not title or not location_text:
        raise ValueError(
            "NVIDIA JobPosting data is incomplete."
        )

    return PublicPosting(
        source_job_id=source_job_id,
        title=title,
        location=location_text,
        posted_on=_date_part(
            structured_data.get("datePosted")
        ),
        job_url=expected_url,
        description_text=_plain_text(
            structured_data.get("description")
        ),
        country_codes=tuple(sorted(country_codes)),
        location_search_text=location_text,
    )


def parse_microsoft_israel_jobs(
    payload: str,
) -> tuple[PublicPosting, ...]:
    soup = BeautifulSoup(payload, "html.parser")
    jobs: list[PublicPosting] = []
    seen_ids: set[str] = set()

    for card in soup.select(
        ".careers-joblistResponsive-columnList"
    ):
        title_element = card.select_one(
            ".careers-joblistResponsive-subheading"
        )
        location_element = card.select_one(
            ".careers-joblistResponsive-primarylocation"
        )
        date_element = card.select_one(
            ".careers-joblistResponsive-postdate"
        )
        link = card.select_one(
            "a.careers-joblistResponsive-button[href]"
        )

        if (
            title_element is None
            or location_element is None
            or link is None
        ):
            continue

        job_url = _expected_https_url(
            link.get("href"),
            hostname="apply.careers.microsoft.com",
            path_prefix="/careers/job/",
        )
        path_match = re.fullmatch(
            r"/careers/job/([0-9]+)/?",
            urlsplit(job_url).path,
        )

        if path_match is None:
            continue

        source_job_id = path_match.group(1)
        title = _plain_text(
            title_element.get_text(" ", strip=True)
        )
        location = _plain_text(
            location_element.get_text(" ", strip=True)
        )

        if (
            not title
            or not location
            or source_job_id in seen_ids
        ):
            continue

        seen_ids.add(source_job_id)
        description_element = card.select_one(
            ".careers-joblistResponsive-desc"
        )
        description = _plain_text(
            description_element.get_text(
                " ",
                strip=True,
            )
            if description_element is not None
            else title
        )
        jobs.append(
            PublicPosting(
                source_job_id=source_job_id,
                title=title,
                location=location,
                posted_on=_date_part(
                    date_element.get_text(
                        " ",
                        strip=True,
                    )
                    if date_element is not None
                    else None
                ),
                job_url=job_url,
                description_text=description or title,
                country_codes=("IL",),
                location_search_text=location,
            )
        )

    if not jobs:
        raise ValueError(
            "Microsoft Israel page contains no job cards."
        )

    return tuple(jobs)


def is_relevant_location(
    posting: PublicPosting,
    registry: SourceRegistry,
) -> bool:
    if any(
        code.strip().upper()
        in registry.country_codes
        for code in posting.country_codes
    ):
        return True

    search_text = " ".join(
        (
            posting.location or "",
            posting.location_search_text,
        )
    ).casefold()

    return any(
        term in search_text
        for term in registry.location_terms
    )


def _get_json(
    session: requests.Session,
    url: str,
) -> Any:
    response = get_public_response(
        session,
        url,
        accept="application/json",
    )

    try:
        if response.status_code != 200:
            raise requests.HTTPError(
                "Public job feed returned HTTP "
                f"{response.status_code}.",
                response=response,
            )

        content_length = response.headers.get(
            "Content-Length"
        )

        if (
            content_length
            and int(content_length) > MAX_FEED_BYTES
        ):
            raise ValueError(
                "Public job feed exceeds the size limit."
            )

        if len(response.content) > MAX_FEED_BYTES:
            raise ValueError(
                "Public job feed exceeds the size limit."
            )

        return response.json()
    finally:
        response.close()


def _get_text(
    session: requests.Session,
    url: str,
    *,
    accept: str,
) -> str:
    response = get_public_response(
        session,
        url,
        accept=accept,
    )

    try:
        if response.status_code != 200:
            raise requests.HTTPError(
                "Official careers page returned HTTP "
                f"{response.status_code}.",
                response=response,
            )

        content_length = response.headers.get(
            "Content-Length"
        )

        if (
            content_length
            and int(content_length) > MAX_FEED_BYTES
        ):
            raise ValueError(
                "Official careers page exceeds the size limit."
            )

        if len(response.content) > MAX_FEED_BYTES:
            raise ValueError(
                "Official careers page exceeds the size limit."
            )

        return response.text
    finally:
        response.close()


def _fetch_source_postings(
    session: requests.Session,
    source: PublicSource,
) -> tuple[PublicPosting, ...]:
    identifier = quote(
        source.identifier,
        safe="",
    )

    if source.adapter == "greenhouse":
        payload = _get_json(
            session,
            "https://boards-api.greenhouse.io/"
            f"v1/boards/{identifier}/jobs?content=true",
        )

        return parse_greenhouse_jobs(payload)

    if source.adapter == "lever":
        api_host = (
            "api.eu.lever.co"
            if source.region == "eu"
            else "api.lever.co"
        )
        payload = _get_json(
            session,
            f"https://{api_host}/v0/postings/"
            f"{identifier}?mode=json",
        )

        return parse_lever_jobs(payload)

    if source.adapter == "ashby":
        payload = _get_json(
            session,
            "https://api.ashbyhq.com/posting-api/"
            f"job-board/{identifier}",
        )

        return parse_ashby_jobs(payload)

    if source.adapter == "smartrecruiters":
        payloads: list[Any] = []
        offset = 0

        for _ in range(MAX_FEED_PAGES):
            payload = _get_json(
                session,
                "https://api.smartrecruiters.com/v1/"
                f"companies/{identifier}/postings"
                "?limit=100"
                f"&offset={offset}"
                "&destination=PUBLIC",
            )
            payloads.append(payload)

            if not isinstance(payload, Mapping):
                break

            content = payload.get("content")
            total = payload.get("totalFound")

            if (
                not isinstance(content, list)
                or not content
            ):
                break

            offset += len(content)

            try:
                if offset >= int(total):
                    break
            except (
                TypeError,
                ValueError,
            ):
                if len(content) < 100:
                    break
        else:
            raise ValueError(
                "SmartRecruiters feed exceeded the "
                "pagination limit."
            )

        return parse_smartrecruiters_jobs(
            payloads,
            company_identifier=source.identifier,
        )

    if source.adapter == "workable":
        payload = _get_json(
            session,
            "https://www.workable.com/api/accounts/"
            f"{identifier}",
        )

        return parse_workable_jobs(payload)

    if source.adapter == "microsoft_careers":
        payload = _get_text(
            session,
            "https://careers.microsoft.com/v2/"
            "global/en/locations/israel.html",
            accept="text/html,application/xhtml+xml",
        )

        return parse_microsoft_israel_jobs(payload)

    if source.adapter == "nvidia_workday":
        sitemap = _get_text(
            session,
            "https://nvidia.wd5.myworkdayjobs.com/"
            "NVIDIAExternalCareerSite/siteMap.xml",
            accept="application/xml,text/xml",
        )
        israel_urls = tuple(
            job_url
            for job_url in parse_nvidia_sitemap(sitemap)
            if "/job/israel-" in unquote(
                urlsplit(job_url).path
            ).casefold()
        )

        if not israel_urls:
            raise ValueError(
                "NVIDIA sitemap contains no Israel job URLs."
            )

        if len(israel_urls) > MAX_OFFICIAL_DETAIL_PAGES:
            raise ValueError(
                "NVIDIA Israel jobs exceed the bounded "
                "detail-page limit."
            )

        jobs: list[PublicPosting] = []

        for index, job_url in enumerate(israel_urls):
            payload = _get_text(
                session,
                job_url,
                accept="text/html,application/xhtml+xml",
            )
            jobs.append(
                parse_nvidia_job_page(
                    payload,
                    job_url=job_url,
                )
            )

            if index + 1 < len(israel_urls):
                time.sleep(
                    OFFICIAL_DETAIL_DELAY_SECONDS
                )

        return tuple(jobs)

    raise ValueError(
        f"Unsupported adapter: {source.adapter!r}."
    )


def collect_public_source(
    session: requests.Session,
    source: PublicSource,
    registry: SourceRegistry,
) -> SourceCollection:
    postings = _fetch_source_postings(
        session,
        source,
    )
    relevant_jobs: list[NormalizedJob] = []

    for posting in postings:
        if not is_relevant_location(
            posting,
            registry,
        ):
            continue

        raw_text = (
            posting.description_text
            or " | ".join(
                part
                for part in (
                    posting.title,
                    source.company,
                    posting.location,
                )
                if part
            )
        )

        relevant_jobs.append(
            NormalizedJob(
                source=source.adapter,
                source_group=source.id,
                source_message_id=(
                    posting.source_job_id
                ),
                source_message_url=posting.job_url,
                message_date=posting.posted_on,
                title=posting.title,
                company=source.company,
                location=posting.location,
                posted_on=posting.posted_on,
                job_url=posting.job_url,
                raw_text=raw_text,
                parse_confidence=1.0,
            )
        )

    return SourceCollection(
        postings_seen=len(postings),
        relevant_jobs=tuple(relevant_jobs),
    )


def ensure_source_collection_table(
    connection: sqlite3.Connection,
) -> None:
    """Create additive source-run history for dashboard status."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            source_id TEXT NOT NULL,
            adapter TEXT NOT NULL,
            company TEXT NOT NULL,

            started_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,

            status TEXT NOT NULL
                CHECK (
                    status IN (
                        'running',
                        'success',
                        'error'
                    )
                ),

            postings_seen INTEGER NOT NULL
                DEFAULT 0,
            postings_relevant INTEGER NOT NULL
                DEFAULT 0,
            new_jobs INTEGER NOT NULL
                DEFAULT 0,
            new_postings INTEGER NOT NULL
                DEFAULT 0,

            error TEXT
        );

        CREATE INDEX IF NOT EXISTS
            idx_source_collection_runs_source
        ON source_collection_runs(source_id, id DESC);

        CREATE INDEX IF NOT EXISTS
            idx_source_collection_runs_status
        ON source_collection_runs(status);
        """
    )

    connection.commit()


def start_collection_run(
    connection: sqlite3.Connection,
    source: PublicSource,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO source_collection_runs (
            source_id,
            adapter,
            company,
            status
        )
        VALUES (?, ?, ?, 'running')
        """,
        (
            source.id,
            source.adapter,
            source.company,
        ),
    )
    connection.commit()

    return int(cursor.lastrowid)


def finish_collection_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    postings_seen: int = 0,
    postings_relevant: int = 0,
    new_jobs: int = 0,
    new_postings: int = 0,
    error: str | None = None,
) -> None:
    if status not in {"success", "error"}:
        raise ValueError(
            "Collection status must be success or error."
        )

    connection.execute(
        """
        UPDATE source_collection_runs
        SET
            finished_at = CURRENT_TIMESTAMP,
            status = ?,
            postings_seen = ?,
            postings_relevant = ?,
            new_jobs = ?,
            new_postings = ?,
            error = ?
        WHERE id = ?
        """,
        (
            status,
            postings_seen,
            postings_relevant,
            new_jobs,
            new_postings,
            error[:2000] if error else None,
            run_id,
        ),
    )
    connection.commit()


def get_collection_statuses(
    connection: sqlite3.Connection,
    registry: SourceRegistry,
) -> tuple[CollectionStatus, ...]:
    ensure_source_collection_table(connection)

    rows = connection.execute(
        """
        SELECT runs.*
        FROM source_collection_runs AS runs
        INNER JOIN (
            SELECT
                source_id,
                MAX(id) AS latest_id
            FROM source_collection_runs
            GROUP BY source_id
        ) AS latest
            ON latest.latest_id = runs.id
        """
    ).fetchall()
    latest_by_source = {
        str(row["source_id"]).casefold(): row
        for row in rows
    }
    statuses: list[CollectionStatus] = []

    for source in registry.sources:
        row = latest_by_source.get(
            source.id.casefold()
        )

        statuses.append(
            CollectionStatus(
                source_id=source.id,
                company=source.company,
                adapter=source.adapter,
                enabled=source.enabled,
                status=(
                    str(row["status"])
                    if row is not None
                    else "not_run"
                ),
                last_collection_at=(
                    row["finished_at"]
                    or row["started_at"]
                    if row is not None
                    else None
                ),
                postings_seen=(
                    int(row["postings_seen"])
                    if row is not None
                    else 0
                ),
                postings_relevant=(
                    int(row["postings_relevant"])
                    if row is not None
                    else 0
                ),
                new_jobs=(
                    int(row["new_jobs"])
                    if row is not None
                    else 0
                ),
                new_postings=(
                    int(row["new_postings"])
                    if row is not None
                    else 0
                ),
                error=(
                    row["error"]
                    if row is not None
                    else None
                ),
            )
        )

    return tuple(
        sorted(
            statuses,
            key=lambda item: (
                item.company.casefold(),
                item.adapter,
            ),
        )
    )
