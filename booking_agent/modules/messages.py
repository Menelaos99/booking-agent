from __future__ import annotations

from playwright.async_api import Page

from booking_agent.config import Settings
from booking_agent.utils.selectors import (
    MESSAGE_BODY,
    MESSAGE_DATE,
    MESSAGE_GUEST_NAME,
    MESSAGE_ITEM,
    MESSAGE_REPLY_INPUT,
    MESSAGE_SEND_BUTTON,
    MESSAGE_SUBJECT,
    MESSAGE_UNREAD,
    MESSAGES_LIST,
)
from booking_agent.utils.waits import human_delay, safe_click, safe_fill

_MESSAGES_PATH = "/hotel/hoteladmin/extranet_ng/manage/messaging.html"


def _messages_url(settings: Settings) -> str:
    return (
        f"https://admin.booking.com{_MESSAGES_PATH}"
        f"?hotel_id={settings.booking_hotel_id}"
    )


async def list_messages(page: Page, settings: Settings, unread_only: bool = False) -> list[dict]:
    """Scrape the messages / inbox page."""
    await page.goto(_messages_url(settings), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    try:
        await page.wait_for_selector(MESSAGES_LIST, timeout=15_000)
    except Exception:
        return []

    items = await page.query_selector_all(MESSAGE_ITEM)
    results: list[dict] = []

    for idx, item in enumerate(items):
        guest_el = await item.query_selector(MESSAGE_GUEST_NAME)
        subject_el = await item.query_selector(MESSAGE_SUBJECT)
        date_el = await item.query_selector(MESSAGE_DATE)
        unread = await item.query_selector(MESSAGE_UNREAD) is not None

        if unread_only and not unread:
            continue

        results.append({
            "id": str(idx),
            "guest_name": (await guest_el.inner_text()).strip() if guest_el else "",
            "subject": (await subject_el.inner_text()).strip() if subject_el else "",
            "date": (await date_el.inner_text()).strip() if date_el else "",
            "unread": unread,
        })

    return results


async def read_message(page: Page, settings: Settings, message_id: str) -> dict:
    """Open a specific message thread and return its content."""
    await page.goto(_messages_url(settings), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(1500, 3000)

    try:
        await page.wait_for_selector(MESSAGES_LIST, timeout=15_000)
    except Exception:
        return {"error": "Messages list not found"}

    items = await page.query_selector_all(MESSAGE_ITEM)
    idx = int(message_id)
    if idx >= len(items):
        return {"error": f"Message {message_id} not found"}

    await items[idx].click()
    await human_delay(1000, 2000)

    body_el = await page.query_selector(MESSAGE_BODY)
    guest_el = await page.query_selector(MESSAGE_GUEST_NAME)
    subject_el = await page.query_selector(MESSAGE_SUBJECT)

    return {
        "id": message_id,
        "guest_name": (await guest_el.inner_text()).strip() if guest_el else "",
        "subject": (await subject_el.inner_text()).strip() if subject_el else "",
        "body": (await body_el.inner_text()).strip() if body_el else "",
    }


async def reply_to_message(page: Page, settings: Settings, message_id: str, text: str) -> bool:
    """Reply to a message thread."""
    # First open the message
    await read_message(page, settings, message_id)
    await human_delay(500, 1000)

    filled = await safe_fill(page, MESSAGE_REPLY_INPUT, text, timeout=5_000)
    if not filled:
        return False

    await human_delay(500, 1000)
    return await safe_click(page, MESSAGE_SEND_BUTTON, timeout=5_000)
