from __future__ import annotations

import re
from datetime import datetime

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.antibot import wait_for_waf_challenge
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

_CURRENCY_BY_SYMBOL = {"€": "EUR", "$": "USD", "£": "GBP"}
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d %b %Y",
    "%d %B %Y",
    "%d/%m/%Y",
    "%b %d, %Y",
)


def parse_booking_date(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value.strip())
    if not cleaned:
        return None
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_money(value: str) -> tuple[int | None, str | None]:
    """Convert a localized Booking amount to integer minor units and currency."""

    cleaned = value.strip()
    if not cleaned:
        return None, None
    currency = next(
        (code for symbol, code in _CURRENCY_BY_SYMBOL.items() if symbol in cleaned),
        None,
    )
    if currency is None:
        code_match = re.search(r"\b([A-Z]{3})\b", cleaned.upper())
        currency = code_match.group(1) if code_match else None

    numeric = re.sub(r"[^0-9,.-]", "", cleaned)
    if not numeric or numeric in {"-", ".", ","}:
        return None, currency

    sign = -1 if numeric.startswith("-") else 1
    numeric = numeric.lstrip("-")
    last_dot = numeric.rfind(".")
    last_comma = numeric.rfind(",")
    decimal_index = max(last_dot, last_comma)
    decimal_digits = len(numeric) - decimal_index - 1 if decimal_index >= 0 else 0
    if decimal_index >= 0 and decimal_digits in {1, 2}:
        whole = re.sub(r"\D", "", numeric[:decimal_index]) or "0"
        fraction = re.sub(r"\D", "", numeric[decimal_index + 1 :]).ljust(2, "0")[:2]
    else:
        whole = re.sub(r"\D", "", numeric) or "0"
        fraction = "00"
    return sign * (int(whole) * 100 + int(fraction)), currency


def parse_guest_count(value: str) -> int | None:
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


async def _text_for(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            element = await page.query_selector(selector)
            if element and await element.is_visible():
                value = (await element.inner_text()).strip()
                if value:
                    return value
        except Exception:
            continue
    return ""


async def _navigate_reservation_page(page: Page, url: str) -> None:
    try:
        await page.goto(url, wait_until="commit", timeout=60_000)
    except Exception:
        if "admin.booking.com" not in page.url:
            raise
    await wait_for_waf_challenge(page, timeout_s=30)
    await human_delay(1500, 3000)


def _reservations_url(settings: Settings, status: str = "upcoming") -> str:
    mapped = _STATUS_MAP.get(status, "upcoming")
    return (
        f"https://admin.booking.com{_RESERVATIONS_PATH}"
        f"?hotel_id={settings.booking_hotel_id}&status={mapped}"
    )


async def list_reservations(page: Page, settings: Settings, status: str = "upcoming") -> list[dict]:
    """Scrape the reservations list page and return structured data."""
    url = _reservations_url(settings, status)
    await _navigate_reservation_page(page, url)

    # Wait for the table to appear
    try:
        await page.wait_for_selector(RESERVATIONS_TABLE, timeout=30_000)
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

        total = (await total_el.inner_text()).strip() if total_el else ""
        amount_minor, currency = parse_money(total)
        results.append({
            "booking_id": (await id_el.inner_text()).strip() if id_el else "",
            "guest_name": (await guest_el.inner_text()).strip() if guest_el else "",
            "check_in": parse_booking_date((await checkin_el.inner_text()).strip()) if checkin_el else None,
            "check_out": parse_booking_date((await checkout_el.inner_text()).strip()) if checkout_el else None,
            "status": (await status_el.inner_text()).strip() if status_el else "",
            "total": total,
            "amount_raw": total,
            "amount_minor": amount_minor,
            "currency": currency,
            "hotel_id": settings.booking_hotel_id,
        })

    return results


async def show_reservation(page: Page, settings: Settings, booking_id: str) -> dict:
    """Navigate to a specific reservation detail page and scrape it."""
    detail_url = (
        f"https://admin.booking.com{_RESERVATIONS_PATH}"
        f"?hotel_id={settings.booking_hotel_id}&res_id={booking_id}"
    )
    await _navigate_reservation_page(page, detail_url)

    # Scrape whatever detail fields are available
    detail: dict = {"booking_id": booking_id}

    selectors_map = {
        "guest_name": [".guest-name", "[data-testid='guest-name']"],
        "check_in": [".check-in-date", "[data-testid='checkin']"],
        "check_out": [".check-out-date", "[data-testid='checkout']"],
        "room_type": [".room-type", "[data-testid='room-type']"],
        "status": [".reservation-status", "[data-testid='status']"],
        "total": [".total-price", "[data-testid='total']"],
        "payment_status": [".payment-status", "[data-testid='payment']"],
        "special_requests": [".special-requests", "[data-testid='requests']"],
        "guest_email": [".guest-email", "[data-testid='email']", "a[href^='mailto:']"],
        "guest_phone": [".guest-phone", "[data-testid='phone']", "a[href^='tel:']"],
        "guest_count_raw": [".guest-count", "[data-testid='guest-count']"],
        "booked_at": [".booking-date", "[data-testid='booking-date']"],
        "expected_arrival_time": [".arrival-time", "[data-testid='arrival-time']"],
        "preferred_language": [".guest-language", "[data-testid='guest-language']"],
        "declared_country": [".guest-country", "[data-testid='guest-country']"],
        "commission": [".commission", "[data-testid='commission']"],
        "net_payout": [".net-payout", "[data-testid='net-payout']"],
    }

    for key, selectors in selectors_map.items():
        detail[key] = await _text_for(page, selectors)

    detail["hotel_id"] = settings.booking_hotel_id
    detail["check_in"] = parse_booking_date(detail["check_in"])
    detail["check_out"] = parse_booking_date(detail["check_out"])
    detail["amount_raw"] = detail.pop("total")
    detail["amount_minor"], detail["currency"] = parse_money(detail["amount_raw"])
    detail["commission_minor"], _ = parse_money(detail.pop("commission"))
    detail["net_payout_minor"], _ = parse_money(detail.pop("net_payout"))
    detail["guest_count"] = parse_guest_count(detail.pop("guest_count_raw"))

    return detail
