import json
import sqlite3
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


import job_filter  # noqa: E402
from job_filter import (  # noqa: E402
    PREFERENCE_CRITERIA,
    classify_location_preference,
    deduplicate_terms,
    ensure_evaluation_table,
    evaluate_job,
    get_preference_settings,
    load_profile,
    update_profile_preferences,
    validate_v2_profile,
)


def make_job(**overrides):
    job = {
        "id": 1,
        "title": "Platform Developer",
        "company": "Example",
        "location": "Example City",
        "job_url": "https://jobs.example.com/role/1",
        "experience_min": 2,
        "experience_max": 3,
        "experience_label": "junior-to-mid",
        "technologies_json": json.dumps(
            ["Python", "FastAPI", "PostgreSQL"]
        ),
        "seniority_signals_json": json.dumps(
            ["junior"]
        ),
        "education_signals_json": json.dumps(
            ["degree-mentioned"]
        ),
        "analysis_confidence": 0.9,
        "description_text": (
            "Hybrid platform development with Python."
        ),
    }
    job.update(overrides)
    return job


class WeightedPreferenceScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_profile()

    def test_profile_exposes_seven_editable_preferences(
        self,
    ) -> None:
        settings = get_preference_settings(
            self.profile
        )

        self.assertEqual(
            [item["id"] for item in settings["criteria"]],
            list(PREFERENCE_CRITERIA),
        )
        self.assertEqual(settings["weight_min"], 1)
        self.assertEqual(settings["weight_max"], 5)
        self.assertTrue(
            all(
                "selection_summary" in item
                for item in settings["criteria"]
            )
        )

    def test_preference_terms_are_deduplicated_safely(
        self,
    ) -> None:
        self.assertEqual(
            deduplicate_terms(
                (
                    "Example City",
                    " Sample Harbor ",
                    "Demo-Metro",
                    "demo metro",
                    "",
                )
            ),
            [
                "Example City",
                "Sample Harbor",
                "Demo-Metro",
            ],
        )

    def test_location_classification_uses_scoring_precedence(
        self,
    ) -> None:
        settings = {
            "preferred_keywords": [
                "remote",
            ],
            "acceptable_keywords": [
                "exampleland",
            ],
            "neutral_keywords": [],
            "excluded_keywords": [
                "outside region",
            ],
        }

        self.assertEqual(
            classify_location_preference(
                "Remote, Exampleland",
                settings,
            ),
            "preferred",
        )
        self.assertEqual(
            classify_location_preference(
                "Outside Region",
                settings,
            ),
            "excluded",
        )
        self.assertEqual(
            classify_location_preference(
                "Athens",
                settings,
            ),
            "other",
        )

    def test_excluded_technology_reduces_fit(
        self,
    ) -> None:
        profile = deepcopy(self.profile)
        technology = profile["criteria"]["technology"]
        technology["positive_technology_keywords"] = {}
        technology["negative_technology_keywords"] = {
            "Python": -30,
        }
        evaluation = evaluate_job(
            make_job(
                technologies_json=json.dumps(
                    ["Python"]
                )
            ),
            profile,
        )
        component = next(
            item
            for item in evaluation.score_components
            if item["criterion"] == "technology"
        )

        self.assertEqual(
            component["status"],
            "not preferred",
        )
        self.assertLess(component["fit"], 0)

    def test_strong_role_and_location_match_is_high(
        self,
    ) -> None:
        evaluation = evaluate_job(
            make_job(),
            self.profile,
        )

        self.assertEqual(
            evaluation.match_bucket,
            "high_match",
        )
        self.assertGreaterEqual(
            evaluation.match_score,
            self.profile["thresholds"]["high_match"],
        )
        self.assertEqual(
            evaluation.scoring_model,
            "weighted_preferences_v2",
        )
        self.assertEqual(
            evaluation.profile_schema_version,
            2,
        )

    def test_excluded_location_is_a_hard_rejection(
        self,
    ) -> None:
        evaluation = evaluate_job(
            make_job(location="Outside Region"),
            self.profile,
        )

        self.assertEqual(
            evaluation.match_bucket,
            "rejected",
        )
        self.assertTrue(
            any(
                "Hard rejection: Excluded location."
                in reason
                for reason in evaluation.reasons
            )
        )

    def test_location_alone_cannot_make_irrelevant_role_high(
        self,
    ) -> None:
        evaluation = evaluate_job(
            make_job(
                title="Product Designer",
                technologies_json="[]",
                experience_min=None,
                experience_max=None,
                experience_label="not-specified",
                seniority_signals_json="[]",
                education_signals_json="[]",
                analysis_confidence=0.0,
            ),
            self.profile,
        )

        self.assertNotEqual(
            evaluation.match_bucket,
            "high_match",
        )
        self.assertTrue(
            any(
                "Role and professional domain"
                in reason
                and "Required for a high match"
                in reason
                for reason in evaluation.reasons
            )
        )

    def test_any_required_preference_can_cap_high_match(
        self,
    ) -> None:
        profile = deepcopy(self.profile)
        profile["criteria"]["technology"][
            "required_for_high_match"
        ] = True
        evaluation = evaluate_job(
            make_job(technologies_json="[]"),
            profile,
        )

        self.assertNotEqual(
            evaluation.match_bucket,
            "high_match",
        )
        self.assertTrue(
            any(
                "Technologies"
                in reason
                and "Required for a high match"
                in reason
                for reason in evaluation.reasons
            )
        )

    def test_higher_location_weight_increases_unknown_penalty(
        self,
    ) -> None:
        low_location = deepcopy(self.profile)
        high_location = deepcopy(self.profile)
        low_location["criteria"]["location"]["weight"] = 1
        high_location["criteria"]["location"]["weight"] = 5
        job = make_job(location="Undisclosed")

        low_score = evaluate_job(
            job,
            low_location,
        ).match_score
        high_score = evaluate_job(
            job,
            high_location,
        ).match_score

        self.assertLess(high_score, low_score)

    def test_components_reconstruct_the_bounded_score(
        self,
    ) -> None:
        evaluation = evaluate_job(
            make_job(),
            self.profile,
        )
        raw_score = 50 + sum(
            float(item["contribution"])
            for item in evaluation.score_components
        )

        self.assertEqual(
            evaluation.match_score,
            max(0, min(100, round(raw_score))),
        )
        self.assertEqual(
            {
                item["criterion"]
                for item in evaluation.score_components
                if item["criterion"] != "interaction"
            },
            set(PREFERENCE_CRITERIA),
        )

    def test_missing_data_remains_bounded_and_explained(
        self,
    ) -> None:
        evaluation = evaluate_job(
            make_job(
                title="Developer",
                location="",
                experience_min=None,
                experience_max=None,
                experience_label="not-specified",
                technologies_json="[]",
                seniority_signals_json="[]",
                education_signals_json="[]",
                analysis_confidence=0.0,
                description_text="",
            ),
            self.profile,
        )

        self.assertGreaterEqual(
            evaluation.match_score,
            0,
        )
        self.assertLessEqual(
            evaluation.match_score,
            100,
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in evaluation.score_components
                    if item["criterion"] != "interaction"
                ]
            ),
            len(PREFERENCE_CRITERIA),
        )


