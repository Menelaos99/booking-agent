from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright
from rich.console import Console

from booking_agent.antibot import (
    STEALTH_LAUNCH_ARGS,
    apply_stealth,
    get_context_kwargs,
    wait_for_waf_challenge,
)
from booking_agent.config import SESSION_FILE, STATE_DIR, Settings
from booking_agent.utils.selectors import LOGIN_EMAIL_INPUT, LOGIN_PASSWORD_INPUT

console = Console()
LOCK_FILE = STATE_DIR / ".lock"
NONINTERACTIVE_ENV = "BOOKING_AGENT_NONINTERACTIVE"


class BookingAuthenticationRequired(RuntimeError):
    """Raised when an agent call needs an explicit interactive login workflow."""


def is_noninteractive_mode() -> bool:
    return os.environ.get(NONINTERACTIVE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _log(msg: str) -> None:
    ts = datetime.now().astimezone().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] {msg}")


@asynccontextmanager
async def _file_lock() -> AsyncIterator[None]:
    """Simple file lock to prevent concurrent CLI instances."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fp = open(LOCK_FILE, "w")  # noqa: SIM115
    os.fchmod(fp.fileno(), 0o600)
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
    """Persist cookies and browser storage atomically with owner-only access."""

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    storage = await context.storage_state(indexed_db=True)

    fd, temporary_name = tempfile.mkstemp(
        dir=STATE_DIR,
        prefix=".session-",
        suffix=".json",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(storage, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, SESSION_FILE)
        SESSION_FILE.chmod(0o600)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
        SESSION_FILE.chmod(0o600)
        context_kwargs["storage_state"] = str(SESSION_FILE)

    try:
        context = await browser.new_context(**context_kwargs)
    except Exception as exc:
        if "storage_state" not in context_kwargs:
            raise
        _log(f"[yellow]Saved session could not be loaded; starting clean ({exc})[/yellow]")
        context_kwargs.pop("storage_state")
        context = await browser.new_context(**context_kwargs)
    return browser, context


async def _has_visible_login_form(page: Page) -> bool:
    for selector in (LOGIN_EMAIL_INPUT, LOGIN_PASSWORD_INPUT):
        try:
            element = await page.query_selector(selector)
            if element and await element.is_visible():
                return True
        except Exception:
            continue
    return False


async def is_session_valid(page: Page, settings: Settings) -> bool:
    """Navigate to the extranet and check whether we're still authenticated."""
    _log("Checking saved session...")
    try:
        await page.goto(settings.extranet_base, wait_until="domcontentloaded", timeout=60_000)
        consecutive_extranet_checks = 0
        for i in range(8):
            await asyncio.sleep(3)
            url = page.url
            _log(f"[dim]Session check ({i+1}/8): {url[:80]}[/dim]")
            if "account.booking.com/sign-in" in url:
                _log("[yellow]Session expired (redirected to sign-in)[/yellow]")
                return False
            if "admin.booking.com" in url and not await _has_visible_login_form(page):
                consecutive_extranet_checks += 1
                if consecutive_extranet_checks >= 2:
                    _log("[green]Session valid (verified extranet page)[/green]")
                    return True
            else:
                consecutive_extranet_checks = 0
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
    async with _file_lock(), async_playwright() as pw:
        browser, context = await _create_context(
            pw,
            settings,
            headless=headless,
            restore_session=restore_session,
        )
        page = await context.new_page()
        await apply_stealth(page)
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


@asynccontextmanager
async def get_authenticated_page(settings: Settings) -> AsyncIterator[Page]:
    """Yield a Page that is logged in to the Booking.com extranet.

    1. Tries to restore a saved session.
    2. If invalid, performs a fresh login.
    """
    from booking_agent.auth.login import perform_login

    async with _file_lock(), async_playwright() as pw:
        browser, context = await _create_context(pw, settings, restore_session=True)
        page = await context.new_page()
        await apply_stealth(page)

        try:
            has_session = _has_saved_session()
            if has_session:
                _log("Restoring saved session...")
            authenticated = has_session and await is_session_valid(page, settings)
            if authenticated:
                # Handle any WAF challenge on the extranet page
                await wait_for_waf_challenge(page)
                yield page
            else:
                if is_noninteractive_mode():
                    raise BookingAuthenticationRequired(
                        "BOOKING_AUTH_REQUIRED: Saved Booking session is missing or expired; "
                        "start the explicit authentication workflow"
                    )
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
                    await page.goto(
                        settings.extranet_base,
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    # Wait for redirects to settle
                    if not await is_session_valid(page, settings):
                        await page.screenshot(path="state/debug_session_verify.png")
                        raise RuntimeError(
                            "Login completed but the Extranet session could not be verified"
                        )
                except Exception as exc:
                    _log(f"[red]Session verification error: {exc}[/red]")
                    raise
                await save_session(context)
                yield page
        finally:
            try:
                url = page.url
                if "admin.booking.com" in url and "auth-assurance" not in url:
                    await save_session(context)
            except Exception:
                pass
            await context.close()
            await browser.close()
