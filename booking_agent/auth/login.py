from __future__ import annotations

import asyncio
from datetime import datetime

from playwright.async_api import Page
from rich.console import Console

from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    CAPTCHA_INDICATOR,
    LOGIN_EMAIL_INPUT,
    LOGIN_NEXT_BUTTON,
    LOGIN_PASSWORD_INPUT,
    LOGIN_SUBMIT_BUTTON,
    OTP_INPUT,
    OTP_SUBMIT_BUTTON,
    TWO_FA_INDICATOR,
)
from booking_agent.antibot import (
    human_click,
    human_scroll,
    human_type,
)
from booking_agent.utils.waits import human_delay, safe_click, safe_fill

console = Console()

TWO_FA_TIMEOUT_MS = 300_000  # 5 minutes
MAX_LOGIN_ITERATIONS = 25


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] {msg}")


# =========================================================================
# State detection
# =========================================================================

async def _detect_page_state(page: Page, settings: Settings) -> str:
    """Detect page state. Uses vision LLM if enabled, otherwise DOM-based."""
    if settings.vision_login:
        try:
            from booking_agent.auth.vision import detect_page_state_vision
            state = await detect_page_state_vision(page, hf_token=settings.hf_token)
            if state != "unknown":
                return state
            _log("[dim][vision] Got 'unknown', falling back to DOM[/dim]")
        except Exception as e:
            _log(f"[dim][vision] Failed ({e}), falling back to DOM[/dim]")

    return await _detect_page_state_dom(page)


async def _detect_page_state_dom(page: Page) -> str:
    """DOM-based page state detection (fallback).

    Returns one of:
      "extranet"           — on admin.booking.com (done!)
      "logged_in"          — on booking.com but not sign-in (need to navigate to extranet)
      "captcha"            — CAPTCHA/challenge blocking the page
      "2fa"                — OTP input visible
      "password_form"      — password field visible
      "email_form"         — email/username field visible
      "email_verification" — "check your email" interstitial
      "unknown"            — can't determine state
    """
    try:
        url = page.url
    except Exception:
        return "unknown"

    # Already at extranet
    if "admin.booking.com" in url:
        return "extranet"

    # On booking.com but not sign-in = logged in
    if "booking.com" in url and "account.booking.com/sign-in" not in url:
        return "logged_in"

    # On sign-in page — check what's visible
    try:
        # CAPTCHA elements?
        for sel in CAPTCHA_INDICATOR.split(", "):
            if await page.query_selector(sel):
                return "captcha"

        # 2FA input?
        for sel in TWO_FA_INDICATOR.split(", "):
            if await page.query_selector(sel):
                return "2fa"

        # Password field visible? (check before email — both may exist but password means we're past email)
        if await page.query_selector(LOGIN_PASSWORD_INPUT):
            return "password_form"

        # Email field visible?
        if await page.query_selector(LOGIN_EMAIL_INPUT):
            return "email_form"

        # Check for verification text
        body = (await page.text_content("body") or "").lower()
        verification_phrases = [
            "verify your email", "check your email", "confirmation link",
            "confirm it", "verify it", "sent you a link", "verification code",
        ]
        if any(phrase in body for phrase in verification_phrases):
            return "email_verification"

        # Check for AWS WAF challenge text
        challenge_phrases = ["verify you are human", "not a robot", "checking your browser"]
        if any(phrase in body for phrase in challenge_phrases):
            return "captcha"

    except Exception:
        return "unknown"

    return "unknown"


# =========================================================================
# Action helpers (single-purpose, simple)
# =========================================================================

async def _fill_email(page: Page, settings: Settings) -> None:
    """Enter email and click next."""
    _log("Entering email...")
    filled = await human_type(page, LOGIN_EMAIL_INPUT, settings.booking_email, timeout=5_000, fast=True)
    if not filled:
        filled = await safe_fill(page, LOGIN_EMAIL_INPUT, settings.booking_email, timeout=5_000)
    if not filled:
        _log("[yellow]Could not fill email field[/yellow]")
        return

    await human_delay()
    clicked = await human_click(page, LOGIN_NEXT_BUTTON, timeout=5_000)
    if not clicked:
        await safe_click(page, LOGIN_NEXT_BUTTON, timeout=5_000)
    await human_delay(1500, 3000)


