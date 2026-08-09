from __future__ import annotations

from playwright.async_api import Page

from booking_agent.auth.assurance import ensure_messages_access
from booking_agent.config import Settings
from booking_agent.utils.waits import human_delay


async def _message_descriptor(name_el, index: int) -> dict:
    descriptor = await name_el.evaluate(
        """(el, index) => {
            const button = el.closest('button') || el.parentElement?.parentElement?.parentElement;
            const candidates = [
                ['conversation-id', button?.getAttribute('data-conversation-id')],
                ['thread-id', button?.getAttribute('data-thread-id')],
                ['reservation-id', button?.getAttribute('data-reservation-id')],
                ['id', button?.id],
            ];
            for (const [key, value] of candidates) {
                if (value) return {thread_ref: `data:${key}:${value}`, stable: true};
            }
            const link = button?.querySelector('a[href]') || button?.closest('a[href]');
            if (link?.href) return {thread_ref: `href:${link.href}`, stable: true};
            return {thread_ref: `index:${index}`, stable: false};
        }""",
        index,
    )
    return descriptor or {"thread_ref": f"index:{index}", "stable": False}


async def _message_elements(page: Page):
    return await page.query_selector_all(
        '.list-item__title-text, [class*="list-item__title-text"]'
    )


async def _find_message_element(page: Page, message_ref: str):
    elements = await _message_elements(page)
    if message_ref.isdigit():
        index = int(message_ref)
        return (elements[index], index, await _message_descriptor(elements[index], index)) if index < len(elements) else None
    for index, element in enumerate(elements):
        descriptor = await _message_descriptor(element, index)
        if descriptor["thread_ref"] == message_ref:
            return element, index, descriptor
    return None

