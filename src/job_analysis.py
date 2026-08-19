import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any


TECHNOLOGY_ALIASES: dict[str, list[str]] = {
    "Java": [
        "java",
    ],
    "Spring": [
        "spring",
        "spring framework",
    ],
    "Spring Boot": [
        "spring boot",
    ],
    "React": [
        "react",
        "react.js",
        "reactjs",
    ],
    "TypeScript": [
        "typescript",
    ],
    "JavaScript": [
        "javascript",
        "java script",
    ],
    "Node.js": [
        "node.js",
        "nodejs",
        "node js",
    ],
    "Python": [
        "python",
    ],
    "C": [
        "c language",
    ],
    "C++": [
        "c++",
        "cpp",
    ],
    "C#": [
        "c#",
        "c sharp",
    ],
    ".NET": [
        ".net",
        "dotnet",
    ],
    "Go": [
        "golang",
        "go language",
    ],
    "Rust": [
        "rust",
    ],
    "Linux": [
        "linux",
    ],
    "Docker": [
        "docker",
    ],
    "Kubernetes": [
        "kubernetes",
        "k8s",
    ],
    "REST": [
        "rest api",
        "restful",
        "rest",
    ],
    "SQL": [
        "sql",
    ],
    "PostgreSQL": [
        "postgresql",
        "postgres",
    ],
    "MySQL": [
        "mysql",
    ],
    "MongoDB": [
        "mongodb",
        "mongo db",
    ],
    "Redis": [
        "redis",
    ],
    "Git": [
        "git",
    ],
    "Azure DevOps": [
        "azure devops",
    ],
    "AWS": [
        "aws",
        "amazon web services",
    ],
    "Azure": [
        "microsoft azure",
        "azure",
    ],
    "GCP": [
        "gcp",
        "google cloud platform",
    ],
    "Networking": [
        "networking",
        "computer networks",
    ],
    "TCP/IP": [
        "tcp/ip",
        "tcp ip",
        "tcp",
        "ip networking",
    ],
    "Embedded": [
        "embedded",
        "firmware",
    ],
    "JavaFX": [
        "javafx",
    ],
    "Maven": [
        "maven",
    ],
    "Gradle": [
        "gradle",
    ],
    "CI/CD": [
        "ci/cd",
        "continuous integration",
        "continuous delivery",
    ],
    "Microservices": [
        "microservices",
        "micro services",
    ],
    "HTML": [
        "html",
        "html5",
    ],
    "CSS": [
        "css",
        "css3",
    ],
    "Angular": [
        "angular",
        "angularjs",
    ],
    "Vue": [
        "vue",
        "vue.js",
        "vuejs",
    ],
    "Selenium": [
        "selenium",
    ],
    "Playwright": [
        "playwright",
    ],
    "Cypress": [
        "cypress",
    ],
    "Jenkins": [
        "jenkins",
    ],
    "GitHub Actions": [
        "github actions",
    ],
    "Kafka": [
        "kafka",
        "apache kafka",
    ],
    "RabbitMQ": [
        "rabbitmq",
        "rabbit mq",
    ],
    "GraphQL": [
        "graphql",
    ],
}


SENIORITY_ALIASES: dict[str, list[str]] = {
    "student": [
        "student",
        "student position",
        "student role",
        "currently pursuing",
        "currently enrolled",
    ],
    "intern": [
        "intern",
        "internship",
        "intern position",
        "software intern",
    ],
    "junior": [
        "junior",
        "entry level",
        "entry-level",
        "new graduate",
        "recent graduate",
        "graduate position",
        "graduate developer",
        "graduate engineer",
    ],
    "senior": [
        "senior",
        "staff engineer",
        "principal engineer",
        "technical lead",
        "tech lead",
        "team lead",
        "architect",
        "engineering manager",
    ],
}


EDUCATION_ALIASES: dict[str, list[str]] = {
    "degree-required": [
        "bachelor's degree required",
        "bachelors degree required",
        "b.sc. required",
        "bsc required",
        "degree in computer science required",
    ],
    "degree-preferred": [
        "bachelor's degree preferred",
        "bachelors degree preferred",
        "degree preferred",
        "b.sc. preferred",
        "bsc preferred",
    ],
    "degree-mentioned": [
        "bachelor's degree",
        "bachelors degree",
        "bachelor degree",
        "b.sc.",
        "bsc",
        "computer science degree",
        "degree in computer science",
        "software engineering degree",
    ],
    "active-student": [
        "currently pursuing",
        "currently enrolled",
        "active student",
        "student with",
        "remaining semesters",
    ],
}


