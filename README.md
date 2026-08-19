# Job Radar

Job Radar is a free, local-first job-ingestion and review application built with Python, FastAPI, and SQLite. It converts heterogeneous source messages into a consistent model, deduplicates postings, performs deterministic local analysis, and applies configurable relevance scoring in a browser dashboard.

This repository contains fictional configuration and synthetic fixtures only.

## What It Does

- Imports jobs through bounded source adapters and native export parsers.
- Normalizes URLs, source metadata, and channel-specific message formats.
- Deduplicates jobs while retaining provenance for each source posting.
- Extracts experience, technology, seniority, and education signals.
- Scores jobs using an editable, deterministic preference model.
- Presents filters, refresh status, source status, and manual feedback locally.

## Architecture

```text
Sources
  -> adapters / collectors
  -> normalization / parser registry
  -> deduplication + SQLite
  -> deterministic analysis
  -> configurable relevance scoring
  -> FastAPI dashboard
```

All persistence and analysis remain on the user's computer. Live integrations are optional and are never exercised by the test suite or CI.

## Source Ingestion

The ingestion boundary supports public ATS feeds, read-only Telegram collection, provider-authorized email imports, native exports, and a Windows notification companion. Adapters are bounded by source allowlists, pagination limits, response-size limits, timeouts, and conservative schedules. The included public source registry is fictional and disabled by default.

See [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) for integration boundaries.

## Normalization & Parsing

Source messages are converted into a shared `NormalizedJob` model. A parser registry selects a bounded parser from source context, while URL normalization removes tracking parameters and rejects unsafe or unsupported destinations.

## Deduplication & Storage

SQLite stores canonical jobs, source postings, identity keys, fetched descriptions, structured analysis, relevance evaluations, refresh state, and user feedback. URL identities and position fingerprints merge repeated postings without losing provenance.

## Analysis & Relevance Scoring

Job Radar uses **deterministic local analysis and configurable relevance scoring**. It does not use LLMs, machine learning, embeddings, external AI APIs, or local AI models.

The example profile demonstrates weighted role, location, technology, experience, seniority, education, and work-model criteria. Criteria can be required for a high match, and explicit exclusion policies can reject unsuitable jobs.

## Privacy & Local-First Design

- The dashboard accepts only loopback clients and rejects non-loopback bind configuration.
- Data is stored in a local ignored SQLite database.
- Credentials, OAuth tokens, Telegram sessions, native exports, logs, and local profiles are ignored.
- Telegram and notification integrations are read-only; email access uses user-controlled local OAuth.
- Public configuration and fixtures are synthetic; automatic public collection is off by default.

The application is intentionally unauthenticated because it is local-only. Do not place it behind a public proxy or expose it to a LAN.

## Dashboard

The FastAPI dashboard provides relevance, location, technology, source, and feedback filters; safe external links; editable preference weights and selections; aggregate collection status; and manual workflow states.

## Testing

The verified suite contains 182 unit tests covering parsing, normalization, URL safety, persistence and deduplication, fetching, deterministic analysis, scoring, refresh coordination, imports, dashboard APIs, and loopback-only enforcement. CI runs only synthetic and non-live paths and separately builds and self-tests the Windows companion.

## Running Locally

Requirements: Python 3.14 and Windows for the optional notification companion. Core Python functionality is cross-platform.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python scripts\seed_demo_data.py
python src\web_app.py
```

Open `http://127.0.0.1:8000`. The seed command refuses to overwrite an existing database. Copy `config/job_profile.example.json` to `config/job_profile.local.json` if you want a separate editable profile before first launch.

Optional Gmail imports require `requirements-gmail.txt` and local OAuth setup. Optional provider integrations are documented separately and are not needed for the synthetic demo.

Run safe verification with:

```powershell
python -m compileall src scripts tests
python -m unittest discover -s tests -v
python -m pip check
```

## Limitations

- Designed for one local user, not multi-user or hosted deployment.
- Parser and provider adapters depend on documented source formats and templates.
- The notification listener is Windows-specific and requires explicit local permission.
- SQLite and local process coordination are intentional; distributed operation is out of scope.
- There is no ML, LLM, semantic embedding, or generative component.

No license is included. All sample companies, jobs, locations, URLs, notes, and statuses are fictional.
