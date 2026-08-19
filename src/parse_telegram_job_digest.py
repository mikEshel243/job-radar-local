import asyncio
import json
import os
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    MessageEntityTextUrl,
    MessageEntityUrl,
)

# Re-export shared helpers for compatibility with existing imports.
from job_urls import (
    TRACKING_PARAMETERS,
    clean_visible_url,
    normalize_url,
)
from source_parsing import (
    NormalizedJob,
    SourceContext,
    SourceMessage,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
SESSION_PATH = PROJECT_ROOT / "data" / "telegram_account"
OUTPUT_PATH = PROJECT_ROOT / "data" / "parsed_jobs_preview.json"


# Backwards-compatible name for existing imports and type hints.
ParsedJob = NormalizedJob


def get_required_env(name: str) -> str:
    """Read a required value from the .env file."""

    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value.strip()


def load_settings() -> tuple[int, str, str, str, int]:
    """Load Telegram and parser settings."""

    load_dotenv(ENV_PATH)

    api_id_raw = get_required_env("TELEGRAM_API_ID")
    api_hash = get_required_env("TELEGRAM_API_HASH")
    phone = get_required_env("TELEGRAM_PHONE")

    channel_username = get_required_env(
        "TELEGRAM_CHANNEL_USERNAME"
    ).lstrip("@")

    message_limit_raw = os.getenv(
        "TELEGRAM_MESSAGE_LIMIT",
        "10",
    )

    try:
        api_id = int(api_id_raw)
    except ValueError as error:
        raise RuntimeError(
            "TELEGRAM_API_ID must contain numbers only."
        ) from error

    try:
        message_limit = int(message_limit_raw)
    except ValueError as error:
        raise RuntimeError(
            "TELEGRAM_MESSAGE_LIMIT must contain numbers only."
        ) from error

    if message_limit < 1 or message_limit > 100:
        raise RuntimeError(
            "TELEGRAM_MESSAGE_LIMIT must be between 1 and 100."
        )

    return (
        api_id,
        api_hash,
        phone,
        channel_username,
        message_limit,
    )


def extract_urls(message: Any) -> list[str]:
    """
    Extract visible URLs, hidden Telegram links,
    button links and preview links.
    """

    found_urls: list[str] = []
    raw_text = message.raw_text or ""

    for entity, inner_text in message.get_entities_text():
        if isinstance(entity, MessageEntityTextUrl):
            found_urls.append(entity.url)

        elif isinstance(entity, MessageEntityUrl):
            found_urls.append(inner_text)

    found_urls.extend(
        re.findall(
            r"https?://[^\s<>()]+",
            raw_text,
            flags=re.IGNORECASE,
        )
    )

    for row in message.buttons or []:
        for button in row:
            button_url = getattr(button, "url", None)

            if button_url:
                found_urls.append(button_url)

    media = getattr(message, "media", None)
    webpage = getattr(media, "webpage", None)
    preview_url = getattr(webpage, "url", None)

    if preview_url:
        found_urls.append(preview_url)

    unique_urls: list[str] = []
    seen: set[str] = set()

    for url in found_urls:
        normalized = normalize_url(url)

        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_urls.append(normalized)

    return unique_urls


def parse_posted_date(value: str) -> str | None:
    """
    Convert dates such as 27-07-2026 to 2026-07-27.
    """

    cleaned = value.strip()

    date_formats = [
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]

    for date_format in date_formats:
        try:
            parsed = datetime.strptime(
                cleaned,
                date_format,
            )
            return parsed.date().isoformat()
        except ValueError:
            continue

    return None


def split_title_and_company(
    header: str,
) -> tuple[str | None, str | None]:
    """
    Split:
    Job Title @ Company

    rsplit is used in case the title unexpectedly contains @.
    """

    cleaned = header.strip()

    if not cleaned:
        return None, None

    if " @ " not in cleaned:
        return cleaned, None

    title, company = cleaned.rsplit(" @ ", 1)

    return (
        title.strip() or None,
        company.strip() or None,
    )


def calculate_confidence(
    title: str | None,
    company: str | None,
    location: str | None,
    posted_on: str | None,
    job_url: str | None,
) -> float:
    """Calculate confidence based on successfully parsed fields."""

    field_scores = {
        "title": 0.30,
        "company": 0.20,
        "location": 0.15,
        "posted_on": 0.10,
        "job_url": 0.25,
    }

    score = 0.0

    if title:
        score += field_scores["title"]

    if company:
        score += field_scores["company"]

    if location:
        score += field_scores["location"]

    if posted_on:
        score += field_scores["posted_on"]

    if job_url:
        score += field_scores["job_url"]

    return round(score, 2)


def parse_telegram_job_digest_message(
    message: Any,
    context: SourceContext | None = None,
    *,
    channel_title: str | None = None,
    channel_username: str | None = None,
) -> NormalizedJob | None:
    """Parse one Example Telegram Job Digest message.

    The channel keyword arguments preserve compatibility with callers
    that predate the source-neutral parser context.
    """

    if context is None:
        if (
            not channel_title
            or not channel_username
        ):
            raise TypeError(
                "A SourceContext or both legacy channel "
                "arguments are required."
            )

        context = SourceContext(
            source="telegram",
            group_name=channel_title,
            group_identifier=channel_username,
        )

    elif (
        channel_title is not None
        or channel_username is not None
    ):
        raise TypeError(
            "Use SourceContext or legacy channel "
            "arguments, not both."
        )

    raw_text = (message.raw_text or "").strip()

    if not raw_text:
        return None

    lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    ]

    if not lines:
        return None

    title, company = split_title_and_company(lines[0])

    posted_on: str | None = None
    location: str | None = None

    for line in lines[1:]:
        lowered = line.lower()

        if lowered.startswith("posted on:"):
            value = line.split(":", 1)[1]
            posted_on = parse_posted_date(value)

        elif lowered.startswith("location:"):
            value = line.split(":", 1)[1]
            location = value.strip() or None

    if isinstance(message, SourceMessage):
        urls = []
        seen_urls: set[str] = set()

        for url in message.urls:
            normalized = normalize_url(url)

            if (
                normalized
                and normalized not in seen_urls
            ):
                seen_urls.add(normalized)
                urls.append(normalized)
    else:
        urls = extract_urls(message)

    job_url = urls[0] if urls else None

    if isinstance(message, SourceMessage):
        source_message_id = message.source_message_id
        message_date = message.message_date
        source_message_url = message.source_message_url
    else:
        source_message_id = message.id
        message_date = (
            message.date.astimezone().isoformat()
            if message.date
            else ""
        )
        source_message_url = (
            "https://t.me/"
            f"{context.group_identifier.lstrip('@')}/"
            f"{message.id}"
        )

    confidence = calculate_confidence(
        title=title,
        company=company,
        location=location,
        posted_on=posted_on,
        job_url=job_url,
    )

    return NormalizedJob(
        source=context.source,
        source_group=context.group_name,
        source_message_id=source_message_id,
        source_message_url=source_message_url,
        message_date=message_date,
        title=title,
        company=company,
        location=location,
        posted_on=posted_on,
        job_url=job_url,
        raw_text=raw_text,
        parse_confidence=confidence,
    )


