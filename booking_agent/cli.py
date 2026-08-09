from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import typer
from rich.console import Console

from booking_agent.config import get_settings
from booking_agent.utils.output import (
    arrivals_table,
    messages_table,
    print_error,
    print_info,
    print_success,
    print_warning,
    pricing_table,
    reservations_table,
    stats_panel,
    unreplied_table,
)

app = typer.Typer(name="booking", help="Booking.com Extranet Agent")
console = Console()

# --- Sub-apps ---
auth_app = typer.Typer(help="Authentication and Gmail OAuth")
res_app = typer.Typer(help="Reservation management")
avail_app = typer.Typer(help="Availability management")
price_app = typer.Typer(help="Pricing management")
msg_app = typer.Typer(help="Guest messages")
db_app = typer.Typer(help="Local operational database")
arrivals_app = typer.Typer(help="Pre-arrival customer workflow")
email_templates_app = typer.Typer(help="Gmail arrival-email templates")
identity_app = typer.Typer(help="Passport and AFM receipt tracking")

app.add_typer(res_app, name="reservations")
app.add_typer(avail_app, name="availability")
app.add_typer(price_app, name="pricing")
app.add_typer(msg_app, name="messages")
app.add_typer(auth_app, name="auth")
app.add_typer(db_app, name="db")
app.add_typer(arrivals_app, name="arrivals")
app.add_typer(email_templates_app, name="email-templates")
app.add_typer(identity_app, name="identity")


def _run(coro):
    """Run an async coroutine from sync context."""
    return asyncio.run(coro)


def _workflow_database(config_path: Path):
    from booking_agent.storage.database import BookingDatabase
    from booking_agent.workflow_config import load_workflow_config

    workflow = load_workflow_config(config_path)
    database = BookingDatabase(workflow.database.resolved_path())
    database.initialize()
    return workflow, database


DEFAULT_ARRIVAL_CONFIG = Path("config/arrivals.yaml")


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
                if not await is_session_valid(page, get_settings()):
                    print_error("Login completed, but the Extranet session could not be verified.")
                    raise typer.Exit(1)
                await save_session(page.context)

    _run(_login())


@auth_app.command("gmail-connect")
def auth_gmail_connect():
    """Authorize Gmail read access and arrival-draft creation."""
    from booking_agent.auth.gmail_otp import authorize_gmail
    from booking_agent.utils.logging_utils import setup_colored_logging

    setup_colored_logging()
    try:
        status = authorize_gmail(get_settings())
    except Exception as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"Gmail connected: {status.account}")


@auth_app.command("gmail-status")
def auth_gmail_status():
    """Check the configured Gmail OAuth connection."""
    from booking_agent.auth.gmail_otp import gmail_status

    status = gmail_status(get_settings())
    if not status.connected:
        print_error(status.detail)
        raise typer.Exit(1)
    if status.compose_enabled:
        print_success(f"Gmail read and draft access connected: {status.account}")
    else:
        print_warning(status.detail)


@auth_app.command("ensure")
def auth_ensure(
    method: str | None = typer.Option(
        None,
        "--method",
        help="Verification method: auto|pulse|email|sms",
    ),
):
    """Ensure access to sensitive Extranet pages."""
    import aioconsole

    from booking_agent.auth.assurance import AuthEvent, ensure_messages_access
    from booking_agent.browser import get_authenticated_page

    selected_method = method or get_settings().auth_assurance_method
    if selected_method not in {"auto", "pulse", "email", "sms"}:
        print_error("--method must be auto, pulse, email, or sms")
        raise typer.Exit(2)

    async def _ensure():
        async def emit(event: AuthEvent) -> None:
            if event.event == "error" or event.event.endswith("failed"):
                print_error(event.message)
            else:
                print_info(event.message)

        async def read_input(kind: str) -> str:
            if kind == "confirm_sms":
                return await aioconsole.ainput("  Use SMS fallback? (y/n): ")
            return await aioconsole.ainput("  Enter SMS code: ")

        settings = get_settings()
        async with get_authenticated_page(settings) as page:
            result = await ensure_messages_access(
                page,
                settings,
                method=selected_method,
                emit=emit,
                read_input=read_input,
            )
            if not result.verified:
                raise typer.Exit(1)

    _run(_ensure())


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