async def _fill_password(page: Page, settings: Settings) -> None:
    """Enter password and click submit."""
    _log("Entering password...")
    filled = await human_type(page, LOGIN_PASSWORD_INPUT, settings.booking_password, timeout=5_000)
    if not filled:
        filled = await safe_fill(page, LOGIN_PASSWORD_INPUT, settings.booking_password, timeout=5_000)
    if not filled:
        _log("[yellow]Could not fill password field[/yellow]")
        return

    await human_delay()
    submitted = await human_click(page, LOGIN_SUBMIT_BUTTON, timeout=5_000)
    if not submitted:
        submitted = await safe_click(page, LOGIN_SUBMIT_BUTTON, timeout=5_000)
    if not submitted:
        _log("[yellow]Could not click submit button[/yellow]")
    await human_delay(2000, 4000)


async def _wait_for_challenge_cleared(page: Page, settings: Settings, *, timeout_s: float = 300) -> None:
    """Wait for a challenge (CAPTCHA, email verification) to clear.

    Polls the page state until it changes from captcha/email_verification to something else.
    """
    _log("[bold yellow]ACTION:[/bold yellow] Complete the challenge in the browser window.")
    _log(f"[dim]Waiting up to {timeout_s / 60:.0f} minutes...[/dim]")

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_s:
        await asyncio.sleep(2)
        state = await _detect_page_state(page, settings)
        if state not in ("captcha", "email_verification", "unknown"):
            _log(f"[green]Challenge cleared → {state}[/green]")
            return

    raise TimeoutError("Timed out waiting for challenge to be solved.")


async def _handle_otp(page: Page, settings: Settings) -> None:
    """Attempt to auto-fill OTP from Gmail, falling back to manual wait."""
    if not settings.gmail_otp_enabled:
        _log("[bold yellow]ACTION:[/bold yellow] Complete 2FA in the browser window.")
        _log(f"[dim]Waiting up to 5 minutes...[/dim]")
        await _wait_for_challenge_cleared(page, settings, timeout_s=TWO_FA_TIMEOUT_MS / 1000)
        return

    from booking_agent.auth.gmail_otp import fetch_otp_from_gmail

    _log("Fetching OTP from Gmail...")
    otp = await fetch_otp_from_gmail()

    if otp:
        filled = await safe_fill(page, OTP_INPUT, otp, timeout=5_000)
        if filled:
            await human_delay()
            await safe_click(page, OTP_SUBMIT_BUTTON, timeout=5_000)
            await human_delay(2000, 4000)
            _log("[green]OTP auto-filled & submitted[/green]")
            return
        else:
            _log("[yellow]Found OTP but could not fill the input field.[/yellow]")

    _log("[yellow]Falling back to manual OTP entry.[/yellow]")
    await _wait_for_challenge_cleared(page, settings, timeout_s=TWO_FA_TIMEOUT_MS / 1000)


async def _navigate_to_extranet(page: Page, settings: Settings) -> bool:
    """Navigate to the extranet and verify we got there.

    Returns True if we reached the extranet, False if we got redirected back
    to sign-in (meaning we need to re-enter credentials).
    """
    _log("Navigating to extranet...")
    try:
        await page.goto(settings.extranet_base, wait_until="commit", timeout=30_000)
    except Exception:
        pass

    for i in range(5):
        await asyncio.sleep(3)
        try:
            current = page.url
        except Exception:
            continue
        _log(f"[dim]({i+1}/5) URL: {current[:80]}...[/dim]")
        if "admin.booking.com" in current:
            _log("[bold green]Login successful![/bold green]")
            return True

    try:
        url = page.url
    except Exception:
        url = ""

    if "account.booking.com/sign-in" in url:
        _log("[yellow]Redirected back to sign-in — not logged in yet, retrying...[/yellow]")
        return False

    if "booking.com" in url:
        _log("[bold yellow]Reached booking.com but not the extranet directly.[/bold yellow]")
        _log("Session saved — the extranet should work on next command.")
        return True

    _log(f"[yellow]Unexpected URL: {url[:80]} — retrying...[/yellow]")
    return False


