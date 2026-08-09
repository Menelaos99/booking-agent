"""Read recent Booking.com verification codes through Gmail OAuth."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from booking_agent.config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    Settings,
    get_settings,
)
from booking_agent.utils.logging_utils import log_group

logger = logging.getLogger(__name__)

READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
COMPOSE_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
WORKFLOW_SCOPES = [*READONLY_SCOPES, *COMPOSE_SCOPES]
# Backward-compatible alias for tests and callers that only need OTP access.
SCOPES = READONLY_SCOPES
_CODE_TOKEN = r"(?=[A-Z0-9]{4,8}\b)(?=[A-Z0-9]*\d)[A-Z0-9]{4,8}"
_CODE_PATTERNS = [
    re.compile(
        rf"(?:verification|security|confirmation|one[- ]time|login)\s+code"
        rf"(?:\s+is)?\s*[:\-]?\s*({_CODE_TOKEN})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b({_CODE_TOKEN})\b\s+is\s+your\s+"
        rf"(?:Booking\.com\s+)?(?:verification|security|confirmation|one[- ]time|login)?\s*code",
        re.IGNORECASE,
    ),
]


class GmailAuthorizationRequired(RuntimeError):
    """Raised when Gmail OAuth must be connected again."""


@dataclass(frozen=True)
class GmailStatus:
    connected: bool
    account: str | None
    detail: str
    compose_enabled: bool = False


def _secure_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}-",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            if not contents.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load_credentials(
    *,
    required_scopes: list[str] | None = None,
    persist_refresh: bool = True,
) -> Credentials:
    if not GMAIL_TOKEN_FILE.exists():
        raise GmailAuthorizationRequired("Gmail is not connected")

    GMAIL_TOKEN_FILE.chmod(0o600)
    required_scopes = required_scopes or READONLY_SCOPES
    credentials = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE))
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise GmailAuthorizationRequired(
                "Gmail authorization expired or was revoked; reconnect it"
            ) from exc
        if persist_refresh:
            _secure_write(GMAIL_TOKEN_FILE, credentials.to_json())

    if not credentials.valid or not credentials.has_scopes(required_scopes):
        scope_label = "draft" if set(COMPOSE_SCOPES) & set(required_scopes) else "read-only"
        raise GmailAuthorizationRequired(
            f"Gmail {scope_label} authorization is not valid; reconnect Gmail"
        )
    return credentials


def _profile_email(credentials: Credentials) -> str:
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    return str(profile.get("emailAddress", "")).strip().lower()


def authorize_gmail(settings: Settings | None = None) -> GmailStatus:
    """Run explicit OAuth consent and bind the token to the configured account."""

    settings = settings or get_settings()
    expected_account = settings.gmail_account.strip().lower()
    if not GMAIL_CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Gmail OAuth client credentials are missing at {GMAIL_CREDENTIALS_FILE}"
        )

    GMAIL_CREDENTIALS_FILE.chmod(0o600)
    flow = InstalledAppFlow.from_client_secrets_file(
        str(GMAIL_CREDENTIALS_FILE), WORKFLOW_SCOPES
    )
    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        login_hint=expected_account,
        success_message="Gmail access connected. You can close this tab.",
    )
    account = _profile_email(credentials)
    if account != expected_account:
        raise GmailAuthorizationRequired(
            f"Authorized {account or 'an unknown account'}, expected {expected_account}; token was not saved"
        )

    _secure_write(GMAIL_TOKEN_FILE, credentials.to_json())
    return GmailStatus(
        True,
        account,
        "Gmail read and draft access is connected",
        compose_enabled=True,
    )


def gmail_status(settings: Settings | None = None) -> GmailStatus:
    settings = settings or get_settings()
    expected_account = settings.gmail_account.strip().lower()
    try:
        credentials = _load_credentials(required_scopes=READONLY_SCOPES)
        account = _profile_email(credentials)
    except (GmailAuthorizationRequired, OSError, ValueError) as exc:
        return GmailStatus(False, None, str(exc))

    if account != expected_account:
        return GmailStatus(
            False,
            account,
            f"Connected account does not match configured account {expected_account}",
        )
    compose_enabled = credentials.has_scopes(COMPOSE_SCOPES)
    detail = (
        "Gmail read and draft access is connected"
        if compose_enabled
        else "Gmail read-only access is connected; reconnect Gmail before creating drafts"
    )
    return GmailStatus(True, account, detail, compose_enabled=compose_enabled)


def _extract_otp(text: str) -> str | None:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    for pattern in _CODE_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return match.group(1).upper()
    return None


def _decode_email_body(payload: dict) -> str:
    chunks: list[str] = []

    def visit(part: dict) -> None:
        data = part.get("body", {}).get("data")
        if data and part.get("mimeType", "").startswith(("text/plain", "text/html")):
            try:
                chunks.append(
                    base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                )
            except (ValueError, TypeError):
                pass
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    return "\n".join(chunks)


def _header(payload: dict, name: str) -> str:
    for header in payload.get("headers", []):
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _is_booking_sender(value: str) -> bool:
    address = parseaddr(value)[1].lower()
    domain = address.rpartition("@")[2]
    return domain == "booking.com" or domain.endswith(".booking.com")


def _find_recent_code(
    credentials: Credentials,
    *,
    not_before: float,
) -> str | None:
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    response = service.users().messages().list(
        userId="me",
        q='newer_than:10m ("verification code" OR "security code" OR "one-time code")',
        maxResults=10,
    ).execute()

    for index, message_metadata in enumerate(response.get("messages", []), start=1):
        with log_group(f"candidate email {index}", level=logging.DEBUG, logger=logger):
            message = service.users().messages().get(
                userId="me",
                id=message_metadata["id"],
                format="full",
            ).execute()
            received_at = int(message.get("internalDate", "0")) / 1000
            if received_at < not_before:
                logger.debug("Skipping stale message")
                continue

            payload = message.get("payload", {})
            if not _is_booking_sender(_header(payload, "From")):
                logger.debug("Skipping non-Booking.com sender")
                continue

            subject = _header(payload, "Subject")
            code = _extract_otp(f"{subject}\n{_decode_email_body(payload)}")
            if code:
                logger.info("Fresh Booking.com verification email found")
                return code
            logger.debug("No context-bound verification code found")
    return None


async def fetch_otp_from_gmail(
    max_retries: int = 10,
    retry_interval: float = 5.0,
    max_age_seconds: int = 180,
    *,
    not_before: float | None = None,
    timeout_seconds: float | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Poll for a fresh Booking.com code without exposing mailbox contents."""

    settings = settings or get_settings()
    credentials = await asyncio.to_thread(
        _load_credentials, required_scopes=READONLY_SCOPES
    )
    account = await asyncio.to_thread(_profile_email, credentials)
    if account != settings.gmail_account.strip().lower():
        raise GmailAuthorizationRequired(
            f"Connected Gmail account does not match {settings.gmail_account}"
        )

    started_at = time.time()
    cutoff = not_before if not_before is not None else started_at - max_age_seconds
    deadline = started_at + (
        timeout_seconds
        if timeout_seconds is not None
        else max_retries * retry_interval
    )
    attempt = 0
    while attempt < max_retries and time.time() <= deadline:
        attempt += 1
        logger.info(
            "Polling Gmail for a recent Booking.com verification email (%s/%s)",
            attempt,
            max_retries,
        )
        code = await asyncio.to_thread(
            _find_recent_code,
            credentials,
            not_before=cutoff,
        )
        if code:
            return code
        if attempt < max_retries and time.time() + retry_interval <= deadline:
            await asyncio.sleep(retry_interval)

    logger.warning("No recent Booking.com verification email was found")
    return None
