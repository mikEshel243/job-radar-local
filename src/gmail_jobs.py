import base64
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDENTIALS_PATH = (
    PROJECT_ROOT / "config" / "gmail_oauth_client.local.json"
)
DEFAULT_TOKEN_PATH = (
    PROJECT_ROOT / "data" / "gmail_token.local.json"
)
GMAIL_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/gmail.readonly"
)
MAX_GMAIL_MESSAGES = 50
MAX_RAW_MESSAGE_BYTES = 5 * 1024 * 1024
LINKEDIN_GMAIL_QUERY = (
    "{from:jobs-noreply@linkedin.com "
    "from:jobalerts-noreply@linkedin.com} "
    "-in:spam -in:trash"
)
AMAZON_GMAIL_QUERY = (
    'from:noreply@mail.amazon.jobs '
    'subject:"Recommended Amazon jobs for" '
    "-in:spam -in:trash"
)


class GmailJobsError(RuntimeError):
    """Raised when scoped Gmail collection cannot continue."""


def build_readonly_gmail_service(
    *,
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
) -> Any:
    """Authorize a local desktop client with Gmail read-only access."""

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        raise GmailJobsError(
            "Google Gmail dependencies are not installed. "
            "Install the pinned project requirements first."
        ) from error

    scopes = [GMAIL_READONLY_SCOPE]
    credentials = None

    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            scopes,
        )

    if not credentials or not credentials.valid:
        if (
            credentials
            and credentials.expired
            and credentials.refresh_token
        ):
            credentials.refresh(Request())
        else:
            if not credentials_path.is_file():
                raise GmailJobsError(
                    "Gmail OAuth desktop credentials are missing. "
                    "See the README Gmail setup section."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path),
                scopes,
            )
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
            )

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    return build(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def _decode_raw_message(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise GmailJobsError(
            "Gmail returned a message without MIME content."
        )

    try:
        encoded = value.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(encoded + padding)
    except (UnicodeEncodeError, ValueError) as error:
        raise GmailJobsError(
            "Gmail returned invalid MIME content."
        ) from error

    if len(decoded) > MAX_RAW_MESSAGE_BYTES:
        raise GmailJobsError(
            "Gmail message exceeds the supported size limit."
        )

    return decoded


def _fetch_job_email_mime(
    service: Any,
    *,
    base_query: str,
    newer_than_days: int = 14,
    max_messages: int = 20,
) -> tuple[bytes, ...]:
    """Fetch one bounded class of job emails as raw MIME bytes."""

    if not 1 <= newer_than_days <= 90:
        raise ValueError(
            "newer_than_days must be between 1 and 90."
        )

    if not 1 <= max_messages <= MAX_GMAIL_MESSAGES:
        raise ValueError(
            f"max_messages must be between 1 and {MAX_GMAIL_MESSAGES}."
        )

    query = (
        f"{base_query} "
        f"newer_than:{newer_than_days}d"
    )

    try:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=max_messages,
            )
            .execute(num_retries=2)
        )
        message_rows = response.get("messages", [])
        messages: list[bytes] = []

        for row in message_rows:
            message_id = row.get("id")

            if not isinstance(message_id, str) or not message_id:
                continue

            payload = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="raw",
                )
                .execute(num_retries=2)
            )
            messages.append(
                _decode_raw_message(payload.get("raw"))
            )

    except GmailJobsError:
        raise
    except Exception as error:
        raise GmailJobsError(
            "The read-only Gmail request failed."
        ) from error

    return tuple(messages)


def fetch_linkedin_job_email_mime(
    service: Any,
    *,
    newer_than_days: int = 14,
    max_messages: int = 20,
) -> tuple[bytes, ...]:
    """Fetch only bounded LinkedIn job emails as raw MIME bytes."""

    return _fetch_job_email_mime(
        service,
        base_query=LINKEDIN_GMAIL_QUERY,
        newer_than_days=newer_than_days,
        max_messages=max_messages,
    )


def fetch_amazon_job_email_mime(
    service: Any,
    *,
    newer_than_days: int = 14,
    max_messages: int = 20,
) -> tuple[bytes, ...]:
    """Fetch only bounded Amazon recommendation emails."""

    return _fetch_job_email_mime(
        service,
        base_query=AMAZON_GMAIL_QUERY,
        newer_than_days=newer_than_days,
        max_messages=max_messages,
    )
