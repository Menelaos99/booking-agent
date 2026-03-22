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

_MESSAGES_PATH = "/hotel/hoteladmin/extranet_ng/manage/messaging/inbox.html"


def _messages_url(settings: Settings, ses: str = "") -> str:
    return (
        f"https://admin.booking.com{_MESSAGES_PATH}"
        f"?hotel_id={settings.booking_hotel_id}&lang=en&ses={ses}"
    )


def _extract_ses(url: str) -> str:
    """Extract the ses= session parameter from an extranet URL."""
    import re
    match = re.search(r"ses=([^&]+)", url)
    return match.group(1) if match else ""


async def list_messages(page: Page, settings: Settings, unread_only: bool = False) -> list[dict]:
    """Scrape the messages / inbox page."""
    ses = _extract_ses(page.url)
    await page.goto(_messages_url(settings, ses=ses), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(2000, 4000)

    # Handle auth-assurance if triggered
    if "auth-assurance" in page.url or "verify" in page.url:
        from booking_agent.auth.tools import verify_identity
        await verify_identity(page, settings)
        await human_delay(2000, 3000)
        # After verification, we may need to navigate to inbox again
        if "messaging" not in page.url:
            ses = _extract_ses(page.url)
            await page.goto(_messages_url(settings, ses=ses), wait_until="domcontentloaded", timeout=30_000)
            await human_delay(2000, 4000)

    # Wait for the inbox to load — look for message buttons with list-item__title-text
    try:
        await page.wait_for_selector('.list-item__title-text, [class*="list-item__title"]', timeout=15_000)
    except Exception:
        return []

    # Each message in the left panel is a <button> containing list-item__title-text (guest name)
    # Find all message buttons by looking for the guest name containers
    name_elements = await page.query_selector_all('.list-item__title-text, [class*="list-item__title-text"]')
    results: list[dict] = []

    for idx, name_el in enumerate(name_elements):
        guest_name = (await name_el.inner_text()).strip() if name_el else ""

        # Walk up to the button parent to get the full message item
        try:
            item_info = await name_el.evaluate('''el => {
                // Walk up to find the button container
                let btn = el.closest('button') || el.parentElement?.parentElement?.parentElement;
                if (!btn) return {date: '', preview: '', text: ''};
                let text = btn.innerText || '';
                return {text: text.substring(0, 200)};
            }''')
            full_text = item_info.get("text", "")
            # Extract date and preview from the button text
            # Format is typically: "Guest Name\n4 Mar 2026\nPreview text..."
            lines = [l.strip() for l in full_text.split("\n") if l.strip()]
            date = lines[1] if len(lines) > 1 else ""
            preview = lines[2] if len(lines) > 2 else ""
        except Exception:
            date = ""
            preview = ""

        results.append({
            "id": str(idx),
            "guest_name": guest_name,
            "subject": preview,
            "date": date,
            "unread": False,
        })

    return results


async def scrape_past_conversations(page: Page, settings: Settings, max_messages: int = 5) -> list[dict]:
    """Scrape past conversations from the inbox for use as reply examples.

    Clicks through messages, extracts conversation threads.

    Returns list of {"guest_name": str, "conversation": str}
    """
    from datetime import datetime
    from rich.console import Console
    _console = Console()

    def _log(msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        _console.print(f"[dim][{ts}][/dim] [bold cyan][AGENT][/bold cyan] {msg}")

    # Navigate to inbox
    ses = _extract_ses(page.url)
    await page.goto(_messages_url(settings, ses=ses), wait_until="domcontentloaded", timeout=30_000)
    await human_delay(2000, 4000)

    # Handle auth-assurance
    if "auth-assurance" in page.url or "verify" in page.url:
        from booking_agent.auth.tools import verify_identity
        _log("Auth-assurance triggered — need SMS verification")
        await verify_identity(page, settings)
        await human_delay(2000, 3000)
        if "messaging" not in page.url:
            ses = _extract_ses(page.url)
            await page.goto(_messages_url(settings, ses=ses), wait_until="domcontentloaded", timeout=30_000)
            await human_delay(2000, 4000)

    _log(f"Current URL: {page.url[:80]}")

    # Switch to "Sent messages" to find conversations where we replied
    _log("Switching to 'Sent messages'...")
    try:
        # Find the "Sort messages by:" dropdown
        dropdown = await page.query_selector('select')
        if dropdown:
            await dropdown.select_option(label="Sent messages")
            _log("Selected 'Sent messages' from dropdown")
        else:
            # Try clicking text
            await page.click('text=Sort messages by', timeout=3_000)
            await human_delay(500, 1000)
            await page.click('text=Sent messages', timeout=3_000)
            _log("Clicked 'Sent messages' option")
    except Exception:
        # Try broader approach — find any dropdown/select near "Sort"
        try:
            selects = await page.query_selector_all('select')
            for sel in selects:
                options = await sel.evaluate("el => Array.from(el.options).map(o => o.text)")
                _log(f"[dim]Found select with options: {options}[/dim]")
                if any("sent" in o.lower() for o in options):
                    await sel.select_option(label=[o for o in options if "sent" in o.lower()][0])
                    _log("Selected sent messages option")
                    break
        except Exception:
            _log("[yellow]Could not find sort dropdown[/yellow]")

    await human_delay(2000, 3000)

    try:
        await page.wait_for_selector('.list-item__title-text', timeout=15_000)
    except Exception:
        _log("[yellow]Could not find message list[/yellow]")
        return []

    name_elements = await page.query_selector_all('.list-item__title-text, [class*="list-item__title-text"]')
    _log(f"Found {len(name_elements)} sent messages")

    conversations = []

    for idx, name_el in enumerate(name_elements):
        if len(conversations) >= max_messages:
            break

        guest_name = (await name_el.inner_text()).strip()
        _log(f"Opening message {idx}: {guest_name}")

        # Click the message to open it
        try:
            await name_el.evaluate("el => el.closest('button')?.click() || el.click()")
        except Exception:
            _log(f"[yellow]Could not click message {idx}[/yellow]")
            continue
        await human_delay(1500, 2500)

        # Extract the conversation from the right panel
        chat_el = await page.query_selector('.guest-chat, [class*="guest-chat"]')
        if not chat_el:
            _log(f"[dim]No chat panel found for {guest_name}[/dim]")
            continue

        chat_text = (await chat_el.inner_text()).strip()
        _log(f"[dim]Chat text length: {len(chat_text)} chars[/dim]")

        # Include all conversations — even if we haven't replied yet,
        # the guest's message itself is useful context
        if chat_text:
            conversations.append({
                "guest_name": guest_name,
                "conversation": chat_text[:1500],
            })
            _log(f"[green]Captured conversation with {guest_name}[/green]")

    return conversations


async def read_message(page: Page, settings: Settings, message_id: str) -> dict:
    """Open a specific message thread and return its content."""
    # Make sure we're on the inbox page
    if "messaging" not in page.url:
        ses = _extract_ses(page.url)
        await page.goto(_messages_url(settings, ses=ses), wait_until="domcontentloaded", timeout=30_000)
        await human_delay(2000, 4000)

        if "auth-assurance" in page.url or "verify" in page.url:
            from booking_agent.auth.tools import verify_identity
            from booking_agent.config import get_settings
            await verify_identity(page, get_settings())
            await human_delay(2000, 3000)
            if "messaging" not in page.url:
                ses = _extract_ses(page.url)
                await page.goto(_messages_url(settings, ses=ses), wait_until="domcontentloaded", timeout=30_000)
                await human_delay(2000, 4000)

    try:
        await page.wait_for_selector('.list-item__title-text', timeout=15_000)
    except Exception:
        return {"error": "Messages list not found"}

    # Click the message at the given index
    name_elements = await page.query_selector_all('.list-item__title-text, [class*="list-item__title-text"]')
    idx = int(message_id)
    if idx >= len(name_elements):
        return {"error": f"Message {message_id} not found"}

    # Click the parent button of the name element
    try:
        await name_elements[idx].evaluate("el => el.closest('button')?.click() || el.click()")
    except Exception:
        await name_elements[idx].click()
    await human_delay(1500, 3000)

    # Extract guest name from the clicked item
    guest_name = (await name_elements[idx].inner_text()).strip()

    # Extract the conversation body from the right panel
    body = ""
    body_el = await page.query_selector('.guest-chat, [class*="guest-chat"]')
    if body_el:
        body = (await body_el.inner_text()).strip()

    return {
        "id": message_id,
        "guest_name": guest_name,
        "subject": "",
        "body": body,
    }


async def reply_to_message(page: Page, settings: Settings, message_id: str, text: str) -> bool:
    """Reply to a message thread.

    Assumes the message is already open in the conversation panel
    (i.e. read_message was called before this).
    """
    from datetime import datetime
    from rich.console import Console
    _console = Console()

    def _log(msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        _console.print(f"[dim][{ts}][/dim] [bold cyan][AGENT][/bold cyan] {msg}")

    import asyncio as _asyncio

    # Step 1: Remove all overlays (security banner, cookie banner)
    _log("Removing overlays...")
    await page.evaluate("""() => {
        document.querySelectorAll('[class*="bbe73dce14"]').forEach(el => el.remove());
        document.querySelectorAll('[class*="dc7e768484"]').forEach(el => el.remove());
        document.querySelectorAll('[class*="cookie"]').forEach(el => el.remove());
        document.querySelectorAll('[id*="cookie"]').forEach(el => el.remove());
        document.querySelectorAll('[class*="consent"]').forEach(el => el.remove());
    }""")
    await _asyncio.sleep(1)

    # Step 2: Focus textarea via JS and type via keyboard
    _log("Focusing textarea...")
    has_textarea = await page.evaluate("""() => {
        const ta = document.querySelector('textarea');
        if (!ta) return false;
        ta.focus();
        ta.click();
        return true;
    }""")

    if not has_textarea:
        _log("[yellow]No textarea found[/yellow]")
        return False

    _log("Typing reply via keyboard...")
    await page.keyboard.type(text, delay=5)
    await _asyncio.sleep(1)

    # Step 3: Click Send via JS (bypasses any visual overlay)
    _log("Clicking Send via JS...")
    sent = await page.evaluate("""() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const send = btns.find(b => b.textContent.trim() === 'Send');
        if (!send) return false;
        send.click();
        return true;
    }""")

    if sent:
        await _asyncio.sleep(3)
        _log("[green]Reply sent[/green]")
        return True

    _log("[yellow]Could not find Send button[/yellow]")
    return False
