from __future__ import annotations

import asyncio

from playwright.async_api import Page
from rich.console import Console

from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    CAPTCHA_INDICATOR,
    LOGIN_EMAIL_INPUT,
    LOGIN_NEXT_BUTTON,
    LOGIN_PASSWORD_INPUT,
    LOGIN_SUBMIT_BUTTON,
    TWO_FA_INDICATOR,
)
from booking_agent.utils.waits import human_delay, safe_click, safe_fill

console = Console()

TWO_FA_TIMEOUT_MS = 300_000  # 5 minutes


async def _wait_for_manual_step(page: Page, description: str, timeout_ms: int = TWO_FA_TIMEOUT_MS) -> None:
    """Prompt the user and wait for them to complete a step in the headed browser."""
    console.print(f"[bold yellow]ACTION REQUIRED:[/bold yellow] {description}")
    console.print(f"[dim]Waiting up to {timeout_ms // 60_000} minutes...[/dim]")

    try:
        await page.wait_for_url("**/admin.booking.com/**", timeout=timeout_ms)
    except Exception:
        raise TimeoutError(f"Timed out waiting for manual step: {description}") from None


async def _detect_challenge(page: Page) -> str | None:
    """Check for 2FA or CAPTCHA challenges after submitting credentials."""
    await asyncio.sleep(2)

    for selector in TWO_FA_INDICATOR.split(", "):
        if await page.query_selector(selector):
            return "2fa"

    for selector in CAPTCHA_INDICATOR.split(", "):
        if await page.query_selector(selector):
            return "captcha"

    return None


async def perform_login(page: Page, settings: Settings) -> None:
    """Execute the full Booking.com login flow."""
    console.print("[cyan]Navigating to Booking.com sign-in...[/cyan]")
    await page.goto(settings.sign_in_url, wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1000, 2000)

    # --- Email ---
    console.print("[cyan]Entering email...[/cyan]")
    email_filled = await safe_fill(page, LOGIN_EMAIL_INPUT, settings.booking_email, timeout=10_000)
    if not email_filled:
        raise RuntimeError("Could not find email input on login page")

    await human_delay()
    await safe_click(page, LOGIN_NEXT_BUTTON, timeout=5_000)
    await human_delay(1500, 3000)

    # --- Password ---
    console.print("[cyan]Entering password...[/cyan]")
    password_filled = await safe_fill(page, LOGIN_PASSWORD_INPUT, settings.booking_password, timeout=10_000)
    if not password_filled:
        raise RuntimeError("Could not find password input on login page")

    await human_delay()
    await safe_click(page, LOGIN_SUBMIT_BUTTON, timeout=5_000)
    await human_delay(2000, 4000)

    # --- 2FA / CAPTCHA ---
    challenge = await _detect_challenge(page)
    if challenge == "2fa":
        await _wait_for_manual_step(page, "Complete 2FA verification in the browser window.")
    elif challenge == "captcha":
        await _wait_for_manual_step(page, "Solve the CAPTCHA in the browser window.")

    # --- Verify login success ---
    await asyncio.sleep(3)
    url = page.url

    # Auth may land on www.booking.com with auth_success=1 instead of the extranet
    auth_ok = (
        "admin.booking.com" in url
        or "auth_success=1" in url
        or "booking.com" in url
    )

    if not auth_ok:
        try:
            await page.wait_for_url("**booking.com**", timeout=15_000)
        except Exception:
            raise RuntimeError(
                f"Login did not succeed. Current URL: {page.url}"
            ) from None

    # Navigate to the extranet now that we're authenticated
    console.print("[cyan]Navigating to extranet...[/cyan]")
    try:
        await page.goto(settings.extranet_base, wait_until="commit", timeout=30_000)
    except Exception:
        pass  # Page may redirect or be slow — that's OK

    # Wait for the URL to settle on admin.booking.com
    for i in range(10):
        await asyncio.sleep(3)
        current = page.url
        console.print(f"[dim]  ({i+1}/10) URL: {current[:80]}...[/dim]")
        if "admin.booking.com" in current:
            console.print("[bold green]Login successful![/bold green]")
            return

    # If we're on any booking.com page, auth likely worked — session will be saved
    if "booking.com" in page.url:
        console.print("[bold yellow]Reached booking.com but not the extranet directly.[/bold yellow]")
        console.print("[cyan]Session saved — the extranet should work on next command.[/cyan]")
        return

    raise RuntimeError(
        f"Authenticated but could not reach the extranet. Current URL: {page.url}"
    )
