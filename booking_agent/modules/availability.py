from __future__ import annotations

from datetime import date, timedelta

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    AVAILABILITY_CALENDAR,
    AVAILABILITY_CELL,
    AVAILABILITY_CLOSE_BUTTON,
    AVAILABILITY_OPEN_BUTTON,
)
from booking_agent.utils.waits import human_delay, safe_click

_AVAILABILITY_PATH = "/hotel/hoteladmin/extranet_ng/manage/rates_and_availability/calendar.html"


def _availability_url(settings: Settings, month: str | None = None) -> str:
    base = (
        f"https://admin.booking.com{_AVAILABILITY_PATH}"
        f"?hotel_id={settings.booking_hotel_id}"
    )
    if month:
        base += f"&month={month}"
    return base


async def view_availability(page: Page, settings: Settings, month: str | None = None) -> list[dict]:
    """Scrape the availability calendar."""
    await page.goto(_availability_url(settings, month), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    try:
        await page.wait_for_selector(AVAILABILITY_CALENDAR, timeout=15_000)
    except Exception:
        return []

    cells = await page.query_selector_all(AVAILABILITY_CELL)
    results: list[dict] = []

    for cell in cells:
        date_attr = await cell.get_attribute("data-date")
        status_attr = await cell.get_attribute("data-status")
        room_el = await cell.query_selector(".room-name, [data-testid='room']")

        results.append({
            "date": date_attr or "",
            "status": status_attr or "unknown",
            "room": (await room_el.inner_text()).strip() if room_el else "",
        })

    return results


async def _toggle_availability(
    page: Page,
    settings: Settings,
    room_id: str,
    date_from: date,
    date_to: date,
    action: str,  # "open" or "close"
) -> bool:
    """Open or close availability for a room over a date range."""
    month_str = date_from.strftime("%Y-%m")
    await page.goto(_availability_url(settings, month_str), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    try:
        await page.wait_for_selector(AVAILABILITY_CALENDAR, timeout=15_000)
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

            btn_selector = AVAILABILITY_OPEN_BUTTON if action == "open" else AVAILABILITY_CLOSE_BUTTON
            await safe_click(page, btn_selector, timeout=3_000)
            await human_delay(300, 600)

        current += timedelta(days=1)

    await human_delay(1000, 2000)
    return True


async def close_availability(
    page: Page, settings: Settings, room_id: str, date_from: date, date_to: date,
) -> bool:
    return await _toggle_availability(page, settings, room_id, date_from, date_to, "close")


async def open_availability(
    page: Page, settings: Settings, room_id: str, date_from: date, date_to: date,
) -> bool:
    return await _toggle_availability(page, settings, room_id, date_from, date_to, "open")
