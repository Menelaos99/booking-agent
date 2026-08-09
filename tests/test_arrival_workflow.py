import asyncio
from datetime import date
from pathlib import Path

from booking_agent.config import Settings
from booking_agent.integrations.gmail import CreatedDraft, GmailMessage
from booking_agent.storage.database import BookingDatabase
from booking_agent.workflow_config import WorkflowConfig
from booking_agent.workflows import arrival
from booking_agent.workflows.arrival import (
    _gmail_query_for_reservation,
    render_arrival_email,
    render_template,
)
from booking_agent.workflows.reservation_sync import ReservationSyncResult


def test_render_template_rejects_unknown_placeholder() -> None:
    try:
        render_template("Hello {{ unknown }}", {"customer_name": "Alice"})
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("unknown placeholders must fail")


def test_instructions_append_identity_request() -> None:
    template = {
        "kind": "instructions",
        "subject_template": "Arrival {{ arrival_date }}",
        "body_template": "Hello {{ customer_name }}",
    }
    reservation = {
        "customer_name": "Alice",
        "check_in": "2026-08-13",
        "check_out": "2026-08-16",
        "nights": 3,
        "booking_id": "B-100",
    }

    subject, body = render_arrival_email(template, reservation)

    assert subject == "Arrival 2026-08-13"
    assert "ΑΦΜ" in body
    assert "passport" in body


def test_gmail_query_uses_an_or_group_for_exact_identifiers() -> None:
    query = _gmail_query_for_reservation(
        {
            "booking_id": "B-100",
            "email": "alice@example.com",
            "phone": "+30 690 000 0000",
        },
        180,
        WorkflowConfig(),
    )

    assert query.startswith("newer_than:180d {")
    assert '"B-100"' in query
    assert "from:alice@example.com" in query
    assert "to:alice@example.com" in query


def test_arrival_drafts_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    database = BookingDatabase(tmp_path / "booking.sqlite3")
    database.upsert_reservation(
        {
            "booking_id": "B-200",
            "hotel_id": "7455203",
            "guest_name": "Alice Example",
            "guest_email": "alice@example.com",
            "check_in": "2026-08-13",
            "check_out": "2026-08-16",
            "status": "confirmed",
        }
    )
    for kind in ("instructions", "recommendations"):
        database.save_template(
            kind=kind,
            subject_template=f"{kind.title()} {{{{ arrival_date }}}}",
            body_template="Hello {{ customer_name }}",
            source_message_id=f"source-{kind}",
        )

    async def fake_sync(*args, **kwargs):
        return ReservationSyncResult(1, 1, 0, 0)

    monkeypatch.setattr(arrival, "sync_reservations", fake_sync)

    class FakeGmail:
        def __init__(self):
            self.created = []

        def search(self, query, *, max_results=100):
            return []

        def create_draft(self, *, to, subject, body):
            self.created.append((to, subject, body))
            number = len(self.created)
            return CreatedDraft(
                draft_id=f"draft-{number}",
                message_id=f"message-{number}",
                thread_id=f"thread-{number}",
            )

    gmail = FakeGmail()
    settings = Settings(
        _env_file=None,
        booking_email="host@example.com",
        booking_password="password",
    )
    config = WorkflowConfig()

    first = asyncio.run(
        arrival.run_arrival_workflow(
            object(),
            settings,
            database,
            config,
            reference_date=date(2026, 8, 9),
            gmail=gmail,
        )
    )
    second = asyncio.run(
        arrival.run_arrival_workflow(
            object(),
            settings,
            database,
            config,
            reference_date=date(2026, 8, 9),
            gmail=gmail,
        )
    )

    assert first.drafts_created == 2
    assert second.drafts_created == 0
    assert second.drafts_existing == 2
    assert len(gmail.created) == 2


def test_rejected_gmail_match_is_not_processed_as_exact_match(
    tmp_path: Path, monkeypatch
) -> None:
    database = BookingDatabase(tmp_path / "booking.sqlite3")
    database.upsert_reservation(
        {
            "booking_id": "B-300",
            "hotel_id": "7455203",
            "guest_name": "Alice Example",
            "guest_email": "alice@example.com",
            "check_in": "2026-08-13",
            "check_out": "2026-08-16",
            "status": "confirmed",
        }
    )
    database.save_gmail_match(
        reservation_id="B-300",
        gmail_thread_id="thread-rejected",
        gmail_message_id="message-rejected",
        match_method="name_date",
        confidence=0.5,
        status="rejected",
    )

    class FakeGmail:
        def search(self, query, *, max_results=100):
            return [
                GmailMessage(
                    id="message-rejected",
                    thread_id="thread-rejected",
                    from_address="alice@example.com",
                    subject="Booking B-300",
                    body="Booking B-300",
                )
            ]

    processed = []
    monkeypatch.setattr(
        arrival,
        "_record_matched_message",
        lambda *args, **kwargs: processed.append(True),
    )

    count = arrival.correlate_gmail(
        database,
        FakeGmail(),
        database.get_reservation("B-300"),
        WorkflowConfig(),
    )

    assert count == 0
    assert processed == []
    assert database.list_gmail_matches(reservation_id="B-300")[0]["status"] == "rejected"
