from __future__ import annotations

import asyncio
from datetime import date

import typer
from rich.console import Console

from booking_agent.config import get_settings
from booking_agent.utils.output import (
    messages_table,
    print_error,
    print_info,
    print_success,
    pricing_table,
    reservations_table,
    stats_panel,
)

app = typer.Typer(name="booking", help="Booking.com Extranet Agent")
console = Console()

# --- Sub-apps ---
login_app = typer.Typer(help="Authentication commands")
res_app = typer.Typer(help="Reservation management")
avail_app = typer.Typer(help="Availability management")
price_app = typer.Typer(help="Pricing management")
msg_app = typer.Typer(help="Guest messages")

app.add_typer(res_app, name="reservations")
app.add_typer(avail_app, name="availability")
app.add_typer(price_app, name="pricing")
app.add_typer(msg_app, name="messages")


def _run(coro):
    """Run an async coroutine from sync context."""
    return asyncio.run(coro)


# ─────────────────────────── Login ───────────────────────────


@app.command("login")
def login(
    check: bool = typer.Option(False, "--check", help="Only validate saved session"),
):
    """Log in to Booking.com extranet or validate an existing session."""
    from booking_agent.browser import get_browser_page, is_session_valid, save_session

    async def _login():
        if check:
            async with get_browser_page(get_settings(), headless=True) as page:
                if await is_session_valid(page, get_settings()):
                    print_success("Session is valid.")
                else:
                    print_error("Session is invalid or expired. Run `booking login` to re-authenticate.")
                    raise typer.Exit(1)
        else:
            from booking_agent.auth.login import perform_login

            async with get_browser_page(get_settings(), headless=False, restore_session=False) as page:
                await perform_login(page, get_settings())

    _run(_login())


# ─────────────────────────── Reservations ───────────────────────────


@res_app.command("list")
def reservations_list(
    status: str = typer.Option("upcoming", "--status", "-s", help="upcoming|past|cancelled"),
):
    """List reservations."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.reservations import list_reservations

    async def _list():
        async with get_authenticated_page(get_settings()) as page:
            data = await list_reservations(page, get_settings(), status)
            if data:
                reservations_table(data)
            else:
                print_info("No reservations found.")

    _run(_list())


@res_app.command("show")
def reservations_show(
    booking_id: str = typer.Argument(..., help="Booking / reservation ID"),
):
    """Show details for a specific reservation."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.reservations import show_reservation

    async def _show():
        async with get_authenticated_page(get_settings()) as page:
            detail = await show_reservation(page, get_settings(), booking_id)
            from rich.table import Table

            table = Table(title=f"Reservation {booking_id}", show_lines=True)
            table.add_column("Field", style="cyan")
            table.add_column("Value")
            for k, v in detail.items():
                if v:
                    table.add_row(k.replace("_", " ").title(), str(v))
            console.print(table)

    _run(_show())


# ─────────────────────────── Availability ───────────────────────────


@avail_app.command("view")
def availability_view(
    month: str = typer.Option(None, "--month", "-m", help="Month to view (YYYY-MM)"),
):
    """View room availability calendar."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.availability import view_availability

    async def _view():
        async with get_authenticated_page(get_settings()) as page:
            data = await view_availability(page, get_settings(), month)
            if data:
                from rich.table import Table

                table = Table(title="Availability", show_lines=True)
                table.add_column("Date", style="cyan")
                table.add_column("Room")
                table.add_column("Status", style="green")
                for row in data:
                    table.add_row(row["date"], row["room"], row["status"])
                console.print(table)
            else:
                print_info("No availability data found.")

    _run(_view())


@avail_app.command("close")
def availability_close(
    room: str = typer.Option(..., "--room", "-r", help="Room ID"),
    date_from: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)"),
    date_to: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)"),
):
    """Close availability for a room over a date range."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.availability import close_availability

    async def _close():
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
        async with get_authenticated_page(get_settings()) as page:
            ok = await close_availability(page, get_settings(), room, d_from, d_to)
            if ok:
                print_success(f"Closed availability for room {room} from {date_from} to {date_to}.")
            else:
                print_error("Failed to close availability.")

    _run(_close())


