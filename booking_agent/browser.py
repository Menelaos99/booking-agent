from __future__ import annotations

import asyncio
import fcntl
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, Page, async_playwright

from booking_agent.config import SESSION_FILE, STATE_DIR, Settings

LOCK_FILE = STATE_DIR / ".lock"


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
    )

    context_kwargs: dict = {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    if restore_session and _has_saved_session():
        context_kwargs["storage_state"] = str(SESSION_FILE)

    context = await browser.new_context(**context_kwargs)
    return browser, context


async def is_session_valid(page: Page, settings: Settings) -> bool:
    """Navigate to the extranet and check whether we land there (not redirected to login)."""
    try:
        await page.goto(settings.extranet_base, wait_until="domcontentloaded", timeout=60_000)
        # Allow time for OAuth redirect chain to settle
        for _ in range(6):
            await asyncio.sleep(3)
            url = page.url
            if "admin.booking.com" in url:
                return True
        return False
    except Exception:
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

            try:
                if _has_saved_session() and await is_session_valid(page, settings):
                    yield page
                else:
                    # Session absent or expired — need to log in.
                    # If headless, relaunch headed for potential 2FA/CAPTCHA.
                    if settings.headless:
                        await context.close()
                        await browser.close()
                        browser, context = await _create_context(
                            pw, settings, headless=False, restore_session=False,
                        )
                        page = await context.new_page()

                    await perform_login(page, settings)
                    await save_session(context)
                    yield page
            finally:
                try:
                    await save_session(context)
                except Exception:
                    pass
                await context.close()
                await browser.close()
