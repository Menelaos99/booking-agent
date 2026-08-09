from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Page
from pydantic import BaseModel, Field, field_validator

from booking_agent.antibot import wait_for_waf_challenge
from booking_agent.browser import BookingAuthenticationRequired
from booking_agent.config import Settings
from booking_agent.modules.availability import view_availability
from booking_agent.modules.messages import list_messages
from booking_agent.modules.reservations import list_reservations

NavigationAction = Literal[
    "open_home",
    "open_reservations",
    "open_messages",
    "open_calendar",
    "current_page",
    "go_back",
    "close",
]
NavigationSection = Literal["home", "reservations", "messages", "calendar"]
ReservationStatus = Literal["upcoming", "past", "cancelled"]

AVAILABLE_DESTINATIONS: tuple[NavigationSection, ...] = (
    "home",
    "reservations",
    "messages",
    "calendar",
)
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SESSION_PATTERN = re.compile(r"([?&]?ses=)[^&\s]*", re.IGNORECASE)


def _redact_browser_values(value: Any) -> Any:
    if isinstance(value, str):
        redacted = _URL_PATTERN.sub("[REDACTED_URL]", value)
        return _SESSION_PATTERN.sub(r"\1[REDACTED]", redacted)
    if isinstance(value, list):
        return [_redact_browser_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_browser_values(item) for key, item in value.items()}
    return value


def _safe_message_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        safe_message = dict(message)
        thread_ref = str(safe_message.get("thread_ref") or "")
        if thread_ref.startswith("href:"):
            safe_message["thread_ref"] = f"index:{safe_message.get('id', index)}"
            safe_message["stable_ref"] = False
        results.append(_redact_browser_values(safe_message))
    return results


class NavigationRequest(BaseModel):
    """Validated command accepted by the navigation worker."""

    request_id: str = Field(min_length=1, max_length=100)
    action: NavigationAction
    status: ReservationStatus = "upcoming"
    month: str | None = None
    unread: bool = False

    @field_validator("month")
    @classmethod
    def validate_month(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}", value):
            raise ValueError("month must use YYYY-MM format")
        return value


class NavigationState(BaseModel):
    """Safe semantic state returned to the agent."""

    active: bool = True
    section: NavigationSection
    history_depth: int = Field(ge=1)
    can_go_back: bool
    available_destinations: list[NavigationSection]
    parameters: dict[str, str | bool] = Field(default_factory=dict)
    result_count: int = Field(ge=0)


class NavigationResponse(BaseModel):
    """Boundary response emitted by the worker as one JSON line."""

    request_id: str
    ok: bool
    result: list[dict[str, Any]] | dict[str, Any] = Field(default_factory=dict)
    navigation: NavigationState | None = None
    error_code: str | None = None
    error: str = ""
    fatal: bool = False


@dataclass(frozen=True)
class Destination:
    section: NavigationSection
    status: ReservationStatus = "upcoming"
    month: str | None = None
    unread: bool = False

    @property
    def parameters(self) -> dict[str, str | bool]:
        if self.section == "reservations":
            return {"status": self.status}
        if self.section == "messages":
            return {"unread": self.unread}
        if self.section == "calendar" and self.month:
            return {"month": self.month}
        return {}


class NoNavigationHistory(RuntimeError):
    """Raised when a semantic back transition is unavailable."""


class NavigationChallengeRequired(RuntimeError):
    """Raised when a WAF or CAPTCHA challenge requires human action."""


class UnexpectedNavigation(RuntimeError):
    """Raised when Booking redirects outside the allowlisted destinations."""


