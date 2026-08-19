import json
import os
import re
import sqlite3
import threading
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = (
    PROJECT_ROOT / "config" / "job_profile.local.json"
)
PROFILE_EXAMPLE_PATH = (
    PROJECT_ROOT / "config" / "job_profile.example.json"
)
PROFILE_SCHEMA_VERSION = 2
SCORING_MODEL_V2 = "weighted_preferences_v2"
PREFERENCE_WEIGHT_MIN = 1
PREFERENCE_WEIGHT_MAX = 5
PREFERENCE_TERM_CATEGORIES = {
    "role_domain": (
        "preferred",
        "neutral",
        "excluded",
    ),
    "location": (
        "preferred",
        "acceptable",
        "neutral",
        "excluded",
        "unclassified",
    ),
    "technology": (
        "preferred",
        "neutral",
        "excluded",
    ),
    "seniority": (
        "preferred",
        "student",
        "excluded",
    ),
    "work_model": (
        "preferred",
        "acceptable",
        "excluded",
    ),
}
MAX_PREFERENCE_TERMS = 500
MAX_PREFERENCE_TERM_LENGTH = 120
MAX_EXPERIENCE_YEARS = 50
PREFERENCE_CRITERIA = (
    "role_domain",
    "location",
    "technology",
    "experience",
    "seniority",
    "education",
    "work_model",
)
PREFERENCE_LABELS = {
    "role_domain": "Role and professional domain",
    "location": "Location",
    "technology": "Technologies",
    "experience": "Experience requirement",
    "seniority": "Seniority",
    "education": "Education",
    "work_model": "Work model",
}
PROFILE_WRITE_LOCK = threading.Lock()


@dataclass
class JobEvaluation:
    job_id: int
    profile_name: str
    seniority_label: str
    role_category: str
    location_label: str
    match_score: int
    match_bucket: str
    reasons: list[str]
    score_components: list[dict[str, Any]]
    profile_schema_version: int
    scoring_model: str


