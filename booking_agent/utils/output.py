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


def stats_panel(stats: dict) -> None:
    table = Table(title="Performance Stats", show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    for key, value in stats.items():
        table.add_row(key, str(value))
    console.print(table)
