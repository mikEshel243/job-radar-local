import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import (
    Mock,
    patch,
)

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from database import initialize_database  # noqa: E402
from fetch_job_details import (  # noqa: E402
    fetch_job,
    fill_missing_job_metadata,
    main,
)
from job_details import JobDetails  # noqa: E402


def make_response(
    *,
    url: str,
    status_code: int = 200,
    content_type: str = "text/html",
    body: str = "",
) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    response.headers["Content-Type"] = content_type
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    response.raw = Mock()
    return response


class FetchJobDetailsTests(unittest.TestCase):
    def test_main_reports_exact_job_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.db"
            progress_path = Path(directory) / "progress.json"
            setup_connection = sqlite3.connect(database_path)
            setup_connection.row_factory = sqlite3.Row
            initialize_database(setup_connection)
            cursor = setup_connection.execute(
                """
                INSERT INTO jobs (
                    dedupe_key,
                    title,
                    company,
                    job_url
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    "progress-fixture",
                    "Backend Engineer",
                    "Example",
                    "https://jobs.example.com/1",
                ),
            )
            job_id = int(cursor.lastrowid)
            setup_connection.commit()
            setup_connection.close()

            def connect_fixture_database():
                connection = sqlite3.connect(database_path)
                connection.row_factory = sqlite3.Row
                return connection

            result = JobDetails(
                job_id=job_id,
                final_url="https://jobs.example.com/1",
                page_title="Backend Engineer",
                description_text="A" * 250,
                extractor="html-fallback",
                fetch_status="success",
                fetch_error=None,
                http_status=200,
            )
            session = Mock()

            with (
                patch(
                    "fetch_job_details.parse_arguments",
                    return_value=SimpleNamespace(
                        limit=10,
                        delay=0,
                        retry_failed=False,
                        progress_file=progress_path,
                    ),
                ),
                patch(
                    "fetch_job_details.connect_database",
                    side_effect=connect_fixture_database,
                ),
                patch(
                    "fetch_job_details.requests.Session",
                    return_value=session,
                ),
                patch(
                    "fetch_job_details.fetch_job",
                    return_value=result,
                ),
                patch("builtins.print"),
            ):
                main()

            progress = json.loads(
                progress_path.read_text(encoding="utf-8")
            )

        self.assertEqual(
            progress,
            {
                "stage_key": "job_page_enrichment",
                "progress_mode": "determinate",
                "progress_completed": 1,
                "progress_total": 1,
                "progress_unit": "jobs",
            },
        )

    @patch(
        "fetch_job_details.is_public_web_url",
        return_value=True,
    )
    def test_hireme_fetches_only_original_company_page(
        self,
        public_url_check: Mock,
    ) -> None:
        hireme_url = (
            "https://hiremetech.com/job/142029757"
        )
        company_url = (
            "https://company.example/careers/"
            "backend-engineer"
        )
        resolver_response = make_response(
            url=(
                "https://hiremetech.com/api/jobs/"
                "142029757"
            ),
            content_type="application/json",
            body=json.dumps(
                {
                    "job": {
                        "job_url": company_url,
                        "description": (
                            "Aggregator description must "
                            "not be used."
                        ),
                    }
                }
            ),
        )
        company_description = (
            "Original employer description. "
            "Python Spark ETL GCP CI/CD. "
        ) * 12
        company_response = make_response(
            url=company_url,
            body=(
                "<html><head>"
                "<script type=\"application/ld+json\">"
                + json.dumps(
                    {
                        "@graph": [
                            {
                                "@type": "Organization",
                                "name": "Original Company",
                                "url": (
                                    "https://company.example"
                                ),
                            }
                        ]
                    }
                )
                + "</script><title>"
                "Backend Engineer | Company"
                "</title></head><body><main>"
                "<div class=\"location\">Example City</div>"
                f"{company_description}"
                "</main></body></html>"
            ),
        )
        session = Mock(spec=requests.Session)
        session.get.side_effect = [
            resolver_response,
            company_response,
        ]

        result = fetch_job(
            session=session,
            job_id=42,
            job_url=hireme_url,
        )

        self.assertEqual(
            result.fetch_status,
            "success",
        )
        self.assertEqual(
            result.final_url,
            company_url,
        )
        self.assertEqual(
            result.extractor,
            "original-source/html-fallback",
        )
        self.assertEqual(
            result.resolved_company,
            "Original Company",
        )
        self.assertEqual(
            result.resolved_location,
            "Example City",
        )
        self.assertIn(
            "Original employer description",
            result.description_text or "",
        )
        self.assertNotIn(
            "Aggregator description",
            result.description_text or "",
        )
        self.assertEqual(
            [
                call.args[0]
                for call in session.get.call_args_list
            ],
            [
                (
                    "https://hiremetech.com/api/jobs/"
                    "142029757"
                ),
                company_url,
            ],
        )
        self.assertNotIn(
            hireme_url,
            [
                call.args[0]
                for call in session.get.call_args_list
            ],
        )
        self.assertGreaterEqual(
            public_url_check.call_count,
            2,
        )

    @patch(
        "fetch_job_details.is_public_web_url",
        return_value=True,
    )
    def test_hireme_requires_an_original_url(
        self,
        public_url_check: Mock,
    ) -> None:
        resolver_response = make_response(
            url=(
                "https://hiremetech.com/api/jobs/7"
            ),
            content_type="application/json",
            body=json.dumps(
                {
                    "job": {
                        "description": (
                            "Aggregator-only description"
                        ),
                    }
                }
            ),
        )
        session = Mock(spec=requests.Session)
        session.get.return_value = (
            resolver_response
        )

        result = fetch_job(
            session=session,
            job_id=7,
            job_url=(
                "https://hiremetech.com/job/7"
            ),
        )

        self.assertEqual(
            result.fetch_status,
            "source_resolution_error",
        )
        self.assertIsNone(
            result.description_text
        )
        self.assertIn(
            "original employer URL",
            result.fetch_error or "",
        )
        self.assertEqual(
            session.get.call_count,
            1,
        )
        public_url_check.assert_called_once()

    @patch(
        "fetch_job_details.is_public_web_url",
        return_value=True,
    )
    def test_hireme_rejects_another_intermediary(
        self,
        public_url_check: Mock,
    ) -> None:
        resolver_response = make_response(
            url=(
                "https://hiremetech.com/api/jobs/8"
            ),
            content_type="application/json",
            body=json.dumps(
                {
                    "job": {
                        "job_url": (
                            "https://www.linkedin.com/"
                            "jobs/view/8"
                        ),
                    }
                }
            ),
        )
        session = Mock(spec=requests.Session)
        session.get.return_value = (
            resolver_response
        )

        result = fetch_job(
            session=session,
            job_id=8,
            job_url=(
                "https://hiremetech.com/job/8"
            ),
        )

        self.assertEqual(
            result.fetch_status,
            "source_resolution_error",
        )
        self.assertIn(
            "another listing intermediary",
            result.fetch_error or "",
        )
        self.assertEqual(
            session.get.call_count,
            1,
        )
        public_url_check.assert_called_once()

    def test_linkedin_job_page_is_never_fetched(
        self,
    ) -> None:
        session = Mock(spec=requests.Session)

        result = fetch_job(
            session=session,
            job_id=81,
            job_url=(
                "https://www.linkedin.com/jobs/view/123456789"
            ),
        )

        self.assertEqual(
            result.fetch_status,
            "source_automation_prohibited",
        )
        self.assertIsNone(result.description_text)
        session.get.assert_not_called()

    def test_amazon_job_page_is_never_fetched(
        self,
    ) -> None:
        session = Mock(spec=requests.Session)

        result = fetch_job(
            session=session,
            job_id=82,
            job_url=(
                "https://www.amazon.jobs/jobs/12345001"
            ),
        )

        self.assertEqual(
            result.fetch_status,
            "source_automation_prohibited",
        )
        self.assertIsNone(result.description_text)
        session.get.assert_not_called()

    @patch(
        "fetch_job_details.is_public_web_url",
        side_effect=[True, False],
    )
    def test_rejects_a_redirect_to_a_private_target(
        self,
        public_url_check: Mock,
    ) -> None:
        redirect_response = make_response(
            url=(
                "https://company.example/job/9"
            ),
            status_code=302,
        )
        redirect_response.headers["Location"] = (
            "http://127.0.0.1/private"
        )
        session = Mock(spec=requests.Session)
        session.get.return_value = (
            redirect_response
        )

        result = fetch_job(
            session=session,
            job_id=9,
            job_url=(
                "https://company.example/job/9"
            ),
        )

        self.assertEqual(
            result.fetch_status,
            "unsafe_url",
        )
        self.assertIsNone(
            result.description_text
        )
        self.assertEqual(
            session.get.call_count,
            1,
        )
        self.assertEqual(
            public_url_check.call_count,
            2,
        )

    def test_original_metadata_only_fills_blank_fields(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        cursor = connection.execute(
            """
            INSERT INTO jobs (
                dedupe_key,
                title,
                company,
                location
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                "fixture",
                "Backend Engineer",
                "Existing Company",
                None,
            ),
        )
        job_id = int(cursor.lastrowid)
        result = JobDetails(
            job_id=job_id,
            final_url=(
                "https://company.example/job/42"
            ),
            page_title="Backend Engineer",
            description_text="Description",
            extractor="html-fallback",
            fetch_status="success",
            fetch_error=None,
            http_status=200,
            resolved_company="Different Company",
            resolved_location="Example City",
        )

        fill_missing_job_metadata(
            connection,
            result,
        )

        row = connection.execute(
            """
            SELECT company, location
            FROM jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        self.assertEqual(
            dict(row),
            {
                "company": "Existing Company",
                "location": "Example City",
            },
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