def _require_mapping(
    value: Any,
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(
            f"{field_name} must be an object."
        )

    return value


def validate_v2_profile(
    profile: dict[str, Any],
) -> None:
    """Validate the configurable weighted-preference profile."""

    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise RuntimeError(
            "Unsupported profile schema version."
        )

    if profile.get("scoring_model") != SCORING_MODEL_V2:
        raise RuntimeError(
            "Unsupported scoring model."
        )

    if not str(profile.get("profile_name", "")).strip():
        raise RuntimeError(
            "profile_name must not be empty."
        )

    criteria = _require_mapping(
        profile.get("criteria"),
        "criteria",
    )
    missing_criteria = [
        criterion
        for criterion in PREFERENCE_CRITERIA
        if criterion not in criteria
    ]

    if missing_criteria:
        raise RuntimeError(
            "Missing preference criteria: "
            + ", ".join(missing_criteria)
        )

    for criterion in PREFERENCE_CRITERIA:
        settings = _require_mapping(
            criteria[criterion],
            f"criteria.{criterion}",
        )
        weight = settings.get("weight")

        if (
            isinstance(weight, bool)
            or not isinstance(weight, int)
            or not (
                PREFERENCE_WEIGHT_MIN
                <= weight
                <= PREFERENCE_WEIGHT_MAX
            )
        ):
            raise RuntimeError(
                f"criteria.{criterion}.weight must be "
                f"between {PREFERENCE_WEIGHT_MIN} and "
                f"{PREFERENCE_WEIGHT_MAX}."
            )

        if not isinstance(
            settings.get("required_for_high_match"),
            bool,
        ):
            raise RuntimeError(
                "criteria."
                f"{criterion}.required_for_high_match "
                "must be boolean."
            )

    thresholds = _require_mapping(
        profile.get("thresholds"),
        "thresholds",
    )

    try:
        high_match = int(thresholds["high_match"])
        review = int(thresholds["review"])
    except (
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise RuntimeError(
            "Profile thresholds must be whole numbers."
        ) from error

    if not 0 <= review < high_match <= 100:
        raise RuntimeError(
            "Profile thresholds must satisfy "
            "0 <= review < high_match <= 100."
        )

    interactions = profile.get("interactions", [])

    if not isinstance(interactions, list):
        raise RuntimeError(
            "interactions must be a list."
        )

    for index, interaction in enumerate(interactions):
        item = _require_mapping(
            interaction,
            f"interactions[{index}]",
        )
        criteria_names = item.get("criteria")

        if (
            not isinstance(criteria_names, list)
            or len(criteria_names) < 2
            or any(
                name not in PREFERENCE_CRITERIA
                for name in criteria_names
            )
        ):
            raise RuntimeError(
                f"interactions[{index}].criteria "
                "contains invalid criteria."
            )

        bonus = item.get("bonus")

        if (
            isinstance(bonus, bool)
            or not isinstance(bonus, int)
            or not -10 <= bonus <= 10
        ):
            raise RuntimeError(
                f"interactions[{index}].bonus must be "
                "between -10 and 10."
            )


def _validate_legacy_profile(
    profile: dict[str, Any],
) -> None:
    required_fields = [
        "profile_name",
        "preferred_location_keywords",
        "allowed_location_keywords",
        "blocked_location_keywords",
        "allow_student_roles",
        "max_preferred_experience",
        "max_review_experience",
        "hard_reject_experience",
        "minimum_analysis_confidence",
        "hard_reject_seniority",
        "junior_keywords",
        "student_keywords",
        "positive_title_keywords",
        "negative_title_keywords",
        "positive_technology_keywords",
        "technology_score_cap",
        "thresholds",
    ]
    missing_fields = [
        field
        for field in required_fields
        if field not in profile
    ]

    if missing_fields:
        raise RuntimeError(
            "Missing profile fields: "
            + ", ".join(missing_fields)
        )


def load_profile() -> dict[str, Any]:
    """Load and validate the local matching profile."""

    profile_path = (
        PROFILE_PATH
        if PROFILE_PATH.exists()
        else PROFILE_EXAMPLE_PATH
    )

    if not profile_path.exists():
        raise RuntimeError(
            "Neither the local profile nor the example profile "
            "exists."
        )

    with profile_path.open(
        "r",
        encoding="utf-8",
    ) as profile_file:
        profile = json.load(profile_file)

    if not isinstance(profile, dict):
        raise RuntimeError(
            "Profile root must be an object."
        )

    if profile.get("schema_version") is None:
        _validate_legacy_profile(profile)
    else:
        validate_v2_profile(profile)

    return profile


def get_preference_settings(
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Return only safe, user-editable preference settings."""

    validate_v2_profile(profile)
    criteria = profile["criteria"]

    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "scoring_model": SCORING_MODEL_V2,
        "weight_min": PREFERENCE_WEIGHT_MIN,
        "weight_max": PREFERENCE_WEIGHT_MAX,
        "criteria": [
            {
                "id": criterion,
                "label": PREFERENCE_LABELS[criterion],
                "weight": int(
                    criteria[criterion]["weight"]
                ),
                "required_for_high_match": bool(
                    criteria[criterion][
                        "required_for_high_match"
                    ]
                ),
                "selection_summary":
                    _preference_selection_summary(
                        criterion,
                        criteria[criterion],
                    ),
            }
            for criterion in PREFERENCE_CRITERIA
        ],
    }


def update_profile_preferences(
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Validate and atomically persist editable preference settings."""

    if not isinstance(updates, dict):
        raise RuntimeError(
            "Preference update must be an object."
        )

    requested = updates.get("criteria")

    if not isinstance(requested, list):
        raise RuntimeError(
            "Preference criteria must be a list."
        )

    by_id: dict[str, dict[str, Any]] = {}

    for item in requested:
        if not isinstance(item, dict):
            raise RuntimeError(
                "Each preference must be an object."
            )

        criterion = str(item.get("id", "")).strip()

        if (
            criterion not in PREFERENCE_CRITERIA
            or criterion in by_id
        ):
            raise RuntimeError(
                "Preference criteria contain an invalid "
                "or duplicate identifier."
            )

        weight = item.get("weight")
        required = item.get(
            "required_for_high_match"
        )

        if (
            isinstance(weight, bool)
            or not isinstance(weight, int)
            or not (
                PREFERENCE_WEIGHT_MIN
                <= weight
                <= PREFERENCE_WEIGHT_MAX
            )
        ):
            raise RuntimeError(
                f"Preference weights must be between "
                f"{PREFERENCE_WEIGHT_MIN} and "
                f"{PREFERENCE_WEIGHT_MAX}."
            )

        if not isinstance(required, bool):
            raise RuntimeError(
                "Required preference flags must be boolean."
            )

        by_id[criterion] = {
            "weight": weight,
            "required_for_high_match": required,
        }

        selection_summary = item.get(
            "selection_summary"
        )

        if selection_summary is not None:
            by_id[criterion]["selection_summary"] = (
                _validate_selection_summary(
                    criterion,
                    selection_summary
                )
            )

    if set(by_id) != set(PREFERENCE_CRITERIA):
        raise RuntimeError(
            "Every preference criterion must be included."
        )

    with PROFILE_WRITE_LOCK:
        profile = load_profile()
        validate_v2_profile(profile)
        updated = deepcopy(profile)

        for criterion, values in by_id.items():
            updated["criteria"][criterion].update(
                {
                    "weight": values["weight"],
                    "required_for_high_match": values[
                        "required_for_high_match"
                    ],
                }
            )

            if "selection_summary" in values:
                _apply_selection_summary(
                    updated["criteria"][criterion],
                    criterion,
                    values["selection_summary"],
                )

        validate_v2_profile(updated)
        temporary_path = PROFILE_PATH.with_suffix(
            ".json.tmp"
        )

        try:
            temporary_path.write_text(
                json.dumps(
                    updated,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(
                temporary_path,
                PROFILE_PATH,
            )
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    return updated


def normalize_text(value: str | None) -> str:
    """Normalize text for reliable keyword matching."""

    if not value:
        return ""

    normalized = unicodedata.normalize(
        "NFKC",
        value,
    )

    normalized = normalized.casefold()

    normalized = re.sub(
        r"[’'`]",
        "",
        normalized,
    )

    normalized = re.sub(
        r"[-_/(),:+&]+",
        " ",
        normalized,
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


def deduplicate_terms(
    values: list[Any] | tuple[Any, ...],
) -> list[str]:
    """Return clean terms without case or punctuation duplicates."""

    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        display_value = re.sub(
            r"\s+",
            " ",
            unicodedata.normalize(
                "NFKC",
                str(value),
            ),
        ).strip()
        normalized_value = normalize_text(display_value)

        if (
            not normalized_value
            or normalized_value in seen
        ):
            continue

        seen.add(normalized_value)
        result.append(display_value)

    return result


def _validate_term_groups(
    criterion: str,
    selections: Any,
    optional_fields: tuple[str, ...] = (),
) -> dict[str, list[str]]:
    """Validate one set of mutually exclusive preference terms."""

    if not isinstance(selections, dict):
        raise RuntimeError(
            f"{criterion} selections must be an object."
        )

    if (
        criterion == "location"
        and "unclassified" not in selections
    ):
        selections = {
            **selections,
            "unclassified": [],
        }

    categories = PREFERENCE_TERM_CATEGORIES[criterion]
    keys = set(selections)

    if (
        not set(categories).issubset(keys)
        or not keys.issubset(
            set(categories) | set(optional_fields)
        )
    ):
        raise RuntimeError(
            f"Every {criterion} preference level must be included."
        )

    validated: dict[str, list[str]] = {}
    assigned_categories: dict[str, str] = {}
    term_count = 0

    for category in categories:
        raw_terms = selections[category]

        if not isinstance(raw_terms, list):
            raise RuntimeError(
                "Each preference level must be a list."
            )

        for term in raw_terms:
            if (
                not isinstance(term, str)
                or len(term) > MAX_PREFERENCE_TERM_LENGTH
            ):
                raise RuntimeError(
                    "Preference values must be text "
                    f"up to {MAX_PREFERENCE_TERM_LENGTH} "
                    "characters."
                )

        clean_terms = deduplicate_terms(raw_terms)
        term_count += len(clean_terms)

        if term_count > MAX_PREFERENCE_TERMS:
            raise RuntimeError(
                "Too many preference values."
            )

        for term in clean_terms:
            normalized = normalize_text(term)
            previous_category = assigned_categories.get(
                normalized
            )

            if (
                previous_category is not None
                and previous_category != category
            ):
                raise RuntimeError(
                    "A preference value cannot belong to more than "
                    "one preference level."
                )

            assigned_categories[normalized] = category

        validated[category] = clean_terms

    return validated


def _validate_policy(
    value: Any,
    field_name: str,
    allowed: tuple[str, ...],
) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise RuntimeError(
            f"{field_name} contains an invalid policy."
        )

    return value


def _validate_experience_selections(
    selections: Any,
) -> dict[str, Any]:
    fields = {
        "preferred_maximum_years",
        "acceptable_maximum_years",
        "excluded_minimum_years",
        "excluded_policy",
    }

    if (
        not isinstance(selections, dict)
        or set(selections) != fields
    ):
        raise RuntimeError(
            "Every experience preference must be included."
        )

    year_fields = (
        "preferred_maximum_years",
        "acceptable_maximum_years",
        "excluded_minimum_years",
    )
    years: dict[str, int] = {}

    for field in year_fields:
        value = selections[field]

        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_EXPERIENCE_YEARS
        ):
            raise RuntimeError(
                "Experience limits must be whole numbers "
                f"between 0 and {MAX_EXPERIENCE_YEARS}."
            )

        years[field] = value

    if not (
        years["preferred_maximum_years"]
        <= years["acceptable_maximum_years"]
        < years["excluded_minimum_years"]
    ):
        raise RuntimeError(
            "Experience limits must progress from preferred "
            "to acceptable to excluded."
        )

    return {
        **years,
        "excluded_policy": _validate_policy(
            selections["excluded_policy"],
            "Experience exclusion",
            ("reject", "penalty"),
        ),
    }


def _validate_selection_summary(
    criterion: str,
    selections: Any,
) -> dict[str, Any]:
    """Validate the user-editable details for one criterion."""

    if criterion == "experience":
        return _validate_experience_selections(
            selections
        )

    if criterion == "education":
        if (
            not isinstance(selections, dict)
            or set(selections)
            != {"active_student_policy"}
        ):
            raise RuntimeError(
                "Every education preference must be included."
            )

        return {
            "active_student_policy": _validate_policy(
                selections["active_student_policy"],
                "Active-student preference",
                ("reject", "penalty", "neutral"),
            )
        }

    optional_fields: tuple[str, ...] = ()

    if criterion in {
        "location",
        "work_model",
    }:
        optional_fields = ("excluded_policy",)
    elif criterion == "seniority":
        optional_fields = (
            "student_roles_allowed",
            "excluded_policy",
        )

    validated: dict[str, Any] = _validate_term_groups(
        criterion,
        selections,
        optional_fields,
    )

    if "excluded_policy" in selections:
        validated["excluded_policy"] = (
            _validate_policy(
                selections["excluded_policy"],
                f"{criterion} exclusion",
                ("reject", "penalty"),
            )
        )

    if criterion == "seniority":
        allowed = selections.get(
            "student_roles_allowed"
        )

        if not isinstance(allowed, bool):
            raise RuntimeError(
                "Student-role preference must be boolean."
            )

        validated["student_roles_allowed"] = allowed

    return validated


def _rebuild_scored_keyword_groups(
    settings: dict[str, Any],
    selections: dict[str, Any],
    fields: dict[str, str],
    default_magnitude: int,
) -> None:
    magnitudes: dict[str, int] = {}

    for field in fields.values():
        values = settings.get(field, {})

        if not isinstance(values, dict):
            continue

        for term, score in values.items():
            try:
                magnitude = abs(int(score))
            except (TypeError, ValueError):
                magnitude = default_magnitude

            magnitudes[normalize_text(str(term))] = max(
                1,
                magnitude,
            )

    for category, field in fields.items():
        sign = -1 if category == "excluded" else 1
        settings[field] = {
            term: (
                sign
                * magnitudes.get(
                    normalize_text(term),
                    default_magnitude,
                )
            )
            for term in selections[category]
        }


def _apply_selection_summary(
    settings: dict[str, Any],
    criterion: str,
    selections: dict[str, Any],
) -> None:
    """Apply validated preference details without replacing other settings."""

    if criterion == "role_domain":
        _rebuild_scored_keyword_groups(
            settings,
            selections,
            {
                "preferred": "positive_title_keywords",
                "neutral": "neutral_title_keywords",
                "excluded": "negative_title_keywords",
            },
            default_magnitude=10,
        )
        return

    if criterion == "technology":
        _rebuild_scored_keyword_groups(
            settings,
            selections,
            {
                "preferred":
                    "positive_technology_keywords",
                "neutral":
                    "neutral_technology_keywords",
                "excluded":
                    "negative_technology_keywords",
            },
            default_magnitude=8,
        )
        return

    if criterion == "location":
        for category in PREFERENCE_TERM_CATEGORIES[
            criterion
        ]:
            settings[f"{category}_keywords"] = (
                selections[category]
            )

        if "excluded_policy" in selections:
            settings["excluded_policy"] = selections[
                "excluded_policy"
            ]
        return

    if criterion == "experience":
        settings.update(
            {
                "preferred_maximum": selections[
                    "preferred_maximum_years"
                ],
                "acceptable_maximum": selections[
                    "acceptable_maximum_years"
                ],
                "excluded_minimum": selections[
                    "excluded_minimum_years"
                ],
                "excluded_policy": selections[
                    "excluded_policy"
                ],
            }
        )
        return

    if criterion == "seniority":
        field_by_category = {
            "preferred": "junior_keywords",
            "student": "student_keywords",
            "excluded": "excluded_title_keywords",
        }

        for category, field in field_by_category.items():
            settings[field] = selections[category]

        settings["allow_student_roles"] = selections[
            "student_roles_allowed"
        ]

        if "excluded_policy" in selections:
            settings["excluded_policy"] = selections[
                "excluded_policy"
            ]
        return

    if criterion == "education":
        settings["active_student_policy"] = selections[
            "active_student_policy"
        ]
        return

    if criterion == "work_model":
        for category in PREFERENCE_TERM_CATEGORIES[
            criterion
        ]:
            settings[f"{category}_keywords"] = (
                selections[category]
            )

        if "excluded_policy" in selections:
            settings["excluded_policy"] = selections[
                "excluded_policy"
            ]


def _preference_selection_summary(
    criterion: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Build a safe, allowlisted summary for the preferences UI."""

    if criterion == "role_domain":
        return {
            "preferred": deduplicate_terms(
                tuple(
                    settings.get(
                        "positive_title_keywords",
                        {},
                    )
                )
            ),
            "neutral": deduplicate_terms(
                tuple(
                    settings.get(
                        "neutral_title_keywords",
                        {},
                    )
                )
            ),
            "excluded": deduplicate_terms(
                tuple(
                    settings.get(
                        "negative_title_keywords",
                        {},
                    )
                )
            ),
        }

    if criterion == "location":
        return {
            category: deduplicate_terms(
                tuple(
                    settings.get(
                        f"{category}_keywords",
                        [],
                    )
                )
            )
            for category in PREFERENCE_TERM_CATEGORIES[
                criterion
            ]
        } | {
            "excluded_policy": str(
                settings.get(
                    "excluded_policy",
                    "reject",
                )
            )
        }

    if criterion == "technology":
        return {
            "preferred": deduplicate_terms(
                tuple(
                    settings.get(
                        "positive_technology_keywords",
                        {},
                    )
                )
            ),
            "neutral": deduplicate_terms(
                tuple(
                    settings.get(
                        "neutral_technology_keywords",
                        {},
                    )
                )
            ),
            "excluded": deduplicate_terms(
                tuple(
                    settings.get(
                        "negative_technology_keywords",
                        {},
                    )
                )
            ),
        }

    if criterion == "experience":
        return {
            "preferred_maximum_years": int(
                settings.get(
                    "preferred_maximum",
                    0,
                )
            ),
            "acceptable_maximum_years": int(
                settings.get(
                    "acceptable_maximum",
                    0,
                )
            ),
            "excluded_minimum_years": int(
                settings.get(
                    "excluded_minimum",
                    0,
                )
            ),
            "excluded_policy": str(
                settings.get(
                    "excluded_policy",
                    "penalty",
                )
            ),
        }

    if criterion == "seniority":
        return {
            "preferred": deduplicate_terms(
                tuple(
                    settings.get(
                        "junior_keywords",
                        [],
                    )
                )
            ),
            "student": deduplicate_terms(
                tuple(
                    settings.get(
                        "student_keywords",
                        [],
                    )
                )
            ),
            "excluded": deduplicate_terms(
                tuple(
                    settings.get(
                        "excluded_title_keywords",
                        [],
                    )
                )
            ),
            "student_roles_allowed": bool(
                settings.get(
                    "allow_student_roles",
                    False,
                )
            ),
            "excluded_policy": str(
                settings.get(
                    "excluded_policy",
                    "reject",
                )
            ),
        }

    if criterion == "education":
        return {
            "active_student_policy": str(
                settings.get(
                    "active_student_policy",
                    "neutral",
                )
            ),
        }

    if criterion == "work_model":
        return {
            category: deduplicate_terms(
                tuple(
                    settings.get(
                        f"{category}_keywords",
                        [],
                    )
                )
            )
            for category in (
                "preferred",
                "acceptable",
                "excluded",
            )
        } | {
            "excluded_policy": str(
                settings.get(
                    "excluded_policy",
                    "penalty",
                )
            )
        }

    return {}


def classify_location_preference(
    location: str,
    settings: dict[str, Any],
) -> str:
    """Classify one location using the same precedence as scoring."""

    normalized_location = normalize_text(location)

    for category in (
        "excluded",
        "preferred",
        "acceptable",
        "neutral",
    ):
        if find_matching_keywords(
            normalized_location,
            list(
                settings.get(
                    f"{category}_keywords",
                    [],
                )
            ),
        ):
            return category

    return "other"


def contains_keyword(
    text: str,
    keyword: str,
) -> bool:
    """Check whether a complete normalized word or phrase appears."""

    normalized_keyword = normalize_text(keyword)

    if not normalized_keyword:
        return False

    pattern = (
        r"(?<!\w)"
        + re.escape(normalized_keyword)
        + r"(?!\w)"
    )

    return re.search(
        pattern,
        text,
    ) is not None


def find_matching_keywords(
    text: str,
    keywords: list[str],
) -> list[str]:
    """Return matching keywords without exact normalized duplicates."""

    matches: list[str] = []
    seen: set[str] = set()

    for keyword in keywords:
        normalized_keyword = normalize_text(
            keyword
        )

        if not normalized_keyword:
            continue

        if normalized_keyword in seen:
            continue

        if contains_keyword(
            text,
            keyword,
        ):
            seen.add(normalized_keyword)
            matches.append(keyword)

    return matches


def score_keyword_map(
    text: str,
    keyword_scores: dict[str, int],
) -> tuple[int, list[str]]:
    """Calculate a score from matching keywords."""

    total_score = 0
    matches: list[str] = []
    seen: set[str] = set()

    for keyword, keyword_score in (
        keyword_scores.items()
    ):
        normalized_keyword = normalize_text(
            keyword
        )

        if not normalized_keyword:
            continue

        if normalized_keyword in seen:
            continue

        if contains_keyword(
            text,
            keyword,
        ):
            seen.add(normalized_keyword)
            total_score += int(keyword_score)
            matches.append(keyword)

    return total_score, matches


def parse_json_list(
    value: Any,
) -> list[str]:
    """Read a JSON list stored in SQLite safely."""

    if value is None:
        return []

    if isinstance(value, list):
        return [
            str(item)
            for item in value
        ]

    if not isinstance(value, str):
        return []

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []

    return [
        str(item)
        for item in parsed
    ]


def determine_role_category(
    title: str,
) -> str:
    """Assign a broad role category based on the title."""

    if find_matching_keywords(
        title,
        [
            "full stack",
            "fullstack",
        ],
    ):
        return "full-stack"

    if find_matching_keywords(
        title,
        [
            "java",
            "spring",
            "backend",
            "back end",
        ],
    ):
        return "backend"

    if find_matching_keywords(
        title,
        [
            "frontend",
            "front end",
            "react",
            "typescript",
        ],
    ):
        return "frontend"

    if find_matching_keywords(
        title,
        [
            "system software",
            "embedded",
            "networking",
            "linux",
        ],
    ):
        return "system-software"

    if contains_keyword(
        title,
        "software engineer",
    ):
        return "software-engineering"

    if contains_keyword(
        title,
        "software developer",
    ):
        return "software-development"

    if contains_keyword(
        title,
        "developer",
    ):
        return "development"

    return "other"


def score_technologies(
    technologies: list[str],
    profile: dict[str, Any],
) -> tuple[int, list[str]]:
    """Score canonical technologies extracted from the description."""

    technology_weights = profile[
        "positive_technology_keywords"
    ]

    score = 0
    matches: list[str] = []

    seen: set[str] = set()

    for technology in technologies:
        normalized = technology.casefold()

        if normalized in seen:
            continue

        seen.add(normalized)

        if technology not in technology_weights:
            continue

        score += int(
            technology_weights[technology]
        )

        matches.append(technology)

    score_cap = int(
        profile["technology_score_cap"]
    )

    return (
        min(score, score_cap),
        matches,
    )


def _evaluate_job_legacy(
    job: sqlite3.Row,
    profile: dict[str, Any],
) -> JobEvaluation:
    """Evaluate one job with the original fixed-point model."""

    job_id = int(job["id"])

    raw_title = job["title"] or ""
    raw_location = job["location"] or ""
    raw_job_url = job["job_url"] or ""

    title = normalize_text(raw_title)

    location_context = normalize_text(
        f"{raw_location} {raw_job_url}"
    )

    technologies = parse_json_list(
        job["technologies_json"]
    )

    analysis_seniority = parse_json_list(
        job["seniority_signals_json"]
    )

    education_signals = parse_json_list(
        job["education_signals_json"]
    )

    experience_min = job["experience_min"]
    experience_max = job["experience_max"]

    analysis_confidence = float(
        job["analysis_confidence"] or 0.0
    )

    score = 0
    reasons: list[str] = []

    hard_rejected = False
    review_ceiling = False

    title_senior_matches = (
        find_matching_keywords(
            title,
            profile[
                "hard_reject_seniority"
            ],
        )
    )

    title_student_matches = (
        find_matching_keywords(
            title,
            profile["student_keywords"],
        )
    )

    title_junior_matches = (
        find_matching_keywords(
            title,
            profile["junior_keywords"],
        )
    )

    if title_senior_matches:
        seniority_label = "senior-or-lead"
        hard_rejected = True
        score -= 100

        reasons.append(
            "Seniority too high in title: "
            + ", ".join(
                title_senior_matches
            )
        )

    elif title_student_matches:
        seniority_label = "student-or-intern"

        if profile["allow_student_roles"]:
            score += 10

            reasons.append(
                "Student role is allowed."
            )

        else:
            hard_rejected = True
            score -= 100

            reasons.append(
                "Student or internship role."
            )

    elif title_junior_matches:
        seniority_label = "junior"
        score += 20

        reasons.append(
            "Junior indicator in title: "
            + ", ".join(
                title_junior_matches
            )
        )

    else:
        seniority_label = (
            job["experience_label"]
            or "not-specified"
        )

        reasons.append(
            "Seniority is not explicit in the title."
        )

    if (
        "active-student" in education_signals
        and not profile["allow_student_roles"]
    ):
        hard_rejected = True
        score -= 100

        reasons.append(
            "Description requires an active student."
        )

    minimum_confidence = float(
        profile[
            "minimum_analysis_confidence"
        ]
    )

    analysis_is_reliable = (
        analysis_confidence
        >= minimum_confidence
    )

    if experience_min is not None:
        experience_min = int(experience_min)

        if experience_max is not None:
            experience_max = int(
                experience_max
            )

        if experience_max is None:
            experience_text = (
                f"{experience_min}+ years"
            )

        elif experience_min == experience_max:
            experience_text = (
                f"{experience_min} years"
            )

        else:
            experience_text = (
                f"{experience_min}-"
                f"{experience_max} years"
            )

        hard_reject_experience = int(
            profile[
                "hard_reject_experience"
            ]
        )

        max_review_experience = int(
            profile[
                "max_review_experience"
            ]
        )

        max_preferred_experience = int(
            profile[
                "max_preferred_experience"
            ]
        )

        if (
            experience_min
            >= hard_reject_experience
            and analysis_is_reliable
        ):
            hard_rejected = True
            score -= 100

            reasons.append(
                "Experience requirement too high: "
                + experience_text
            )

        elif (
            experience_min
            > max_review_experience
        ):
            review_ceiling = True
            score -= 30

            reasons.append(
                "High experience requirement "
                "with uncertain extraction: "
                + experience_text
            )

        elif (
            experience_min
            > max_preferred_experience
        ):
            review_ceiling = True
            score -= 10

            reasons.append(
                "Experience is above the "
                "preferred range: "
                + experience_text
            )

        elif experience_min == 0:
            score += 25

            reasons.append(
                "No previous experience required."
            )

        elif experience_min == 1:
            score += 22

            reasons.append(
                "Experience requirement fits: "
                + experience_text
            )

        elif experience_min == 2:
            score += 18

            reasons.append(
                "Experience requirement fits: "
                + experience_text
            )

        elif experience_min == 3:
            score += 10

            reasons.append(
                "Experience requirement is "
                "within range: "
                + experience_text
            )

    else:
        if "junior" in analysis_seniority:
            score += 8

            reasons.append(
                "Junior signal found in description."
            )

        if "senior" in analysis_seniority:
            score -= 15
            review_ceiling = True

            reasons.append(
                "Senior wording found in description; "
                "manual review is needed."
            )

        if analysis_confidence == 0:
            reasons.append(
                "Full job description has not "
                "been analyzed yet."
            )

    preferred_locations = (
        find_matching_keywords(
            location_context,
            profile[
                "preferred_location_keywords"
            ],
        )
    )

    allowed_locations = (
        find_matching_keywords(
            location_context,
            profile[
                "allowed_location_keywords"
            ],
        )
    )

    blocked_locations = (
        find_matching_keywords(
            location_context,
            profile[
                "blocked_location_keywords"
            ],
        )
    )

    if preferred_locations:
        location_label = "preferred"
        score += 15

        reasons.append(
            "Preferred location: "
            + ", ".join(
                preferred_locations
            )
        )

    elif allowed_locations:
        location_label = "allowed"
        score += 7

        reasons.append(
            "Location is acceptable: "
            + ", ".join(
                allowed_locations
            )
        )

    elif blocked_locations:
        location_label = "blocked"
        hard_rejected = True
        score -= 100

        reasons.append(
            "Location outside allowed area: "
            + ", ".join(
                blocked_locations
            )
        )

    else:
        location_label = "unknown"
        score -= 5

        reasons.append(
            "Location could not be classified."
        )

    (
        positive_title_score,
        positive_title_matches,
    ) = score_keyword_map(
        title,
        profile[
            "positive_title_keywords"
        ],
    )

    score += positive_title_score

    if positive_title_matches:
        reasons.append(
            "Relevant title keywords: "
            + ", ".join(
                positive_title_matches
            )
        )

    (
        negative_title_score,
        negative_title_matches,
    ) = score_keyword_map(
        title,
        profile[
            "negative_title_keywords"
        ],
    )

    score += negative_title_score

    if negative_title_matches:
        reasons.append(
            "Less relevant title keywords: "
            + ", ".join(
                negative_title_matches
            )
        )

    (
        technology_score,
        technology_matches,
    ) = score_technologies(
        technologies,
        profile,
    )

    score += technology_score

    if technology_matches:
        reasons.append(
            "Relevant technologies: "
            + ", ".join(
                technology_matches
            )
            + f" (+{technology_score})"
        )

    role_category = determine_role_category(
        title
    )

    thresholds = profile["thresholds"]

    high_match_threshold = int(
        thresholds["high_match"]
    )

    review_threshold = int(
        thresholds["review"]
    )

    if hard_rejected:
        match_bucket = "rejected"

    elif review_ceiling:
        if score >= review_threshold:
            match_bucket = "review"
        else:
            match_bucket = "rejected"

    elif score >= high_match_threshold:
        match_bucket = "high_match"

    elif score >= review_threshold:
        match_bucket = "review"

    else:
        match_bucket = "rejected"

    return JobEvaluation(
        job_id=job_id,
        profile_name=profile["profile_name"],
        seniority_label=seniority_label,
        role_category=role_category,
        location_label=location_label,
        match_score=score,
        match_bucket=match_bucket,
        reasons=reasons,
        score_components=[],
        profile_schema_version=1,
        scoring_model="legacy_v1",
    )


def _row_value(
    row: sqlite3.Row,
    field: str,
    default: Any = None,
) -> Any:
    try:
        value = row[field]
    except (
        IndexError,
        KeyError,
    ):
        return default

    return default if value is None else value


def _bounded_fit(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _matched_keyword_score(
    text: str,
    scores: dict[str, Any],
) -> tuple[int, list[str]]:
    normalized_scores = {
        str(keyword): int(value)
        for keyword, value in scores.items()
    }
    return score_keyword_map(
        text,
        normalized_scores,
    )


def _role_domain_component(
    title: str,
    settings: dict[str, Any],
) -> tuple[float, str, list[str], float, str | None]:
    positive_score, positive_matches = (
        _matched_keyword_score(
            title,
            settings.get(
                "positive_title_keywords",
                {},
            ),
        )
    )
    negative_score, negative_matches = (
        _matched_keyword_score(
            title,
            settings.get(
                "negative_title_keywords",
                {},
            ),
        )
    )
    score_cap = max(
        1,
        int(settings.get("score_cap", 45)),
    )
    fit = _bounded_fit(
        (
            max(0, positive_score)
            - abs(min(0, negative_score))
        )
        / score_cap
    )
    matches = [
        *positive_matches,
        *negative_matches,
    ]

    if not matches:
        fit = float(
            settings.get("unknown_fit", -0.5)
        )
        status = "not detected"
    elif fit > 0:
        status = "preferred"
    elif fit < 0:
        status = "outside preferred domain"
    else:
        status = "neutral"

    return fit, status, matches, 1.0, None


def _location_component(
    context: str,
    settings: dict[str, Any],
) -> tuple[float, str, list[str], float, str | None]:
    categories = (
        ("excluded", "excluded_keywords"),
        ("preferred", "preferred_keywords"),
        ("acceptable", "acceptable_keywords"),
        ("neutral", "neutral_keywords"),
    )
    fit_values = settings.get("fit_values", {})

    for label, field in categories:
        matches = find_matching_keywords(
            context,
            list(settings.get(field, [])),
        )

        if not matches:
            continue

        fit = float(
            fit_values.get(
                label,
                {
                    "preferred": 1.0,
                    "acceptable": 0.25,
                    "neutral": -0.35,
                    "excluded": -1.0,
                }[label],
            )
        )
        hard_reason = None

        if (
            label == "excluded"
            and settings.get("excluded_policy")
            == "reject"
        ):
            hard_reason = "Excluded location."

        return (
            _bounded_fit(fit),
            label,
            matches,
            1.0,
            hard_reason,
        )

    return (
        _bounded_fit(
            float(
                fit_values.get(
                    "unknown",
                    settings.get("unknown_fit", -0.25),
                )
            )
        ),
        "unknown",
        [],
        0.5,
        None,
    )


def _technology_component(
    technologies: list[str],
    confidence: float,
    settings: dict[str, Any],
) -> tuple[float, str, list[str], float, str | None]:
    positive_scores = settings.get(
        "positive_technology_keywords",
        {},
    )
    negative_scores = settings.get(
        "negative_technology_keywords",
        {},
    )
    matches: list[str] = []
    score = 0

    for technology in dict.fromkeys(technologies):
        if technology in positive_scores:
            score += int(
                positive_scores[technology]
            )
            matches.append(technology)
        elif technology in negative_scores:
            score += int(
                negative_scores[technology]
            )
            matches.append(technology)

    if not matches:
        fit = float(
            settings.get("unknown_fit", -0.15)
        )
        status = "not detected"
    else:
        score_cap = max(
            1,
            int(settings.get("score_cap", 45)),
        )
        raw_fit = _bounded_fit(
            score / score_cap
        )
        fit = raw_fit * (
            0.5
            + 0.5 * confidence
        )
        status = (
            "preferred"
            if fit > 0
            else "not preferred"
            if fit < 0
            else "neutral"
        )

    return (
        _bounded_fit(fit),
        status,
        matches,
        confidence,
        None,
    )


def _experience_component(
    experience_min: int | None,
    experience_max: int | None,
    analysis_seniority: list[str],
    confidence: float,
    settings: dict[str, Any],
) -> tuple[float, str, list[str], float, str | None]:
    if experience_min is None:
        if "junior" in analysis_seniority:
            return (
                0.5,
                "junior signal",
                ["junior"],
                confidence,
                None,
            )

        if "senior" in analysis_seniority:
            return (
                -0.75,
                "senior signal",
                ["senior"],
                confidence,
                None,
            )

        return (
            float(
                settings.get("unknown_fit", -0.2)
            ),
            "not detected",
            [],
            confidence,
            None,
        )

    minimum = int(experience_min)
    maximum = (
        int(experience_max)
        if experience_max is not None
        else None
    )
    requirement = (
        f"{minimum}+ years"
        if maximum is None
        else (
            f"{minimum} years"
            if minimum == maximum
            else f"{minimum}-{maximum} years"
        )
    )
    excluded_minimum = int(
        settings.get("excluded_minimum", 5)
    )
    acceptable_maximum = int(
        settings.get("acceptable_maximum", 4)
    )
    preferred_maximum = int(
        settings.get("preferred_maximum", 3)
    )
    reliable = confidence >= float(
        settings.get(
            "minimum_analysis_confidence",
            0.6,
        )
    )

    if minimum >= excluded_minimum:
        hard_reason = (
            "Experience requirement is excluded."
            if (
                reliable
                and settings.get("excluded_policy")
                == "reject"
            )
            else None
        )
        return (
            -1.0,
            "excluded",
            [requirement],
            confidence,
            hard_reason,
        )

    if minimum > acceptable_maximum:
        return (
            -0.65,
            "above acceptable range",
            [requirement],
            confidence,
            None,
        )

    if minimum > preferred_maximum:
        return (
            -0.25,
            "above preferred range",
            [requirement],
            confidence,
            None,
        )

    fit_by_minimum = {
        0: 1.0,
        1: 0.9,
        2: 0.8,
        3: 0.6,
    }

    return (
        fit_by_minimum.get(minimum, 0.4),
        "preferred",
        [requirement],
        confidence,
        None,
    )


def _seniority_component(
    title: str,
    experience_label: str,
    analysis_seniority: list[str],
    confidence: float,
    settings: dict[str, Any],
) -> tuple[float, str, list[str], float, str | None]:
    excluded_matches = find_matching_keywords(
        title,
        list(settings.get("excluded_title_keywords", [])),
    )

    if excluded_matches:
        return (
            -1.0,
            "excluded",
            excluded_matches,
            1.0,
            (
                "Seniority is excluded."
                if settings.get("excluded_policy")
                == "reject"
                else None
            ),
        )

    student_matches = find_matching_keywords(
        title,
        list(settings.get("student_keywords", [])),
    )

    if (
        student_matches
        and not settings.get(
            "allow_student_roles",
            False,
        )
    ):
        return (
            -1.0,
            "student-only",
            student_matches,
            1.0,
            "Student-only seniority is excluded.",
        )

    junior_matches = find_matching_keywords(
        title,
        list(settings.get("junior_keywords", [])),
    )

    if junior_matches:
        return (
            1.0,
            "preferred",
            junior_matches,
            1.0,
            None,
        )

    label_fits = {
        "entry-level": 0.9,
        "junior": 0.9,
        "junior-to-mid": 0.65,
        "mid-level": 0.0,
        "senior": -0.8,
        "student": -1.0,
        "intern": -1.0,
        "not-specified": float(
            settings.get("unknown_fit", -0.15)
        ),
    }
    fit = label_fits.get(
        experience_label,
        float(settings.get("unknown_fit", -0.15)),
    )
    matches = [
        signal
        for signal in analysis_seniority
        if signal in {
            "junior",
            "senior",
            "student",
            "intern",
        }
    ]

    return (
        fit,
        (
            "preferred"
            if fit > 0
            else "not preferred"
            if fit < 0
            else "neutral"
        ),
        matches or (
            [experience_label]
            if experience_label != "not-specified"
            else []
        ),
        confidence,
        None,
    )


def _education_component(
    education_signals: list[str],
    confidence: float,
    settings: dict[str, Any],
) -> tuple[float, str, list[str], float, str | None]:
    if "active-student" in education_signals:
        policy = str(
            settings.get(
                "active_student_policy",
                "neutral",
            )
        )
        fit = {
            "reject": -1.0,
            "penalty": -0.5,
            "neutral": 0.0,
        }.get(policy, 0.0)

        return (
            fit,
            "active student required",
            ["active-student"],
            confidence,
            (
                "Active-student requirement is excluded."
                if policy == "reject"
                else None
            ),
        )

    fit_by_signal = {
        "degree-required": 0.7,
        "degree-preferred": 0.5,
        "degree-mentioned": 0.25,
    }
    fits = [
        fit_by_signal[signal]
        for signal in education_signals
        if signal in fit_by_signal
    ]

    if fits:
        return (
            max(fits),
            "compatible",
            education_signals,
            confidence,
            None,
        )

    return (
        float(settings.get("unknown_fit", 0.0)),
        "not specified",
        [],
        confidence,
        None,
    )


def _work_model_component(
    context: str,
    confidence: float,
    settings: dict[str, Any],
) -> tuple[float, str, list[str], float, str | None]:
    categories = (
        ("excluded", "excluded_keywords", -1.0),
        ("preferred", "preferred_keywords", 1.0),
        ("acceptable", "acceptable_keywords", 0.25),
    )

    for label, field, default_fit in categories:
        matches = find_matching_keywords(
            context,
            list(settings.get(field, [])),
        )

        if not matches:
            continue

        hard_reason = None

        if (
            label == "excluded"
            and settings.get("excluded_policy")
            == "reject"
        ):
            hard_reason = "Work model is excluded."

        return (
            float(
                settings.get(
                    f"{label}_fit",
                    default_fit,
                )
            ),
            label,
            matches,
            confidence,
            hard_reason,
        )

    return (
        float(settings.get("unknown_fit", 0.0)),
        "not specified",
        [],
        confidence,
        None,
    )


def _component_reason(
    component: dict[str, Any],
) -> str:
    contribution = float(component["contribution"])
    points = f"{contribution:+.1f}"
    reason = (
        f"{component['label']}: "
        f"{component['status']} ({points} points)."
    )
    matches = component.get("matches", [])

    if matches:
        reason += " Signals: " + ", ".join(matches) + "."

    if (
        component["required_for_high_match"]
        and not component["required_satisfied"]
    ):
        reason += " Required for a high match."

    return reason


def _evaluate_job_v2(
    job: sqlite3.Row,
    profile: dict[str, Any],
) -> JobEvaluation:
    """Evaluate a job using normalized configurable preferences."""

    criteria = profile["criteria"]
    raw_title = str(_row_value(job, "title", ""))
    raw_location = str(_row_value(job, "location", ""))
    raw_job_url = str(_row_value(job, "job_url", ""))
    description = str(
        _row_value(job, "description_text", "")
    )
    title = normalize_text(raw_title)
    location_context = normalize_text(
        f"{raw_location} {raw_job_url}"
    )
    work_model_context = normalize_text(
        f"{raw_location} {description}"
    )
    technologies = parse_json_list(
        _row_value(job, "technologies_json")
    )
    analysis_seniority = parse_json_list(
        _row_value(job, "seniority_signals_json")
    )
    education_signals = parse_json_list(
        _row_value(job, "education_signals_json")
    )
    analysis_confidence = max(
        0.0,
        min(
            1.0,
            float(
                _row_value(
                    job,
                    "analysis_confidence",
                    0.0,
                )
            ),
        ),
    )
    experience_min_raw = _row_value(
        job,
        "experience_min",
    )
    experience_max_raw = _row_value(
        job,
        "experience_max",
    )
    experience_min = (
        int(experience_min_raw)
        if experience_min_raw is not None
        else None
    )
    experience_max = (
        int(experience_max_raw)
        if experience_max_raw is not None
        else None
    )
    experience_label = str(
        _row_value(
            job,
            "experience_label",
            "not-specified",
        )
    )
    component_values = {
        "role_domain": _role_domain_component(
            title,
            criteria["role_domain"],
        ),
        "location": _location_component(
            location_context,
            criteria["location"],
        ),
        "technology": _technology_component(
            technologies,
            analysis_confidence,
            criteria["technology"],
        ),
        "experience": _experience_component(
            experience_min,
            experience_max,
            analysis_seniority,
            analysis_confidence,
            criteria["experience"],
        ),
        "seniority": _seniority_component(
            title,
            experience_label,
            analysis_seniority,
            analysis_confidence,
            criteria["seniority"],
        ),
        "education": _education_component(
            education_signals,
            analysis_confidence,
            criteria["education"],
        ),
        "work_model": _work_model_component(
            work_model_context,
            analysis_confidence,
            criteria["work_model"],
        ),
    }
    total_weight = sum(
        int(criteria[name]["weight"])
        for name in PREFERENCE_CRITERIA
    )
    components: list[dict[str, Any]] = []
    hard_reasons: list[str] = []

    for criterion in PREFERENCE_CRITERIA:
        (
            fit,
            status,
            matches,
            confidence,
            hard_reason,
        ) = component_values[criterion]
        fit = _bounded_fit(float(fit))
        weight = int(criteria[criterion]["weight"])
        normalized_weight = weight / total_weight
        contribution = (
            50.0
            * normalized_weight
            * fit
        )
        required = bool(
            criteria[criterion][
                "required_for_high_match"
            ]
        )
        component = {
            "criterion": criterion,
            "label": PREFERENCE_LABELS[criterion],
            "status": status,
            "fit": round(fit, 4),
            "confidence": round(
                max(0.0, min(1.0, confidence)),
                4,
            ),
            "weight": weight,
            "normalized_weight": round(
                normalized_weight,
                4,
            ),
            "contribution": round(
                contribution,
                2,
            ),
            "matches": matches,
            "required_for_high_match": required,
            "required_satisfied": (
                not required
                or fit > 0
            ),
        }
        components.append(component)

        if hard_reason:
            hard_reasons.append(hard_reason)

    component_by_name = {
        component["criterion"]: component
        for component in components
    }
    interaction_total = 0
    interaction_labels: list[str] = []

    for interaction in profile.get("interactions", []):
        minimum_fit = float(
            interaction.get("minimum_fit", 0.5)
        )
        names = interaction["criteria"]

        if all(
            float(component_by_name[name]["fit"])
            >= minimum_fit
            for name in names
        ):
            bonus = int(interaction["bonus"])
            interaction_total += bonus
            interaction_labels.append(
                " + ".join(
                    PREFERENCE_LABELS[name]
                    for name in names
                )
            )

    interaction_total = max(
        -10,
        min(10, interaction_total),
    )

    if interaction_total:
        components.append(
            {
                "criterion": "interaction",
                "label": "Combined preferences",
                "status": "; ".join(
                    interaction_labels
                ),
                "fit": 1.0,
                "confidence": 1.0,
                "weight": 0,
                "normalized_weight": 0.0,
                "contribution": float(
                    interaction_total
                ),
                "matches": [],
                "required_for_high_match": False,
                "required_satisfied": True,
            }
        )

    raw_score = (
        50.0
        + sum(
            float(component["contribution"])
            for component in components
            if component["criterion"] != "interaction"
        )
        + interaction_total
    )
    score = max(
        0,
        min(100, round(raw_score)),
    )
    required_failures = [
        str(component["label"])
        for component in components
        if (
            component["criterion"] != "interaction"
            and component["required_for_high_match"]
            and not component["required_satisfied"]
        )
    ]
    thresholds = profile["thresholds"]
    high_match_threshold = int(
        thresholds["high_match"]
    )
    review_threshold = int(
        thresholds["review"]
    )

    if hard_reasons:
        match_bucket = "rejected"
    elif (
        score >= high_match_threshold
        and not required_failures
    ):
        match_bucket = "high_match"
    elif score >= review_threshold:
        match_bucket = "review"
    else:
        match_bucket = "rejected"

    reasons = [
        _component_reason(component)
        for component in components
    ]

    for hard_reason in dict.fromkeys(hard_reasons):
        reasons.append(
            "Hard rejection: " + hard_reason
        )

    if required_failures:
        reasons.append(
            "High-match requirements not met: "
            + ", ".join(required_failures)
            + "."
        )

    location_component = component_by_name["location"]
    seniority_component = component_by_name["seniority"]
    seniority_label = (
        "senior-or-lead"
        if seniority_component["status"] == "excluded"
        else experience_label
    )

    return JobEvaluation(
        job_id=int(_row_value(job, "id")),
        profile_name=str(profile["profile_name"]),
        seniority_label=seniority_label,
        role_category=determine_role_category(title),
        location_label=str(location_component["status"]),
        match_score=score,
        match_bucket=match_bucket,
        reasons=reasons,
        score_components=components,
        profile_schema_version=PROFILE_SCHEMA_VERSION,
        scoring_model=SCORING_MODEL_V2,
    )


def evaluate_job(
    job: sqlite3.Row,
    profile: dict[str, Any],
) -> JobEvaluation:
    """Evaluate a job with the profile-selected scoring model."""

    if profile.get("schema_version") is None:
        return _evaluate_job_legacy(
            job,
            profile,
        )

    validate_v2_profile(profile)
    return _evaluate_job_v2(
        job,
        profile,
    )


def ensure_evaluation_table(
    connection: sqlite3.Connection,
) -> None:
    """Create the job-evaluation table."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_evaluations (
            job_id INTEGER PRIMARY KEY,

            profile_name TEXT NOT NULL,
            seniority_label TEXT NOT NULL,
            role_category TEXT NOT NULL,
            location_label TEXT NOT NULL,

            match_score INTEGER NOT NULL,
            match_bucket TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            score_components_json TEXT NOT NULL
                DEFAULT '[]',
            profile_schema_version INTEGER NOT NULL
                DEFAULT 1,
            scoring_model TEXT NOT NULL
                DEFAULT 'legacy_v1',

            evaluated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (job_id)
                REFERENCES jobs(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS
            idx_job_evaluations_bucket_score
        ON job_evaluations(
            match_bucket,
            match_score DESC
        );
        """
    )

    existing_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(job_evaluations)"
        ).fetchall()
    }
    additive_columns = {
        "score_components_json": (
            "TEXT NOT NULL DEFAULT '[]'"
        ),
        "profile_schema_version": (
            "INTEGER NOT NULL DEFAULT 1"
        ),
        "scoring_model": (
            "TEXT NOT NULL DEFAULT 'legacy_v1'"
        ),
    }

    for column, declaration in additive_columns.items():
        if column in existing_columns:
            continue

        connection.execute(
            "ALTER TABLE job_evaluations "
            f"ADD COLUMN {column} {declaration}"
        )

    connection.commit()


def save_evaluation(
    connection: sqlite3.Connection,
    evaluation: JobEvaluation,
) -> None:
    """Insert or update one evaluation."""

    reasons_json = json.dumps(
        evaluation.reasons,
        ensure_ascii=False,
    )
    score_components_json = json.dumps(
        evaluation.score_components,
        ensure_ascii=False,
    )

    connection.execute(
        """
        INSERT INTO job_evaluations (
            job_id,
            profile_name,
            seniority_label,
            role_category,
            location_label,
            match_score,
            match_bucket,
            reasons_json,
            score_components_json,
            profile_schema_version,
            scoring_model
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(job_id)
        DO UPDATE SET
            profile_name = excluded.profile_name,
            seniority_label = excluded.seniority_label,
            role_category = excluded.role_category,
            location_label = excluded.location_label,
            match_score = excluded.match_score,
            match_bucket = excluded.match_bucket,
            reasons_json = excluded.reasons_json,
            score_components_json =
                excluded.score_components_json,
            profile_schema_version =
                excluded.profile_schema_version,
            scoring_model = excluded.scoring_model,
            evaluated_at = CURRENT_TIMESTAMP
        """,
        (
            evaluation.job_id,
            evaluation.profile_name,
            evaluation.seniority_label,
            evaluation.role_category,
            evaluation.location_label,
            evaluation.match_score,
            evaluation.match_bucket,
            reasons_json,
            score_components_json,
            evaluation.profile_schema_version,
            evaluation.scoring_model,
        ),
    )
