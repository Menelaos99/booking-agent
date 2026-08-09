from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.identity import OcrUnavailableError, extract_afm, ocr_attachment, parse_passport_mrz
from booking_agent.integrations.gmail import GmailClient, GmailMessage
from booking_agent.storage.database import BookingDatabase, normalize_name, normalize_phone
from booking_agent.utils.logging_utils import log_group
from booking_agent.workflow_config import WorkflowConfig
from booking_agent.workflows.reservation_sync import ReservationSyncResult, sync_reservations

logger = logging.getLogger(__name__)

IDENTITY_REQUEST = (
    "If you are a Greek guest, please reply with your ΑΦΜ. "
    "If you are not Greek, please reply with your passport details or attach a passport image."
)
_PLACEHOLDER = re.compile(r"{{\s*([a-z_]+)\s*}}")


@dataclass(frozen=True)
class ArrivalRunResult:
    target_date: date
    reservations_synced: int
    reservations_due: int
    drafts_created: int
    drafts_existing: int
    blocked: int
    dry_run: bool


def render_template(template: str, values: dict[str, Any]) -> str:
    unknown = sorted(set(_PLACEHOLDER.findall(template)) - set(values))
    if unknown:
        raise ValueError(f"Unknown template placeholders: {', '.join(unknown)}")
    return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)] or ""), template)


def template_values(reservation: dict[str, Any]) -> dict[str, Any]:
    amount = ""
    if reservation.get("amount_minor") is not None:
        amount = str(Decimal(int(reservation["amount_minor"])) / Decimal(100))
    return {
        "customer_name": reservation.get("customer_name", ""),
        "email": reservation.get("email", ""),
        "arrival_date": reservation.get("check_in", ""),
        "checkout_date": reservation.get("check_out", ""),
        "nights": reservation.get("nights", ""),
        "room_type": reservation.get("room_type", ""),
        "guest_count": reservation.get("guest_count", ""),
        "booking_id": reservation.get("booking_id", ""),
        "amount": amount,
        "currency": reservation.get("currency", ""),
        "identity_request": IDENTITY_REQUEST,
    }


def render_arrival_email(
    template: dict[str, Any], reservation: dict[str, Any]
) -> tuple[str, str]:
    values = template_values(reservation)
    subject = render_template(str(template["subject_template"]), values)
    original_body = str(template["body_template"])
    body = render_template(original_body, values)
    if template["kind"] == "instructions" and "identity_request" not in _PLACEHOLDER.findall(original_body):
        body = f"{body.rstrip()}\n\n{IDENTITY_REQUEST}"
    return subject, body


def _gmail_query_for_reservation(
    reservation: dict[str, Any], days: int, config: WorkflowConfig
) -> str:
    booking_id = str(reservation.get("booking_id", "")).strip()
    email = str(reservation.get("email", "")).strip().lower()
    phone = normalize_phone(str(reservation.get("phone", "")))
    terms: list[str] = []
    if booking_id and config.matching.auto_match_booking_id:
        terms.append(f'"{booking_id}"')
    if email and config.matching.auto_match_exact_email:
        terms.extend((f"from:{email}", f"to:{email}"))
    if phone and config.matching.auto_match_exact_phone:
        terms.append(f'"{phone}"')
    return f"newer_than:{days}d {{{' '.join(terms)}}}" if terms else ""


def _message_category(
    message: GmailMessage,
    database: BookingDatabase,
    reservation: dict[str, Any],
) -> str:
    if message.direction == "inbound" and message.attachments:
        return "identity_submission"
    if message.direction == "inbound":
        return "guest_email"
    for kind in ("instructions", "recommendations"):
        template = database.active_template(kind)
        if template is None:
            continue
        try:
            subject, _ = render_arrival_email(template, reservation)
        except ValueError:
            continue
        if subject.strip().casefold() == message.subject.strip().casefold():
            return kind
    return "host_email"


def _record_matched_message(
    database: BookingDatabase,
    gmail: GmailClient,
    reservation: dict[str, Any],
    message: GmailMessage,
    config: WorkflowConfig,
) -> None:
    category = _message_category(message, database, reservation)
    database.record_communication(
        reservation_id=str(reservation["booking_id"]),
        customer_id=int(reservation["customer_id"]),
        channel="gmail",
        direction=message.direction,
        category=category,
        external_id=message.id,
        thread_id=message.thread_id,
        contact_value=str(reservation.get("email", "")) or message.from_address,
        occurred_at=message.occurred_at,
        attachment_count=len(message.attachments),
    )
    if category in {"instructions", "recommendations"}:
        template = database.active_template(category)
        check_in = date.fromisoformat(str(reservation["check_in"]))
        database.mark_arrival_task(
            reservation_id=str(reservation["booking_id"]),
            template_kind=category,
            template_version=int(template["version"]),
            due_date=check_in - timedelta(days=config.arrivals.days_before),
            state="sent",
            gmail_thread_id=message.thread_id,
            rendered_subject=message.subject,
            sent_at=message.occurred_at,
        )
    if message.direction == "inbound" and (
        message.attachments or extract_afm(message.body)
    ):
        _process_identity_message(
            database, gmail, str(reservation["booking_id"]), message
        )


