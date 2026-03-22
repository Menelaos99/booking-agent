from __future__ import annotations

import asyncio
import fcntl
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, Page, async_playwright
from rich.console import Console

from booking_agent.antibot import (
    STEALTH_LAUNCH_ARGS,
    apply_stealth,
    get_context_kwargs,
    wait_for_waf_challenge,
)
from booking_agent.config import SESSION_FILE, STATE_DIR, Settings

console = Console()
LOCK_FILE = STATE_DIR / ".lock"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] {msg}")


@asynccontextmanager
async def _file_lock() -> AsyncIterator[None]:
    """Simple file lock to prevent concurrent CLI instances."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fp = open(LOCK_FILE, "w")  # noqa: SIM115
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError("Another booking-agent instance is running") from None
    try:
        yield
    finally:
        fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()


def _has_saved_session() -> bool:
    return SESSION_FILE.exists() and SESSION_FILE.stat().st_size > 10


async def save_session(context: BrowserContext) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    storage = await context.storage_state()
    SESSION_FILE.write_text(json.dumps(storage, indent=2))


async def _create_context(
    playwright_instance,
    settings: Settings,
    *,
    headless: bool | None = None,
    restore_session: bool = True,
) -> tuple:
    """Return (browser, context). Caller must close browser."""
    use_headless = headless if headless is not None else settings.headless
    browser = await playwright_instance.chromium.launch(
        headless=use_headless,
        slow_mo=settings.slow_mo,
        args=STEALTH_LAUNCH_ARGS,
    )

    context_kwargs = get_context_kwargs()

    if restore_session and _has_saved_session():
        context_kwargs["storage_state"] = str(SESSION_FILE)

    context = await browser.new_context(**context_kwargs)
    return browser, context


async def is_session_valid(page: Page, settings: Settings) -> bool:
    """Navigate to the extranet and check whether we're still authenticated."""
    _log("Checking saved session...")
    try:
        await page.goto(settings.extranet_base, wait_until="domcontentloaded", timeout=60_000)
        for i in range(6):
            await asyncio.sleep(3)
            url = page.url
            _log(f"[dim]Session check ({i+1}/6): {url[:80]}[/dim]")
            if "admin.booking.com" in url:
                _log("[green]Session valid (extranet)[/green]")
                return True
            if "account.booking.com/sign-in" in url:
                _log("[yellow]Session expired (redirected to sign-in)[/yellow]")
                return False
            if "booking.com" in url:
                _log("[green]Session valid (booking.com)[/green]")
                return True
        _log("[yellow]Session check timed out[/yellow]")
        return False
    except Exception:
        _log("[red]Session check failed[/red]")
        return False


@asynccontextmanager
async def get_browser_page(
    settings: Settings,
    *,
    headless: bool | None = None,
    restore_session: bool = True,
) -> AsyncIterator[Page]:
    """Low-level: yields a Page. Does NOT guarantee authentication."""
    async with _file_lock():
        async with async_playwright() as pw:
            browser, context = await _create_context(
                pw, settings, headless=headless, restore_session=restore_session,
            )
            page = await context.new_page()
            await apply_stealth(page)
            try:
                yield page
            finally:
                await save_session(context)
                await context.close()
                await browser.close()


@asynccontextmanager
async def get_authenticated_page(settings: Settings) -> AsyncIterator[Page]:
    """Yield a Page that is logged in to the Booking.com extranet.

    1. Tries to restore a saved session.
    2. If invalid, performs a fresh login.
    """
    from booking_agent.auth.login import perform_login

    async with _file_lock():
        async with async_playwright() as pw:
            browser, context = await _create_context(pw, settings, restore_session=True)
            page = await context.new_page()
            await apply_stealth(page)

            try:
                has_session = _has_saved_session()
                if has_session:
                    _log("Restoring saved session...")
                if has_session and await is_session_valid(page, settings):
                    # Handle any WAF challenge on the extranet page
                    await wait_for_waf_challenge(page)
                    yield page
                else:
                    _log("Session invalid — starting fresh login...")
                    # If headless, relaunch headed for potential 2FA/CAPTCHA.
                    if settings.headless:
                        await context.close()
                        await browser.close()
                        browser, context = await _create_context(
                            pw, settings, headless=False, restore_session=False,
                        )
                        page = await context.new_page()
                        await apply_stealth(page)

                    await perform_login(page, settings)
                    # Verify session by loading extranet home
                    _log("Verifying session on extranet...")
                    _log(f"[dim]Current URL before nav: {page.url[:100]}[/dim]")
                    try:
                        await page.goto(settings.extranet_base, wait_until="domcontentloaded", timeout=30_000)
                        # Wait for redirects to settle
                        for i in range(10):
                            await asyncio.sleep(3)
                            url = page.url
                            _log(f"[dim]Verify ({i+1}/10): {url[:80]}[/dim]")
                            if "admin.booking.com" in url:
                                _log("[green]Session verified — extranet loaded[/green]")
                                break
                        else:
                            _log(f"[yellow]Session verification failed — URL: {page.url[:80]}[/yellow]")
                            await page.screenshot(path="state/debug_session_verify.png")
                    except Exception as e:
                        _log(f"[yellow]Session verification error: {e}[/yellow]")
                    await save_session(context)
                    yield page
            finally:
                try:
                    await save_session(context)
                except Exception:
                    pass
                await context.close()
                await browser.close()
