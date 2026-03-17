import asyncio
import random

from playwright.async_api import Page


async def human_delay(min_ms: int = 500, max_ms: int = 1500) -> None:
    """Sleep for a random duration to mimic human behaviour."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


async def wait_for_any(page: Page, selectors: list[str], timeout: float = 10_000) -> str | None:
    """Wait until any one of *selectors* appears. Returns the first matched selector or None."""
    js_expr = " || ".join(
        f'document.querySelector({s!r})' for s in selectors
    )
    try:
        await page.wait_for_function(js_expr, timeout=timeout)
    except Exception:
        return None

    for sel in selectors:
        if await page.query_selector(sel):
            return sel
    return None


async def wait_for_navigation_to(page: Page, url_substring: str, timeout: float = 30_000) -> bool:
    """Wait until the page URL contains *url_substring*."""
    try:
        await page.wait_for_url(f"**{url_substring}**", timeout=timeout)
        return True
    except Exception:
        return False


async def safe_click(page: Page, selector: str, timeout: float = 5_000) -> bool:
    """Click an element if it exists within *timeout*."""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await human_delay(200, 600)
        await page.click(selector)
        return True
    except Exception:
        return False


async def safe_fill(page: Page, selector: str, value: str, timeout: float = 5_000) -> bool:
    """Fill an input if it exists within *timeout*."""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await human_delay(100, 400)
        await page.fill(selector, value)
        return True
    except Exception:
        return False