@res_app.command("unreplied")
def reservations_unreplied():
    """Find guests with future reservations who haven't been replied to."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.unreplied import find_unreplied_guests

    async def _unreplied():
        settings = get_settings()
        async with get_authenticated_page(settings) as page:
            results = await find_unreplied_guests(page, settings)

            needs_reply = [r for r in results if r.get("reply_status") == "needs_reply"]
            no_contact = [r for r in results if r.get("reply_status") == "no_contact"]

            if needs_reply:
                console.print()
                unreplied_table(needs_reply, title="Needs Reply -- Messaged but Unreplied")
            if no_contact:
                console.print()
                unreplied_table(no_contact, title="No Contact -- No Messages Yet")
            if not needs_reply and not no_contact:
                print_success("All future guests have been replied to!")

    _run(_unreplied())


@res_app.command("sync")
def reservations_sync(
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
):
    """Sync upcoming Booking reservations into the local database."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.utils.logging_utils import setup_colored_logging
    from booking_agent.workflows.reservation_sync import sync_reservations

    workflow, database = _workflow_database(config)
    setup_colored_logging()

    async def _sync():
        settings = get_settings()
        async with get_authenticated_page(settings) as page:
            result = await sync_reservations(page, settings, database)
        if result.failed:
            print_warning(
                f"Stored {result.stored}/{result.discovered}; {result.failed} failed and "
                f"{result.review_required} require review."
            )
        else:
            print_success(
                f"Stored {result.stored} reservations; "
                f"{result.review_required} require customer review."
            )

    _run(_sync())


# ───────────────────── Database and arrivals ─────────────────────


@db_app.command("status")
def database_status(
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
):
    """Show the local operational database status."""
    _, database = _workflow_database(config)
    status = database.status()
    console.print(f"Database: {status.path}")
    console.print(f"Schema: {status.schema_version}")
    console.print(f"Customers: {status.customers}")
    console.print(f"Reservations: {status.reservations}")
    console.print(f"Communications: {status.communications}")
    console.print(f"Identity records: {status.identity_records}")


