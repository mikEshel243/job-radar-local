from dataclasses import dataclass
from parse_whatsapp_job_digest import (
    parse_whatsapp_job_digest_message,
)
from source_parsing import (
    JobMessageParser,
    JobParserRegistry,
    SourceContext,
)


@dataclass(frozen=True, slots=True)
class WhatsAppSource:
    group_name: str
    parser: JobMessageParser


SUPPORTED_WHATSAPP_GROUPS = {
    "whatsapp_example_digest": WhatsAppSource(
        group_name="Example Developer Jobs",
        parser=parse_whatsapp_job_digest_message,
    ),
}


def prepare_whatsapp_parser(
    group_identifier: str,
    *,
    exact_group_name: str | None = None,
) -> tuple[SourceContext, JobParserRegistry]:
    """Build the shared parser context for one allowlisted group."""

    group_key = group_identifier.strip().casefold()

    try:
        source = SUPPORTED_WHATSAPP_GROUPS[group_key]
    except KeyError as error:
        supported = ", ".join(
            sorted(SUPPORTED_WHATSAPP_GROUPS)
        )
        raise ValueError(
            "Unsupported WhatsApp group ID "
            f"{group_identifier!r}. Supported IDs: {supported}."
        ) from error

    if (
        exact_group_name is not None
        and exact_group_name != source.group_name
    ):
        raise ValueError(
            "Configured WhatsApp group name does not exactly "
            "match the registered group name."
        )

    context = SourceContext(
        source="whatsapp",
        group_name=source.group_name,
        group_identifier=group_key,
    )
    parser_registry = JobParserRegistry()
    parser_registry.register(
        source=context.source,
        group_identifier=context.group_identifier,
        parser=source.parser,
    )

    return context, parser_registry
