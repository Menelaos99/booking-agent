from datetime import date
from pathlib import Path

from booking_agent.storage.database import BookingDatabase


def reservation(**overrides):
    data = {
        "booking_id": "B-100",
        "hotel_id": "7455203",
        "guest_name": "Alice Example",
        "guest_email": "Alice@Example.com",
        "guest_phone": "+30 690 000 0000",
        "check_in": "2026-08-13",
        "check_out": "2026-08-16",
        "status": "confirmed",
        "amount_minor": 34567,
        "currency": "EUR",
        "amount_raw": "€345.67",
    }
    data.update(overrides)
    return data


def test_database_initializes_and_upserts_idempotently(tmp_path: Path) -> None:
    database = BookingDatabase(tmp_path / "state" / "booking.sqlite3")
    first = database.upsert_reservation(reservation())
    second = database.upsert_reservation(reservation(status="modified"))

    assert first.method == "new_customer"
    assert second.method == "existing_reservation"
    status = database.status()
    assert status.schema_version == 1
    assert status.customers == 1
    assert status.reservations == 1
    assert database.path.stat().st_mode & 0o777 == 0o600
    assert database.get_reservation("B-100")["status"] == "modified"


def test_exact_email_links_repeat_customer_but_name_does_not(tmp_path: Path) -> None:
    database = BookingDatabase(tmp_path / "booking.sqlite3")
    database.upsert_reservation(reservation())
    matched = database.upsert_reservation(
        reservation(booking_id="B-101", guest_name="Alice E.", guest_phone="")
    )
    unmatched = database.upsert_reservation(
        reservation(
            booking_id="B-102",
            guest_email="different@example.com",
            guest_phone="",
        )
    )

    assert matched.method == "exact_email"
    assert unmatched.method == "new_customer"
    assert database.status().customers == 2


def test_reservation_contact_change_selects_the_new_primary_email(tmp_path: Path) -> None:
    database = BookingDatabase(tmp_path / "booking.sqlite3")
    database.upsert_reservation(reservation())
    database.upsert_reservation(
        reservation(guest_email="new-address@example.com")
    )

    assert database.get_reservation("B-100")["email"] == "new-address@example.com"


def test_arrival_tasks_and_identity_are_exposed_in_arrivals(tmp_path: Path) -> None:
    database = BookingDatabase(tmp_path / "booking.sqlite3")
    database.upsert_reservation(reservation())
    version = database.save_template(
        kind="instructions",
        subject_template="Welcome {{ customer_name }}",
        body_template="Arrival {{ arrival_date }}",
        source_message_id="gmail-1",
    )
    database.mark_arrival_task(
        reservation_id="B-100",
        template_kind="instructions",
        template_version=version,
        due_date=date(2026, 8, 9),
        state="drafted",
        gmail_draft_id="draft-1",
    )
    database.record_identity(
        reservation_id="B-100",
        kind="afm",
        identifier="123456783",
        nationality="GR",
        source_channel="whatsapp",
    )

    row = database.list_arrivals(date(2026, 8, 13))[0]
    assert row["nights"] == 3
    assert row["instructions_status"] == "drafted"
    assert row["identity_status"] == "needs_review"
    assert database.verify_identity("B-100", accepted=True)
    assert database.list_arrivals()[0]["identity_status"] == "verified"


def test_prime_views_exclude_contact_and_identity_values(tmp_path: Path) -> None:
    database = BookingDatabase(tmp_path / "booking.sqlite3")
    database.upsert_reservation(reservation())
    database.record_identity(
        reservation_id="B-100",
        kind="passport",
        identifier="AB1234567",
        nationality="GBR",
        source_channel="gmail",
        source_external_id="message-1",
    )
    database.save_gmail_match(
        reservation_id="B-100",
        gmail_thread_id="thread-1",
        gmail_message_id="message-1",
        match_method="name_date",
        confidence=0.5,
        status="review_required",
    )

    task = database.list_pending_arrival_tasks(
        arrival_date=date(2026, 8, 13),
        status="action_required",
    )[0]
    identity = database.safe_identity_status("B-100")
    match = database.list_gmail_matches(reservation_id="B-100")[0]

    assert task["email_available"] is True
    assert "email" not in task
    assert "phone" not in task
    assert identity["status"] == "needs_review"
    assert "identifier" not in identity["records"][0]
    assert "nationality" not in identity["records"][0]
    assert "gmail_message_id" not in match
    assert "gmail_thread_id" not in match
