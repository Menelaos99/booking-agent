from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import Literal

from googleapiclient.discovery import build
from pydantic import BaseModel, ConfigDict, Field

from booking_agent.auth.gmail_otp import (
    COMPOSE_SCOPES,
    READONLY_SCOPES,
    _load_credentials,
)


class GmailAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attachment_id: str
    filename: str
    mime_type: str
    size: int = 0


class GmailMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    thread_id: str
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    occurred_at: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    attachments: list[GmailAttachment] = Field(default_factory=list)

    @property
    def direction(self) -> Literal["inbound", "outbound"]:
        return "outbound" if "SENT" in self.label_ids else "inbound"


class CreatedDraft(BaseModel):
    draft_id: str
    message_id: str
    thread_id: str | None = None


class GmailMatchPreview(BaseModel):
    match_id: int
    booking_id: str
    customer_name: str
    arrival_date: str | None = None
    match_method: str
    confidence: float
    status: str
    occurred_at: str | None = None
    direction: Literal["inbound", "outbound"]
    masked_from: str
    masked_to: list[str] = Field(default_factory=list)
    subject: str
    excerpt: str
    attachment_count: int


_PREVIEW_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PREVIEW_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_PREVIEW_IDENTIFIER = re.compile(
    r"\b(?:\d{6,}|(?=[A-Z0-9]{7,}\b)(?=[A-Z0-9]*\d)[A-Z0-9]+)\b"
)
_PREVIEW_IDENTITY_LINE = re.compile(
    r"passport|identity document|tax id|ΑΦΜ|διαβατ", re.IGNORECASE
)


def mask_email_address(value: str) -> str:
    local, separator, domain = value.strip().lower().partition("@")
    if not separator:
        return "[REDACTED_ADDRESS]" if value else ""
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def redact_gmail_preview(value: str) -> str:
    lines: list[str] = []
    for line in value.splitlines():
        if _PREVIEW_IDENTITY_LINE.search(line):
            lines.append("[IDENTITY DATA REDACTED]")
            continue
        redacted = _PREVIEW_EMAIL.sub("[EMAIL REDACTED]", line)
        redacted = _PREVIEW_PHONE.sub("[PHONE REDACTED]", redacted)
        redacted = _PREVIEW_IDENTIFIER.sub("[IDENTIFIER REDACTED]", redacted)
        lines.append(redacted)
    return "\n".join(lines)


def build_gmail_match_preview(
    match: dict,
    message: GmailMessage,
    *,
    excerpt_characters: int = 240,
) -> GmailMatchPreview:
    excerpt = re.sub(r"\s+", " ", redact_gmail_preview(message.body)).strip()
    return GmailMatchPreview(
        match_id=int(match["id"]),
        booking_id=str(match["reservation_id"]),
        customer_name=str(match["customer_name"]),
        arrival_date=str(match["check_in"]) if match.get("check_in") else None,
        match_method=str(match["match_method"]),
        confidence=float(match["confidence"]),
        status=str(match["status"]),
        occurred_at=message.occurred_at,
        direction=message.direction,
        masked_from=mask_email_address(message.from_address),
        masked_to=[mask_email_address(value) for value in message.to_addresses],
        subject=redact_gmail_preview(message.subject),
        excerpt=excerpt[:excerpt_characters],
        attachment_count=len(message.attachments),
    )


def _header(payload: dict, name: str) -> str:
    for item in payload.get("headers", []):
        if str(item.get("name", "")).casefold() == name.casefold():
            return str(item.get("value", ""))
    return ""


def _decode_data(value: str) -> str:
    try:
        return base64.urlsafe_b64decode(value).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _plain_text_from_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", value)).strip()


def _extract_body_and_attachments(payload: dict) -> tuple[str, list[GmailAttachment]]:
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[GmailAttachment] = []

    def visit(part: dict) -> None:
        body = part.get("body", {})
        data = body.get("data")
        mime_type = str(part.get("mimeType", ""))
        filename = str(part.get("filename", ""))
        attachment_id = str(body.get("attachmentId", ""))
        if filename and attachment_id:
            attachments.append(
                GmailAttachment(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime_type=mime_type,
                    size=int(body.get("size", 0) or 0),
                )
            )
        elif data and mime_type == "text/plain":
            plain.append(_decode_data(data))
        elif data and mime_type == "text/html":
            rich.append(_plain_text_from_html(_decode_data(data)))
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    chunks = plain or rich
    return "\n".join(chunk.strip() for chunk in chunks if chunk.strip()), attachments


class GmailClient:
    def __init__(self, *, require_compose: bool = False):
        required_scopes = [*READONLY_SCOPES, *COMPOSE_SCOPES] if require_compose else READONLY_SCOPES
        credentials = _load_credentials(required_scopes=required_scopes)
        self.service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def search(self, query: str, *, max_results: int = 100) -> list[GmailMessage]:
        messages: list[GmailMessage] = []
        page_token: str | None = None
        while len(messages) < max_results:
            response = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(100, max_results - len(messages)),
                    pageToken=page_token,
                )
                .execute()
            )
            for metadata in response.get("messages", []):
                messages.append(self.get_message(str(metadata["id"])))
                if len(messages) >= max_results:
                    break
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return messages

    def get_message(self, message_id: str) -> GmailMessage:
        raw = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = raw.get("payload", {})
        body, attachments = _extract_body_and_attachments(payload)
        to_addresses = [
            address.lower()
            for _, address in getaddresses([_header(payload, "To")])
            if address
        ]
        timestamp = int(raw.get("internalDate", "0") or 0) / 1000
        occurred_at = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
            if timestamp
            else None
        )
        return GmailMessage(
            id=str(raw["id"]),
            thread_id=str(raw.get("threadId", raw["id"])),
            from_address=parseaddr(_header(payload, "From"))[1].lower(),
            to_addresses=to_addresses,
            subject=_header(payload, "Subject").strip(),
            body=body,
            occurred_at=occurred_at,
            label_ids=[str(value) for value in raw.get("labelIds", [])],
            attachments=attachments,
        )

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raw = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        data = str(raw.get("data", ""))
        return base64.urlsafe_b64decode(data) if data else b""

    def create_draft(self, *, to: str, subject: str, body: str) -> CreatedDraft:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        raw = (
            self.service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": encoded}})
            .execute()
        )
        raw_message = raw.get("message", {})
        return CreatedDraft(
            draft_id=str(raw["id"]),
            message_id=str(raw_message.get("id", "")),
            thread_id=str(raw_message["threadId"]) if raw_message.get("threadId") else None,
        )
