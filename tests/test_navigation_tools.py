from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from booking_agent.config import Settings
from booking_agent.tools import navigation


class FakePage:
    def __init__(self) -> None:
        self.url = "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/home.html"

    async def goto(self, url: str, **kwargs) -> None:
        self.url = url


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        booking_email="test@example.com",
        booking_password="test-password",
        booking_hotel_id="123",
    )


def test_semantic_history_returns_to_previous_destination(monkeypatch) -> None:
    page = FakePage()

    async def fake_reservations(page_arg, settings, status):
        page_arg.url = (
            "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/"
            f"search_reservations.html?hotel_id=123&status={status}"
        )
        return [{"booking_id": "B-1"}]

    async def fake_calendar(page_arg, settings, month):
        page_arg.url = (
            "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/"
            f"rates_and_availability/calendar.html?hotel_id=123&month={month}"
        )
        return [{"date": "2026-08-10", "status": "open"}]

    monkeypatch.setattr(navigation, "list_reservations", fake_reservations)
    monkeypatch.setattr(navigation, "view_availability", fake_calendar)
    monkeypatch.setattr(
        navigation,
        "wait_for_waf_challenge",
        AsyncMock(return_value=True),
    )
    navigator = navigation.ExtranetNavigator(page, _settings())

    async def run_flow():
        await navigator.execute(
            navigation.NavigationRequest(request_id="1", action="open_home")
        )
        reservations = await navigator.execute(
            navigation.NavigationRequest(
                request_id="2",
                action="open_reservations",
                status="past",
            )
        )
        calendar = await navigator.execute(
            navigation.NavigationRequest(
                request_id="3",
                action="open_calendar",
                month="2026-08",
            )
        )
        back = await navigator.execute(
            navigation.NavigationRequest(request_id="4", action="go_back")
        )
        return reservations, calendar, back

    reservations, calendar, back = asyncio.run(run_flow())

    assert reservations.navigation is not None
    assert reservations.navigation.section == "reservations"
    assert reservations.navigation.parameters == {"status": "past"}
    assert calendar.navigation is not None
    assert calendar.navigation.history_depth == 3
    assert back.navigation is not None
    assert back.navigation.section == "reservations"
    assert back.navigation.history_depth == 2
    assert back.navigation.can_go_back is True


def test_reopening_same_destination_does_not_duplicate_history(monkeypatch) -> None:
    page = FakePage()
    monkeypatch.setattr(
        navigation,
        "wait_for_waf_challenge",
        AsyncMock(return_value=True),
    )
    navigator = navigation.ExtranetNavigator(page, _settings())

    async def run_flow():
        await navigator.execute(
            navigation.NavigationRequest(request_id="1", action="open_home")
        )
        return await navigator.execute(
            navigation.NavigationRequest(request_id="2", action="open_home")
        )

    response = asyncio.run(run_flow())

    assert response.navigation is not None
    assert response.navigation.history_depth == 1
    assert response.navigation.can_go_back is False


def test_current_page_reconciles_partial_back_transition(monkeypatch) -> None:
    page = FakePage()

    async def fake_reservations(page_arg, settings, status):
        page_arg.url = (
            "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/"
            f"search_reservations.html?hotel_id=123&status={status}"
        )
        return []

    async def fake_calendar(page_arg, settings, month):
        page_arg.url = (
            "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/"
            f"rates_and_availability/calendar.html?hotel_id=123&month={month}"
        )
        return []

    monkeypatch.setattr(navigation, "list_reservations", fake_reservations)
    monkeypatch.setattr(navigation, "view_availability", fake_calendar)
    monkeypatch.setattr(
        navigation,
        "wait_for_waf_challenge",
        AsyncMock(return_value=True),
    )
    navigator = navigation.ExtranetNavigator(page, _settings())

    async def run_flow():
        await navigator.execute(
            navigation.NavigationRequest(
                request_id="1",
                action="open_reservations",
                status="upcoming",
            )
        )
        await navigator.execute(
            navigation.NavigationRequest(
                request_id="2",
                action="open_calendar",
                month="2026-08",
            )
        )
        page.url = (
            "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/"
            "search_reservations.html?hotel_id=123&status=upcoming&ses=secret"
        )
        return await navigator.execute(
            navigation.NavigationRequest(request_id="3", action="current_page")
        )

    response = asyncio.run(run_flow())

    assert response.navigation is not None
    assert response.navigation.section == "reservations"
    assert response.navigation.history_depth == 1
    assert "secret" not in response.model_dump_json()
    assert "http" not in response.model_dump_json()


def test_navigation_request_rejects_invalid_month() -> None:
    with pytest.raises(ValidationError):
        navigation.NavigationRequest(
            request_id="1",
            action="open_calendar",
            month="next month",
        )


def test_go_back_requires_semantic_history() -> None:
    navigator = navigation.ExtranetNavigator(FakePage(), _settings())

    with pytest.raises(navigation.NoNavigationHistory):
        asyncio.run(
            navigator.execute(
                navigation.NavigationRequest(request_id="1", action="go_back")
            )
        )


def test_navigation_response_does_not_define_raw_browser_fields() -> None:
    schema = json.dumps(navigation.NavigationResponse.model_json_schema())

    assert '"url"' not in schema
    assert '"html"' not in schema
    assert '"screenshot"' not in schema
    assert '"cookies"' not in schema


def test_message_navigation_redacts_href_references_and_urls(monkeypatch) -> None:
    page = FakePage()

    async def fake_messages(page_arg, settings, unread_only):
        page_arg.url = "https://admin.booking.com/messaging/inbox.html?ses=secret"
        return [
            {
                "id": "0",
                "thread_ref": (
                    "href:https://admin.booking.com/messaging/thread?ses=secret&id=1"
                ),
                "stable_ref": True,
                "subject": "See https://example.com/details",
            }
        ]

    monkeypatch.setattr(navigation, "list_messages", fake_messages)
    monkeypatch.setattr(
        navigation,
        "wait_for_waf_challenge",
        AsyncMock(return_value=True),
    )
    navigator = navigation.ExtranetNavigator(page, _settings())

    response = asyncio.run(
        navigator.execute(
            navigation.NavigationRequest(request_id="1", action="open_messages")
        )
    )

    assert isinstance(response.result, list)
    assert response.result[0]["thread_ref"] == "index:0"
    assert response.result[0]["stable_ref"] is False
    assert response.result[0]["subject"] == "See [REDACTED_URL]"
    assert "secret" not in response.model_dump_json()
