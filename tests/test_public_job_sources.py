import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from public_job_sources import (  # noqa: E402
    SourceCollection,
    PublicPosting,
    PublicSource,
    SourceRegistry,
    ensure_source_collection_table,
    finish_collection_run,
    get_collection_statuses,
    is_relevant_location,
    load_source_registry,
    parse_ashby_jobs,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_microsoft_israel_jobs,
    parse_nvidia_job_page,
    parse_nvidia_sitemap,
    parse_smartrecruiters_jobs,
    parse_source_registry,
    parse_workable_jobs,
    start_collection_run,
)
from collect_public_jobs import run_collection  # noqa: E402


class SourceRegistryTests(unittest.TestCase):
    def test_local_registry_overrides_the_example(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_path = root / "job_sources.local.json"
            example_path = root / "job_sources.example.json"

            def payload(country_code: str) -> dict[str, object]:
                return {
                    "version": 1,
                    "location_policy": {
                        "country_codes": [country_code],
                        "terms": [],
                    },
                    "sources": [
                        {
                            "id": "example",
                            "company": "Example Company",
                            "adapter": "greenhouse",
                            "identifier": "example-company",
                            "enabled": False,
                        }
                    ],
                }

            example_path.write_text(
                json.dumps(payload("EX")),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps(payload("LC")),
                encoding="utf-8",
            )

            with (
                patch(
                    "public_job_sources.LOCAL_REGISTRY_PATH",
                    local_path,
                ),
                patch(
                    "public_job_sources.EXAMPLE_REGISTRY_PATH",
                    example_path,
                ),
            ):
                self.assertEqual(
                    load_source_registry().country_codes,
                    frozenset({"LC"}),
                )
                local_path.unlink()
                self.assertEqual(
                    load_source_registry().country_codes,
                    frozenset({"EX"}),
                )

    def test_repository_registry_is_valid_and_separate(
        self,
    ) -> None:
        registry = load_source_registry()

        self.assertEqual(
            registry.country_codes,
            frozenset({"EX"}),
        )
        self.assertEqual(
            {
                source.adapter
                for source in registry.sources
            },
            {
                "ashby",
                "greenhouse",
                "lever",
                "smartrecruiters",
                "workable",
            },
        )
        self.assertGreaterEqual(
            len(registry.sources),
            5,
        )

    def test_registry_rejects_arbitrary_endpoint_values(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "identifiers",
        ):
            parse_source_registry(
                {
                    "version": 1,
                    "location_policy": {
                        "country_codes": ["IL"],
                        "terms": [],
                    },
                    "sources": [
                        {
                            "id": "unsafe",
                            "company": "Unsafe",
                            "adapter": "greenhouse",
                            "identifier": (
                                "https://127.0.0.1/private"
                            ),
                        }
                    ],
                }
            )

    def test_registry_rejects_nonfixed_official_page(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "fixed official",
        ):
            parse_source_registry(
                {
                    "version": 1,
                    "location_policy": {
                        "country_codes": ["IL"],
                        "terms": [],
                    },
                    "sources": [
                        {
                            "id": "unsafe_workday",
                            "company": "Unsafe",
                            "adapter": "nvidia_workday",
                            "identifier": "AnotherCareerSite",
                        }
                    ],
                }
            )


class AdapterParsingTests(unittest.TestCase):
    def test_greenhouse_parses_original_url_and_content(
        self,
    ) -> None:
        jobs = parse_greenhouse_jobs(
            {
                "jobs": [
                    {
                        "id": 42,
                        "title": "Backend Engineer",
                        "absolute_url": (
                            "https://company.example/jobs/42"
                        ),
                        "first_published": (
                            "2026-07-20T08:00:00+03:00"
                        ),
                        "location": {
                            "name": "Example City; Demo Metro",
                        },
                        "content": (
                            "<p>Java and Spring</p>"
                        ),
                    }
                ]
            }
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0].job_url,
            "https://company.example/jobs/42",
        )
        self.assertEqual(
            jobs[0].description_text,
            "Java and Spring",
        )

    def test_lever_parses_country_and_all_locations(
        self,
    ) -> None:
        jobs = parse_lever_jobs(
            [
                {
                    "id": "lever-42",
                    "text": "Full Stack Engineer",
                    "hostedUrl": (
                        "https://jobs.lever.co/example/42"
                    ),
                    "createdAt": 1784678400000,
                    "country": "IL",
                    "categories": {
                        "location": "Demo Metro",
                        "allLocations": [
                            "Demo Metro",
                            "Example City",
                        ],
                    },
                    "descriptionPlain": (
                        "React and Java"
                    ),
                }
            ]
        )

        self.assertEqual(
            jobs[0].country_codes,
            ("IL",),
        )
        self.assertIn(
            "Example City",
            jobs[0].location_search_text,
        )

    def test_ashby_skips_unlisted_jobs(
        self,
    ) -> None:
        jobs = parse_ashby_jobs(
            {
                "jobs": [
                    {
                        "id": "hidden",
                        "title": "Hidden",
                        "isListed": False,
                        "jobUrl": (
                            "https://jobs.ashbyhq.com/example/hidden"
                        ),
                    },
                    {
                        "id": "visible",
                        "title": "Software Engineer",
                        "isListed": True,
                        "location": "Caesarea Office",
                        "publishedAt": (
                            "2026-07-20T08:00:00+03:00"
                        ),
                        "jobUrl": (
                            "https://jobs.ashbyhq.com/example/visible"
                        ),
                        "descriptionPlain": (
                            "Distributed systems"
                        ),
                        "address": {
                            "postalAddress": {
                                "addressCountry": "Israel",
                            }
                        },
                    },
                ]
            }
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0].source_job_id,
            "visible",
        )
        self.assertEqual(
            jobs[0].country_codes,
            ("IL",),
        )

    def test_smartrecruiters_builds_public_posting_url(
        self,
    ) -> None:
        jobs = parse_smartrecruiters_jobs(
            [
                {
                    "content": [
                        {
                            "id": "107",
                            "name": (
                                "Junior Software Engineer"
                            ),
                            "releasedDate": (
                                "2026-07-20T08:00:00Z"
                            ),
                            "location": {
                                "country": "il",
                                "fullLocation": (
                                    "Demo Metro, Israel"
                                ),
                            },
                        }
                    ]
                }
            ],
            company_identifier="Example",
        )

        self.assertEqual(
            jobs[0].job_url,
            (
                "https://jobs.smartrecruiters.com/"
                "Example/107"
            ),
        )
        self.assertEqual(
            jobs[0].country_codes,
            ("IL",),
        )

    def test_workable_prefers_public_shortlink(
        self,
    ) -> None:
        jobs = parse_workable_jobs(
            {
                "jobs": [
                    {
                        "title": "Frontend Engineer",
                        "shortcode": "ABC123",
                        "shortlink": (
                            "https://apply.workable.com/j/ABC123"
                        ),
                        "application_url": (
                            "https://apply.workable.com/"
                            "j/ABC123/apply"
                        ),
                        "published_on": "2026-07-20",
                        "country": "Israel",
                        "state": "Example District",
                        "city": "Binyamina",
                    }
                ]
            }
        )

        self.assertEqual(
            jobs[0].job_url,
            "https://apply.workable.com/j/ABC123",
        )
        self.assertEqual(
            jobs[0].country_codes,
            ("IL",),
        )

    def test_nvidia_parses_declared_sitemap_and_jobposting(
        self,
    ) -> None:
        job_url = (
            "https://nvidia.wd5.myworkdayjobs.com/"
            "NVIDIAExternalCareerSite/job/Israel-Example Heights/"
            "Backend-Engineer_JR42"
        )
        urls = parse_nvidia_sitemap(
            """<?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>"""
            + job_url
            + """</loc></url>
              <url><loc>https://example.com/job/unsafe</loc></url>
            </urlset>"""
        )
        posting = parse_nvidia_job_page(
            """
            <html><head>
              <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Backend Engineer",
                "datePosted": "2026-07-20",
                "description": "<p>Python and Linux</p>",
                "identifier": {
                  "@type": "PropertyValue",
                  "name": "NVIDIA",
                  "value": "JR42"
                },
                "jobLocation": {
                  "@type": "Place",
                  "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Example Heights",
                    "addressCountry": "Israel"
                  }
                }
              }
              </script>
            </head></html>
            """,
            job_url=job_url,
        )

        self.assertEqual(urls, (job_url,))
        self.assertEqual(posting.source_job_id, "JR42")
        self.assertEqual(posting.location, "Example Heights, Israel")
        self.assertEqual(posting.country_codes, ("IL",))
        self.assertEqual(
            posting.description_text,
            "Python and Linux",
        )

    def test_microsoft_parses_official_israel_cards(
        self,
    ) -> None:
        jobs = parse_microsoft_israel_jobs(
            """
            <div class="careers-joblistResponsive-columnList">
              <h3 class="careers-joblistResponsive-subheading">
                Software Engineer
              </h3>
              <div class="careers-joblistResponsive-postdate">
                2026-07-20
              </div>
              <div class="careers-joblistResponsive-primarylocation">
                Israel, Multiple Locations
              </div>
              <div class="careers-joblistResponsive-desc">
                &lt;p&gt;Build Azure services.&lt;/p&gt;
              </div>
              <a class="careers-joblistResponsive-button"
                 href="https://apply.careers.microsoft.com/careers/job/42?hl=en">
                See details
              </a>
            </div>
            """
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_job_id, "42")
        self.assertEqual(
            jobs[0].description_text,
            "Build Azure services.",
        )
        self.assertEqual(jobs[0].country_codes, ("IL",))

    def test_location_filter_uses_structured_country(
        self,
    ) -> None:
        registry = SourceRegistry(
            country_codes=frozenset({"IL"}),
            location_terms=("haifa",),
            sources=(),
        )
        posting = PublicPosting(
            source_job_id="42",
            title="Remote Engineer",
            location="Remote",
            posted_on="2026-07-20",
            job_url=(
                "https://jobs.example.com/42"
            ),
            description_text="Description",
            country_codes=("IL",),
        )

        self.assertTrue(
            is_relevant_location(
                posting,
                registry,
            )
        )