class ExtranetNavigator:
    """Navigate one authenticated Playwright page through allowlisted sections."""

    def __init__(self, page: Page, settings: Settings) -> None:
        self._page = page
        self._settings = settings
        self._history: list[Destination] = []

    async def execute(self, request: NavigationRequest) -> NavigationResponse:
        if request.action == "current_page":
            return await self._current_page(request.request_id)
        if request.action == "go_back":
            return await self._go_back(request.request_id)
        if request.action == "close":
            return NavigationResponse(
                request_id=request.request_id,
                ok=True,
                result={"closed": True},
            )

        destination = self._destination_for(request)
        result = await self._load(destination)
        if not self._history or self._history[-1] != destination:
            self._history.append(destination)
        return self._success(request.request_id, destination, result)

    def _destination_for(self, request: NavigationRequest) -> Destination:
        section = request.action.removeprefix("open_")
        if section not in AVAILABLE_DESTINATIONS:
            raise UnexpectedNavigation("Navigation destination is not allowlisted")
        return Destination(
            section=section,  # type: ignore[arg-type]
            status=request.status,
            month=request.month,
            unread=request.unread,
        )

    async def _current_page(self, request_id: str) -> NavigationResponse:
        inferred = self._infer_destination()
        if inferred is None:
            raise UnexpectedNavigation("Current page is outside the allowlisted destinations")
        self._reconcile_history(inferred)
        result = await self._load(inferred)
        return self._success(request_id, inferred, result)

    async def _go_back(self, request_id: str) -> NavigationResponse:
        if len(self._history) < 2:
            raise NoNavigationHistory("No previous semantic destination is available")
        destination = self._history[-2]
        result = await self._load(destination)
        self._history.pop()
        return self._success(request_id, destination, result)

    def _reconcile_history(self, destination: Destination) -> None:
        for index in range(len(self._history) - 1, -1, -1):
            if self._history[index] == destination:
                self._history = self._history[: index + 1]
                return
        if self._history:
            self._history[-1] = destination
        else:
            self._history.append(destination)

    async def _load(
        self,
        destination: Destination,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        if destination.section == "home":
            await self._page.goto(
                self._settings.extranet_base,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            result: list[dict[str, Any]] | dict[str, Any] = {"loaded": True}
        elif destination.section == "reservations":
            result = await list_reservations(
                self._page,
                self._settings,
                destination.status,
            )
        elif destination.section == "messages":
            result = _safe_message_results(
                await list_messages(
                    self._page,
                    self._settings,
                    unread_only=destination.unread,
                )
            )
        else:
            result = await view_availability(
                self._page,
                self._settings,
                destination.month,
            )

        if not await wait_for_waf_challenge(self._page, timeout_s=15):
            raise NavigationChallengeRequired(
                "A Booking browser challenge requires human action"
            )
        self._assert_expected_destination(destination.section)
        return _redact_browser_values(result)

    def _assert_expected_destination(self, expected: NavigationSection) -> None:
        url = self._page.url.casefold()
        if "account.booking.com/sign-in" in url or "auth-assurance" in url:
            raise BookingAuthenticationRequired(
                "BOOKING_AUTH_REQUIRED: Booking authentication is required"
            )
        inferred = self._infer_destination()
        if inferred is None or inferred.section != expected:
            raise UnexpectedNavigation(
                "Booking redirected outside the requested allowlisted section"
            )

    def _infer_destination(self) -> Destination | None:
        parsed = urlsplit(self._page.url)
        url = self._page.url.casefold()
        query = parse_qs(parsed.query)
        if "messaging" in url:
            current = self._history[-1] if self._history else None
            unread = current.unread if current and current.section == "messages" else False
            return Destination("messages", unread=unread)
        if "search_reservations" in url:
            status = query.get("status", ["upcoming"])[0]
            if status not in {"upcoming", "past", "cancelled"}:
                status = "upcoming"
            return Destination("reservations", status=status)  # type: ignore[arg-type]
        if "rates_and_availability/calendar" in url:
            month = query.get("month", [None])[0]
            return Destination("calendar", month=month)
        if "extranet_ng/manage/home" in url:
            return Destination("home")
        return None

    def _success(
        self,
        request_id: str,
        destination: Destination,
        result: list[dict[str, Any]] | dict[str, Any],
    ) -> NavigationResponse:
        result_count = len(result) if isinstance(result, list) else int(bool(result))
        return NavigationResponse(
            request_id=request_id,
            ok=True,
            result=result,
            navigation=NavigationState(
                section=destination.section,
                history_depth=len(self._history),
                can_go_back=len(self._history) > 1,
                available_destinations=list(AVAILABLE_DESTINATIONS),
                parameters=destination.parameters,
                result_count=result_count,
            ),
        )
