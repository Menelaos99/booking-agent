"""Agent tools — atomic browser actions the vision agent can invoke."""

from __future__ import annotations

import asyncio
from datetime import datetime

from playwright.async_api import Page
from rich.console import Console

from booking_agent.antibot import human_click, human_type
from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    LOGIN_EMAIL_INPUT,
    LOGIN_NEXT_BUTTON,
    LOGIN_PASSWORD_INPUT,
    LOGIN_SUBMIT_BUTTON,
)
from booking_agent.utils.waits import human_delay, safe_click, safe_fill

console = Console()


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] [bold cyan][AGENT][/bold cyan] {msg}")


# ── Atomic actions ──────────────────────────────────────────────


async def enter_email(page: Page, settings: Settings) -> None:
    """Clear field → type email → click Next. One atomic step."""
    _log("Typing email...")
    try:
        await page.click(LOGIN_EMAIL_INPUT, timeout=3_000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    filled = await human_type(page, LOGIN_EMAIL_INPUT, settings.booking_email, timeout=5_000, fast=True)
    if not filled:
        filled = await safe_fill(page, LOGIN_EMAIL_INPUT, settings.booking_email, timeout=5_000)
    if not filled:
        _log("[yellow]Could not fill email field[/yellow]")
        return
    await human_delay(500, 1000)

    _log("Clicking Next...")
    clicked = await human_click(page, LOGIN_NEXT_BUTTON, timeout=5_000)
    if not clicked:
        await safe_click(page, LOGIN_NEXT_BUTTON, timeout=5_000)
    await human_delay(2000, 4000)


async def enter_password(page: Page, settings: Settings) -> None:
    """Clear field → type password → click Submit. One atomic step."""
    _log("Typing password...")
    try:
        await page.click(LOGIN_PASSWORD_INPUT, timeout=3_000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    filled = await human_type(page, LOGIN_PASSWORD_INPUT, settings.booking_password, timeout=5_000)
    if not filled:
        filled = await safe_fill(page, LOGIN_PASSWORD_INPUT, settings.booking_password, timeout=5_000)
    if not filled:
        _log("[yellow]Could not fill password field[/yellow]")
        return
    await human_delay(500, 1000)

    _log("Clicking Submit...")
    clicked = await human_click(page, LOGIN_SUBMIT_BUTTON, timeout=5_000)
    if not clicked:
        await safe_click(page, LOGIN_SUBMIT_BUTTON, timeout=5_000)
    await human_delay(2000, 4000)


async def verify_identity(page: Page, settings: Settings, *, timeout_s: float = 300) -> None:
    """Handle auth-assurance: click SMS option, then wait for human to enter the code.

    Future: could automate SMS reading via Google Messages or similar.
    """
    _log("Identity verification required — selecting SMS...")

    # Try to click the SMS option
    sms_selectors = [
        'button:has-text("Text message")',
        'button:has-text("SMS")',
        'a:has-text("Text message")',
        'a:has-text("SMS")',
        '[data-testid*="sms"]',
        'label:has-text("Text message")',
    ]
    clicked = False
    for sel in sms_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                clicked = True
                _log("Clicked SMS verification option")
                break
        except Exception:
            continue

    if not clicked:
        # Fallback: try clicking any element with "sms" or "text message" text
        try:
            await page.click('text=Text message', timeout=3_000)
            clicked = True
            _log("Clicked SMS verification option (text match)")
        except Exception:
            _log("[yellow]Could not find SMS option — complete verification manually[/yellow]")

    await human_delay(2000, 3000)

    # The SMS page shows a dropdown to select phone number, then a "Send verification code" button
    _log("Looking for phone number dropdown...")

    # Find and interact with the phone number dropdown/select
    dropdown_selectors = [
        'select',
        '[role="combobox"]',
        '[role="listbox"]',
        'div[class*="select"]',
        'div[class*="dropdown"]',
    ]
    dropdown_found = False
    for sel in dropdown_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                tag = await el.evaluate("el => el.tagName")
                _log(f"[dim]Found dropdown: <{tag}> via {sel}[/dim]")

                if tag == "SELECT":
                    # Native <select> — use Playwright's select_option
                    options = await el.evaluate("el => Array.from(el.options).map(o => ({value: o.value, text: o.text}))")
                    _log(f"[dim]Dropdown options: {options}[/dim]")
                    # Find the +49 option
                    for opt in options:
                        if "+49" in opt.get("text", "") or "+49" in opt.get("value", ""):
                            await el.select_option(value=opt["value"])
                            _log(f"Selected: {opt['text']}")
                            dropdown_found = True
                            break
                else:
                    # Custom dropdown — click to open, then find +49 option
                    await el.click()
                    await human_delay(500, 1000)
                    # Look for +49 in the opened dropdown items
                    items = await page.query_selector_all('[role="option"], li, div[class*="option"]')
                    for item in items:
                        try:
                            text = (await item.inner_text()).strip()
                            if "+49" in text:
                                await item.click()
                                _log(f"Selected: {text[:60]}")
                                dropdown_found = True
                                break
                        except Exception:
                            continue

                if dropdown_found:
                    break
        except Exception:
            continue

    if not dropdown_found:
        _log("[yellow]Could not find/select +49 in dropdown[/yellow]")
        # Debug: take screenshot
        try:
            await page.screenshot(path="state/debug_sms_page.png")
            _log("[dim]Screenshot saved to state/debug_sms_page.png[/dim]")
        except Exception:
            pass

    await human_delay(1000, 2000)

    # Click "Send verification code" button
    _log("Clicking 'Send verification code'...")
    send_clicked = False
    for btn_sel in [
        'button:has-text("Send verification code")',
        'button:has-text("Send code")',
        'button:has-text("Send")',
        'button[type="submit"]',
    ]:
        try:
            btn = await page.query_selector(btn_sel)
            if btn and await btn.is_visible():
                await btn.click()
                _log(f"Clicked: {btn_sel}")
                send_clicked = True
                break
        except Exception:
            continue

    if not send_clicked:
        _log("[yellow]Could not click send button[/yellow]")

    await human_delay(2000, 3000)

    # Wait for the SMS code input field to appear
    await asyncio.sleep(2)

    # Ask the user to type the SMS code in the terminal
    console.print(
        f"[dim][{datetime.now().strftime('%H:%M:%S')}][/dim] "
        f"[bold magenta][HUMAN][/bold magenta] An SMS code has been sent to your phone"
    )

    import aioconsole  # noqa: F811
    try:
        code = await asyncio.wait_for(
            aioconsole.ainput("  Enter SMS code: "),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        raise TimeoutError("Timed out waiting for SMS code input.") from None

    code = code.strip()
    if not code:
        _log("[yellow]No code entered[/yellow]")
        return

    _log(f"Got SMS code: [bold]{code}[/bold] — typing it in...")

    # Find and fill the code input field
    code_selectors = [
        'input[name="code"]',
        'input[name="otp"]',
        'input[name="pin"]',
        'input[type="text"]',
        'input[autocomplete="one-time-code"]',
        'input:not([type="hidden"]):not([type="password"]):not([type="email"])',
    ]
    filled = False
    for sel in code_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await page.click(sel)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(code, delay=50)
                filled = True
                _log(f"[dim]Typed code into: {sel}[/dim]")
                break
        except Exception:
            continue

    if not filled:
        _log("[yellow]Could not find code input — try entering it manually in the browser[/yellow]")
        # Fall back to waiting for manual completion
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_s:
            await asyncio.sleep(3)
            try:
                if "auth-assurance" not in page.url and "verify" not in page.url:
                    return
            except Exception:
                return
        return

    await human_delay(500, 1000)

    # Click verify/submit button
    submit_selectors = [
        'button[type="submit"]',
        'button:has-text("Verify")',
        'button:has-text("Continue")',
        'button:has-text("Submit")',
        'button:has-text("Confirm")',
    ]
    for sel in submit_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                _log("Clicked verify button")
                break
        except Exception:
            continue

    await human_delay(2000, 4000)

    # Wait for navigation away from auth-assurance
    for _ in range(10):
        await asyncio.sleep(2)
        try:
            url = page.url
            if "auth-assurance" not in url and "verify" not in url:
                _log(f"[green]Identity verified — navigated to {url[:60]}[/green]")
                return
        except Exception:
            return
    _log("[yellow]Still on verification page — may need manual completion[/yellow]")


async def wait_human(page: Page, settings: Settings, *, timeout_s: float = 300) -> None:
    """Wait for a human to solve a CAPTCHA or complete verification."""
    from booking_agent.auth.vision import detect_page_state_vision

    console.print(
        f"[dim][{datetime.now().strftime('%H:%M:%S')}][/dim] "
        f"[bold magenta][HUMAN][/bold magenta] Solve the challenge in the browser window"
    )
    console.print(
        f"[dim][{datetime.now().strftime('%H:%M:%S')}][/dim] "
        f"[dim]Waiting up to {timeout_s / 60:.0f} minutes...[/dim]"
    )

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_s:
        await asyncio.sleep(3)
        try:
            if settings.vision_login:
                state = await detect_page_state_vision(page, hf_token=settings.hf_token)
            else:
                # Fallback: check URL
                url = page.url
                if "admin.booking.com" in url:
                    state = "extranet"
                elif "account.booking.com/sign-in" in url:
                    # Still on sign-in — check for form fields
                    if await page.query_selector(LOGIN_EMAIL_INPUT) or await page.query_selector(LOGIN_PASSWORD_INPUT):
                        state = "email_form"
                    else:
                        continue
                else:
                    state = "logged_in"
        except Exception:
            continue

        if state not in ("captcha", "email_verification", "unknown"):
            _log(f"[green]Challenge cleared → {state}[/green]")
            return

    raise TimeoutError("Timed out waiting for challenge to be solved.")


async def navigate_extranet(page: Page, settings: Settings) -> bool:
    """Navigate to the extranet. Returns True if successful, False if redirected back to sign-in."""

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
        _log(f"[dim]({i+1}/5) {current[:80]}...[/dim]")
        if "admin.booking.com" in current:
            console.print(
                f"[dim][{datetime.now().strftime('%H:%M:%S')}][/dim] "
                f"[bold green][AGENT][/bold green] Login successful — reached extranet!"
            )
            return True

    try:
        url = page.url
    except Exception:
        url = ""

    if "account.booking.com/sign-in" in url:
        _log("[yellow]Redirected back to sign-in — not logged in yet[/yellow]")
        return False

    if "booking.com" in url:
        _log("[yellow]On booking.com but not extranet — session saved[/yellow]")
        return True

    _log(f"[yellow]Unexpected URL: {url[:80]}[/yellow]")
    return False


async def fetch_and_type_otp(page: Page, settings: Settings) -> None:
    """Fetch the latest verification code from Gmail and type it into the OTP field."""
    from booking_agent.auth.gmail_otp import fetch_otp_from_gmail
    from booking_agent.utils.selectors import OTP_INPUT, OTP_SUBMIT_BUTTON

    _log("Fetching verification code from Gmail...")
    otp = await fetch_otp_from_gmail()

    if not otp:
        console.print(
            f"[dim][{datetime.now().strftime('%H:%M:%S')}][/dim] "
            f"[bold magenta][HUMAN][/bold magenta] Could not find code in Gmail — enter it manually"
        )
        return

    _log(f"Got code: [bold]{otp}[/bold] — typing it in...")

    # Try the configured selector first
    filled = await human_type(page, OTP_INPUT, otp, timeout=3_000, fast=True)
    if not filled:
        filled = await safe_fill(page, OTP_INPUT, otp, timeout=3_000)

    # If that failed, try broader selectors that Booking.com might use
    if not filled:
        broader_selectors = [
            'input[type="text"]',
            'input[name="pin"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[data-testid]',
            'input:not([type="hidden"]):not([type="password"]):not([type="email"])',
        ]
        for sel in broader_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    _log(f"[dim]Found input via: {sel}[/dim]")
                    await page.click(sel)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(otp, delay=50)
                    filled = True
                    break
            except Exception:
                continue

    # Debug: dump all visible inputs if we still can't fill
    if not filled:
        try:
            inputs = await page.query_selector_all("input")
            for inp in inputs:
                visible = await inp.is_visible()
                if visible:
                    attrs = await inp.evaluate("el => ({name: el.name, type: el.type, id: el.id, placeholder: el.placeholder, class: el.className})")
                    _log(f"[dim]Visible input: {attrs}[/dim]")
        except Exception:
            pass

    if not filled:
        _log("[yellow]Could not fill OTP field — saving debug screenshot[/yellow]")
        try:
            await page.screenshot(path="state/debug_otp_page.png")
            _log("[dim]Screenshot saved to state/debug_otp_page.png[/dim]")
        except Exception:
            pass
        return

    await human_delay(500, 1000)

    _log("Clicking Verify...")
    # Try multiple submit button selectors
    clicked = await human_click(page, OTP_SUBMIT_BUTTON, timeout=3_000)
    if not clicked:
        await safe_click(page, OTP_SUBMIT_BUTTON, timeout=3_000)
    if not clicked:
        # Try broader button selectors
        for btn_sel in ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")', 'button:has-text("Submit")']:
            try:
                btn = await page.query_selector(btn_sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue
    await human_delay(2000, 4000)
    _log("[green]Verification code submitted[/green]")
