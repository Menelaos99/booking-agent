from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

PRIME_SKILL_SRC = (
    Path(__file__).resolve().parents[1]
    / ".prime"
    / "agent"
    / "skills"
    / "booking-extranet"
    / "src"
)
sys.path.insert(0, str(PRIME_SKILL_SRC))

import booking_extranet  # noqa: E402


def test_stage_reply_requires_stable_reference() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(
            booking_extranet.stage_reply(
                "index:0",
                "Example Guest",
                "Thank you for your message.",
            )
        )


def test_pending_reply_can_be_cancelled_once() -> None:
    staged = asyncio.run(
        booking_extranet.stage_reply(
            "data:thread-id:thread-123",
            "Example Guest",
            "Thank you for your message.",
        )
    )
    cancelled = asyncio.run(
        booking_extranet.confirm_reply(str(staged["pending_id"]), False)
    )
    consumed = asyncio.run(
        booking_extranet.confirm_reply(str(staged["pending_id"]), False)
    )

    assert cancelled["cancelled"] is True
    assert consumed["ok"] is False


def test_arrival_tools_are_scoped_to_typed_identifiers(monkeypatch) -> None:
    calls = []

    async def fake_run(operation, arguments):
        calls.append((operation, arguments))
        return {"ok": True, "operation": operation, "result": {}}

    monkeypatch.setattr(booking_extranet, "_run_json_cli", fake_run)

    asyncio.run(booking_extranet.refresh_gmail_matches("B-100"))
    asyncio.run(booking_extranet.preview_gmail_match(7))
    asyncio.run(booking_extranet.review_gmail_match(7, False))
    asyncio.run(booking_extranet.prepare_arrival_drafts("2026-08-09"))
    asyncio.run(booking_extranet.identity_status("B-100"))

    assert calls[0][1] == ["arrivals", "refresh-matches", "B-100", "--json"]
    assert calls[1][1] == ["arrivals", "preview-match", "7", "--json"]
    assert "--reject" in calls[2][1]
    assert calls[3][1] == ["arrivals", "run", "--json", "--date", "2026-08-09"]
    assert calls[4][1] == ["identity", "status", "B-100", "--json"]


def test_arrival_tools_reject_unscoped_inputs() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(booking_extranet.refresh_gmail_matches("B-100 OR newer_than:1d"))
    with pytest.raises(ValidationError):
        asyncio.run(booking_extranet.preview_gmail_match(0))
    with pytest.raises(ValidationError):
        asyncio.run(booking_extranet.prepare_arrival_drafts("next Friday"))


def test_safe_read_retries_transient_failures_then_succeeds(monkeypatch) -> None:
    execute = AsyncMock(
        side_effect=[
            booking_extranet._CliAttempt(1, "", "Service unavailable"),
            booking_extranet._CliAttempt(1, "", "database is locked"),
            booking_extranet._CliAttempt(0, "[]", ""),
        ]
    )
    monkeypatch.setattr(booking_extranet, "_execute_cli_once", execute)
    monkeypatch.setattr(booking_extranet.asyncio, "sleep", AsyncMock())

    result = asyncio.run(
        booking_extranet._run_cli("list_messages", ["messages", "list", "--json"])
    )

    assert result["ok"] is True
    assert result["retry"]["decision"] == "complete"
    assert result["retry"]["attempts"] == 3
    assert execute.await_count == 3


def test_auth_failure_does_not_repeat_the_original_operation(monkeypatch) -> None:
    execute = AsyncMock(
        return_value=booking_extranet._CliAttempt(
            1,
            "",
            "BOOKING_AUTH_REQUIRED: Saved Booking session is missing or expired",
        )
    )
    monkeypatch.setattr(booking_extranet, "_execute_cli_once", execute)

    result = asyncio.run(
        booking_extranet._run_cli("list_messages", ["messages", "list", "--json"])
    )

    assert result["ok"] is False
    assert result["retry"]["decision"] == "authenticate_then_retry"
    assert result["retry"]["error_code"] == "booking_auth_required"
    assert execute.await_count == 1


def test_ambiguous_write_is_never_automatically_retried(monkeypatch) -> None:
    execute = AsyncMock(
        return_value=booking_extranet._CliAttempt(
            124,
            "",
            "Booking CLI timed out",
            timed_out=True,
        )
    )
    monkeypatch.setattr(booking_extranet, "_execute_cli_once", execute)

    result = asyncio.run(
        booking_extranet._run_cli(
            "prepare_arrival_drafts",
            ["arrivals", "run", "--json"],
        )
    )

    assert result["ok"] is False
    assert result["retry"]["decision"] == "inspect_before_retry"
    assert result["retry"]["error_code"] == "ambiguous_write_result"
    assert execute.await_count == 1