# =========================================================================
# Main login — reactive state machine
# =========================================================================

async def perform_login(page: Page, settings: Settings) -> None:
    """Execute the Booking.com login flow as a reactive state machine.

    Instead of a linear script, we detect the current page state and act accordingly.
    This handles form resets, redirects, and challenges gracefully.
    """
    _log("Navigating to Booking.com sign-in...")
    await page.goto(settings.sign_in_url, wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1000, 2000)
    await human_scroll(page)

    last_action = None

    for attempt in range(MAX_LOGIN_ITERATIONS):
        if settings.vision_login:
            # ── Agent mode: vision decides the action, tools execute it ──
            from booking_agent.auth.vision import get_agent_action
            from booking_agent.auth import tools

            try:
                agent_action = await get_agent_action(page, hf_token=settings.hf_token)
            except Exception as e:
                _log(f"[dim][AGENT] Vision failed ({e}), falling back to DOM[/dim]")
                agent_action = None

            if agent_action and agent_action.action != "wait":
                action = agent_action.action

                # Dedup: if agent repeats the same action, wait instead
                if action == last_action and action in ("enter_email", "enter_password"):
                    _log(f"[dim][AGENT] Same action repeated ({action}) — waiting for page to update[/dim]")
                    await asyncio.sleep(3)
                    last_action = None  # Reset so next iteration can retry
                    continue

                last_action = action

                if action == "enter_email":
                    await tools.enter_email(page, settings)
                elif action == "enter_password":
                    await tools.enter_password(page, settings)
                elif action == "fetch_otp":
                    await tools.fetch_and_type_otp(page, settings)
                elif action == "verify_identity":
                    await tools.verify_identity(page, settings)
                    last_action = None
                elif action == "wait_human":
                    await tools.wait_human(page, settings)
                    last_action = None  # Reset after human interaction
                elif action == "navigate_extranet":
                    if await tools.navigate_extranet(page, settings):
                        return
                elif action == "done":
                    _log("[bold green][AGENT][/bold green] Reached extranet — login successful!")
                    return
                else:
                    await asyncio.sleep(2)
                continue

            # Agent returned "wait" or failed — fall through to DOM-based
            if agent_action:
                await asyncio.sleep(2)
                continue

        # ── DOM mode: state-based detection ──
        state = await _detect_page_state_dom(page)

        if state == "email_form":
            _log("[bold cyan][AGENT][/bold cyan] Detected email form → filling email & clicking Next")
            await _fill_email(page, settings)

        elif state == "password_form":
            _log("[bold cyan][AGENT][/bold cyan] Detected password form → filling password & submitting")
            await _fill_password(page, settings)

        elif state == "captcha":
            _log("[bold yellow][WEBSITE][/bold yellow] CAPTCHA challenge detected")
            _log("[bold magenta][HUMAN][/bold magenta] Please solve the CAPTCHA in the browser window")
            await _wait_for_challenge_cleared(page, settings)

        elif state == "2fa":
            _log("[bold yellow][WEBSITE][/bold yellow] 2FA/OTP challenge detected")
            await _handle_otp(page, settings)

        elif state == "email_verification":
            _log("[bold yellow][WEBSITE][/bold yellow] Email verification required")
            _log("[bold magenta][HUMAN][/bold magenta] Check your inbox and verify your email")
            await _wait_for_challenge_cleared(page, settings)

        elif state == "extranet":
            _log("[bold green][AGENT][/bold green] Reached extranet — login successful!")
            return

        elif state == "logged_in":
            _log("[bold cyan][AGENT][/bold cyan] Logged in to Booking.com → navigating to extranet")
            if await _navigate_to_extranet(page, settings):
                return
            _log("[bold yellow][WEBSITE][/bold yellow] Redirected back to sign-in — retrying")

        else:
            _log(f"[dim][AGENT] Unknown state, waiting... URL: {page.url[:80]}[/dim]")
            await asyncio.sleep(3)

    raise RuntimeError("Login failed — exceeded maximum attempts.")
