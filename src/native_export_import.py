import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from database import save_parsed_job
from source_parsing import (
    JobParserRegistry,
    NormalizedJob,
    SourceContext,
    SourceMessage,
)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    source_messages_checked: int
    jobs_parsed: int
    new_jobs: int
    new_postings: int
    existing_postings: int


@dataclass(frozen=True, slots=True)
class JobPreview:
    title: str | None
    company: str | None
    location: str | None
    posted_on: str | None
    job_url: str | None


@dataclass(frozen=True, slots=True)
class PreviewSummary:
    source_messages_checked: int
    jobs_parsed: int
    jobs: tuple[JobPreview, ...]


def filter_source_messages_since(
    messages: Iterable[SourceMessage],
    since: date | None,
) -> tuple[SourceMessage, ...]:
    """Keep messages on or after an optional calendar date."""

    if since is None:
        return tuple(messages)

    filtered: list[SourceMessage] = []

    for message in messages:
        if not message.message_date:
            continue

        try:
            message_day = date.fromisoformat(
                message.message_date[:10]
            )
        except ValueError as error:
            raise ValueError(
                "Source message has an invalid ISO date: "
                f"{message.message_date!r}."
            ) from error

        if message_day >= since:
            filtered.append(message)

    return tuple(filtered)


def preview_source_messages(
    messages: Iterable[SourceMessage],
    context: SourceContext,
    parser_registry: JobParserRegistry,
    preview_limit: int = 5,
) -> PreviewSummary:
    """Parse messages without opening or changing a database."""

    if preview_limit < 0:
        raise ValueError(
            "Preview limit must not be negative."
        )

    source_messages_checked = 0
    jobs_parsed = 0
    preview_jobs: list[JobPreview] = []

    for message in messages:
        source_messages_checked += 1
        parsed_job: NormalizedJob | None = (
            parser_registry.parse(
                message,
                context,
            )
        )

        if parsed_job is None:
            continue

        jobs_parsed += 1

        if len(preview_jobs) < preview_limit:
            preview_jobs.append(
                JobPreview(
                    title=parsed_job.title,
                    company=parsed_job.company,
                    location=parsed_job.location,
                    posted_on=parsed_job.posted_on,
                    job_url=parsed_job.job_url,
                )
            )

    return PreviewSummary(
        source_messages_checked=source_messages_checked,
        jobs_parsed=jobs_parsed,
        jobs=tuple(preview_jobs),
    )


def import_source_messages(
    connection: sqlite3.Connection,
    messages: Iterable[SourceMessage],
    context: SourceContext,
    parser_registry: JobParserRegistry,
) -> ImportSummary:
    """Parse and save local source messages in one transaction."""

    source_messages_checked = 0
    jobs_parsed = 0
    new_jobs = 0
    new_postings = 0
    existing_postings = 0

    with connection:
        for message in messages:
            source_messages_checked += 1

            parsed_job = parser_registry.parse(
                message,
                context,
            )

            if parsed_job is None:
                continue

            jobs_parsed += 1
            (
                _,
                was_new_job,
                was_new_posting,
            ) = save_parsed_job(
                connection,
                parsed_job,
            )

            if was_new_job:
                new_jobs += 1

            if was_new_posting:
                new_postings += 1
            else:
                existing_postings += 1

    return ImportSummary(
        source_messages_checked=source_messages_checked,
        jobs_parsed=jobs_parsed,
        new_jobs=new_jobs,
        new_postings=new_postings,
        existing_postings=existing_postings,
    )