def test_auth_events_explain_when_original_operation_can_retry() -> None:
    verified = booking_extranet._auth_payload(
        booking_extranet.AuthStatus(event="verified", message="done")
    )
    sms = booking_extranet._auth_payload(
        booking_extranet.AuthStatus(event="sms_code_required", message="enter code")
    )
    failed = booking_extranet._auth_payload(
        booking_extranet.AuthStatus(event="error", message="failed")
    )

    assert verified["retry"]["decision"] == "retry_original_once"
    assert sms["retry"]["decision"] == "wait_for_user"
    assert failed["retry"]["decision"] == "do_not_retry"


def test_prime_cli_environment_is_noninteractive_but_auth_worker_is_not() -> None:
    assert booking_extranet._clean_environment()["BOOKING_AGENT_NONINTERACTIVE"] == "1"
    assert (
        "BOOKING_AGENT_NONINTERACTIVE"
        not in booking_extranet._clean_environment(noninteractive=False)
    )


def test_navigation_read_retries_transient_failure(monkeypatch) -> None:
    execute = AsyncMock(
        side_effect=[
            booking_extranet._NavigationAttempt(
                ok=False,
                error="navigation_timeout: Navigation request timed out",
                timed_out=True,
            ),
            booking_extranet._NavigationAttempt(
                ok=True,
                payload={
                    "ok": True,
                    "result": [{"date": "2026-08-10"}],
                    "navigation": {"section": "calendar"},
                },
            ),
        ]
    )
    monkeypatch.setattr(booking_extranet, "_execute_navigation_once", execute)
    monkeypatch.setattr(booking_extranet.asyncio, "sleep", AsyncMock())

    result = asyncio.run(booking_extranet.open_calendar("2026-08"))

    assert result["ok"] is True
    assert result["navigation"]["section"] == "calendar"
    assert result["retry"]["attempts"] == 2
    assert execute.await_count == 2


def test_go_back_timeout_requires_state_inspection(monkeypatch) -> None:
    execute = AsyncMock(
        return_value=booking_extranet._NavigationAttempt(
            ok=False,
            error="navigation_timeout: Navigation request timed out",
            timed_out=True,
        )
    )
    monkeypatch.setattr(booking_extranet, "_execute_navigation_once", execute)

    result = asyncio.run(booking_extranet.go_back())

    assert result["ok"] is False
    assert result["retry"]["decision"] == "inspect_before_retry"
    assert result["retry"]["error_code"] == "navigation_state_uncertain"
    assert execute.await_count == 1


def test_fatal_navigation_auth_failure_closes_worker(monkeypatch) -> None:
    execute = AsyncMock(
        return_value=booking_extranet._NavigationAttempt(
            ok=False,
            error="booking_auth_required: BOOKING_AUTH_REQUIRED",
            fatal=True,
        )
    )
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(booking_extranet, "_execute_navigation_once", execute)
    monkeypatch.setattr(booking_extranet, "_stop_navigation_worker", stop)

    result = asyncio.run(booking_extranet.open_home())

    assert result["retry"]["decision"] == "authenticate_then_retry"
    stop.assert_awaited_once_with(graceful=False)


def test_legacy_cli_closes_navigation_first(monkeypatch) -> None:
    stop = AsyncMock(return_value=True)
    execute = AsyncMock(return_value=booking_extranet._CliAttempt(0, "[]", ""))
    monkeypatch.setattr(booking_extranet, "_stop_navigation_worker", stop)
    monkeypatch.setattr(booking_extranet, "_execute_cli_once", execute)

    result = asyncio.run(
        booking_extranet._run_cli("list_messages", ["messages", "list", "--json"])
    )

    assert result["ok"] is True
    stop.assert_awaited_once_with()


def test_navigation_inputs_are_typed(monkeypatch) -> None:
    execute = AsyncMock()
    monkeypatch.setattr(booking_extranet, "_execute_navigation_once", execute)

    with pytest.raises(ValidationError):
        asyncio.run(booking_extranet.open_calendar("next month"))
    with pytest.raises(ValidationError):
        asyncio.run(booking_extranet.open_reservations("everything"))

    execute.assert_not_awaited()