class PreferencePersistenceTests(unittest.TestCase):
    def test_updates_are_validated_and_written_atomically(
        self,
    ) -> None:
        profile = load_profile()

        with tempfile.TemporaryDirectory() as directory:
            profile_path = (
                Path(directory) / "job_profile.json"
            )
            profile_path.write_text(
                json.dumps(profile),
                encoding="utf-8",
            )
            updates = {
                "criteria": [
                    {
                        "id": criterion,
                        "weight": (
                            4
                            if criterion == "location"
                            else 2
                        ),
                        "required_for_high_match": (
                            criterion == "location"
                        ),
                        **(
                            {
                                "selection_summary": {
                                    "preferred": [
                                        "Example City",
                                        "Sample Harbor",
                                    ],
                                    "acceptable": [
                                        "Demo Metro",
                                    ],
                                    "neutral": [
                                        "Fictional Capital",
                                    ],
                                    "excluded": [
                                        "Outside Region",
                                    ],
                                }
                            }
                            if criterion == "location"
                            else {}
                        ),
                    }
                    for criterion in PREFERENCE_CRITERIA
                ]
            }

            with patch.object(
                job_filter,
                "PROFILE_PATH",
                profile_path,
            ):
                updated = update_profile_preferences(
                    updates
                )

            saved = json.loads(
                profile_path.read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                updated["criteria"]["location"]["weight"],
                4,
            )
            self.assertTrue(
                saved["criteria"]["location"][
                    "required_for_high_match"
                ]
            )
            self.assertEqual(
                saved["criteria"]["location"][
                    "preferred_keywords"
                ],
                [
                    "Example City",
                    "Sample Harbor",
                ],
            )
            self.assertEqual(
                saved["criteria"]["location"][
                    "neutral_keywords"
                ],
                [
                    "Fictional Capital",
                ],
            )
            self.assertEqual(
                saved["criteria"]["location"][
                    "fit_values"
                ],
                profile["criteria"]["location"][
                    "fit_values"
                ],
            )
            self.assertFalse(
                profile_path.with_suffix(
                    ".json.tmp"
                ).exists()
            )

    def test_all_preference_details_are_editable(
        self,
    ) -> None:
        profile = load_profile()
        settings = get_preference_settings(profile)
        updates: list[dict[str, object]] = []

        for item in settings["criteria"]:
            criterion = item["id"]
            summary = deepcopy(
                item["selection_summary"]
            )

            if criterion == "role_domain":
                summary["preferred"].remove(
                    "platform developer"
                )
                summary["excluded"].append(
                    "platform developer"
                )
                summary["neutral"].append(
                    summary["preferred"].pop()
                )
            elif criterion == "technology":
                summary["preferred"].remove("Python")
                summary["excluded"].append("Python")
                summary["preferred"].remove("Git")
                summary["neutral"].append("Git")
            elif criterion == "experience":
                summary.update(
                    {
                        "preferred_maximum_years": 2,
                        "acceptable_maximum_years": 4,
                        "excluded_minimum_years": 6,
                        "excluded_policy": "penalty",
                    }
                )
            elif criterion == "seniority":
                summary["preferred"].remove("associate")
                summary["excluded"].append("associate")
                summary["student_roles_allowed"] = True
                summary["excluded_policy"] = "penalty"
            elif criterion == "education":
                summary["active_student_policy"] = "neutral"
            elif criterion == "work_model":
                summary["preferred"].remove("remote")
                summary["acceptable"].append("remote")
                summary["excluded_policy"] = "reject"

            updates.append(
                {
                    "id": criterion,
                    "weight": item["weight"],
                    "required_for_high_match": item[
                        "required_for_high_match"
                    ],
                    "selection_summary": summary,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            profile_path = (
                Path(directory) / "job_profile.json"
            )
            profile_path.write_text(
                json.dumps(profile),
                encoding="utf-8",
            )

            with patch.object(
                job_filter,
                "PROFILE_PATH",
                profile_path,
            ):
                updated = update_profile_preferences(
                    {"criteria": updates}
                )

        role = updated["criteria"]["role_domain"]
        technology = updated["criteria"]["technology"]
        experience = updated["criteria"]["experience"]
        seniority = updated["criteria"]["seniority"]
        education = updated["criteria"]["education"]
        work_model = updated["criteria"]["work_model"]

        self.assertEqual(
            role["negative_title_keywords"][
                "platform developer"
            ],
            -45,
        )
        self.assertNotIn(
            "platform developer",
            role["positive_title_keywords"],
        )
        self.assertEqual(
            technology[
                "negative_technology_keywords"
            ]["Python"],
            -18,
        )
        self.assertEqual(
            technology[
                "neutral_technology_keywords"
            ]["Git"],
            4,
        )
        self.assertEqual(
            experience["preferred_maximum"],
            2,
        )
        self.assertEqual(
            experience["excluded_minimum"],
            6,
        )
        self.assertTrue(
            seniority["allow_student_roles"]
        )
        self.assertIn(
            "associate",
            seniority["excluded_title_keywords"],
        )
        self.assertEqual(
            education["active_student_policy"],
            "neutral",
        )
        self.assertIn(
            "remote",
            work_model["acceptable_keywords"],
        )
        self.assertEqual(
            work_model["excluded_policy"],
            "reject",
        )

    def test_rejects_invalid_or_incomplete_updates(
        self,
    ) -> None:
        with self.assertRaises(RuntimeError):
            update_profile_preferences(
                {
                    "criteria": [
                        {
                            "id": "location",
                            "weight": 6,
                            "required_for_high_match": True,
                        }
                    ]
                }
            )

    def test_rejects_location_in_multiple_preference_levels(
        self,
    ) -> None:
        settings = get_preference_settings(
            load_profile()
        )
        updates = {
            "criteria": [
                {
                    "id": item["id"],
                    "weight": item["weight"],
                    "required_for_high_match": item[
                        "required_for_high_match"
                    ],
                    **(
                        {
                            "selection_summary": {
                                "preferred": [
                                    "Demo-Metro",
                                ],
                                "acceptable": [
                                    "demo metro",
                                ],
                                "neutral": [],
                                "excluded": [],
                            }
                        }
                        if item["id"] == "location"
                        else {}
                    ),
                }
                for item in settings["criteria"]
            ]
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "more than one preference level",
        ):
            update_profile_preferences(updates)

    def test_rejects_invalid_threshold_order(
        self,
    ) -> None:
        profile = load_profile()
        profile["thresholds"] = {
            "review": 80,
            "high_match": 70,
        }

        with self.assertRaises(RuntimeError):
            validate_v2_profile(profile)


class EvaluationSchemaMigrationTests(unittest.TestCase):
    def test_additive_columns_preserve_legacy_rows(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE job_evaluations (
                job_id INTEGER PRIMARY KEY,
                profile_name TEXT NOT NULL,
                seniority_label TEXT NOT NULL,
                role_category TEXT NOT NULL,
                location_label TEXT NOT NULL,
                match_score INTEGER NOT NULL,
                match_bucket TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                evaluated_at TEXT NOT NULL
            );

            INSERT INTO job_evaluations (
                job_id,
                profile_name,
                seniority_label,
                role_category,
                location_label,
                match_score,
                match_bucket,
                reasons_json,
                evaluated_at
            )
            VALUES (
                1,
                'legacy',
                'junior',
                'backend',
                'preferred',
                80,
                'high_match',
                '[]',
                CURRENT_TIMESTAMP
            );
            """
        )

        ensure_evaluation_table(connection)

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(job_evaluations)"
            )
        }
        row = connection.execute(
            """
            SELECT
                score_components_json,
                profile_schema_version,
                scoring_model
            FROM job_evaluations
            WHERE job_id = 1
            """
        ).fetchone()

        self.assertIn("score_components_json", columns)
        self.assertIn("profile_schema_version", columns)
        self.assertIn("scoring_model", columns)
        self.assertEqual(row["score_components_json"], "[]")
        self.assertEqual(row["profile_schema_version"], 1)
        self.assertEqual(row["scoring_model"], "legacy_v1")
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM job_evaluations"
            ).fetchone()[0],
            1,
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
