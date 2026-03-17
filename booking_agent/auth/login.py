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
from booking_agent.utils.waits import human_delay, safe_click, safe_fill

console = Console()

TWO_FA_TIMEOUT_MS = 300_000  # 5 minutes


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] {msg}")


async def _wait_for_manual_step(page: Page, description: str, timeout_ms: int = TWO_FA_TIMEOUT_MS) -> None:
    """Prompt the user and wait for them to complete a step in the headed browser."""
    _log(f"[bold yellow]ACTION REQUIRED:[/bold yellow] {description}")
    _log(f"[dim]Waiting up to {timeout_ms // 60_000} minutes...[/dim]")

    try:
        await page.wait_for_url("**/admin.booking.com/**", timeout=timeout_ms)
    except Exception:
        raise TimeoutError(f"Timed out waiting for manual step: {description}") from None


async def _wait_for_captcha_completion(page: Page, timeout_ms: int = TWO_FA_TIMEOUT_MS) -> None:
    """Wait for CAPTCHA elements to disappear after the user solves them."""
    _log("[bold yellow]ACTION:[/bold yellow] Solve the CAPTCHA in the browser window.")
    _log(f"[dim]Waiting up to {timeout_ms // 60_000} minutes...[/dim]")
    try:
        for selector in CAPTCHA_INDICATOR.split(", "):
            await page.wait_for_selector(selector, state="detached", timeout=timeout_ms)
    except Exception:
        raise TimeoutError("Timed out waiting for CAPTCHA to be solved.") from None
    _log("[green]CAPTCHA solved[/green]")
    await human_delay(2000, 4000)


async def _detect_challenge(page: Page) -> str | None:
    """Check for 2FA, CAPTCHA, or other challenges after submitting credentials."""
    await asyncio.sleep(2)

    for selector in TWO_FA_INDICATOR.split(", "):
        if await page.query_selector(selector):
            return "2fa"

    for selector in CAPTCHA_INDICATOR.split(", "):
        if await page.query_selector(selector):
            return "captcha"

    # Check for email-link verification or "confirm it's you" challenges
    page_text = await page.text_content("body") or ""
    verification_phrases = [
        "verify your email",
        "check your email",
        "confirmation link",
        "confirm it",
        "verify it",
        "sent you a link",
        "verification code",
    ]
    lower_text = page_text.lower()
    for phrase in verification_phrases:
        if phrase in lower_text:
            return "email_verification"

    return None


async def _handle_otp_challenge(page: Page, settings: Settings) -> None:
    """Attempt to auto-fill OTP from Gmail, falling back to manual entry."""
    if not settings.gmail_otp_enabled:
        await _wait_for_manual_step(page, "Complete 2FA verification in the browser window.")
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
    await _wait_for_manual_step(page, "Complete 2FA verification in the browser window.")


async def perform_login(page: Page, settings: Settings) -> None:
    """Execute the full Booking.com login flow."""
    _log("Navigating to Booking.com sign-in...")
    await page.goto(settings.sign_in_url, wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1000, 2000)

    # --- Email ---
    _log("Entering email...")
    email_filled = await safe_fill(page, LOGIN_EMAIL_INPUT, settings.booking_email, timeout=10_000)
    if not email_filled:
        raise RuntimeError("Could not find email input on login page")

    await human_delay()
    await safe_click(page, LOGIN_NEXT_BUTTON, timeout=5_000)
    await human_delay(1500, 3000)

    # --- Check for challenges after email (Booking.com may verify before showing password) ---
    pre_password_challenge = await _detect_challenge(page)
    if pre_password_challenge == "email_verification":
        _log("[yellow]Email verification challenge detected (before password)[/yellow]")
        await _wait_for_manual_step(
            page,
            "Complete the email verification in the browser window "
            "(check your inbox for a link or code from Booking.com).",
        )
        # After verification, Booking.com may redirect — re-check if we need to enter password
        if "admin.booking.com" in page.url:
            _log("[bold green]Login successful after verification![/bold green]")
            return
        # Otherwise fall through to password entry
        await human_delay(1000, 2000)
    elif pre_password_challenge == "captcha":
        _log("[yellow]CAPTCHA detected (before password)[/yellow]")
        await _wait_for_captcha_completion(page)

    # --- Password ---
    _log("Entering password...")
    password_filled = await safe_fill(page, LOGIN_PASSWORD_INPUT, settings.booking_password, timeout=10_000)
    if not password_filled:
        raise RuntimeError("Could not find password input on login page")

    await human_delay()
    submitted = await safe_click(page, LOGIN_SUBMIT_BUTTON, timeout=5_000)
    if not submitted:
        _log("[yellow]Warning: could not click submit button[/yellow]")
    await human_delay(2000, 4000)

    # --- Challenges (sequential: CAPTCHA then OTP) ---
    _log("Detecting challenges...")
    for _round in range(3):
        challenge = await _detect_challenge(page)
        if challenge == "captcha":
            _log("[yellow]CAPTCHA detected[/yellow]")
            await _wait_for_captcha_completion(page)
            _log("Re-checking for further challenges...")
        elif challenge == "2fa":
            _log("[yellow]OTP challenge detected[/yellow]")
            await _handle_otp_challenge(page, settings)
            break
        elif challenge == "email_verification":
            _log("[yellow]Email verification challenge detected[/yellow]")
            await _wait_for_manual_step(
                page,
                "Complete the email verification in the browser window "
                "(check your inbox for a link or code from Booking.com).",
            )
            break
        else:
            _log("[green]No challenge detected, proceeding[/green]")
            break

    # --- Verify login success ---
    await asyncio.sleep(3)
    url = page.url

    # Auth may land on www.booking.com with auth_success=1 instead of the extranet
    # Exclude the sign-in page itself — being on account.booking.com/sign-in means login failed
    on_sign_in = "account.booking.com/sign-in" in url
    auth_ok = not on_sign_in and (
        "admin.booking.com" in url
        or "auth_success=1" in url
        or "booking.com" in url
    )

    if not auth_ok:
        # Wait a bit longer — the login may still be redirecting
        try:
            await page.wait_for_url(
                lambda u: "booking.com" in u and "account.booking.com/sign-in" not in u,
                timeout=15_000,
            )
        except Exception:
            raise RuntimeError(
                f"Login did not succeed. Current URL: {page.url}"
            ) from None

    # Navigate to the extranet now that we're authenticated
    _log("Navigating to extranet...")
    try:
        await page.goto(settings.extranet_base, wait_until="commit", timeout=30_000)
    except Exception:
        pass  # Page may redirect or be slow — that's OK

    # Wait for the URL to settle on admin.booking.com
    for i in range(10):
        await asyncio.sleep(3)
        current = page.url
        _log(f"[dim]({i+1}/10) URL: {current[:80]}...[/dim]")
        if "admin.booking.com" in current:
            _log("[bold green]Login successful![/bold green]")
            return

    # If we ended up back on the sign-in page, login actually failed
    if "account.booking.com/sign-in" in page.url:
        raise RuntimeError(
            "Login failed — redirected back to sign-in page. "
            "Check your credentials or look for an undetected challenge (email verification, CAPTCHA, etc.)."
        )

    # If we're on any other booking.com page, auth likely worked — session will be saved
    if "booking.com" in page.url:
        _log("[bold yellow]Reached booking.com but not the extranet directly.[/bold yellow]")
        _log("Session saved — the extranet should work on next command.")
        return

    raise RuntimeError(
        f"Authenticated but could not reach the extranet. Current URL: {page.url}"
    )
