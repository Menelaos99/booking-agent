from rich.console import Console
from rich.table import Table

console = Console()


def print_success(msg: str) -> None:
    console.print(f"[bold green]{msg}[/bold green]")


def print_error(msg: str) -> None:
    console.print(f"[bold red]{msg}[/bold red]")


def print_warning(msg: str) -> None:
    console.print(f"[bold yellow]{msg}[/bold yellow]")


def print_info(msg: str) -> None:
    console.print(f"[bold cyan]{msg}[/bold cyan]")


def reservations_table(reservations: list[dict]) -> None:
    table = Table(title="Reservations", show_lines=True)
    table.add_column("Booking ID", style="cyan")
    table.add_column("Guest")
    table.add_column("Check-in")
    table.add_column("Check-out")
    table.add_column("Status", style="green")
    table.add_column("Total", justify="right")

    for r in reservations:
        table.add_row(
            r.get("booking_id", ""),
            r.get("guest_name", ""),
            r.get("check_in", ""),
            r.get("check_out", ""),
            r.get("status", ""),
            r.get("total", ""),
        )
    console.print(table)


def messages_table(messages: list[dict]) -> None:
    table = Table(title="Messages", show_lines=True)
    table.add_column("ID", style="cyan")
    table.add_column("Guest")
    table.add_column("Subject")
    table.add_column("Date")
    table.add_column("Unread", style="yellow")

    for m in messages:
        table.add_row(
            m.get("id", ""),
            m.get("guest_name", ""),
            m.get("subject", ""),
            m.get("date", ""),
            "YES" if m.get("unread") else "",
        )
    console.print(table)


def pricing_table(rates: list[dict]) -> None:
    table = Table(title="Pricing", show_lines=True)
    table.add_column("Room", style="cyan")
    table.add_column("Date")
    table.add_column("Price", justify="right", style="green")

    for r in rates:
        table.add_row(
            r.get("room", ""),
            r.get("date", ""),
            r.get("price", ""),
        )
    console.print(table)


def unreplied_table(guests: list[dict], title: str = "Unreplied Guests") -> None:
    table = Table(title=title, show_lines=True)
    table.add_column("#", style="dim")
    table.add_column("Guest", style="bold")
    table.add_column("Booking ID", style="cyan")
    table.add_column("Check-in")
    table.add_column("Check-out")
    table.add_column("Status", style="bold")
    table.add_column("Total", justify="right")

    for i, r in enumerate(guests):
        status = r.get("reply_status", "")
        if status == "needs_reply":
            status_display = "[bold red]NEEDS REPLY[/bold red]"
        elif status == "no_contact":
            status_display = "[yellow]No contact[/yellow]"
        else:
            status_display = status

        table.add_row(
            str(i),
            r.get("guest_name", ""),
            r.get("booking_id", ""),
            r.get("check_in", ""),
            r.get("check_out", ""),
            status_display,
            r.get("total", ""),
        )
    console.print(table)


def arrivals_table(arrivals: list[dict]) -> None:
    table = Table(title="Arrival Operations", show_lines=True)
    table.add_column("Customer", style="bold")
    table.add_column("Email")
    table.add_column("Arrival", style="cyan")
    table.add_column("Checkout")
    table.add_column("Nights", justify="right")
    table.add_column("Guests", justify="right")
    table.add_column("Room")
    table.add_column("Amount", justify="right")
    table.add_column("Booking")
    table.add_column("Instructions")
    table.add_column("Suggestions")
    table.add_column("Identity")
    table.add_column("Match")

    for row in arrivals:
        amount = row.get("amount_raw") or ""
        if not amount and row.get("amount_minor") is not None:
            amount = f"{int(row['amount_minor']) / 100:.2f} {row.get('currency') or ''}".strip()
        match = row.get("customer_match_method", "")
        if row.get("customer_match_review_required"):
            match = "[yellow]REVIEW[/yellow]"
        table.add_row(
            str(row.get("customer_name", "")),
            str(row.get("email", "") or ""),
            str(row.get("check_in", "") or ""),
            str(row.get("check_out", "") or ""),
            str(row.get("nights", "") or ""),
            str(row.get("guest_count", "") or ""),
            str(row.get("room_type", "") or ""),
            str(amount),
            str(row.get("status", "") or ""),
            str(row.get("instructions_status", "") or "pending"),
            str(row.get("recommendations_status", "") or "pending"),
            str(row.get("identity_status", "") or "missing"),
            str(match),
        )
    console.print(table)


def stats_panel(stats: dict) -> None:
    table = Table(title="Performance Stats", show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    for key, value in stats.items():
        table.add_row(key, str(value))
    console.print(table)