@dataclass
class JobAnalysis:
    job_id: int
    experience_min: int | None
    experience_max: int | None
    experience_label: str
    technologies: list[str]
    seniority_signals: list[str]
    education_signals: list[str]
    analysis_confidence: float
    analysis_notes: list[str]


def ensure_job_analysis_table(
    connection: sqlite3.Connection,
) -> None:
    """Create the table used for structured description analysis."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_analysis (
            job_id INTEGER PRIMARY KEY,

            experience_min INTEGER,
            experience_max INTEGER,
            experience_label TEXT NOT NULL,

            technologies_json TEXT NOT NULL,
            seniority_signals_json TEXT NOT NULL,
            education_signals_json TEXT NOT NULL,

            analysis_confidence REAL NOT NULL,
            analysis_notes_json TEXT NOT NULL,

            analyzed_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (job_id)
                REFERENCES jobs(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS
            idx_job_analysis_experience
        ON job_analysis(
            experience_min,
            experience_max
        );

        CREATE INDEX IF NOT EXISTS
            idx_job_analysis_label
        ON job_analysis(experience_label);
        """
    )

    connection.commit()


def normalize_text(
    value: str | None,
) -> str:
    """Normalize text while preserving useful technology punctuation."""

    if not value:
        return ""

    normalized = unicodedata.normalize(
        "NFKC",
        value,
    )

    normalized = normalized.casefold()
    normalized = normalized.replace("\u00a0", " ")
    normalized = normalized.replace("–", "-")
    normalized = normalized.replace("—", "-")
    normalized = normalized.replace("’", "'")
    normalized = normalized.replace("`", "'")

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


def phrase_is_present(
    normalized_text: str,
    phrase: str,
) -> bool:
    """
    Match a word or phrase without accidental substring matches.
    """

    normalized_phrase = normalize_text(phrase)

    if not normalized_phrase:
        return False

    pattern = (
        r"(?<![a-z0-9])"
        + re.escape(normalized_phrase)
        + r"(?![a-z0-9])"
    )

    return re.search(
        pattern,
        normalized_text,
        flags=re.IGNORECASE,
    ) is not None


def extract_alias_groups(
    normalized_text: str,
    aliases: dict[str, list[str]],
) -> list[str]:
    """Return canonical names whose aliases appear in the text."""

    matches: list[str] = []

    for canonical_name, canonical_aliases in aliases.items():
        found = any(
            phrase_is_present(
                normalized_text,
                alias,
            )
            for alias in canonical_aliases
        )

        if found:
            matches.append(canonical_name)

    return matches


def extract_technologies(
    normalized_text: str,
) -> list[str]:
    """Extract a canonical list of technologies."""

    matches = extract_alias_groups(
        normalized_text,
        TECHNOLOGY_ALIASES,
    )

    # Avoid reporting both Spring and Spring Boot when the only
    # occurrence of Spring is part of the phrase Spring Boot.
    if (
        "Spring Boot" in matches
        and "Spring" in matches
    ):
        text_without_spring_boot = normalized_text.replace(
            "spring boot",
            "",
        )

        if not phrase_is_present(
            text_without_spring_boot,
            "spring",
        ):
            matches.remove("Spring")

    return matches


def split_requirement_fragments(
    normalized_text: str,
) -> list[str]:
    """
    Split text into smaller fragments so experience expressions
    are checked in requirement-like context.
    """

    fragments = re.split(
        r"(?<=[.!?;])\s+|\n+|(?:\s+[•●▪]\s+)",
        normalized_text,
    )

    return [
        fragment.strip()
        for fragment in fragments
        if fragment.strip()
    ]


