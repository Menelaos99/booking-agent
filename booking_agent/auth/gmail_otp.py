"""Fetch OTP codes from Gmail using the Gmail API."""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import datetime

from rich.console import Console

from booking_agent.config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE

console = Console()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] {msg}")


# Booking.com sends alphanumeric codes like "9RKUQF" (6 chars, uppercase + digits)
OTP_PATTERN = re.compile(r"\b([A-Z0-9]{4,8})\b")


def _get_gmail_service():
    """Build and return an authenticated Gmail API v1 service."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Credentials | None = None

    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not GMAIL_CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"Gmail credentials file not found at {GMAIL_CREDENTIALS_FILE}. "
                "Download OAuth2 credentials from Google Cloud Console and place them there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(GMAIL_CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)

    # Save token for future runs
    GMAIL_TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _extract_otp(body: str) -> str | None:
    """Extract a 4-6 digit OTP code from an email body."""
    match = OTP_PATTERN.search(body)
    return match.group(1) if match else None


def _decode_email_body(payload: dict) -> str:
    """Decode the email body from a Gmail API message payload."""
    # Simple single-part message
    if "body" in payload and payload["body"].get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    # Multipart message — check parts recursively
    parts = payload.get("parts", [])
    for part in parts:
        mime = part.get("mimeType", "")
        if mime == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    # Fallback: try any part with data
    for part in parts:
        if part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    return ""


async def fetch_otp_from_gmail(
    max_retries: int = 10,
    retry_interval: float = 5.0,
    max_age_seconds: int = 180,
) -> str | None:
    """Poll Gmail for a recent Booking.com OTP email and extract the code.

    Returns the OTP code string, or None if not found within the retry window.
    """
    try:
        service = _get_gmail_service()
    except Exception as exc:
        _log(f"[red]Gmail API setup failed: {exc}[/red]")
        return None

    query = "from:noreply-iam@booking.com subject:verification code newer_than:5m"
    cutoff = time.time() - max_age_seconds

    for attempt in range(1, max_retries + 1):
        try:
            _log(f"Polling Gmail ({attempt}/{max_retries})...")

            results = service.users().messages().list(
                userId="me", q=query, maxResults=5
            ).execute()

            messages = results.get("messages", [])
            for msg_meta in messages:
                msg = service.users().messages().get(
                    userId="me", id=msg_meta["id"], format="metadata",
                    metadataHeaders=["Subject"],
                ).execute()

                # Check message age
                internal_date = int(msg.get("internalDate", "0")) / 1000
                if internal_date < cutoff:
                    continue

                # Extract code from subject line (e.g. "Booking.com – 9RKUQF is your verification code")
                subject = ""
                for header in msg.get("payload", {}).get("headers", []):
                    if header["name"].lower() == "subject":
                        subject = header["value"]
                        break

                otp = _extract_otp(subject)
                if otp:
                    _log(f"[green]Verification code found: {otp}[/green]")
                    return otp

        except Exception as exc:
            _log(f"[yellow]Gmail API error: {exc}[/yellow]")

        if attempt < max_retries:
            await asyncio.sleep(retry_interval)

    _log("[yellow]Could not find OTP in Gmail within retry window.[/yellow]")
    return None
