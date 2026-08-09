"""JSON-lines worker used by the Prime Agent booking skill."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import redirect_stdout
from typing import Literal

from pydantic import BaseModel, SecretStr, ValidationError

from booking_agent.auth.assurance import AuthEvent, AuthMethod, ensure_messages_access
from booking_agent.browser import get_authenticated_page
from booking_agent.config import get_settings
from booking_agent.utils.logging_utils import setup_colored_logging


class WorkerCommand(BaseModel):
    type: Literal["confirm_sms", "sms_code"]
    approved: bool | None = None
    code: SecretStr | None = None


def _write_event(event: AuthEvent) -> None:
    payload: dict[str, object] = {
        "event": event.event,
        "message": event.message,
    }
    if event.details:
        payload["details"] = event.details
    print(json.dumps(payload), file=_PROTOCOL_STDOUT, flush=True)


async def _emit(event: AuthEvent) -> None:
    _write_event(event)


async def _read_command(expected: str) -> str:
    settings = get_settings()
    try:
        line = await asyncio.wait_for(
            asyncio.to_thread(sys.stdin.readline),
            timeout=settings.auth_assurance_timeout_seconds,
        )
    except TimeoutError as exc:
        raise TimeoutError(f"Timed out waiting for {expected}") from exc
    if not line:
        raise RuntimeError("Prime Agent closed the authentication input stream")

    try:
        command = WorkerCommand.model_validate_json(line)
    except ValidationError as exc:
        raise ValueError("Invalid authentication command") from exc

    if command.type != expected:
        raise ValueError(f"Expected {expected}, received {command.type}")
    if expected == "confirm_sms":
        return "yes" if command.approved else "no"
    if command.code is None:
        raise ValueError("SMS code is required")
    return command.code.get_secret_value()


async def run(method: AuthMethod) -> int:
    settings = get_settings()
    try:
        with redirect_stdout(sys.stderr):
            async with get_authenticated_page(settings) as page:
                result = await ensure_messages_access(
                    page,
                    settings,
                    method=method,
                    emit=_emit,
                    read_input=_read_command,
                )
        return 0 if result.verified else 20
    except Exception as exc:
        await _emit(AuthEvent("error", f"Authentication worker failed: {exc}"))
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Booking.com authentication worker")
    parser.add_argument(
        "--method",
        choices=("auto", "pulse", "email", "sms"),
        default="auto",
    )
    arguments = parser.parse_args()
    setup_colored_logging()
    raise SystemExit(asyncio.run(run(arguments.method)))


_PROTOCOL_STDOUT = sys.stdout


if __name__ == "__main__":
    main()