def _process_identity_message(
    database: BookingDatabase,
    gmail: GmailClient,
    reservation_id: str,
    message: GmailMessage,
) -> None:
    afm = extract_afm(message.body)
    if afm:
        database.record_identity(
            reservation_id=reservation_id,
            kind="afm",
            identifier=afm,
            nationality="GR",
            source_channel="gmail",
            source_external_id=message.id,
            received_at=message.occurred_at,
        )
        return

    for attachment in message.attachments:
        if not (
            attachment.mime_type.startswith("image/")
            or attachment.mime_type == "application/pdf"
        ):
            continue
        try:
            contents = gmail.download_attachment(message.id, attachment.attachment_id)
            extracted = ocr_attachment(
                contents,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
            )
        except (OcrUnavailableError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning("Identity attachment needs manual review: %s", type(exc).__name__)
            database.record_identity(
                reservation_id=reservation_id,
                kind="passport",
                identifier="",
                nationality=None,
                source_channel="gmail",
                source_external_id=message.id,
                received_at=message.occurred_at,
            )
            return
        passport = parse_passport_mrz(extracted)
        if passport:
            database.record_identity(
                reservation_id=reservation_id,
                kind="passport",
                identifier=passport.document_number,
                nationality=passport.nationality,
                source_channel="gmail",
                source_external_id=message.id,
                received_at=message.occurred_at,
            )
            return
        image_afm = extract_afm(extracted)
        database.record_identity(
            reservation_id=reservation_id,
            kind="afm" if image_afm else "passport",
            identifier=image_afm or "",
            nationality="GR" if image_afm else None,
            source_channel="gmail",
            source_external_id=message.id,
            received_at=message.occurred_at,
        )
        return


def correlate_gmail(
    database: BookingDatabase,
    gmail: GmailClient,
    reservation: dict[str, Any],
    config: WorkflowConfig,
    *,
    process_communications: bool = True,
) -> int:
    query = _gmail_query_for_reservation(
        reservation,
        max(config.gmail.sent_search_days, config.gmail.inbox_search_days),
        config,
    )
    if not query:
        return 0
    messages = gmail.search(query, max_results=100)
    email = str(reservation.get("email", "")).strip().lower()
    phone = normalize_phone(str(reservation.get("phone", "")))
    matched = 0
    for message in messages:
        exact_contact = email and (
            message.from_address == email or email in message.to_addresses
        )
        booking_id_match = str(reservation["booking_id"]) in (
            f"{message.subject}\n{message.body}"
        )
        phone_match = bool(phone) and phone.lstrip("+") in re.sub(
            r"\D", "", f"{message.subject}\n{message.body}"
        )
        if booking_id_match and config.matching.auto_match_booking_id:
            match_method, confidence, status = "booking_id", 1.0, "matched"
        elif exact_contact and config.matching.auto_match_exact_email:
            match_method, confidence, status = "exact_email", 0.95, "matched"
        elif phone_match and config.matching.auto_match_exact_phone:
            match_method, confidence, status = "exact_phone", 0.9, "matched"
        else:
            continue
        database.save_gmail_match(
            reservation_id=str(reservation["booking_id"]),
            gmail_thread_id=message.thread_id,
            gmail_message_id=message.id,
            match_method=match_method,
            confidence=confidence,
            status=status,
        )
        if database.gmail_match_status(
            str(reservation["booking_id"]), message.thread_id
        ) != "matched":
            continue
        if process_communications:
            _record_matched_message(database, gmail, reservation, message, config)
        matched += 1

    if matched == 0 and config.matching.require_review_for_name_date:
        name = str(reservation.get("customer_name", "")).strip()
        if name:
            fallback = gmail.search(
                f'newer_than:{config.gmail.inbox_search_days}d "{name}"',
                max_results=25,
            )
            for message in fallback:
                haystack = normalize_name(f"{message.subject}\n{message.body}")
                if normalize_name(name) not in haystack:
                    continue
                existing_status = database.gmail_match_status(
                    str(reservation["booking_id"]), message.thread_id
                )
                database.save_gmail_match(
                    reservation_id=str(reservation["booking_id"]),
                    gmail_thread_id=message.thread_id,
                    gmail_message_id=message.id,
                    match_method="name_date",
                    confidence=0.5,
                    status="matched" if existing_status == "matched" else "review_required",
                )
                effective_status = database.gmail_match_status(
                    str(reservation["booking_id"]), message.thread_id
                )
                if effective_status == "rejected":
                    continue
                matched += 1
                if effective_status != "matched":
                    continue
                if process_communications:
                    _record_matched_message(database, gmail, reservation, message, config)
    return matched


def _reconcile_sent_task(
    database: BookingDatabase,
    gmail: GmailClient,
    reservation: dict[str, Any],
    task: dict[str, Any],
) -> bool:
    email = str(reservation.get("email", "")).strip().lower()
    subject = str(task.get("rendered_subject", "")).strip()
    if not email or not subject:
        return False
    escaped_subject = subject.replace('"', "")
    messages = gmail.search(
        f'in:sent to:{email} subject:"{escaped_subject}" newer_than:30d',
        max_results=10,
    )
    if not messages:
        return False
    message = messages[0]
    database.mark_arrival_task(
        reservation_id=str(reservation["booking_id"]),
        template_kind=str(task["template_kind"]),
        template_version=int(task["template_version"]),
        due_date=date.fromisoformat(str(task["due_date"])),
        state="sent",
        gmail_draft_id=task.get("gmail_draft_id"),
        gmail_thread_id=message.thread_id,
        rendered_subject=subject,
        sent_at=message.occurred_at,
    )
    database.record_communication(
        reservation_id=str(reservation["booking_id"]),
        customer_id=int(reservation["customer_id"]),
        channel="gmail",
        direction="outbound",
        category=str(task["template_kind"]),
        external_id=message.id,
        thread_id=message.thread_id,
        contact_value=email,
        occurred_at=message.occurred_at,
    )
    return True


async def run_arrival_workflow(
    page: Page,
    settings: Settings,
    database: BookingDatabase,
    config: WorkflowConfig,
    *,
    reference_date: date | None = None,
    dry_run: bool = False,
    gmail: GmailClient | None = None,
) -> ArrivalRunResult:
    timezone = ZoneInfo(config.arrivals.property_timezone)
    today = reference_date or datetime.now(timezone).date()
    target_date = today + timedelta(days=config.arrivals.days_before)
    run_id = database.start_sync("arrival_workflow", run_date=today) if not dry_run else None
    try:
        sync_result: ReservationSyncResult = await sync_reservations(
            page, settings, database, status="upcoming"
        )
        reservations = database.list_arrivals(target_date)
        gmail = gmail or GmailClient(require_compose=not dry_run)
        drafts_created = 0
        drafts_existing = 0
        blocked = 0
        for reservation in reservations:
            with log_group(f"arrival {reservation['booking_id']}", logger=logger):
                correlate_gmail(database, gmail, reservation, config)
                email = str(reservation.get("email", "") or "").strip()
                if not email or reservation.get("customer_match_review_required"):
                    blocked += 1
                    logger.warning("Drafting blocked: missing or ambiguous guest email")
                    continue
                for kind in ("instructions", "recommendations"):
                    template = database.active_template(kind)
                    if template is None:
                        blocked += 1
                        logger.warning("Drafting blocked: no approved %s template", kind)
                        continue
                    task = database.arrival_task(
                        str(reservation["booking_id"]), kind, int(template["version"])
                    )
                    if task and task["state"] in {"drafted", "sent"}:
                        if task["state"] == "drafted" and _reconcile_sent_task(
                            database, gmail, reservation, task
                        ):
                            logger.info("Detected manually sent %s email", kind)
                        else:
                            drafts_existing += 1
                            logger.info("Existing %s task: %s", kind, task["state"])
                        continue
                    subject, body = render_arrival_email(template, reservation)
                    if dry_run:
                        drafts_created += 1
                        logger.info("Would create %s draft", kind)
                        continue
                    draft = gmail.create_draft(to=email, subject=subject, body=body)
                    database.mark_arrival_task(
                        reservation_id=str(reservation["booking_id"]),
                        template_kind=kind,
                        template_version=int(template["version"]),
                        due_date=today,
                        state="drafted",
                        gmail_draft_id=draft.draft_id,
                        gmail_thread_id=draft.thread_id,
                        rendered_subject=subject,
                    )
                    drafts_created += 1
                    logger.info("Created %s Gmail draft", kind)

        result = ArrivalRunResult(
            target_date=target_date,
            reservations_synced=sync_result.stored,
            reservations_due=len(reservations),
            drafts_created=drafts_created,
            drafts_existing=drafts_existing,
            blocked=blocked + sync_result.failed,
            dry_run=dry_run,
        )
        if run_id is not None:
            database.finish_sync(
                run_id,
                status="success",
                summary={**result.__dict__, "target_date": result.target_date.isoformat()},
            )
        return result
    except Exception as exc:
        if run_id is not None:
            database.finish_sync(
                run_id,
                status="blocked",
                error_code=type(exc).__name__,
            )
        raise
