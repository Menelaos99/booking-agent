from __future__ import annotations

import asyncio
import json
import sys

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError

from booking_agent.browser import BookingAuthenticationRequired, get_authenticated_page
from booking_agent.config import get_settings
from booking_agent.tools.navigation import (
    ExtranetNavigator,
    NavigationChallengeRequired,
    NavigationRequest,
    NavigationResponse,
    NoNavigationHistory,
    UnexpectedNavigation,
)

IDLE_TIMEOUT_SECONDS = 600


def _emit(response: NavigationResponse) -> None:
    print(response.model_dump_json(), flush=True)


def _request_id_from_invalid_line(line: bytes) -> str:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "invalid"
    request_id = value.get("request_id") if isinstance(value, dict) else None
    return str(request_id)[:100] if request_id else "invalid"


async def _read_request(
    reader: asyncio.StreamReader,
    *,
    idle_timeout: bool,
) -> NavigationRequest | None:
    try:
        if idle_timeout:
            line = await asyncio.wait_for(
                reader.readline(),
                timeout=IDLE_TIMEOUT_SECONDS,
            )
        else:
            line = await reader.readline()
    except TimeoutError:
        return None
    if not line:
        return None
    try:
        return NavigationRequest.model_validate_json(line)
    except ValidationError:
        _emit(
            NavigationResponse(
                request_id=_request_id_from_invalid_line(line),
                ok=False,
                error_code="invalid_navigation_request",
                error="Navigation request failed validation",
            )
        )
        return await _read_request(reader, idle_timeout=idle_timeout)


async def _respond(
    navigator: ExtranetNavigator,
    request: NavigationRequest,
) -> bool:
    if request.action == "close":
        _emit(await navigator.execute(request))
        return False
    try:
        _emit(await navigator.execute(request))
        return True
    except BookingAuthenticationRequired:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="booking_auth_required",
                error="BOOKING_AUTH_REQUIRED: Booking authentication is required",
                fatal=True,
            )
        )
        return False
    except NavigationChallengeRequired:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="navigation_challenge_required",
                error="A Booking browser challenge requires human action",
                fatal=True,
            )
        )
        return False
    except NoNavigationHistory:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="no_navigation_history",
                error="No previous semantic destination is available",
            )
        )
    except PlaywrightTimeoutError:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="navigation_timeout",
                error="Navigation timed out",
            )
        )
    except UnexpectedNavigation:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="unexpected_navigation",
                error="Booking redirected outside the allowlisted sections",
                fatal=True,
            )
        )
        return False
    except Exception:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="navigation_failed",
                error="Navigation failed without exposing browser details",
            )
        )
    return True


async def run_worker() -> None:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    request = await _read_request(reader, idle_timeout=False)
    if request is None:
        return
    if request.action == "close":
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=True,
                result={"closed": True},
            )
        )
        return

    try:
        async with get_authenticated_page(get_settings()) as page:
            navigator = ExtranetNavigator(page, get_settings())
            while request is not None:
                if not await _respond(navigator, request):
                    return
                request = await _read_request(reader, idle_timeout=True)
    except BookingAuthenticationRequired:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="booking_auth_required",
                error="BOOKING_AUTH_REQUIRED: Booking authentication is required",
                fatal=True,
            )
        )
    except Exception:
        _emit(
            NavigationResponse(
                request_id=request.request_id,
                ok=False,
                error_code="navigation_worker_failed",
                error="Navigation worker could not start",
                fatal=True,
            )
        )


if __name__ == "__main__":
    asyncio.run(run_worker())
