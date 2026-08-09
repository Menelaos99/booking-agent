from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    prefix = "+" if value.strip().startswith("+") else ""
    digits = re.sub(r"\D", "", value)
    return f"{prefix}{digits}" if digits else ""


@dataclass(frozen=True)
class CustomerResolution:
    customer_id: int
    method: str
    needs_review: bool = False


@dataclass(frozen=True)
class DatabaseStatus:
    path: Path
    schema_version: int
    customers: int
    reservations: int
    communications: int
    identity_records: int


class BookingDatabase:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            if self.path.exists():
                self.path.chmod(0o600)

    def initialize(self) -> None:
        with self.connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if current < 1:
                self._migrate_v1(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE customer_contacts (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN ('email', 'phone')),
                value TEXT NOT NULL,
                normalized_value TEXT NOT NULL,
                source TEXT NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
                created_at TEXT NOT NULL,
                UNIQUE(customer_id, kind, normalized_value)
            );
            CREATE INDEX customer_contacts_lookup
                ON customer_contacts(kind, normalized_value);

            CREATE TABLE reservations (
                booking_id TEXT PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                hotel_id TEXT NOT NULL,
                check_in TEXT,
                check_out TEXT,
                status TEXT NOT NULL DEFAULT '',
                amount_minor INTEGER,
                currency TEXT,
                amount_raw TEXT,
                room_type TEXT,
                guest_count INTEGER,
                booked_at TEXT,
                expected_arrival_time TEXT,
                preferred_language TEXT,
                declared_country TEXT,
                payment_status TEXT,
                special_requests TEXT,
                commission_minor INTEGER,
                net_payout_minor INTEGER,
                customer_match_method TEXT NOT NULL,
                customer_match_review_required INTEGER NOT NULL DEFAULT 0,
                last_scraped_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX reservations_check_in ON reservations(check_in);

            CREATE TABLE communications (
                id INTEGER PRIMARY KEY,
                reservation_id TEXT REFERENCES reservations(booking_id) ON DELETE SET NULL,
                customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
                channel TEXT NOT NULL CHECK (channel IN ('gmail', 'booking', 'whatsapp')),
                direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                category TEXT NOT NULL,
                external_id TEXT NOT NULL,
                thread_id TEXT,
                contact_value TEXT,
                occurred_at TEXT,
                attachment_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(channel, external_id)
            );

            CREATE TABLE gmail_matches (
                id INTEGER PRIMARY KEY,
                reservation_id TEXT NOT NULL REFERENCES reservations(booking_id) ON DELETE CASCADE,
                gmail_thread_id TEXT NOT NULL,
                gmail_message_id TEXT,
                match_method TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('matched', 'review_required', 'rejected')),
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                UNIQUE(reservation_id, gmail_thread_id)
            );

            CREATE TABLE email_templates (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('instructions', 'recommendations')),
                version INTEGER NOT NULL,
                subject_template TEXT NOT NULL,
                body_template TEXT NOT NULL,
                source_message_id TEXT,
                approved_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                UNIQUE(kind, version)
            );

            CREATE TABLE arrival_tasks (
                id INTEGER PRIMARY KEY,
                reservation_id TEXT NOT NULL REFERENCES reservations(booking_id) ON DELETE CASCADE,
                template_kind TEXT NOT NULL CHECK (template_kind IN ('instructions', 'recommendations')),
                template_version INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                gmail_draft_id TEXT,
                gmail_thread_id TEXT,
                rendered_subject TEXT,
                state TEXT NOT NULL CHECK (state IN ('pending', 'drafted', 'sent', 'blocked')),
                blocked_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE(reservation_id, template_kind, template_version)
            );

            CREATE TABLE identity_records (
                id INTEGER PRIMARY KEY,
                reservation_id TEXT NOT NULL REFERENCES reservations(booking_id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN ('passport', 'afm')),
                identifier TEXT NOT NULL,
                nationality TEXT,
                source_channel TEXT NOT NULL CHECK (source_channel IN ('gmail', 'whatsapp')),
                source_external_id TEXT,
                received_at TEXT NOT NULL,
                verification_status TEXT NOT NULL CHECK (
                    verification_status IN ('needs_review', 'verified', 'rejected')
                ),
                verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(reservation_id, kind)
            );

            CREATE TABLE sync_runs (
                id INTEGER PRIMARY KEY,
                job_type TEXT NOT NULL,
                run_date TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'blocked')),
                summary_json TEXT,
                error_code TEXT
            );
            CREATE INDEX sync_runs_job_date ON sync_runs(job_type, run_date, status);
            """
        )

    def status(self) -> DatabaseStatus:
        self.initialize()
        with self.connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("customers", "reservations", "communications", "identity_records")
            }
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return DatabaseStatus(
            path=self.path,
            schema_version=version,
            customers=counts["customers"],
            reservations=counts["reservations"],
            communications=counts["communications"],
            identity_records=counts["identity_records"],
        )

    def _resolve_customer(
        self,
        connection: sqlite3.Connection,
        *,
        display_name: str,
        email: str = "",
        phone: str = "",
        existing_customer_id: int | None = None,
    ) -> CustomerResolution:
        now = utc_now()
        if existing_customer_id is not None:
            connection.execute(
                "UPDATE customers SET display_name = ?, normalized_name = ?, updated_at = ? WHERE id = ?",
                (display_name, normalize_name(display_name), now, existing_customer_id),
            )
            resolution = CustomerResolution(existing_customer_id, "existing_reservation")
        else:
            matches: dict[int, set[str]] = {}
            normalized = {
                "email": normalize_email(email) if email else "",
                "phone": normalize_phone(phone) if phone else "",
            }
            for kind, value in normalized.items():
                if not value:
                    continue
                rows = connection.execute(
                    "SELECT customer_id FROM customer_contacts WHERE kind = ? AND normalized_value = ?",
                    (kind, value),
                ).fetchall()
                for row in rows:
                    matches.setdefault(int(row["customer_id"]), set()).add(kind)

            if len(matches) == 1:
                customer_id = next(iter(matches))
                method = "+".join(sorted(matches[customer_id]))
                connection.execute(
                    "UPDATE customers SET display_name = ?, normalized_name = ?, updated_at = ? WHERE id = ?",
                    (display_name, normalize_name(display_name), now, customer_id),
                )
                resolution = CustomerResolution(customer_id, f"exact_{method}")
            else:
                cursor = connection.execute(
                    "INSERT INTO customers(display_name, normalized_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (display_name, normalize_name(display_name), now, now),
                )
                resolution = CustomerResolution(
                    int(cursor.lastrowid),
                    "new_customer" if not matches else "contact_conflict",
                    needs_review=bool(matches),
                )

        for kind, value, normalized_value in (
            ("email", email, normalize_email(email) if email else ""),
            ("phone", phone, normalize_phone(phone) if phone else ""),
        ):
            if not normalized_value:
                continue
            connection.execute(
                "UPDATE customer_contacts SET is_primary = 0 WHERE customer_id = ? AND kind = ?",
                (resolution.customer_id, kind),
            )
            connection.execute(
                """
                INSERT INTO customer_contacts(
                    customer_id, kind, value, normalized_value, source, is_primary, created_at
                ) VALUES (?, ?, ?, ?, 'booking', 1, ?)
                ON CONFLICT(customer_id, kind, normalized_value) DO UPDATE SET
                    value = excluded.value,
                    source = excluded.source,
                    is_primary = 1
                """,
                (resolution.customer_id, kind, value.strip(), normalized_value, now),
            )
        return resolution

    def upsert_reservation(self, record: dict[str, Any]) -> CustomerResolution:
        self.initialize()
        booking_id = str(record.get("booking_id", "")).strip()
        if not booking_id:
            raise ValueError("reservation booking_id is required")
        display_name = str(record.get("guest_name", "")).strip() or "Unknown guest"
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT customer_id, created_at FROM reservations WHERE booking_id = ?",
                (booking_id,),
            ).fetchone()
            resolution = self._resolve_customer(
                connection,
                display_name=display_name,
                email=str(record.get("guest_email", "") or ""),
                phone=str(record.get("guest_phone", "") or ""),
                existing_customer_id=int(existing["customer_id"]) if existing else None,
            )
            values = (
                booking_id,
                resolution.customer_id,
                str(record.get("hotel_id", "")),
                record.get("check_in"),
                record.get("check_out"),
                str(record.get("status", "") or ""),
                record.get("amount_minor"),
                record.get("currency"),
                record.get("amount_raw"),
                record.get("room_type"),
                record.get("guest_count"),
                record.get("booked_at"),
                record.get("expected_arrival_time"),
                record.get("preferred_language"),
                record.get("declared_country"),
                record.get("payment_status"),
                record.get("special_requests"),
                record.get("commission_minor"),
                record.get("net_payout_minor"),
                resolution.method,
                int(resolution.needs_review),
                now,
                existing["created_at"] if existing else now,
                now,
            )
            connection.execute(
                """
                INSERT INTO reservations(
                    booking_id, customer_id, hotel_id, check_in, check_out, status,
                    amount_minor, currency, amount_raw, room_type, guest_count, booked_at,
                    expected_arrival_time, preferred_language, declared_country,
                    payment_status, special_requests, commission_minor, net_payout_minor,
                    customer_match_method, customer_match_review_required,
                    last_scraped_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(booking_id) DO UPDATE SET
                    customer_id = excluded.customer_id,
                    hotel_id = excluded.hotel_id,
                    check_in = excluded.check_in,
                    check_out = excluded.check_out,
                    status = excluded.status,
                    amount_minor = excluded.amount_minor,
                    currency = excluded.currency,
                    amount_raw = excluded.amount_raw,
                    room_type = excluded.room_type,
                    guest_count = excluded.guest_count,
                    booked_at = excluded.booked_at,
                    expected_arrival_time = excluded.expected_arrival_time,
                    preferred_language = excluded.preferred_language,
                    declared_country = excluded.declared_country,
                    payment_status = excluded.payment_status,
                    special_requests = excluded.special_requests,
                    commission_minor = excluded.commission_minor,
                    net_payout_minor = excluded.net_payout_minor,
                    customer_match_method = excluded.customer_match_method,
                    customer_match_review_required = excluded.customer_match_review_required,
                    last_scraped_at = excluded.last_scraped_at,
                    updated_at = excluded.updated_at
                """,
                values,
            )
        return resolution

    def list_arrivals(self, arrival_date: date | None = None) -> list[dict[str, Any]]:
        self.initialize()
        where = "WHERE r.check_in = ?" if arrival_date else ""
        parameters: tuple[str, ...] = (arrival_date.isoformat(),) if arrival_date else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    r.*,
                    c.display_name AS customer_name,
                    (SELECT value FROM customer_contacts cc
                     WHERE cc.customer_id = c.id AND cc.kind = 'email'
                     ORDER BY cc.is_primary DESC, cc.id LIMIT 1) AS email,
                    (SELECT value FROM customer_contacts cc
                     WHERE cc.customer_id = c.id AND cc.kind = 'phone'
                     ORDER BY cc.is_primary DESC, cc.id LIMIT 1) AS phone,
                    CAST(julianday(r.check_out) - julianday(r.check_in) AS INTEGER) AS nights,
                    (SELECT state FROM arrival_tasks a
                     WHERE a.reservation_id = r.booking_id AND a.template_kind = 'instructions'
                     ORDER BY a.template_version DESC LIMIT 1) AS instructions_status,
                    (SELECT state FROM arrival_tasks a
                     WHERE a.reservation_id = r.booking_id AND a.template_kind = 'recommendations'
                     ORDER BY a.template_version DESC LIMIT 1) AS recommendations_status,
                    (SELECT verification_status FROM identity_records i
                     WHERE i.reservation_id = r.booking_id
                     ORDER BY i.updated_at DESC LIMIT 1) AS identity_status,
                    (SELECT CASE
                        WHEN EXISTS (
                            SELECT 1 FROM gmail_matches gm
                            WHERE gm.reservation_id = r.booking_id
                              AND gm.status = 'review_required'
                        ) THEN 'review_required'
                        WHEN EXISTS (
                            SELECT 1 FROM gmail_matches gm
                            WHERE gm.reservation_id = r.booking_id
                              AND gm.status = 'matched'
                        ) THEN 'matched'
                        WHEN EXISTS (
                            SELECT 1 FROM gmail_matches gm
                            WHERE gm.reservation_id = r.booking_id
                              AND gm.status = 'rejected'
                        ) THEN 'rejected'
                        ELSE NULL
                    END) AS gmail_match_status,
                    (SELECT MAX(occurred_at) FROM communications m
                     WHERE m.reservation_id = r.booking_id) AS last_contact
                FROM reservations r
                JOIN customers c ON c.id = r.customer_id
                {where}
                ORDER BY r.check_in, c.display_name
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_reservation(self, booking_id: str) -> dict[str, Any] | None:
        rows = [row for row in self.list_arrivals() if row["booking_id"] == booking_id]
        return rows[0] if rows else None

    def list_pending_arrival_tasks(
        self,
        *,
        arrival_date: date | None = None,
        from_date: date | None = None,
        status: Literal[
            "all",
            "action_required",
            "draft_pending",
            "identity_review",
            "match_review",
        ] = "action_required",
    ) -> list[dict[str, Any]]:
        """Return an agent-safe operational view without contact or identity values."""

        rows = self.list_arrivals(arrival_date)
        safe_rows: list[dict[str, Any]] = []
        for row in rows:
            if from_date and str(row.get("check_in") or "") < from_date.isoformat():
                continue
            instructions = str(row.get("instructions_status") or "pending")
            recommendations = str(row.get("recommendations_status") or "pending")
            identity = str(row.get("identity_status") or "missing")
            gmail_match = str(row.get("gmail_match_status") or "missing")
            draft_pending = instructions not in {"drafted", "sent"} or recommendations not in {
                "drafted",
                "sent",
            }
            identity_review = identity in {"missing", "needs_review"}
            match_review = bool(row.get("customer_match_review_required")) or gmail_match == (
                "review_required"
            )
            missing_email = not bool(str(row.get("email") or "").strip())
            action_required = draft_pending or identity_review or match_review or missing_email
            include = {
                "all": True,
                "action_required": action_required,
                "draft_pending": draft_pending,
                "identity_review": identity_review,
                "match_review": match_review,
            }[status]
            if not include:
                continue
            reasons: list[str] = []
            if missing_email:
                reasons.append("missing_email")
            if draft_pending:
                reasons.append("draft_pending")
            if identity_review:
                reasons.append("identity_review")
            if match_review:
                reasons.append("match_review")
            safe_rows.append(
                {
                    "booking_id": row["booking_id"],
                    "customer_name": row["customer_name"],
                    "arrival_date": row.get("check_in"),
                    "checkout_date": row.get("check_out"),
                    "email_available": not missing_email,
                    "instructions_status": instructions,
                    "recommendations_status": recommendations,
                    "identity_status": identity,
                    "gmail_match_status": gmail_match,
                    "action_reasons": reasons,
                }
            )
        return safe_rows

    def save_template(
        self,
        *,
        kind: Literal["instructions", "recommendations"],
        subject_template: str,
        body_template: str,
        source_message_id: str | None,
    ) -> int:
        self.initialize()
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM email_templates WHERE kind = ?",
                (kind,),
            ).fetchone()
            version = int(row["version"])
            connection.execute("UPDATE email_templates SET active = 0 WHERE kind = ?", (kind,))
            connection.execute(
                """
                INSERT INTO email_templates(
                    kind, version, subject_template, body_template, source_message_id, approved_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (kind, version, subject_template, body_template, source_message_id, now),
            )
        return version

    def active_template(self, kind: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM email_templates WHERE kind = ? AND active = 1 ORDER BY version DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return dict(row) if row else None

    def record_communication(
        self,
        *,
        reservation_id: str | None,
        customer_id: int | None,
        channel: Literal["gmail", "booking", "whatsapp"],
        direction: Literal["inbound", "outbound"],
        category: str,
        external_id: str,
        thread_id: str | None,
        contact_value: str | None,
        occurred_at: str | None,
        attachment_count: int = 0,
    ) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO communications(
                    reservation_id, customer_id, channel, direction, category,
                    external_id, thread_id, contact_value, occurred_at,
                    attachment_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, external_id) DO UPDATE SET
                    reservation_id = COALESCE(excluded.reservation_id, communications.reservation_id),
                    customer_id = COALESCE(excluded.customer_id, communications.customer_id),
                    category = excluded.category,
                    thread_id = excluded.thread_id,
                    contact_value = excluded.contact_value,
                    occurred_at = excluded.occurred_at,
                    attachment_count = excluded.attachment_count
                """,
                (
                    reservation_id,
                    customer_id,
                    channel,
                    direction,
                    category,
                    external_id,
                    thread_id,
                    contact_value,
                    occurred_at,
                    attachment_count,
                    utc_now(),
                ),
            )

    def has_communication(self, channel: str, external_id: str) -> bool:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM communications WHERE channel = ? AND external_id = ? LIMIT 1",
                (channel, external_id),
            ).fetchone()
        return row is not None

    def identity_for_reservation(self, reservation_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM identity_records WHERE reservation_id = ? ORDER BY updated_at DESC",
                (reservation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_gmail_match(
        self,
        *,
        reservation_id: str,
        gmail_thread_id: str,
        gmail_message_id: str | None,
        match_method: str,
        confidence: float,
        status: Literal["matched", "review_required", "rejected"],
    ) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO gmail_matches(
                    reservation_id, gmail_thread_id, gmail_message_id,
                    match_method, confidence, status, created_at, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(reservation_id, gmail_thread_id) DO UPDATE SET
                    gmail_message_id = excluded.gmail_message_id,
                    match_method = excluded.match_method,
                    confidence = excluded.confidence,
                    status = CASE
                        WHEN gmail_matches.status IN ('matched', 'rejected') THEN gmail_matches.status
                        ELSE excluded.status
                    END
                """,
                (
                    reservation_id,
                    gmail_thread_id,
                    gmail_message_id,
                    match_method,
                    confidence,
                    status,
                    utc_now(),
                ),
            )

    def gmail_match_status(self, reservation_id: str, thread_id: str) -> str | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM gmail_matches WHERE reservation_id = ? AND gmail_thread_id = ?",
                (reservation_id, thread_id),
            ).fetchone()
        return str(row["status"]) if row else None

    def pending_gmail_matches(self) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT gm.*, c.display_name AS customer_name, r.check_in
                FROM gmail_matches gm
                JOIN reservations r ON r.booking_id = gm.reservation_id
                JOIN customers c ON c.id = r.customer_id
                WHERE gm.status = 'review_required'
                ORDER BY r.check_in, c.display_name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_gmail_matches(
        self,
        *,
        reservation_id: str | None = None,
        status: Literal["matched", "review_required", "rejected"] | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[str] = []
        if reservation_id:
            clauses.append("gm.reservation_id = ?")
            parameters.append(reservation_id)
        if status:
            clauses.append("gm.status = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    gm.id,
                    gm.reservation_id,
                    gm.match_method,
                    gm.confidence,
                    gm.status,
                    gm.created_at,
                    gm.reviewed_at,
                    c.display_name AS customer_name,
                    r.check_in,
                    CASE WHEN gm.gmail_message_id IS NULL THEN 0 ELSE 1 END AS preview_available
                FROM gmail_matches gm
                JOIN reservations r ON r.booking_id = gm.reservation_id
                JOIN customers c ON c.id = r.customer_id
                {where}
                ORDER BY r.check_in, c.display_name, gm.id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_gmail_match(self, match_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT gm.*, c.display_name AS customer_name, r.check_in
                FROM gmail_matches gm
                JOIN reservations r ON r.booking_id = gm.reservation_id
                JOIN customers c ON c.id = r.customer_id
                WHERE gm.id = ?
                """,
                (match_id,),
            ).fetchone()
        return dict(row) if row else None

    def review_gmail_match(self, match_id: int, *, accepted: bool) -> bool:
        self.initialize()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE gmail_matches SET status = ?, reviewed_at = ?
                WHERE id = ? AND status = 'review_required'
                """,
                ("matched" if accepted else "rejected", utc_now(), match_id),
            )
        return cursor.rowcount > 0

    def mark_arrival_task(
        self,
        *,
        reservation_id: str,
        template_kind: str,
        template_version: int,
        due_date: date,
        state: Literal["pending", "drafted", "sent", "blocked"],
        gmail_draft_id: str | None = None,
        gmail_thread_id: str | None = None,
        rendered_subject: str | None = None,
        blocked_reason: str | None = None,
        sent_at: str | None = None,
    ) -> None:
        self.initialize()
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO arrival_tasks(
                    reservation_id, template_kind, template_version, due_date,
                    gmail_draft_id, gmail_thread_id, rendered_subject, state,
                    blocked_reason, created_at, updated_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(reservation_id, template_kind, template_version) DO UPDATE SET
                    gmail_draft_id = COALESCE(excluded.gmail_draft_id, arrival_tasks.gmail_draft_id),
                    gmail_thread_id = COALESCE(excluded.gmail_thread_id, arrival_tasks.gmail_thread_id),
                    rendered_subject = COALESCE(excluded.rendered_subject, arrival_tasks.rendered_subject),
                    state = excluded.state,
                    blocked_reason = excluded.blocked_reason,
                    updated_at = excluded.updated_at,
                    sent_at = COALESCE(excluded.sent_at, arrival_tasks.sent_at)
                """,
                (
                    reservation_id,
                    template_kind,
                    template_version,
                    due_date.isoformat(),
                    gmail_draft_id,
                    gmail_thread_id,
                    rendered_subject,
                    state,
                    blocked_reason,
                    now,
                    now,
                    sent_at,
                ),
            )

    def arrival_task(self, reservation_id: str, kind: str, version: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM arrival_tasks
                WHERE reservation_id = ? AND template_kind = ? AND template_version = ?
                """,
                (reservation_id, kind, version),
            ).fetchone()
        return dict(row) if row else None

    def record_identity(
        self,
        *,
        reservation_id: str,
        kind: Literal["passport", "afm"],
        identifier: str,
        nationality: str | None,
        source_channel: Literal["gmail", "whatsapp"],
        source_external_id: str | None = None,
        received_at: str | None = None,
    ) -> None:
        self.initialize()
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO identity_records(
                    reservation_id, kind, identifier, nationality, source_channel,
                    source_external_id, received_at, verification_status,
                    verified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'needs_review', NULL, ?, ?)
                ON CONFLICT(reservation_id, kind) DO UPDATE SET
                    identifier = excluded.identifier,
                    nationality = excluded.nationality,
                    source_channel = excluded.source_channel,
                    source_external_id = excluded.source_external_id,
                    received_at = excluded.received_at,
                    verification_status = 'needs_review',
                    verified_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    reservation_id,
                    kind,
                    identifier.strip().upper(),
                    nationality.strip().upper() if nationality else None,
                    source_channel,
                    source_external_id,
                    received_at or now,
                    now,
                    now,
                ),
            )

    def verify_identity(self, reservation_id: str, *, accepted: bool) -> bool:
        self.initialize()
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE identity_records
                SET verification_status = ?, verified_at = ?, updated_at = ?
                WHERE reservation_id = ? AND verification_status = 'needs_review'
                """,
                ("verified" if accepted else "rejected", now, now, reservation_id),
            )
        return cursor.rowcount > 0

    def safe_identity_status(self, reservation_id: str) -> dict[str, Any] | None:
        """Return identity workflow state without document identifiers or nationality."""

        self.initialize()
        with self.connect() as connection:
            reservation = connection.execute(
                "SELECT booking_id FROM reservations WHERE booking_id = ?",
                (reservation_id,),
            ).fetchone()
            if reservation is None:
                return None
            rows = connection.execute(
                """
                SELECT kind, source_channel, received_at, verification_status, verified_at
                FROM identity_records
                WHERE reservation_id = ?
                ORDER BY updated_at DESC
                """,
                (reservation_id,),
            ).fetchall()
        records = [dict(row) for row in rows]
        overall = "missing"
        if records:
            statuses = {str(row["verification_status"]) for row in records}
            if "needs_review" in statuses:
                overall = "needs_review"
            elif "verified" in statuses:
                overall = "verified"
            else:
                overall = "rejected"
        return {
            "booking_id": reservation_id,
            "status": overall,
            "records": records,
        }

    def start_sync(self, job_type: str, *, run_date: date | None = None) -> int:
        self.initialize()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO sync_runs(job_type, run_date, started_at, status) VALUES (?, ?, ?, 'running')",
                (job_type, run_date.isoformat() if run_date else None, utc_now()),
            )
        return int(cursor.lastrowid)

    def finish_sync(
        self,
        run_id: int,
        *,
        status: Literal["success", "failed", "blocked"],
        summary: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?, status = ?, summary_json = ?, error_code = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status,
                    json.dumps(summary or {}, sort_keys=True),
                    error_code,
                    run_id,
                ),
            )

    def successful_run_exists(self, job_type: str, run_date: date) -> bool:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM sync_runs
                WHERE job_type = ? AND run_date = ? AND status = 'success'
                LIMIT 1
                """,
                (job_type, run_date.isoformat()),
            ).fetchone()
        return row is not None
