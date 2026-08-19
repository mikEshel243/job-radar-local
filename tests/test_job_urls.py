import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from job_urls import (  # noqa: E402
    TRACKING_PARAMETERS,
    is_public_web_url,
    normalize_url,
)
from parse_telegram_job_digest import (  # noqa: E402
    TRACKING_PARAMETERS as legacy_tracking_parameters,
    normalize_url as legacy_normalize_url,
)


class NormalizeUrlTests(unittest.TestCase):
    def test_parser_keeps_compatibility_import(self) -> None:
        self.assertIs(
            legacy_normalize_url,
            normalize_url,
        )
        self.assertIs(
            legacy_tracking_parameters,
            TRACKING_PARAMETERS,
        )

    def test_normalizes_host_and_removes_tracking(self) -> None:
        normalized = normalize_url(
            "HTTPS://Jobs.Example.COM/roles/42/"
            "?utm_source=telegram&team=platform"
            "&REF=channel#apply"
        )

        self.assertEqual(
            normalized,
            (
                "https://jobs.example.com/roles/42"
                "?team=platform"
            ),
        )

    def test_preserves_unknown_query_segments(self) -> None:
        normalized = normalize_url(
            "https://example.com/job?"
            "filter=a%2Fb&unusual-segment&utm_medium=social"
        )

        self.assertEqual(
            normalized,
            (
                "https://example.com/job?"
                "filter=a%2Fb&unusual-segment"
            ),
        )

    def test_removes_fragment_and_trailing_punctuation(
        self,
    ) -> None:
        normalized = normalize_url(
            "https://example.com/job/42/#apply)."
        )

        self.assertEqual(
            normalized,
            "https://example.com/job/42",
        )

    def test_rejects_non_web_schemes(self) -> None:
        for value in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "mailto:jobs@example.com",
            "/relative/job/42",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_url(value),
                    "",
                )

    def test_rejects_web_url_without_host(self) -> None:
        self.assertEqual(
            normalize_url("https:///job/42"),
            "",
        )

    def test_rejects_embedded_credentials(self) -> None:
        self.assertEqual(
            normalize_url(
                "https://user:secret@example.com/job/42"  # pragma: allowlist secret
            ),
            "",
        )

    def test_rejects_private_and_loopback_targets(
        self,
    ) -> None:
        for value in (
            "http://127.0.0.1/job/42",
            "http://10.0.0.5/job/42",
            "http://169.254.1.2/job/42",
            "http://[::1]/job/42",
            "http://localhost/job/42",
        ):
            with self.subTest(value=value):
                self.assertFalse(
                    is_public_web_url(value)
                )

    def test_requires_every_resolved_address_to_be_public(
        self,
    ) -> None:
        public_result = [
            (
                2,
                1,
                6,
                "",
                ("8.8.8.8", 443),
            ),
        ]
        mixed_result = [
            *public_result,
            (
                2,
                1,
                6,
                "",
                ("192.168.1.20", 443),
            ),
        ]

        self.assertTrue(
            is_public_web_url(
                "https://jobs.example/role/42",
                resolver=lambda *args, **kwargs: (
                    public_result
                ),
            )
        )
        self.assertFalse(
            is_public_web_url(
                "https://jobs.example/role/42",
                resolver=lambda *args, **kwargs: (
                    mixed_result
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