def is_experience_context(
    fragment: str,
) -> bool:
    """Check whether a fragment probably describes experience."""

    context_terms = [
        "experience",
        "professional",
        "hands-on",
        "hands on",
        "proven",
        "required",
        "requirement",
        "minimum",
        "at least",
        "must have",
        "working with",
        "development",
        "engineering",
        "software",
        "backend",
        "frontend",
        "full stack",
        "programming",
        "years in",
        "years with",
    ]

    return any(
        phrase_is_present(
            fragment,
            term,
        )
        for term in context_terms
    )


def extract_experience_candidates(
    normalized_text: str,
) -> list[tuple[int, int | None, str]]:
    """
    Extract experience requirements.

    Each result contains:
        minimum years
        maximum years, or None for an open-ended requirement
        source fragment
    """

    candidates: list[
        tuple[int, int | None, str]
    ] = []

    no_experience_patterns = [
        (
            r"\bno\s+(?:prior\s+)?experience\s+"
            r"(?:is\s+)?required\b"
        ),
        r"\bwithout\s+(?:prior\s+)?experience\b",
        r"\bexperience\s+is\s+not\s+required\b",
    ]

    for pattern in no_experience_patterns:
        if re.search(
            pattern,
            normalized_text,
        ):
            candidates.append(
                (
                    0,
                    0,
                    "No prior experience required",
                )
            )

    range_pattern = re.compile(
        r"\b(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*"
        r"(?:years?|yrs?)\b"
    )

    plus_pattern = re.compile(
        r"\b(\d{1,2})\s*\+\s*(?:years?|yrs?)\b"
    )

    at_least_pattern = re.compile(
        r"\b(?:at\s+least|minimum(?:\s+of)?|min\.?)\s*"
        r"(\d{1,2})\s*(?:years?|yrs?)\b"
    )

    generic_pattern = re.compile(
        r"\b(\d{1,2})\s*(?:years?|yrs?)\s+"
        r"(?:of\s+)?"
        r"(?:relevant\s+|professional\s+|"
        r"hands-on\s+|hands\s+on\s+)?"
        r"experience\b"
    )

    years_in_pattern = re.compile(
        r"\b(\d{1,2})\s*(?:years?|yrs?)\s+"
        r"(?:working\s+)?"
        r"(?:in|with|developing|building)\b"
    )

    fragments = split_requirement_fragments(
        normalized_text
    )

    for fragment in fragments:
        if not is_experience_context(fragment):
            continue

        for match in range_pattern.finditer(fragment):
            minimum = int(match.group(1))
            maximum = int(match.group(2))

            if minimum > maximum:
                minimum, maximum = maximum, minimum

            if maximum <= 20:
                candidates.append(
                    (
                        minimum,
                        maximum,
                        fragment,
                    )
                )

        open_ended_patterns = [
            plus_pattern,
            at_least_pattern,
            generic_pattern,
            years_in_pattern,
        ]

        for pattern in open_ended_patterns:
            for match in pattern.finditer(fragment):
                minimum = int(match.group(1))

                if minimum <= 20:
                    candidates.append(
                        (
                            minimum,
                            None,
                            fragment,
                        )
                    )

    unique_candidates: list[
        tuple[int, int | None, str]
    ] = []

    seen: set[
        tuple[int, int | None]
    ] = set()

    for minimum, maximum, fragment in candidates:
        key = (
            minimum,
            maximum,
        )

        if key in seen:
            continue

        seen.add(key)

        unique_candidates.append(
            (
                minimum,
                maximum,
                fragment,
            )
        )

    return unique_candidates


def choose_experience_requirement(
    candidates: list[
        tuple[int, int | None, str]
    ],
) -> tuple[
    int | None,
    int | None,
    list[str],
]:
    """
    Choose the strongest minimum requirement.

    For example, when a description requires three years overall
    and one year with a specific tool, three is the more meaningful
    minimum for initial filtering.
    """

    if not candidates:
        return None, None, []

    chosen = max(
        candidates,
        key=lambda item: (
            item[0],
            (
                item[1]
                if item[1] is not None
                else 99
            ),
        ),
    )


    minimum = chosen[0]
    maximum = chosen[1]

    notes = [
        (
            f"Experience candidate: "
            f"{candidate_min}-"
            f"{candidate_max if candidate_max is not None else '+'}"
        )
        for (
            candidate_min,
            candidate_max,
            _,
        ) in candidates
    ]

    return (
        minimum,
        maximum,
        notes,
    )


