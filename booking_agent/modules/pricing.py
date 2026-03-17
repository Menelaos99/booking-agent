from __future__ import annotations

from datetime import date, timedelta

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    PRICING_CALENDAR,
    PRICING_CELL,
    PRICING_INPUT,
    PRICING_SAVE,
)
from booking_agent.utils.waits import human_delay, safe_click

_PRICING_PATH = "/hotel/hoteladmin/extranet_ng/manage/rates_and_availability/calendar.html"


def _pricing_url(settings: Settings, month: str | None = None) -> str:
    base = (
        f"https://admin.booking.com{_PRICING_PATH}"
        f"?hotel_id={settings.booking_hotel_id}"
    )
    if month:
        base += f"&month={month}"
    return base


async def view_pricing(page: Page, settings: Settings, month: str | None = None) -> list[dict]:
    """Scrape the pricing calendar for the given month."""
    await page.goto(_pricing_url(settings, month), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    try:
        await page.wait_for_selector(PRICING_CALENDAR, timeout=15_000)
    except Exception:
        return []

    cells = await page.query_selector_all(PRICING_CELL)
    results: list[dict] = []

    for cell in cells:
        date_attr = await cell.get_attribute("data-date")
        price_el = await cell.query_selector(".price, .rate-value, [data-testid='rate']")
        room_el = await cell.query_selector(".room-name, [data-testid='room']")

        results.append({
            "date": date_attr or "",
            "price": (await price_el.inner_text()).strip() if price_el else "",
            "room": (await room_el.inner_text()).strip() if room_el else "",
        })

    return results


async def set_price(
    page: Page,
    settings: Settings,
    room_id: str,
    date_from: date,
    date_to: date,
    price: float,
) -> bool:
    """Set the price for a room over a date range."""
    # Navigate to the pricing calendar for the relevant month
    month_str = date_from.strftime("%Y-%m")
    await page.goto(_pricing_url(settings, month_str), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    try:
        await page.wait_for_selector(PRICING_CALENDAR, timeout=15_000)
    except Exception:
        return False

    current = date_from
    while current <= date_to:
        date_str = current.isoformat()
        cell_selector = f'[data-date="{date_str}"][data-room-id="{room_id}"], [data-date="{date_str}"]'

        cell = await page.query_selector(cell_selector)
        if cell:
            await cell.click()
            await human_delay(300, 600)

            price_input = await page.query_selector(PRICING_INPUT)
            if price_input:
                await price_input.fill("")
                await price_input.fill(str(price))
                await human_delay(200, 400)

        current += timedelta(days=1)

    # Save changes
    clicked = await safe_click(page, PRICING_SAVE, timeout=5_000)
    await human_delay(1000, 2000)
    return clicked