@avail_app.command("open")
def availability_open(
    room: str = typer.Option(..., "--room", "-r", help="Room ID"),
    date_from: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)"),
    date_to: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)"),
):
    """Open availability for a room over a date range."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.availability import open_availability

    async def _open():
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
        async with get_authenticated_page(get_settings()) as page:
            ok = await open_availability(page, get_settings(), room, d_from, d_to)
            if ok:
                print_success(f"Opened availability for room {room} from {date_from} to {date_to}.")
            else:
                print_error("Failed to open availability.")

    _run(_open())


# ─────────────────────────── Pricing ───────────────────────────


@price_app.command("view")
def pricing_view(
    month: str = typer.Option(None, "--month", "-m", help="Month to view (YYYY-MM)"),
):
    """View pricing calendar."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.pricing import view_pricing

    async def _view():
        async with get_authenticated_page(get_settings()) as page:
            data = await view_pricing(page, get_settings(), month)
            if data:
                pricing_table(data)
            else:
                print_info("No pricing data found.")

    _run(_view())


@price_app.command("set")
def pricing_set(
    room: str = typer.Option(..., "--room", "-r", help="Room ID"),
    price: float = typer.Option(..., "--price", "-p", help="Price amount"),
    date_str: str = typer.Option(None, "--date", "-d", help="Single date (YYYY-MM-DD)"),
    date_from: str = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    date_to: str = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
):
    """Set price for a room on a date or date range."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.pricing import set_price

    async def _set():
        if date_str:
            d = date.fromisoformat(date_str)
            d_from, d_to = d, d
        elif date_from and date_to:
            d_from = date.fromisoformat(date_from)
            d_to = date.fromisoformat(date_to)
        else:
            print_error("Provide --date or both --from and --to.")
            raise typer.Exit(1)

        async with get_authenticated_page(get_settings()) as page:
            ok = await set_price(page, get_settings(), room, d_from, d_to, price)
            if ok:
                print_success(f"Price set to {price} for room {room}.")
            else:
                print_error("Failed to set price.")

    _run(_set())


# ─────────────────────────── Messages ───────────────────────────


@msg_app.command("list")
def messages_list(
    unread: bool = typer.Option(False, "--unread", "-u", help="Show only unread messages"),
):
    """List guest messages."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import list_messages

    async def _list():
        async with get_authenticated_page(get_settings()) as page:
            data = await list_messages(page, get_settings(), unread_only=unread)
            if data:
                messages_table(data)
            else:
                print_info("No messages found.")

    _run(_list())


@msg_app.command("read")
def messages_read(
    message_id: str = typer.Argument(..., help="Message ID"),
):
    """Read a specific message."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import read_message

    async def _read():
        async with get_authenticated_page(get_settings()) as page:
            detail = await read_message(page, get_settings(), message_id)
            if "error" in detail:
                print_error(detail["error"])
            else:
                console.print(f"\n[bold cyan]From:[/bold cyan] {detail.get('guest_name', 'N/A')}")
                console.print(f"[bold cyan]Subject:[/bold cyan] {detail.get('subject', 'N/A')}")
                console.print(f"\n{detail.get('body', 'No content')}\n")

    _run(_read())


@msg_app.command("reply")
def messages_reply(
    message_id: str = typer.Argument(..., help="Message ID"),
    text: str = typer.Argument(..., help="Reply text"),
):
    """Reply to a message."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import reply_to_message

    async def _reply():
        async with get_authenticated_page(get_settings()) as page:
            ok = await reply_to_message(page, get_settings(), message_id, text)
            if ok:
                print_success("Reply sent.")
            else:
                print_error("Failed to send reply.")

    _run(_reply())


