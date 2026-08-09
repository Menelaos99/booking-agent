"""Agentic module — find guests with future reservations who haven't been replied to.

Autonomously scrapes the Booking.com extranet to cross-reference upcoming
reservations against the messaging inbox, identifying guests that still
need a reply.

Workflow:
  1. Scrape all upcoming reservations (future check-in dates)
  2. Navigate to inbox — collect all guest names from the default view
  3. Switch to "Sent messages" — collect replied guest names
  4. Cross-reference into three categories:
       needs_reply  — guest messaged, host hasn't replied
       no_contact   — no message thread at all
       replied      — already handled (excluded from output)
  5. Save report to state/unreplied_guests.json
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import Page
from rich.console import Console

from booking_agent.auth.assurance import ensure_messages_access
from booking_agent.config import STATE_DIR, Settings
from booking_agent.modules.reservations import list_reservations
from booking_agent.utils.waits import human_delay

console = Console()
UNREPLIED_REPORT = STATE_DIR / "unreplied_guests.json"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] [bold magenta][UNREPLIED-AGENT][/bold magenta] {msg}")


def _normalize_name(name: str) -> str:
    """Normalize a guest name for fuzzy matching (lowercase, collapse whitespace)."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _is_future_checkin(check_in_str: str) -> bool:
    """Return True if the check-in date string is today or later."""
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(check_in_str, fmt).date() >= date.today()
        except ValueError:
            continue
    # Can't parse — assume future (it came from the "upcoming" filter)
    return True


# ────────────────────────────────────────────────────────────────
# Inbox helpers
# ────────────────────────────────────────────────────────────────


async def _scrape_names_from_current_view(
    page: Page, *, max_pages: int = 10,
) -> set[str]:
    """Collect all guest names visible in the current inbox view.

    Paginates through the "More messages" button until exhausted or
    *max_pages* pages have been loaded.
    """
    try:
        await page.wait_for_selector(".list-item__title-text", timeout=15_000)
    except Exception:
        _log("[yellow]  No messages visible in this view[/yellow]")
        return set()

    names: set[str] = set()
    page_num = 1

    while page_num <= max_pages:
        elements = await page.query_selector_all(
            '.list-item__title-text, [class*="list-item__title-text"]'
        )
        before = len(names)
        for el in elements:
            raw = (await el.inner_text()).strip()
            if raw:
                names.add(_normalize_name(raw))

        _log(f"  Page {page_num}: {len(elements)} items, {len(names) - before} new names")

        # Try to load more
        try:
            loaded = await page.evaluate("""() => {
                const spans = Array.from(document.querySelectorAll('span'));
                const more = spans.find(s => s.textContent.trim() === 'More messages');
                if (!more) return false;
                const btn = more.closest('button') || more.parentElement;
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            if loaded:
                _log("  Loading more messages...")
                await human_delay(3000, 5000)
                page_num += 1
            else:
                break
        except Exception:
            break

    return names


async def _navigate_to_inbox(page: Page, settings: Settings) -> bool:
    """Navigate to the messaging inbox, handling auth-assurance.

    Returns True if the inbox was reached successfully.
    """
    _log("Navigating to inbox...")
    result = await ensure_messages_access(page, settings)
    return result.verified


async def _switch_to_sent_messages(page: Page) -> bool:
    """Switch the inbox dropdown to 'Sent messages'.

    Returns True on success.
    """
    try:
        dropdown = await page.query_selector("select")
        if dropdown:
            await dropdown.select_option(label="Sent messages")
            _log("Switched to 'Sent messages'")
            await human_delay(2000, 3000)
            return True

        # Fallback: scan all <select> elements
        selects = await page.query_selector_all("select")
        for sel in selects:
            options = await sel.evaluate(
                "el => Array.from(el.options).map(o => o.text)"
            )
            if any("sent" in o.lower() for o in options):
                label = next(o for o in options if "sent" in o.lower())
                await sel.select_option(label=label)
                _log(f"Switched to '{label}'")
                await human_delay(2000, 3000)
                return True
    except Exception as e:
        _log(f"[yellow]Failed to switch to Sent messages: {e}[/yellow]")

    return False


# ────────────────────────────────────────────────────────────────
# Report persistence
# ────────────────────────────────────────────────────────────────


def _save_report(results: list[dict], summary: dict) -> Path:
    """Persist the unreplied report to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now().isoformat(),
        "summary": summary,
        "unreplied_guests": results,
    }
    UNREPLIED_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return UNREPLIED_REPORT