async def list_messages(page: Page, settings: Settings, unread_only: bool = False) -> list[dict]:
    """Scrape the messages / inbox page."""
    result = await ensure_messages_access(page, settings)
    if not result.verified:
        return []

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
        descriptor = await _message_descriptor(name_el, idx)

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
            "thread_ref": descriptor["thread_ref"],
            "stable_ref": bool(descriptor["stable"]),
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

    result = await ensure_messages_access(page, settings)
    if not result.verified:
        _log("[yellow]Sensitive messages access could not be verified[/yellow]")
        return []

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

    # Scrape messages page by page — "More messages" loads the next batch
    all_conversations = []
    page_num = 1

    while len(all_conversations) < max_messages:
        name_elements = await page.query_selector_all('.list-item__title-text, [class*="list-item__title-text"]')
        _log(f"Page {page_num}: {len(name_elements)} messages")

        for idx, name_el in enumerate(name_elements):
            if len(all_conversations) >= max_messages:
                break
            guest_name = (await name_el.inner_text()).strip()

            # Skip if already scraped (from previous page)
            if any(c["guest_name"] == guest_name for c in all_conversations):
                continue

            _log(f"Opening message: {guest_name}")
            try:
                await name_el.evaluate("el => el.closest('button')?.click() || el.click()")
            except Exception:
                continue
            await human_delay(1500, 2500)

            # Extract structured messages from the chat panel via JS
            messages = await page.evaluate("""(guestName) => {
                const chat = document.querySelector('.guest-chat, [class*="guest-chat"]');
                if (!chat) return null;

                // Find all message bubbles — they're typically in distinct containers
                // Guest messages and host replies have different styling
                const result = [];
                const allText = chat.querySelectorAll('div, p, span');
                let currentMsg = '';
                let currentSender = '';

                // Simple heuristic: walk through text nodes and group by sender
                // Messages from the host (you) are typically right-aligned or in colored bubbles
                // For now, extract the clean text and label by position
                const rawText = chat.innerText || '';

                // Remove UI noise
                const noise = [
                    'Reply', 'Protect your account security', 'Please do not share sensitive',
                    'Read more', 'Report an issue', 'Images', 'Templates', 'Send',
                    'Never share sensitive information', 'contacting Partner Support',
                    'No reply needed', 'Delivered', 'Seen'
                ];
                let cleaned = rawText;
                for (const n of noise) {
                    cleaned = cleaned.split(n).join('');
                }

                // Clean up whitespace
                cleaned = cleaned.replace(/\\n{3,}/g, '\\n\\n').trim();
                return cleaned;
            }""", guest_name)

            if messages:
                all_conversations.append({
                    "guest_name": guest_name,
                    "conversation": messages[:1500],
                })
                _log(f"[green]Captured: {guest_name} ({len(messages)} chars)[/green]")

        # Try to load more by clicking the "More messages" BUTTON (not span)
        try:
            loaded_more = await page.evaluate("""() => {
                const spans = Array.from(document.querySelectorAll('span'));
                const more = spans.find(s => s.textContent.trim() === 'More messages');
                if (!more) return false;
                // The click handler is on the parent <button>, not the <span>
                const btn = more.closest('button') || more.parentElement;
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            if loaded_more:
                _log("Loading more messages...")
                await human_delay(3000, 5000)
                page_num += 1
            else:
                _log("No more pages — done")
                break
        except Exception:
            break

    _log(f"Total conversations scraped: {len(all_conversations)}")
    return all_conversations


async def read_message(page: Page, settings: Settings, message_id: str) -> dict:
    """Open a specific message thread and return its content."""
    # Make sure we're on the inbox page
    if "messaging" not in page.url:
        result = await ensure_messages_access(page, settings)
        if not result.verified:
            return {"error": "Sensitive messages access could not be verified"}

    try:
        await page.wait_for_selector('.list-item__title-text', timeout=15_000)
    except Exception:
        return {"error": "Messages list not found"}

    # Click the message at the given index
    target = await _find_message_element(page, message_id)
    if target is None:
        return {"error": f"Message {message_id} not found"}
    name_el, idx, descriptor = target

    # Click the parent button of the name element
    try:
        await name_el.evaluate("el => el.closest('button')?.click() || el.click()")
    except Exception:
        await name_el.click()
    await human_delay(1500, 3000)

    # Extract guest name from the clicked item
    guest_name = (await name_el.inner_text()).strip()

    # Extract the conversation body from the right panel
    body = ""
    body_el = await page.query_selector('.guest-chat, [class*="guest-chat"]')
    if body_el:
        body = (await body_el.inner_text()).strip()

    return {
        "id": message_id,
        "index": str(idx),
        "thread_ref": descriptor["thread_ref"],
        "stable_ref": bool(descriptor["stable"]),
        "guest_name": guest_name,
        "subject": "",
        "body": body,
    }


async def reply_to_message(
    page: Page,
    settings: Settings,
    message_id: str,
    text: str,
    *,
    expected_guest: str | None = None,
    require_stable_ref: bool = False,
) -> bool:
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

    detail = await read_message(page, settings, message_id)
    if "error" in detail:
        _log(f"[yellow]{detail['error']}[/yellow]")
        return False
    if require_stable_ref and not detail.get("stable_ref"):
        _log("[yellow]Refusing to send without a stable Booking thread reference[/yellow]")
        return False
    actual_guest = str(detail.get("guest_name", "")).strip()
    if expected_guest and actual_guest.casefold() != expected_guest.strip().casefold():
        _log("[yellow]Target guest changed; refusing to send[/yellow]")
        return False

    # Step 1: Remove known visual overlays (security banner, cookie banner)
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

    _log("Typing reviewed reply...")
    textarea = page.locator("textarea").first
    await textarea.fill(text)
    if await textarea.input_value() != text:
        _log("[yellow]Reply text verification failed[/yellow]")
        return False
    await _asyncio.sleep(1)

    # Step 3: Click Send via JS (bypasses any visual overlay)
    _log("Clicking Send via JS...")
    send = page.get_by_role("button", name="Send", exact=True)
    if await send.count() == 0:
        _log("[yellow]Could not find Send button[/yellow]")
        return False
    await send.first.click()
    sent = True

    if sent:
        await _asyncio.sleep(3)
        _log("[green]Reply sent[/green]")
        return True

    _log("[yellow]Could not find Send button[/yellow]")
    return False
