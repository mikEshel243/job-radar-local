from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Identify the source and group of one collected message."""

    source: str
    group_name: str
    group_identifier: str


@dataclass(frozen=True, slots=True)
class SourceMessage:
    """Source-neutral message read from a local native export."""

    source_message_id: int | str
    message_date: str | None
    sender: str | None
    raw_text: str
    urls: tuple[str, ...]
    source_message_url: str | None


@dataclass(slots=True)
class NormalizedJob:
    """Source-neutral job structure stored by the shared pipeline."""

    source: str
    source_group: str
    source_message_id: int | str
    source_message_url: str | None
    message_date: str | None

    title: str | None
    company: str | None
    location: str | None
    posted_on: str | None
    job_url: str | None

    raw_text: str
    parse_confidence: float


class JobMessageParser(Protocol):
    """Callable contract implemented by each message-format parser."""

    def __call__(
        self,
        message: Any,
        context: SourceContext,
    ) -> NormalizedJob | None:
        ...


ParserKey = tuple[str, str]


class JobParserRegistry:
    """Resolve a parser by message source and stable group ID."""

    def __init__(self) -> None:
        self._parsers: dict[
            ParserKey,
            JobMessageParser,
        ] = {}

    @staticmethod
    def _build_key(
        source: str,
        group_identifier: str,
    ) -> ParserKey:
        source_key = source.strip().casefold()
        group_key = (
            group_identifier
            .strip()
            .lstrip("@")
            .casefold()
        )

        if not source_key:
            raise ValueError(
                "Parser source must not be empty."
            )

        if not group_key:
            raise ValueError(
                "Parser group identifier must not be empty."
            )

        return source_key, group_key

    def register(
        self,
        source: str,
        group_identifier: str,
        parser: JobMessageParser,
    ) -> None:
        """Register one parser without replacing existing entries."""

        key = self._build_key(
            source,
            group_identifier,
        )

        if key in self._parsers:
            raise ValueError(
                "A parser is already registered for "
                f"{source!r} / {group_identifier!r}."
            )

        self._parsers[key] = parser

    def resolve(
        self,
        source: str,
        group_identifier: str,
    ) -> JobMessageParser:
        """Return the parser configured for a source/group pair."""

        key = self._build_key(
            source,
            group_identifier,
        )

        try:
            return self._parsers[key]
        except KeyError as error:
            raise LookupError(
                "No parser is registered for "
                f"{source!r} / {group_identifier!r}."
            ) from error

    def parse(
        self,
        message: Any,
        context: SourceContext,
    ) -> NormalizedJob | None:
        """Resolve and run the parser for one source message."""

        parser = self.resolve(
            context.source,
            context.group_identifier,
        )

        return parser(
            message,
            context,
        )