# ────────────────────────────────────────────────────────────────
# Main agentic workflow
# ────────────────────────────────────────────────────────────────


async def find_unreplied_guests(page: Page, settings: Settings) -> list[dict]:
    """Find guests with future reservations who haven't been replied to.

    Returns a list of reservation dicts annotated with ``reply_status``:
      * ``needs_reply``  — guest has a message thread but no host reply
      * ``no_contact``   — no message thread exists

    Guests who have already been replied to are excluded.
    """
    _log("Starting unreplied-guest scan...")
    _log("=" * 55)

    # ── Step 1: Scrape upcoming reservations ───────────────────
    _log("[Step 1/4] Scraping upcoming reservations...")
    reservations = await list_reservations(page, settings, status="upcoming")

    if not reservations:
        _log("[yellow]No upcoming reservations found — done[/yellow]")
        return []

    future = [r for r in reservations if _is_future_checkin(r.get("check_in", ""))]
    _log(f"Found {len(future)} future reservations (of {len(reservations)} upcoming)")

    if not future:
        _log("[yellow]No future check-ins — done[/yellow]")
        return []

    # Build lookup: normalized name → reservation dict
    guest_lookup: dict[str, dict] = {}
    for r in future:
        name = _normalize_name(r.get("guest_name", ""))
        if name:
            guest_lookup[name] = r

    _log(f"Unique future guests: {len(guest_lookup)}")

    # ── Step 2: Scrape inbox — all conversations ───────────────
    _log("=" * 55)
    _log("[Step 2/4] Scraping inbox (all conversations)...")

    if not await _navigate_to_inbox(page, settings):
        _log("[red]Failed to reach inbox — aborting[/red]")
        return []

    inbox_names = await _scrape_names_from_current_view(page)
    _log(f"Total inbox guests: {len(inbox_names)}")

    # ── Step 3: Switch to Sent messages ────────────────────────
    _log("=" * 55)
    _log("[Step 3/4] Scraping sent messages (replied conversations)...")

    if await _switch_to_sent_messages(page):
        replied_names = await _scrape_names_from_current_view(page)
        _log(f"Total replied guests: {len(replied_names)}")
    else:
        _log("[yellow]Could not access sent messages — treating all inbox as unreplied[/yellow]")
        replied_names: set[str] = set()

    # ── Step 4: Cross-reference ────────────────────────────────
    _log("=" * 55)
    _log("[Step 4/4] Cross-referencing reservations vs messages...")

    results: list[dict] = []
    stats = {"total": len(guest_lookup), "replied": 0, "needs_reply": 0, "no_contact": 0}

    for name, reservation in guest_lookup.items():
        display = reservation.get("guest_name", name)

        if name in replied_names:
            stats["replied"] += 1
            _log(f"  [green]OK[/green]  {display} — replied")

        elif name in inbox_names:
            reservation["reply_status"] = "needs_reply"
            results.append(reservation)
            stats["needs_reply"] += 1
            _log(f"  [red]!![/red]  {display} — [bold red]NEEDS REPLY[/bold red]")

        else:
            reservation["reply_status"] = "no_contact"
            results.append(reservation)
            stats["no_contact"] += 1
            _log(f"  [yellow]--[/yellow]  {display} — no messages yet")

    # ── Summary ────────────────────────────────────────────────
    _log("=" * 55)
    _log("[bold]Scan complete — summary:[/bold]")
    _log(f"  Future reservations:  {stats['total']}")
    _log(f"  Already replied:      [green]{stats['replied']}[/green]")
    _log(f"  Needs reply:          [bold red]{stats['needs_reply']}[/bold red]")
    _log(f"  No contact yet:       [yellow]{stats['no_contact']}[/yellow]")

    if results:
        path = _save_report(results, stats)
        _log(f"Report saved -> {path}")

    return results