@msg_app.command("learn")
def messages_learn(
    count: int = typer.Option(5, "--count", "-n", help="Number of past messages to scrape"),
):
    """Scrape past conversations to learn your reply style."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import scrape_past_conversations
    from booking_agent.modules.smart_reply import save_past_replies

    async def _learn():
        async with get_authenticated_page(get_settings()) as page:
            console.print(f"[bold cyan][AGENT][/bold cyan] Scraping up to {count} past conversations...")
            conversations = await scrape_past_conversations(page, get_settings(), max_messages=count)
            if conversations:
                save_past_replies(conversations)
                print_success(f"Learned from {len(conversations)} conversations.")
                for conv in conversations:
                    console.print(f"  - {conv['guest_name']}")
            else:
                print_info("No replied conversations found to learn from.")

    _run(_learn())


@msg_app.command("smart-reply")
def messages_smart_reply():
    """Interactive smart reply — draft a personalized response using prokat templates + past replies."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import list_messages, read_message, reply_to_message
    from booking_agent.modules.smart_reply import generate_reply, edit_in_terminal

    async def _smart_reply():
        settings = get_settings()
        async with get_authenticated_page(settings) as page:
            # 0. Auto-learn past replies if no cache exists
            from booking_agent.modules.smart_reply import PAST_REPLIES_CACHE
            if not PAST_REPLIES_CACHE.exists():
                console.print("[bold cyan][AGENT][/bold cyan] First run — learning from past conversations...")
                from booking_agent.modules.messages import scrape_past_conversations
                from booking_agent.modules.smart_reply import save_past_replies
                conversations = await scrape_past_conversations(page, settings, max_messages=5)
                if conversations:
                    save_past_replies(conversations)
                    console.print(f"[bold cyan][AGENT][/bold cyan] Learned from {len(conversations)} past conversations")

            # 1. Fetch messages
            console.print("[bold cyan][AGENT][/bold cyan] Fetching messages...")
            data = await list_messages(page, settings)
            if not data:
                print_info("No messages found.")
                return

            # Print messages as simple list instead of wide table
            console.print()
            for msg in data:
                unread_mark = " [bold red]*[/bold red]" if msg.get("unread") else ""
                console.print(f"  [bold]{msg['id']}[/bold] | {msg['guest_name']} | {msg['date']}{unread_mark}")
                console.print(f"      [dim]{msg['subject'][:80]}[/dim]")
            console.print()

            import sys
            sys.stdout.flush()
            sys.stderr.flush()

            msg_id = input("  Which message to reply to? (enter ID): ")
            msg_id = msg_id.strip()

            if not msg_id.isdigit() or int(msg_id) >= len(data):
                print_error(f"Invalid message ID: {msg_id}")
                return

            # 2. Read the full message
            guest = data[int(msg_id)]
            console.print(f"[bold cyan][AGENT][/bold cyan] Reading message from {guest['guest_name']}...")
            detail = await read_message(page, settings, msg_id)
            if "error" in detail:
                print_error(detail["error"])
                return

            guest_message = detail.get("body", detail.get("subject", ""))
            guest_name = detail.get("guest_name", guest["guest_name"])

            console.print(f"[bold cyan][AGENT][/bold cyan] Guest says:")
            console.print(f"  [dim]{guest_message[:300]}[/dim]")
            console.print()

            # 3. Generate reply using prokat templates + LLM
            console.print("[bold cyan][AGENT][/bold cyan] Drafting reply from prokat templates...")
            try:
                draft = await generate_reply(guest_message, guest_name, hf_token=settings.hf_token)
            except Exception as e:
                print_error(f"Failed to generate reply: {e}")
                return

            # 4. Let user review/edit
            final_text = await edit_in_terminal(draft, guest_name=guest_name, guest_message=guest_message, hf_token=settings.hf_token)
            if not final_text:
                print_info("Reply cancelled.")
                return

            # 5. Send the reply
            confirm = input("  Confirm SEND? This will post the reply on Booking.com (y/n): ")
            if confirm.strip().lower() not in ("y", "yes"):
                print_info("Reply not sent.")
                return

            console.print("[bold cyan][AGENT][/bold cyan] Sending reply...")
            # Message is already open from read_message above — send directly
            ok = await reply_to_message(page, settings, msg_id, final_text)
            # Take screenshot for debug if it failed
            if not ok:
                try:
                    await page.screenshot(path="state/debug_send_failed.png")
                except Exception:
                    pass
            if ok:
                print_success("Reply sent!")
            else:
                print_error("Failed to send reply. Check the browser window.")

    _run(_smart_reply())


# ─────────────────────────── Stats ───────────────────────────


@app.command("stats")
def stats():
    """View property performance statistics."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.performance import get_performance_stats

    async def _stats():
        async with get_authenticated_page(get_settings()) as page:
            data = await get_performance_stats(page, get_settings())
            if data:
                stats_panel(data)
            else:
                print_info("No stats available.")

    _run(_stats())


if __name__ == "__main__":
    app()