class CollectionStatusTests(unittest.TestCase):
    def test_latest_run_is_exposed_for_dashboard(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_source_collection_table(connection)
        source = PublicSource(
            id="example_greenhouse",
            company="Example",
            adapter="greenhouse",
            identifier="example",
        )
        registry = SourceRegistry(
            country_codes=frozenset({"IL"}),
            location_terms=("israel",),
            sources=(source,),
        )
        run_id = start_collection_run(
            connection,
            source,
        )
        finish_collection_run(
            connection,
            run_id=run_id,
            status="success",
            postings_seen=10,
            postings_relevant=3,
            new_jobs=2,
            new_postings=3,
        )

        statuses = get_collection_statuses(
            connection,
            registry,
        )

        self.assertEqual(len(statuses), 1)
        self.assertEqual(
            statuses[0].status,
            "success",
        )
        self.assertEqual(
            statuses[0].postings_relevant,
            3,
        )
        connection.close()


class PublicCollectorSafetyTests(unittest.TestCase):
    @patch(
        "collect_public_jobs.collect_public_source",
        return_value=SourceCollection(
            postings_seen=2,
            relevant_jobs=(),
        ),
    )
    @patch("collect_public_jobs.connect_database")
    def test_dry_run_never_opens_sqlite(
        self,
        connect_database,
        _collect_public_source,
    ) -> None:
        source = PublicSource(
            id="example_greenhouse",
            company="Example",
            adapter="greenhouse",
            identifier="example",
        )
        registry = SourceRegistry(
            country_codes=frozenset({"IL"}),
            location_terms=("israel",),
            sources=(source,),
        )

        with tempfile.TemporaryDirectory() as directory:
            progress_path = (
                Path(directory) / "progress.json"
            )
            summaries = run_collection(
                registry=registry,
                sources=(source,),
                dry_run=True,
                limit_per_source=None,
                progress_file=progress_path,
            )
            progress = progress_path.read_text(
                encoding="utf-8"
            )

        connect_database.assert_not_called()
        self.assertEqual(
            summaries[0].postings_seen,
            2,
        )
        self.assertIn(
            '"stage_key":"public_source_collection"',
            progress,
        )
        self.assertIn(
            '"progress_completed":1',
            progress,
        )


if __name__ == "__main__":
    unittest.main()
