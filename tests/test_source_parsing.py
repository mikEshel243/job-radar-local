import sys
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from source_parsing import (  # noqa: E402
    JobParserRegistry,
    NormalizedJob,
    SourceContext,
)


def make_normalized_job(
    context: SourceContext,
) -> NormalizedJob:
    return NormalizedJob(
        source=context.source,
        source_group=context.group_name,
        source_message_id=1,
        source_message_url=None,
        message_date=None,
        title="Backend Developer",
        company="Example",
        location="Example City",
        posted_on=None,
        job_url=None,
        raw_text="Backend Developer @ Example",
        parse_confidence=0.8,
    )


class JobParserRegistryTests(unittest.TestCase):
    def test_resolves_source_and_group_case_insensitively(
        self,
    ) -> None:
        registry = JobParserRegistry()

        def parser(
            _: Any,
            context: SourceContext,
        ) -> NormalizedJob:
            return make_normalized_job(context)

        registry.register(
            "Telegram",
            "@Tech_Jobs",
            parser,
        )

        self.assertIs(
            registry.resolve(
                "telegram",
                "tech_jobs",
            ),
            parser,
        )

    def test_supports_different_formats_per_group(
        self,
    ) -> None:
        registry = JobParserRegistry()

        def first_parser(
            _: Any,
            context: SourceContext,
        ) -> NormalizedJob:
            return make_normalized_job(context)

        def second_parser(
            _: Any,
            context: SourceContext,
        ) -> NormalizedJob:
            return make_normalized_job(context)

        registry.register(
            "whatsapp",
            "backend-jobs",
            first_parser,
        )
        registry.register(
            "whatsapp",
            "fullstack-jobs",
            second_parser,
        )

        self.assertIs(
            registry.resolve(
                "whatsapp",
                "backend-jobs",
            ),
            first_parser,
        )
        self.assertIs(
            registry.resolve(
                "whatsapp",
                "fullstack-jobs",
            ),
            second_parser,
        )

    def test_parse_passes_source_context_to_parser(
        self,
    ) -> None:
        registry = JobParserRegistry()
        context = SourceContext(
            source="telegram",
            group_name="Tech Jobs",
            group_identifier="tech_jobs",
        )

        def parser(
            message: Any,
            parser_context: SourceContext,
        ) -> NormalizedJob:
            self.assertEqual(message, "raw message")
            self.assertIs(parser_context, context)
            return make_normalized_job(parser_context)

        registry.register(
            context.source,
            context.group_identifier,
            parser,
        )

        parsed_job = registry.parse(
            "raw message",
            context,
        )

        self.assertIsNotNone(parsed_job)
        self.assertEqual(
            parsed_job.source_group,
            "Tech Jobs",
        )

    def test_rejects_duplicate_registration(self) -> None:
        registry = JobParserRegistry()

        def parser(
            _: Any,
            context: SourceContext,
        ) -> NormalizedJob:
            return make_normalized_job(context)

        registry.register(
            "telegram",
            "tech_jobs",
            parser,
        )

        with self.assertRaises(ValueError):
            registry.register(
                "telegram",
                "tech_jobs",
                parser,
            )

    def test_missing_parser_has_clear_error(self) -> None:
        registry = JobParserRegistry()

        with self.assertRaisesRegex(
            LookupError,
            "No parser is registered",
        ):
            registry.resolve(
                "whatsapp",
                "unknown-group",
            )


if __name__ == "__main__":
    unittest.main()
