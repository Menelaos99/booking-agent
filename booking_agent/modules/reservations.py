from __future__ import annotations

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    RESERVATION_CHECK_IN,
    RESERVATION_CHECK_OUT,
    RESERVATION_GUEST_NAME,
    RESERVATION_ID_LINK,
    RESERVATION_ROW,
    RESERVATION_STATUS,
    RESERVATION_TOTAL,
    RESERVATIONS_TABLE,
)
from booking_agent.utils.waits import human_delay

# URL patterns for the extranet reservations page
_RESERVATIONS_PATH = "/hotel/hoteladmin/extranet_ng/manage/search_reservations.html"
_STATUS_MAP = {
    "upcoming": "upcoming",
    "past": "past",
    "cancelled": "cancelled",
}


def _reservations_url(settings: Settings, status: str = "upcoming") -> str:
    mapped = _STATUS_MAP.get(status, "upcoming")
    return (
        f"https://admin.booking.com{_RESERVATIONS_PATH}"
        f"?hotel_id={settings.booking_hotel_id}&status={mapped}"
    )


async def list_reservations(page: Page, settings: Settings, status: str = "upcoming") -> list[dict]:
    """Scrape the reservations list page and return structured data."""
    url = _reservations_url(settings, status)
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    # Wait for the table to appear
    try:
        await page.wait_for_selector(RESERVATIONS_TABLE, timeout=15_000)
    except Exception:
        return []

    rows = await page.query_selector_all(RESERVATION_ROW)
    results: list[dict] = []

    for row in rows:
        id_el = await row.query_selector(RESERVATION_ID_LINK)
        guest_el = await row.query_selector(RESERVATION_GUEST_NAME)
        checkin_el = await row.query_selector(RESERVATION_CHECK_IN)
        checkout_el = await row.query_selector(RESERVATION_CHECK_OUT)
        status_el = await row.query_selector(RESERVATION_STATUS)
        total_el = await row.query_selector(RESERVATION_TOTAL)

        results.append({
            "booking_id": (await id_el.inner_text()).strip() if id_el else "",
            "guest_name": (await guest_el.inner_text()).strip() if guest_el else "",
            "check_in": (await checkin_el.inner_text()).strip() if checkin_el else "",
            "check_out": (await checkout_el.inner_text()).strip() if checkout_el else "",
            "status": (await status_el.inner_text()).strip() if status_el else "",
            "total": (await total_el.inner_text()).strip() if total_el else "",
        })

    return results


async def show_reservation(page: Page, settings: Settings, booking_id: str) -> dict:
    """Navigate to a specific reservation detail page and scrape it."""
    detail_url = (
        f"https://admin.booking.com{_RESERVATIONS_PATH}"
        f"?hotel_id={settings.booking_hotel_id}&res_id={booking_id}"
    )
    await page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    # Scrape whatever detail fields are available
    detail: dict = {"booking_id": booking_id}

    selectors_map = {
        "guest_name": ".guest-name, [data-testid='guest-name']",
        "check_in": ".check-in-date, [data-testid='checkin']",
        "check_out": ".check-out-date, [data-testid='checkout']",
        "room_type": ".room-type, [data-testid='room-type']",
        "status": ".reservation-status, [data-testid='status']",
        "total": ".total-price, [data-testid='total']",
        "payment_status": ".payment-status, [data-testid='payment']",
        "special_requests": ".special-requests, [data-testid='requests']",
        "guest_email": ".guest-email, [data-testid='email']",
        "guest_phone": ".guest-phone, [data-testid='phone']",
    }

    for key, selector in selectors_map.items():
        el = await page.query_selector(selector)
        detail[key] = (await el.inner_text()).strip() if el else ""

    return detail
