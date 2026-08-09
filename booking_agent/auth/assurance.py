"""Secondary Booking.com identity verification for sensitive Extranet pages."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from playwright.async_api import Page

from booking_agent.auth.gmail_otp import (
    GmailAuthorizationRequired,
    fetch_otp_from_gmail,
)
from booking_agent.config import Settings
from booking_agent.utils.waits import human_delay

logger = logging.getLogger(__name__)

AuthMethod = Literal["auto", "pulse", "email", "sms"]
AuthEventKind = Literal[
    "checking",
    "pulse_approval_required",
    "pulse_failed",
    "email_code_requested",
    "email_code_found",
    "email_oauth_required",
    "email_failed",
    "sms_confirmation_required",
    "sms_code_required",
    "verified",
    "error",
]
EventEmitter = Callable[["AuthEvent"], Awaitable[None]]
InputReader = Callable[[str], Awaitable[str]]

_MESSAGES_PATH = "/hotel/hoteladmin/extranet_ng/manage/messaging/inbox.html"


@dataclass(frozen=True)
class AuthEvent:
    event: AuthEventKind
    message: str
    details: dict[str, str | int | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthResult:
    verified: bool
    method: AuthMethod | None
    reason: str = ""


async def _noop_emit(event: AuthEvent) -> None:
    logger.info(event.message)


def extract_ses(url: str) -> str:
    match = re.search(r"[?&]ses=([^&]+)", url)
    return match.group(1) if match else ""


def messages_url(settings: Settings, *, ses: str = "") -> str:
    return (
        f"https://admin.booking.com{_MESSAGES_PATH}"
        f"?hotel_id={settings.booking_hotel_id}&lang=en&ses={ses}"
    )


def _is_auth_assurance(page: Page) -> bool:
    url = page.url.lower()
    return "auth-assurance" in url or "/verify" in url


async def _visible(page: Page, selector: str):
    try:
        element = await page.query_selector(selector)
        if element and await element.is_visible():
            return element
    except Exception:
        return None
    return None


async def _click_first(page: Page, selectors: list[str]) -> bool:
    for selector in selectors:
        element = await _visible(page, selector)
        if element:
            try:
                await element.click()
                return True
            except Exception:
                continue
    return False


async def _dismiss_cookie_banner(page: Page) -> None:
    await _click_first(
        page,
        [
            'button:has-text("Decline")',
            'button:has-text("Accept")',
            '[data-testid="cookie-banner-decline"]',
        ],
    )


async def _wait_for_verified(page: Page, timeout_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2)
        try:
            if not _is_auth_assurance(page):
                return "account.booking.com/sign-in" not in page.url
        except Exception:
            return False
    return False


async def _return_to_methods(page: Page, settings: Settings) -> bool:
    try:
        await page.goto(
            messages_url(settings, ses=extract_ses(page.url)),
            wait_until="domcontentloaded",
            timeout=30_000,
        )
    except Exception:
        pass
    await human_delay(1_000, 2_000)
    return _is_auth_assurance(page)


async def _select_pulse(page: Page) -> bool:
    return await _click_first(
        page,
        [
            'button:has-text("via Pulse app")',
            'a:has-text("via Pulse app")',
            '[role="button"]:has-text("Pulse")',
            'text="via Pulse app"',
        ],
    )


async def _select_email(page: Page) -> bool:
    selectors = [
        'button:has-text("via Email")',
        'a:has-text("via Email")',
        '[role="button"]:has-text("Email")',
        'button:has-text("email address")',
        'a:has-text("email address")',
    ]
    if await _click_first(page, selectors):
        return True

    expanded = await _click_first(
        page,
        [
            'button:has-text("Unable to verify")',
            'a:has-text("Unable to verify")',
            'text="Unable to verify?"',
        ],
    )
    if not expanded:
        return False
    await human_delay(800, 1_500)
    return await _click_first(page, selectors)


async def _fill_and_submit_code(page: Page, code: str) -> bool:
    input_selectors = [
        'input[autocomplete="one-time-code"]',
        'input[name="code"]',
        'input[name="otp"]',
        'input[name="pin"]',
        'input[inputmode="numeric"]',
        'input:not([type="hidden"]):not([type="password"]):not([type="email"])',
    ]
    filled = False
    for selector in input_selectors:
        element = await _visible(page, selector)
        if not element:
            continue
        try:
            await element.fill(code)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        return False

    await human_delay(300, 700)
    return await _click_first(
        page,
        [
            'button[type="submit"]',
            'button:has-text("Verify")',
            'button:has-text("Continue")',
            'button:has-text("Confirm")',
        ],
    )


async def _try_pulse(
    page: Page,
    settings: Settings,
    emit: EventEmitter,
) -> bool:
    if not await _select_pulse(page):
        await emit(AuthEvent("pulse_failed", "Pulse verification is not available"))
        return False

    await emit(
        AuthEvent(
            "pulse_approval_required",
            "Approve the Booking.com verification request in the Pulse app",
            {"timeout_seconds": settings.auth_assurance_timeout_seconds},
        )
    )
    if await _wait_for_verified(page, settings.auth_assurance_timeout_seconds):
        return True
    await emit(AuthEvent("pulse_failed", "Pulse approval timed out or was declined"))
    return False


async def _try_email(
    page: Page,
    settings: Settings,
    emit: EventEmitter,
) -> bool:
    requested_at = time.time()
    if not await _select_email(page):
        await emit(AuthEvent("email_failed", "Email verification is not available"))
        return False

    await human_delay(700, 1_200)
    await _click_first(
        page,
        [
            'button:has-text("Send verification code")',
            'button:has-text("Send code")',
            'button[type="submit"]:has-text("Send")',
        ],
    )
    await emit(
        AuthEvent(
            "email_code_requested",
            f"Waiting for a verification email at {settings.gmail_account}",
            {"timeout_seconds": settings.email_otp_timeout_seconds},
        )
    )
    try:
        code = await fetch_otp_from_gmail(
            not_before=requested_at,
            timeout_seconds=settings.email_otp_timeout_seconds,
            settings=settings,
        )
    except GmailAuthorizationRequired as exc:
        await emit(AuthEvent("email_oauth_required", str(exc)))
        return False
    if not code:
        await emit(AuthEvent("email_failed", "No fresh Booking.com email code was found"))
        return False

    await emit(AuthEvent("email_code_found", "A fresh email verification code was found"))
    if not await _fill_and_submit_code(page, code):
        await emit(AuthEvent("email_failed", "The email code input could not be submitted"))
        return False
    return await _wait_for_verified(page, 30)


async def _select_sms_destination(page: Page) -> str:
    select = await _visible(page, "select")
    if not select:
        return "configured phone"

    options = await select.evaluate(
        "el => Array.from(el.options).map(o => ({value: o.value, text: o.text}))"
    )
    candidates = [option for option in options if option.get("value")]
    preferred = next(
        (
            option
            for option in candidates
            if "+49" in option.get("text", "") or "+49" in option.get("value", "")
        ),
        candidates[0] if candidates else None,
    )
    if preferred:
        await select.select_option(value=preferred["value"])
        return str(preferred.get("text", "configured phone"))
    return "configured phone"


async def _try_sms(
    page: Page,
    settings: Settings,
    emit: EventEmitter,
    read_input: InputReader,
) -> bool:
    await emit(
        AuthEvent(
            "sms_confirmation_required",
            "Pulse and email were unavailable. Confirm before sending an SMS code",
        )
    )
    confirmation = (await read_input("confirm_sms")).strip().lower()
    if confirmation not in {"yes", "y", "true", "1"}:
        await emit(AuthEvent("error", "SMS fallback was not approved"))
        return False

    selected = await _click_first(
        page,
        [
            'button:has-text("Text message")',
            'a:has-text("Text message")',
            '[role="button"]:has-text("SMS")',
            'text="via Text message (SMS)"',
        ],
    )
    if not selected:
        await emit(AuthEvent("error", "SMS verification is not available"))
        return False

    await human_delay(800, 1_500)
    destination = await _select_sms_destination(page)
    sent = await _click_first(
        page,
        [
            'button:has-text("Send verification code")',
            'button:has-text("Send code")',
            'button[type="submit"]:has-text("Send")',
        ],
    )
    if not sent:
        await emit(AuthEvent("error", "The SMS verification request could not be sent"))
        return False

    await emit(
        AuthEvent(
            "sms_code_required",
            f"Enter the SMS code sent to {destination}",
            {"timeout_seconds": settings.auth_assurance_timeout_seconds},
        )
    )
    code = (await read_input("sms_code")).strip()
    if not re.fullmatch(r"\d{4,8}", code):
        await emit(AuthEvent("error", "SMS code must contain 4 to 8 digits"))
        return False
    if not await _fill_and_submit_code(page, code):
        await emit(AuthEvent("error", "The SMS code input could not be submitted"))
        return False
    return await _wait_for_verified(page, 30)


async def ensure_messages_access(
    page: Page,
    settings: Settings,
    *,
    method: AuthMethod | None = None,
    emit: EventEmitter | None = None,
    read_input: InputReader | None = None,
) -> AuthResult:
    """Ensure the sensitive messages page is accessible."""

    emit = emit or _noop_emit
    selected_method: AuthMethod = method or settings.auth_assurance_method
    await emit(AuthEvent("checking", "Checking access to the Booking.com messages inbox"))

    ses = extract_ses(page.url)
    await page.goto(
        messages_url(settings, ses=ses),
        wait_until="domcontentloaded",
        timeout=30_000,
    )
    await human_delay(1_000, 2_000)
    if not _is_auth_assurance(page):
        verified = "messaging" in page.url and "account.booking.com/sign-in" not in page.url
        if verified:
            await emit(AuthEvent("verified", "Sensitive Extranet access is already verified"))
        return AuthResult(verified, None, "already verified" if verified else "unexpected redirect")

    from booking_agent.browser import (
        BookingAuthenticationRequired,
        is_noninteractive_mode,
    )

    if is_noninteractive_mode():
        await emit(
            AuthEvent(
                "error",
                "Sensitive Extranet access requires the explicit authentication workflow",
            )
        )
        raise BookingAuthenticationRequired(
            "BOOKING_AUTH_REQUIRED: Sensitive Extranet access requires start_auth()"
        )

    await _dismiss_cookie_banner(page)

    methods: list[AuthMethod]
    if selected_method == "auto":
        methods = ["pulse", "email", "sms"]
    else:
        methods = [selected_method]

    for index, candidate in enumerate(methods):
        if index and not await _return_to_methods(page, settings):
            await emit(AuthEvent("error", "Could not return to verification method selection"))
            break
        await _dismiss_cookie_banner(page)

        if candidate == "pulse":
            verified = await _try_pulse(page, settings, emit)
        elif candidate == "email":
            verified = await _try_email(page, settings, emit)
        else:
            if read_input is None:
                await emit(AuthEvent("sms_confirmation_required", "SMS fallback requires interactive confirmation"))
                return AuthResult(False, None, "sms confirmation required")
            verified = await _try_sms(page, settings, emit, read_input)

        if verified:
            if "messaging" not in page.url:
                await page.goto(
                    messages_url(settings, ses=extract_ses(page.url)),
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await human_delay(700, 1_200)
            success = "messaging" in page.url and not _is_auth_assurance(page)
            if success:
                await emit(AuthEvent("verified", f"Sensitive Extranet access verified with {candidate}"))
                return AuthResult(True, candidate)

    await emit(AuthEvent("error", "No configured verification method succeeded"))
    return AuthResult(False, None, "verification failed")
