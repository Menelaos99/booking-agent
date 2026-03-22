"""Anti-bot detection module for Booking.com (AWS WAF).

Layers (applied in order, all free):
  1. Browser launch args — remove automation signals at the Chromium level
  2. Stealth patches — navigator.webdriver, plugins, chrome runtime, WebGL, etc.
  3. Fingerprint consistency — realistic UA, viewport jitter, platform match
  4. Human-like behaviour — mouse curves, typing cadence, natural scrolling
  5. AWS WAF challenge handling — detect and wait for silent JS challenge

Paid fallback (only if everything above fails):
  6. CAPTCHA solver API (CapSolver / 2Captcha)
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime

from playwright.async_api import BrowserContext, Page
from playwright_stealth import Stealth
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Latest stable Chrome version and matching metadata
# Update this periodically to stay current
# ---------------------------------------------------------------------------
CHROME_VERSION = "133"
CHROME_FULL_VERSION = "133.0.6943.98"
USER_AGENT = (
    f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_FULL_VERSION} Safari/537.36"
)
SEC_CH_UA = f'"Chromium";v="{CHROME_VERSION}", "Google Chrome";v="{CHROME_VERSION}", "Not-A.Brand";v="99"'

# ---------------------------------------------------------------------------
# AWS WAF selectors
# ---------------------------------------------------------------------------
AWS_WAF_CHALLENGE_INDICATOR = (
    "#challenge-container, "
    "#challenge-form, "
    "[id*='aws-waf'], "
    "script[src*='challenge.js'], "
    "script[src*='captcha.js']"
)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] [cyan][antibot][/cyan] {msg}")


# =========================================================================
# 1. BROWSER LAUNCH ARGS
# =========================================================================

STEALTH_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
]


# =========================================================================
# 2 & 3. STEALTH PATCHES + FINGERPRINT
# =========================================================================

def _create_stealth() -> Stealth:
    """Create a Stealth instance tuned for macOS + Chrome."""
    return Stealth(
        # Core evasions
        navigator_webdriver=True,
        navigator_plugins=True,
        navigator_languages=True,
        navigator_platform=True,
        navigator_user_agent=True,
        navigator_vendor=True,
        navigator_permissions=True,
        navigator_hardware_concurrency=True,
        chrome_app=True,
        chrome_csi=True,
        chrome_load_times=True,
        chrome_runtime=True,
        iframe_content_window=True,
        media_codecs=True,
        error_prototype=True,
        hairline=True,
        sec_ch_ua=True,
        webgl_vendor=True,
        # Overrides matching our user-agent
        navigator_platform_override="MacIntel",
        navigator_user_agent_override=USER_AGENT,
        navigator_vendor_override="Google Inc.",
        navigator_languages_override=("en-US", "en"),
        sec_ch_ua_override=SEC_CH_UA,
        webgl_vendor_override="Google Inc. (Apple)",
        webgl_renderer_override="ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
    )


_stealth_instance = _create_stealth()


async def apply_stealth(page: Page) -> None:
    """Apply all stealth patches to a page."""
    await _stealth_instance.apply_stealth_async(page)
    _log("Stealth patches applied")


def get_context_kwargs() -> dict:
    """Return kwargs for browser.new_context() with realistic fingerprint."""
    # Add slight viewport jitter so we're not always exactly 1920x1080
    width = 1920 + random.randint(-20, 20)
    height = 1080 + random.randint(-10, 10)

    return {
        "viewport": {"width": width, "height": height},
        "user_agent": USER_AGENT,
        "locale": "en-US",
        "timezone_id": "Europe/Athens",
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9,el;q=0.8",
        },
    }


# =========================================================================
# 4. HUMAN-LIKE BEHAVIOUR
# =========================================================================

async def human_type(page: Page, selector: str, text: str, *, timeout: float = 5_000, fast: bool = False) -> bool:
    """Type text character-by-character with realistic timing.

    Set fast=True for less sensitive fields (email) — types quicker.
    """
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await page.click(selector)
        await asyncio.sleep(random.uniform(0.05, 0.15))

        for char in text:
            await page.keyboard.type(char, delay=0)
            if fast:
                # Fast mode: 20-60ms per char, no pauses (~1-1.5s for an email)
                delay = random.uniform(0.02, 0.06)
            else:
                # Normal mode: 40-140ms per char with occasional pauses
                delay = random.uniform(0.04, 0.14)
                if random.random() < 0.03:
                    delay += random.uniform(0.15, 0.4)
            await asyncio.sleep(delay)

        return True
    except Exception:
        return False


async def human_mouse_move(page: Page, x: int, y: int, *, steps: int = 0) -> None:
    """Move mouse to (x, y) along a bezier-like curve."""
    if steps == 0:
        steps = random.randint(15, 30)

    # Get current mouse position (default to a random starting point)
    start_x = random.randint(100, 400)
    start_y = random.randint(100, 400)

    # Control points for a quadratic bezier curve
    cp_x = (start_x + x) / 2 + random.randint(-100, 100)
    cp_y = (start_y + y) / 2 + random.randint(-50, 50)

    for i in range(steps + 1):
        t = i / steps
        # Quadratic bezier
        bx = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * cp_x + t ** 2 * x
        by = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * cp_y + t ** 2 * y
        await page.mouse.move(bx, by)
        await asyncio.sleep(random.uniform(0.005, 0.02))


async def human_scroll(page: Page) -> None:
    """Scroll the page naturally before interacting."""
    scroll_amount = random.randint(100, 400)
    scroll_steps = random.randint(3, 6)

    for _ in range(scroll_steps):
        delta = scroll_amount // scroll_steps + random.randint(-20, 20)
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.1, 0.4))

    # Small pause after scrolling
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def human_click(page: Page, selector: str, *, timeout: float = 5_000) -> bool:
    """Click an element with realistic mouse movement first."""
    try:
        el = await page.wait_for_selector(selector, timeout=timeout)
        if not el:
            return False

        box = await el.bounding_box()
        if box:
            # Move mouse to element with bezier curve
            target_x = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
            target_y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
            await human_mouse_move(page, int(target_x), int(target_y))
            await asyncio.sleep(random.uniform(0.05, 0.15))

        await el.click()
        return True
    except Exception:
        return False


# =========================================================================
# 5. AWS WAF CHALLENGE HANDLING
# =========================================================================

async def wait_for_waf_challenge(page: Page, *, timeout_s: float = 15) -> bool:
    """Wait for the silent AWS WAF JS challenge to complete.

    Returns True if we're past the challenge (or there was none),
    False if something is still blocking after timeout.
    """
    if not await _page_has_challenge(page):
        return True

    _log("AWS WAF challenge detected — waiting for silent JS solve...")

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_s:
        await asyncio.sleep(1)
        if not await _page_has_challenge(page):
            _log("[green]AWS WAF challenge passed[/green]")
            return True

    _log("[yellow]AWS WAF challenge still present after timeout[/yellow]")
    return False


CAPTCHA_SELECTORS = [
    'iframe[src*="captcha"]',
    'iframe[src*="recaptcha"]',
    '[class*="captcha"]',
    '#captcha-container',
    '[data-captcha]',
]

# The login form elements we expect to see once challenges are cleared
LOGIN_FORM_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
    'input[name="loginname"]',
    'input[name="username"]',
    'input[type="email"]',
]


async def _page_has_challenge(page: Page) -> bool:
    """Check if the page is blocked by a challenge (CAPTCHA, WAF, or interstitial).

    Returns False if the page navigated (context destroyed) — navigation means
    the challenge is gone.
    """
    try:
        for sel in CAPTCHA_SELECTORS:
            if await page.query_selector(sel):
                return True

        for selector in AWS_WAF_CHALLENGE_INDICATOR.split(", "):
            if await page.query_selector(selector):
                return True

        body_text = (await page.text_content("body") or "").lower()
        challenge_phrases = ["verify you are human", "not a robot", "checking your browser",
                             "just a moment", "please wait"]
        for phrase in challenge_phrases:
            if phrase in body_text:
                return True

        return False
    except Exception:
        # Page navigated / context destroyed — challenge is gone
        return False


async def handle_aws_waf_captcha(page: Page, *, timeout_s: float = 300) -> bool:
    """Handle any challenge blocking the login page (CAPTCHA, WAF interstitial, etc).

    Flow:
      1. Wait briefly to see if it resolves on its own (silent JS challenge)
      2. If still blocked: prompt user for manual solve (headed mode)
      3. TODO: integrate CapSolver/2Captcha as paid fallback

    Returns True once the challenge is cleared, False if timed out.
    """
    _log("Checking if challenge resolves on its own...")

    # Give the silent JS challenge up to 10 seconds to resolve
    for _ in range(5):
        await asyncio.sleep(2)
        if not await _page_has_challenge(page):
            _log("[green]Challenge resolved on its own[/green]")
            return True

    # Still blocked — need manual intervention
    _log("[bold yellow]Challenge detected — solve it in the browser window[/bold yellow]")
    _log(f"[dim]Waiting up to {timeout_s // 60:.0f} minutes...[/dim]")

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_s:
        await asyncio.sleep(2)

        # Success if challenge elements are gone
        if not await _page_has_challenge(page):
            _log("[green]Challenge solved — continuing login flow[/green]")
            return True

        # Also success if we landed on the extranet
        if "admin.booking.com" in page.url:
            _log("[green]Challenge solved — reached extranet[/green]")
            return True

    _log("[red]Challenge solve timed out[/red]")
    return False


# =========================================================================
# 6. PAID CAPTCHA SOLVER (last resort — disabled by default)
# =========================================================================

async def solve_captcha_with_api(
    page: Page,
    *,
    api_key: str | None = None,
    provider: str = "capsolver",
) -> bool:
    """Solve AWS WAF CAPTCHA via paid API. NOT IMPLEMENTED YET.

    To enable: set CAPTCHA_SOLVER_API_KEY in .env and
    CAPTCHA_SOLVER_PROVIDER (capsolver or 2captcha).

    Cost: ~$0.0015 per solve.
    """
    if not api_key:
        _log("[dim]No CAPTCHA solver API key configured — skipping paid solver[/dim]")
        return False

    # TODO: implement when needed
    # 1. Extract awsKey, awsIv, awsContext from page
    # 2. POST to CapSolver/2Captcha API
    # 3. Poll for solution
    # 4. Inject aws-waf-token cookie
    _log(f"[yellow]Paid CAPTCHA solver ({provider}) not yet implemented[/yellow]")
    return False
