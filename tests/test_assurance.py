import asyncio
from unittest.mock import AsyncMock

import pytest

from booking_agent.auth import assurance
from booking_agent.browser import BookingAuthenticationRequired
from booking_agent.config import Settings


class FakePage:
    def __init__(self) -> None:
        self.url = "https://admin.booking.com/home?ses=test"

    async def goto(self, url: str, **kwargs) -> None:
        self.url = "https://account.booking.com/auth-assurance"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        booking_email="test@example.com",
        booking_password="test-password",
    )


def test_auto_auth_uses_email_after_pulse_failure(monkeypatch) -> None:
    page = FakePage()
    calls: list[str] = []
    events: list[str] = []

    async def try_pulse(*args) -> bool:
        calls.append("pulse")
        return False

    async def try_email(page_arg, *args) -> bool:
        calls.append("email")
        page_arg.url = "https://admin.booking.com/messaging/inbox.html"
        return True

    async def emit(event) -> None:
        events.append(event.event)

    monkeypatch.setattr(assurance, "human_delay", AsyncMock())
    monkeypatch.setattr(assurance, "_dismiss_cookie_banner", AsyncMock())
    monkeypatch.setattr(assurance, "_return_to_methods", AsyncMock(return_value=True))
    monkeypatch.setattr(assurance, "_try_pulse", try_pulse)
    monkeypatch.setattr(assurance, "_try_email", try_email)

    result = asyncio.run(
        assurance.ensure_messages_access(page, _settings(), emit=emit)
    )

    assert result.verified
    assert result.method == "email"
    assert calls == ["pulse", "email"]
    assert events[-1] == "verified"


def test_sms_requires_interactive_confirmation(monkeypatch) -> None:
    page = FakePage()
    events: list[str] = []

    async def emit(event) -> None:
        events.append(event.event)

    monkeypatch.setattr(assurance, "human_delay", AsyncMock())
    monkeypatch.setattr(assurance, "_dismiss_cookie_banner", AsyncMock())

    result = asyncio.run(
        assurance.ensure_messages_access(
            page,
            _settings(),
            method="sms",
            emit=emit,
            read_input=None,
        )
    )

    assert not result.verified
    assert result.reason == "sms confirmation required"
    assert "sms_confirmation_required" in events


def test_prime_noninteractive_access_requires_explicit_auth(monkeypatch) -> None:
    page = FakePage()
    events: list[str] = []

    async def emit(event) -> None:
        events.append(event.event)

    monkeypatch.setenv("BOOKING_AGENT_NONINTERACTIVE", "1")
    monkeypatch.setattr(assurance, "human_delay", AsyncMock())
    pulse = AsyncMock()
    monkeypatch.setattr(assurance, "_try_pulse", pulse)

    with pytest.raises(BookingAuthenticationRequired, match="BOOKING_AUTH_REQUIRED"):
        asyncio.run(
            assurance.ensure_messages_access(page, _settings(), emit=emit)
        )

    assert events[-1] == "error"
    pulse.assert_not_awaited()