def parse_telegram_job_digest_export_message(
    message: SourceMessage,
    context: SourceContext,
) -> NormalizedJob | None:
    """Parse only the demonstrated Telegram export job shape."""

    nonempty_lines = [
        line.strip()
        for line in message.raw_text.splitlines()
        if line.strip()
    ]

    if (
        not nonempty_lines
        or " @ " not in nonempty_lines[0]
        or not any(
            line.lower().startswith("posted on:")
            for line in nonempty_lines[1:]
        )
        or not any(
            line.lower().startswith("location:")
            for line in nonempty_lines[1:]
        )
        or not message.urls
    ):
        return None

    return parse_telegram_job_digest_message(
        message=message,
        context=context,
    )


def print_job(
    job: NormalizedJob,
    index: int,
) -> None:
    """Print a readable parsed-job preview."""

    print()
    print(f"PARSED JOB #{index}")
    print("=" * 90)
    print(f"Title: {job.title}")
    print(f"Company: {job.company}")
    print(f"Location: {job.location}")
    print(f"Posted on: {job.posted_on}")
    print(f"Job URL: {job.job_url}")
    print(f"Telegram URL: {job.source_message_url}")
    print(f"Confidence: {job.parse_confidence:.0%}")


async def main() -> None:
    (
        api_id,
        api_hash,
        phone,
        channel_username,
        message_limit,
    ) = load_settings()

    client = TelegramClient(
        str(SESSION_PATH),
        api_id,
        api_hash,
    )

    parsed_jobs: list[NormalizedJob] = []

    try:
        print("Connecting to Telegram...")
        await client.start(phone=phone)

        channel = await client.get_entity(
            channel_username
        )

        channel_title = getattr(
            channel,
            "title",
            channel_username,
        )

        source_context = SourceContext(
            source="telegram",
            group_name=channel_title,
            group_identifier=channel_username,
        )

        print(f"Channel: {channel_title}")
        print(
            f"Reading latest {message_limit} messages..."
        )

        async for message in client.iter_messages(
            channel,
            limit=message_limit,
        ):
            parsed_job = parse_telegram_job_digest_message(
                message=message,
                context=source_context,
            )

            if parsed_job is None:
                continue

            parsed_jobs.append(parsed_job)
            print_job(parsed_job, len(parsed_jobs))

        OUTPUT_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with OUTPUT_PATH.open(
            "w",
            encoding="utf-8",
        ) as output_file:
            json.dump(
                [asdict(job) for job in parsed_jobs],
                output_file,
                ensure_ascii=False,
                indent=2,
            )

        print()
        print("-" * 90)
        print(
            f"Parsed {len(parsed_jobs)} jobs successfully."
        )
        print(f"Saved preview to: {OUTPUT_PATH}")

    except Exception as error:
        print()
        print(
            f"Unexpected error: "
            f"{type(error).__name__}: {error}"
        )

    finally:
        await client.disconnect()
        print("Disconnected safely.")


if __name__ == "__main__":
    asyncio.run(main())