def determine_experience_label(
    experience_min: int | None,
    seniority_signals: list[str],
) -> str:
    """Convert extracted signals into a broad level."""

    if "student" in seniority_signals:
        return "student"

    if "intern" in seniority_signals:
        return "intern"

    if "senior" in seniority_signals:
        return "senior"

    if "junior" in seniority_signals:
        return "junior"

    if experience_min is None:
        return "not-specified"

    if experience_min <= 1:
        return "entry-level"

    if experience_min <= 3:
        return "junior-to-mid"

    if experience_min <= 5:
        return "mid-level"

    return "senior"


def calculate_confidence(
    description_text: str,
    technologies: list[str],
    experience_min: int | None,
    seniority_signals: list[str],
    education_signals: list[str],
) -> float:
    """Calculate confidence from the usable information found."""

    score = 0.35

    if len(description_text) >= 500:
        score += 0.15

    if len(description_text) >= 1500:
        score += 0.10

    if technologies:
        score += 0.15

    if experience_min is not None:
        score += 0.15

    if seniority_signals:
        score += 0.05

    if education_signals:
        score += 0.05

    return round(
        min(score, 1.0),
        2,
    )


def analyze_job_description(
    job_id: int,
    title: str | None,
    description_text: str | None,
) -> JobAnalysis:
    """Analyze one job title and fetched description."""

    description = description_text or ""

    combined_text = normalize_text(
        f"{title or ''}\n{description}"
    )

    technologies = extract_technologies(
        combined_text
    )

    seniority_signals = extract_alias_groups(
        combined_text,
        SENIORITY_ALIASES,
    )

    education_signals = extract_alias_groups(
        combined_text,
        EDUCATION_ALIASES,
    )

    experience_candidates = (
        extract_experience_candidates(
            combined_text
        )
    )

    (
        experience_min,
        experience_max,
        experience_notes,
    ) = choose_experience_requirement(
        experience_candidates
    )

    experience_label = determine_experience_label(
        experience_min,
        seniority_signals,
    )

    confidence = calculate_confidence(
        description_text=description,
        technologies=technologies,
        experience_min=experience_min,
        seniority_signals=seniority_signals,
        education_signals=education_signals,
    )

    notes: list[str] = []

    notes.extend(
        experience_notes
    )

    if not technologies:
        notes.append(
            "No known technologies were detected."
        )

    if experience_min is None:
        notes.append(
            "No explicit experience requirement was detected."
        )

    return JobAnalysis(
        job_id=job_id,
        experience_min=experience_min,
        experience_max=experience_max,
        experience_label=experience_label,
        technologies=technologies,
        seniority_signals=seniority_signals,
        education_signals=education_signals,
        analysis_confidence=confidence,
        analysis_notes=notes,
    )


def save_job_analysis(
    connection: sqlite3.Connection,
    analysis: JobAnalysis,
) -> None:
    """Insert or update analysis for one job."""

    connection.execute(
        """
        INSERT INTO job_analysis (
            job_id,
            experience_min,
            experience_max,
            experience_label,
            technologies_json,
            seniority_signals_json,
            education_signals_json,
            analysis_confidence,
            analysis_notes_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(job_id)
        DO UPDATE SET
            experience_min = excluded.experience_min,
            experience_max = excluded.experience_max,
            experience_label = excluded.experience_label,
            technologies_json = excluded.technologies_json,
            seniority_signals_json = excluded.seniority_signals_json,
            education_signals_json = excluded.education_signals_json,
            analysis_confidence = excluded.analysis_confidence,
            analysis_notes_json = excluded.analysis_notes_json,
            analyzed_at = CURRENT_TIMESTAMP
        """,
        (
            analysis.job_id,
            analysis.experience_min,
            analysis.experience_max,
            analysis.experience_label,
            json.dumps(
                analysis.technologies,
                ensure_ascii=False,
            ),
            json.dumps(
                analysis.seniority_signals,
                ensure_ascii=False,
            ),
            json.dumps(
                analysis.education_signals,
                ensure_ascii=False,
            ),
            analysis.analysis_confidence,
            json.dumps(
                analysis.analysis_notes,
                ensure_ascii=False,
            ),
        ),
    )