@arrivals_app.command("list")
def arrivals_list(
    arrival_date: str | None = typer.Option(None, "--date", help="Arrival date YYYY-MM-DD"),
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """List correlated reservation and arrival-workflow status."""
    _, database = _workflow_database(config)
    parsed_date = date.fromisoformat(arrival_date) if arrival_date else None
    rows = database.list_arrivals(parsed_date)
    if rows:
        if json_output:
            import json

            typer.echo(json.dumps(rows, ensure_ascii=False))
        else:
            arrivals_table(rows)
    else:
        print_info("No stored arrivals found. Run `booking reservations sync` first.")


async def _execute_arrival_run(
    *,
    workflow,
    database,
    reference_date: date | None,
    dry_run: bool,
):
    from booking_agent.browser import get_browser_page, is_session_valid
    from booking_agent.workflows.arrival import run_arrival_workflow

    settings = get_settings()
    if workflow.gmail.account != settings.gmail_account.strip().lower():
        raise RuntimeError(
            "Workflow Gmail account does not match the configured authenticated account"
        )
    async with get_browser_page(settings, headless=True, restore_session=True) as page:
        if not await is_session_valid(page, settings):
            run_date = reference_date or date.today()
            run_id = database.start_sync("arrival_workflow", run_date=run_date)
            database.finish_sync(run_id, status="blocked", error_code="booking_auth_required")
            raise RuntimeError(
                "Booking session needs manual authentication; run `booking login` first"
            )
        return await run_arrival_workflow(
            page,
            settings,
            database,
            workflow,
            reference_date=reference_date,
            dry_run=dry_run,
        )


def _print_arrival_result(result) -> None:
    action = "would create" if result.dry_run else "created"
    print_success(
        f"Arrival {result.target_date}: synced {result.reservations_synced}, "
        f"due {result.reservations_due}, {action} {result.drafts_created} drafts, "
        f"existing {result.drafts_existing}, blocked {result.blocked}."
    )


@arrivals_app.command("run")
def arrivals_run(
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    reference_date: str | None = typer.Option(None, "--date", help="Pretend today is YYYY-MM-DD"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Read and report without creating Gmail drafts"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """Sync reservations and prepare the two four-days-before Gmail drafts."""
    from booking_agent.utils.logging_utils import setup_colored_logging

    workflow, database = _workflow_database(config)
    setup_colored_logging()
    parsed_date = date.fromisoformat(reference_date) if reference_date else None
    try:
        result = _run(
            _execute_arrival_run(
                workflow=workflow,
                database=database,
                reference_date=parsed_date,
                dry_run=dry_run,
            )
        )
    except Exception as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    if json_output:
        import json

        typer.echo(
            json.dumps(
                {**result.__dict__, "target_date": result.target_date.isoformat()},
                ensure_ascii=False,
            )
        )
    else:
        _print_arrival_result(result)


@arrivals_app.command("scheduled")
def arrivals_scheduled(
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
):
    """Run once per Athens day after the configured time; intended for launchd."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from booking_agent.utils.logging_utils import setup_colored_logging

    workflow, database = _workflow_database(config)
    setup_colored_logging()
    now = datetime.now(ZoneInfo(workflow.arrivals.property_timezone))
    if now.time().replace(tzinfo=None) < workflow.arrivals.run_after:
        print_info("Scheduled arrival run is not due yet.")
        return
    if database.successful_run_exists("arrival_workflow", now.date()):
        print_info("Scheduled arrival run already completed today.")
        return
    try:
        result = _run(
            _execute_arrival_run(
                workflow=workflow,
                database=database,
                reference_date=now.date(),
                dry_run=False,
            )
        )
    except Exception as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    _print_arrival_result(result)


@arrivals_app.command("matches")
def arrivals_matches(
    booking_id: str | None = typer.Option(None, "--booking-id"),
    status: str = typer.Option(
        "review_required",
        "--status",
        help="matched|review_required|rejected|all",
    ),
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """List safe Gmail correlation metadata without message contents."""
    _, database = _workflow_database(config)
    if status not in {"matched", "review_required", "rejected", "all"}:
        print_error("Status must be matched|review_required|rejected|all.")
        raise typer.Exit(2)
    rows = database.list_gmail_matches(
        reservation_id=booking_id,
        status=None if status == "all" else status,
    )
    if json_output:
        import json

        typer.echo(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        print_success("No Gmail matches found for that filter.")
        return
    for row in rows:
        console.print(
            f"{row['id']} | {row['customer_name']} | arrival {row['check_in']} | "
            f"{row['match_method']} ({row['confidence']:.2f}) | {row['status']}"
        )


@arrivals_app.command("pending")
def arrivals_pending(
    arrival_date: str | None = typer.Option(None, "--date", help="Arrival date YYYY-MM-DD"),
    status: str = typer.Option(
        "action_required",
        "--status",
        help="all|action_required|draft_pending|identity_review|match_review",
    ),
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """List agent-safe pending arrival tasks without contact or identity values."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    workflow, database = _workflow_database(config)
    allowed = {
        "all",
        "action_required",
        "draft_pending",
        "identity_review",
        "match_review",
    }
    if status not in allowed:
        print_error(
            "Status must be all|action_required|draft_pending|identity_review|match_review."
        )
        raise typer.Exit(2)
    parsed_date = date.fromisoformat(arrival_date) if arrival_date else None
    rows = database.list_pending_arrival_tasks(
        arrival_date=parsed_date,
        from_date=None
        if parsed_date
        else datetime.now(ZoneInfo(workflow.arrivals.property_timezone)).date(),
        status=status,
    )
    if json_output:
        import json

        typer.echo(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        print_success("No pending arrival tasks found for that filter.")
        return
    for row in rows:
        console.print(
            f"{row['booking_id']} | {row['customer_name']} | {row['arrival_date']} | "
            f"{', '.join(row['action_reasons']) or 'no action'}"
        )


@arrivals_app.command("refresh-matches")
def arrivals_refresh_matches(
    booking_id: str,
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """Refresh Gmail correlations for one stored reservation only."""
    from booking_agent.integrations.gmail import GmailClient
    from booking_agent.workflows.arrival import correlate_gmail

    workflow, database = _workflow_database(config)
    reservation = database.get_reservation(booking_id)
    if reservation is None:
        print_error("Reservation not found in the local database.")
        raise typer.Exit(1)
    try:
        count = correlate_gmail(
            database,
            GmailClient(require_compose=False),
            reservation,
            workflow,
            process_communications=False,
        )
    except Exception as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    result = {
        "booking_id": booking_id,
        "matches_found": count,
        "matches": database.list_gmail_matches(reservation_id=booking_id),
    }
    if json_output:
        import json

        typer.echo(json.dumps(result, ensure_ascii=False))
    else:
        print_success(f"Recorded {count} Gmail match candidate(s) for {booking_id}.")


@arrivals_app.command("preview-match")
def arrivals_preview_match(
    match_id: int,
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """Preview one stored Gmail match with masked addresses and redacted text."""
    from booking_agent.integrations.gmail import (
        GmailClient,
        build_gmail_match_preview,
    )

    _, database = _workflow_database(config)
    match = database.get_gmail_match(match_id)
    if match is None or not match.get("gmail_message_id"):
        print_error("Gmail match was not found or cannot be previewed.")
        raise typer.Exit(1)
    try:
        message = GmailClient(require_compose=False).get_message(
            str(match["gmail_message_id"])
        )
        preview = build_gmail_match_preview(match, message).model_dump()
    except Exception as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    if json_output:
        import json

        typer.echo(json.dumps(preview, ensure_ascii=False))
    else:
        console.print_json(data=preview)


@arrivals_app.command("review-match")
def arrivals_review_match(
    match_id: int,
    reject: bool = typer.Option(False, "--reject", help="Reject instead of accepting"),
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """Accept or reject one proposed Gmail correlation."""
    _, database = _workflow_database(config)
    if not database.review_gmail_match(match_id, accepted=not reject):
        print_error("Pending Gmail match not found.")
        raise typer.Exit(1)
    result = {
        "match_id": match_id,
        "status": "rejected" if reject else "matched",
    }
    if json_output:
        import json

        typer.echo(json.dumps(result))
    else:
        print_success("Gmail match rejected." if reject else "Gmail match accepted.")


@email_templates_app.command("import")
def email_templates_import(
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
):
    """Import and approve instructions and recommendations templates from Gmail Sent."""
    import click

    from booking_agent.integrations.gmail import GmailClient
    from booking_agent.workflows.arrival import render_template

    workflow, database = _workflow_database(config)
    try:
        gmail = GmailClient(require_compose=False)
        candidates = [
            message
            for message in gmail.search(
                f"in:sent newer_than:{workflow.gmail.sent_search_days}d",
                max_results=50,
            )
            if message.direction == "outbound"
        ]
    except Exception as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    if not candidates:
        print_error("No recent sent Gmail messages were found.")
        raise typer.Exit(1)

    console.print("Recent sent messages:")
    for index, message in enumerate(candidates):
        recipient = message.to_addresses[0] if message.to_addresses else "unknown recipient"
        console.print(f"  {index}: {message.occurred_at or ''} | {recipient} | {message.subject}")

    console.print(
        "Supported placeholders: {{ customer_name }}, {{ email }}, {{ arrival_date }}, "
        "{{ checkout_date }}, {{ nights }}, {{ room_type }}, {{ guest_count }}, "
        "{{ booking_id }}, {{ amount }}, {{ currency }}, {{ identity_request }}"
    )
    print_warning(
        "Replace every recipient-specific name, email, phone number, and document value "
        "before approving a template."
    )
    placeholder_values = {
        "customer_name": "",
        "email": "",
        "arrival_date": "",
        "checkout_date": "",
        "nights": "",
        "room_type": "",
        "guest_count": "",
        "booking_id": "",
        "amount": "",
        "currency": "",
        "identity_request": "",
    }
    for kind in ("instructions", "recommendations"):
        selected = typer.prompt(f"Select the {kind} message index", type=int)
        if selected < 0 or selected >= len(candidates):
            print_error("Invalid message index.")
            raise typer.Exit(2)
        source = candidates[selected]
        initial = f"Subject: {source.subject}\n---\n{source.body}"
        edited = click.edit(initial, extension=".txt")
        if edited is None or "\n---\n" not in edited:
            print_error("Template import cancelled or separator removed.")
            raise typer.Exit(1)
        subject_line, body = edited.split("\n---\n", 1)
        subject = subject_line.removeprefix("Subject:").strip()
        if not subject_line.startswith("Subject:") or not subject or not body.strip():
            print_error("Template must contain a non-empty `Subject:` and body.")
            raise typer.Exit(2)
        try:
            render_template(subject, placeholder_values)
            render_template(body, placeholder_values)
        except ValueError as exc:
            print_error(str(exc))
            raise typer.Exit(2) from exc
        if not typer.confirm(f"Approve versioned {kind} template?", default=False):
            print_error("Template import cancelled.")
            raise typer.Exit(1)
        version = database.save_template(
            kind=kind,
            subject_template=subject,
            body_template=body.strip(),
            source_message_id=source.id,
        )
        print_success(f"Saved {kind} template version {version}.")


@identity_app.command("record")
def identity_record(
    booking_id: str,
    kind: str = typer.Option(..., "--type", help="passport|afm"),
    identifier: str = typer.Option(..., "--number"),
    nationality: str | None = typer.Option(None, "--nationality"),
    source: str = typer.Option("whatsapp", "--source", help="gmail|whatsapp"),
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
):
    """Record a manually received passport or AFM for review."""
    from booking_agent.identity import validate_afm

    if kind not in {"passport", "afm"} or source not in {"gmail", "whatsapp"}:
        print_error("Type must be passport|afm and source must be gmail|whatsapp.")
        raise typer.Exit(2)
    normalized_identifier = "".join(identifier.split()).upper()
    if kind == "afm" and not validate_afm(normalized_identifier):
        print_error("AFM is not a valid 9-digit checksum value.")
        raise typer.Exit(2)
    if kind == "passport" and not normalized_identifier.isalnum():
        print_error("Passport number must contain only letters and digits.")
        raise typer.Exit(2)
    _, database = _workflow_database(config)
    if database.get_reservation(booking_id) is None:
        print_error("Reservation not found in the local database.")
        raise typer.Exit(1)
    database.record_identity(
        reservation_id=booking_id,
        kind=kind,
        identifier=normalized_identifier,
        nationality=nationality,
        source_channel=source,
    )
    print_success("Identity information recorded as needs review.")


@identity_app.command("verify")
def identity_verify(
    booking_id: str,
    reject: bool = typer.Option(False, "--reject"),
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
):
    """Accept or reject pending identity information after manual inspection."""
    _, database = _workflow_database(config)
    if not database.verify_identity(booking_id, accepted=not reject):
        print_error("No pending identity record was found for that reservation.")
        raise typer.Exit(1)
    print_success("Identity record rejected." if reject else "Identity record verified.")


@identity_app.command("status")
def identity_status(
    booking_id: str,
    config: Path = typer.Option(DEFAULT_ARRIVAL_CONFIG, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """Show identity workflow status without returning document values."""
    _, database = _workflow_database(config)
    result = database.safe_identity_status(booking_id)
    if result is None:
        print_error("Reservation not found in the local database.")
        raise typer.Exit(1)
    if json_output:
        import json

        typer.echo(json.dumps(result, ensure_ascii=False))
    else:
        console.print_json(data=result)


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
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """List guest messages."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import list_messages

    async def _list():
        async with get_authenticated_page(get_settings()) as page:
            data = await list_messages(page, get_settings(), unread_only=unread)
            if data:
                if json_output:
                    import json

                    typer.echo(json.dumps(data, ensure_ascii=False))
                else:
                    messages_table(data)
            else:
                print_info("No messages found.")

    _run(_list())


@msg_app.command("read")
def messages_read(
    message_id: str = typer.Argument(..., help="Message ID"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON"),
):
    """Read a specific message."""
    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import read_message

    async def _read():
        async with get_authenticated_page(get_settings()) as page:
            detail = await read_message(page, get_settings(), message_id)
            if "error" in detail:
                print_error(detail["error"])
                raise typer.Exit(1)
            elif json_output:
                import json

                typer.echo(json.dumps(detail, ensure_ascii=False))
            else:
                console.print(f"\n[bold cyan]From:[/bold cyan] {detail.get('guest_name', 'N/A')}")
                console.print(f"[bold cyan]Subject:[/bold cyan] {detail.get('subject', 'N/A')}")
                console.print(f"\n{detail.get('body', 'No content')}\n")

    _run(_read())


@msg_app.command("reply")
def messages_reply(
    message_id: str = typer.Argument(..., help="Message ID"),
    text: str | None = typer.Argument(None, help="Reply text"),
    read_stdin: bool = typer.Option(False, "--stdin", help="Read exact reply text from stdin"),
    yes: bool = typer.Option(False, "--yes", help="Confirmation already obtained by a trusted caller"),
    expected_guest: str | None = typer.Option(None, "--expected-guest"),
    require_stable_ref: bool = typer.Option(False, "--require-stable-ref"),
):
    """Open, verify, preview, and reply to one Booking message."""
    import sys

    from booking_agent.browser import get_authenticated_page
    from booking_agent.modules.messages import read_message, reply_to_message

    reply_text = sys.stdin.read() if read_stdin else text
    if not reply_text or not reply_text.strip():
        print_error("Reply text is required as an argument or through --stdin.")
        raise typer.Exit(2)
    reply_text = reply_text.strip()

    async def _reply():
        async with get_authenticated_page(get_settings()) as page:
            detail = await read_message(page, get_settings(), message_id)
            if "error" in detail:
                print_error(detail["error"])
                raise typer.Exit(1)
            guest_name = str(detail.get("guest_name", "")).strip()
            if expected_guest and guest_name.casefold() != expected_guest.strip().casefold():
                print_error("Target guest does not match the confirmed recipient.")
                raise typer.Exit(1)
            if require_stable_ref and not detail.get("stable_ref"):
                print_error("A stable Booking thread reference is required.")
                raise typer.Exit(1)
            console.print(f"[bold]Recipient:[/bold] {guest_name}")
            console.print("[bold]Exact reply:[/bold]")
            console.print(reply_text)
            if not yes and not typer.confirm("Post this exact reply to Booking.com?", default=False):
                print_info("Reply not sent.")
                return
            ok = await reply_to_message(
                page,
                get_settings(),
                str(detail.get("thread_ref") or message_id),
                reply_text,
                expected_guest=guest_name,
                require_stable_ref=require_stable_ref,
            )
            if ok:
                print_success("Reply sent.")
            else:
                print_error("Failed to send reply.")
                raise typer.Exit(1)

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
                draft = await generate_reply(guest_message, guest_name)
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
            ok = await reply_to_message(
                page,
                settings,
                str(detail.get("thread_ref") or msg_id),
                final_text,
                expected_guest=guest_name,
            )
            # Take screenshot for debug if it failed
            if not ok:
                try:
                    await page.screenshot(path="state/debug_send_failed.png")
                except Exception:
                    pass
            if ok:
                print_success("Reply sent!")
                # Auto-save the conversation to past_replies.json
                from booking_agent.modules.smart_reply import save_past_replies
                save_past_replies([{
                    "guest_name": guest_name,
                    "conversation": f"{guest_name}:\n{guest_message}\n\nYour reply:\n{final_text}",
                }])
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
