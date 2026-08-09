"""Controlled Prime Agent interface to the Booking.com Extranet tools."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator

AuthMethod = Literal["auto", "pulse", "email", "sms"]
ArrivalTaskStatus = Literal[
    "all",
    "action_required",
    "draft_pending",
    "identity_review",
    "match_review",
]
GmailMatchStatus = Literal["all", "matched", "review_required", "rejected"]
RetryDecision = Literal[
    "complete",
    "poll_status",
    "retry_original_once",
    "authenticate_then_retry",
    "wait_for_user",
    "inspect_before_retry",
    "retry_exhausted",
    "do_not_retry",
]
AuthEventName = Literal[
    "starting",
    "checking",
    "pulse_approval_required",
    "pulse_failed",
    "email_code_requested",
    "email_code_found",
    "email_oauth_required",
    "email_failed",
    "sms_confirmation_required",
    "sms_code_required",
    "verified",
    "error",
]


class AuthStatus(BaseModel):
    event: AuthEventName
    message: str
    details: dict[str, str | int | bool] = Field(default_factory=dict)


class RetryGuidance(BaseModel):
    decision: RetryDecision
    error_code: str | None = None
    attempts: int = 1
    max_attempts: int = 1
    retry_after_seconds: float | None = None
    next_action: str


def _default_failure_guidance() -> RetryGuidance:
    return RetryGuidance(
        decision="do_not_retry",
        error_code="unclassified_failure",
        next_action="Do not repeat the operation; correct the input or report the failure.",
    )


def _complete_guidance(*, attempts: int = 1, max_attempts: int = 1) -> RetryGuidance:
    return RetryGuidance(
        decision="complete",
        attempts=attempts,
        max_attempts=max_attempts,
        next_action="Continue to the next workflow step.",
    )


class CommandResult(BaseModel):
    ok: bool
    operation: str
    output: str = ""
    error: str = ""
    retry: RetryGuidance = Field(default_factory=_default_failure_guidance)


class _AuthRequest(BaseModel):
    method: AuthMethod = "auto"


class _SmsCode(BaseModel):
    code: SecretStr

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"\d{4,8}", value.get_secret_value()):
            raise ValueError("SMS code must contain 4 to 8 digits")
        return value


class _PendingReply(BaseModel):
    pending_id: str
    message_ref: str
    expected_guest: str
    text: SecretStr
    created_at: float


class _ReplyRequest(BaseModel):
    message_ref: str
    expected_guest: str = Field(min_length=1, max_length=200)
    text: SecretStr

    @field_validator("message_ref")
    @classmethod
    def require_stable_reference(cls, value: str) -> str:
        if not value.startswith(("data:", "href:")):
            raise ValueError("A stable data: or href: Booking thread reference is required")
        return value

    @field_validator("text")
    @classmethod
    def validate_reply_text(cls, value: SecretStr) -> SecretStr:
        text = value.get_secret_value().strip()
        if not text or len(text) > 4000:
            raise ValueError("Reply text must contain 1 to 4000 characters")
        return SecretStr(text)


class _BookingIdRequest(BaseModel):
    booking_id: str

    @field_validator("booking_id")
    @classmethod
    def validate_booking_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9-]+", value):
            raise ValueError("booking_id contains unsupported characters")
        return value


class _OptionalDateRequest(BaseModel):
    value: str | None = None

    @field_validator("value")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must use YYYY-MM-DD") from exc
        return value


class _MatchRequest(BaseModel):
    match_id: int = Field(gt=0)


class _NavigationMonthRequest(BaseModel):
    month: str | None = None

    @field_validator("month")
    @classmethod
    def validate_month(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}", value):
            raise ValueError("month must use YYYY-MM format")
        return value


class _NavigationReservationRequest(BaseModel):
    status: Literal["upcoming", "past", "cancelled"] = "upcoming"


@dataclass(frozen=True)
class _RetryPolicy:
    max_attempts: int
    delays_seconds: tuple[float, ...]
    timeout_seconds: float
    safe_to_repeat: bool


@dataclass(frozen=True)
class _CliAttempt:
    returncode: int
    output: str
    error: str
    timed_out: bool = False


@dataclass(frozen=True)
class _NavigationAttempt:
    ok: bool
    payload: dict[str, object] | None = None
    error: str = ""
    timed_out: bool = False
    fatal: bool = False


@dataclass(frozen=True)
class _Failure:
    kind: Literal[
        "booking_auth",
        "gmail_auth",
        "user_action",
        "transient",
        "terminal",
    ]
    error_code: str


_SAFE_RETRY_OPERATIONS = {
    "gmail_status",
    "session_status",
    "list_messages",
    "read_message",
    "prepare_reply",
    "list_reservations",
    "show_reservation",
    "list_unreplied",
    "list_arrivals",
    "list_pending_arrival_tasks",
    "refresh_gmail_matches",
    "list_gmail_matches",
    "preview_gmail_match",
    "identity_status",
    "arrival_dry_run",
    "view_availability",
    "view_pricing",
    "get_stats",
    "open_home",
    "open_reservations",
    "open_messages",
    "open_calendar",
    "current_page",
}
_AMBIGUOUS_WRITE_OPERATIONS = {
    "confirm_reply",
    "prepare_arrival_drafts",
    "review_gmail_match",
}
_LONG_RUNNING_OPERATIONS = {
    "arrival_dry_run",
    "prepare_arrival_drafts",
}
_BROWSER_OPERATIONS = {
    "session_status",
    "list_messages",
    "read_message",
    "prepare_reply",
    "list_reservations",
    "show_reservation",
    "list_unreplied",
    "arrival_dry_run",
    "prepare_arrival_drafts",
    "view_availability",
    "view_pricing",
    "get_stats",
    "open_home",
    "open_reservations",
    "open_messages",
    "open_calendar",
    "current_page",
    "go_back",
}
_IDEMPOTENT_NAVIGATION_OPERATIONS = {
    "open_home",
    "open_reservations",
    "open_messages",
    "open_calendar",
    "current_page",
}


def _retry_policy(operation: str) -> _RetryPolicy:
    if operation in _SAFE_RETRY_OPERATIONS:
        return _RetryPolicy(
            max_attempts=3,
            delays_seconds=(1.0, 3.0),
            timeout_seconds=300.0 if operation in _LONG_RUNNING_OPERATIONS else 120.0,
            safe_to_repeat=True,
        )
    return _RetryPolicy(
        max_attempts=1,
        delays_seconds=(),
        timeout_seconds=600.0 if operation == "connect_gmail" else 300.0,
        safe_to_repeat=False,
    )


def _contains_any(value: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in value for phrase in phrases)


def _classify_failure(
    operation: str,
    *,
    output: str,
    error: str,
    timed_out: bool,
) -> _Failure:
    combined = f"{output}\n{error}".casefold()
    if _contains_any(
        combined,
        (
            "booking_auth_required",
            "saved booking session is missing or expired",
            "booking session needs manual authentication",
            "session is invalid or expired",
            "run `booking login`",
            "run 'booking login'",
        ),
    ):
        return _Failure("booking_auth", "booking_auth_required")
    if _contains_any(
        combined,
        (
            "gmail is not connected",
            "reconnect gmail",
            "gmail authorization expired",
            "gmail draft authorization is not valid",
            "gmail oauth client credentials are missing",
        ),
    ):
        return _Failure("gmail_auth", "gmail_auth_required")
    if _contains_any(
        combined,
        (
            "captcha",
            "waf challenge",
            "navigation_challenge_required",
            "email verification required",
            "timed out waiting for challenge",
            "sms fallback",
            "pulse approval",
            "no approved",
            "no approved template",
        ),
    ):
        return _Failure("user_action", "user_action_required")
    if timed_out and operation in _IDEMPOTENT_NAVIGATION_OPERATIONS:
        return _Failure("transient", "navigation_timeout")
    if (
        timed_out
        and operation in _BROWSER_OPERATIONS
        and operation not in _AMBIGUOUS_WRITE_OPERATIONS
    ):
        return _Failure("user_action", "browser_timeout_requires_inspection")
    if timed_out or _contains_any(
        combined,
        (
            "database is locked",
            "another booking-agent instance is running",
            "connection reset",
            "connection closed",
            "service unavailable",
            "temporarily unavailable",
            "target closed",
            "net::err_",
            "rate limit",
            "too many requests",
            "http 429",
            "http 502",
            "http 503",
            "http 504",
            "navigation_timeout",
            "timed out",
            "timeout",
        ),
    ):
        return _Failure("transient", "command_timeout" if timed_out else "transient_failure")
    return _Failure("terminal", "terminal_failure")


def _failure_guidance(
    operation: str,
    failure: _Failure,
    *,
    attempts: int,
    policy: _RetryPolicy,
) -> RetryGuidance:
    if failure.kind == "booking_auth":
        return RetryGuidance(
            decision="authenticate_then_retry",
            error_code=failure.error_code,
            attempts=attempts,
            max_attempts=attempts,
            next_action=(
                "Call start_auth(), complete the authentication flow, wait for verified, "
                "then retry the original operation once."
            ),
        )
    if failure.kind == "gmail_auth":
        return RetryGuidance(
            decision="wait_for_user",
            error_code=failure.error_code,
            attempts=attempts,
            max_attempts=attempts,
            next_action=(
                "Ask the user to complete connect_gmail(); retry only after Gmail reports connected."
            ),
        )
    if operation == "go_back" and failure.kind in {"transient", "user_action"}:
        return RetryGuidance(
            decision="inspect_before_retry",
            error_code="navigation_state_uncertain",
            attempts=attempts,
            max_attempts=attempts,
            next_action=(
                "Call current_page() to reconcile semantic history before deciding whether "
                "another navigation transition is needed."
            ),
        )
    if failure.kind == "user_action":
        return RetryGuidance(
            decision="wait_for_user",
            error_code=failure.error_code,
            attempts=attempts,
            max_attempts=attempts,
            next_action="Wait for the stated user action; do not repeat the current operation yet.",
        )
    if failure.kind == "transient" and operation in _AMBIGUOUS_WRITE_OPERATIONS:
        return RetryGuidance(
            decision="inspect_before_retry",
            error_code="ambiguous_write_result",
            attempts=attempts,
            max_attempts=attempts,
            next_action=(
                "Do not repeat this write. Inspect Booking, Gmail, or local match state with a "
                "read-only tool, then ask the user before any new attempt."
            ),
        )
    if failure.kind == "transient" and policy.safe_to_repeat:
        return RetryGuidance(
            decision="retry_exhausted",
            error_code="retry_exhausted",
            attempts=attempts,
            max_attempts=policy.max_attempts,
            next_action=(
                "Automatic retries are exhausted. Stop retrying this operation in the current "
                "turn and report the transient failure."
            ),
        )
    return RetryGuidance(
        decision="do_not_retry",
        error_code=failure.error_code,
        attempts=attempts,
        max_attempts=attempts,
        next_action="Do not repeat the operation; correct the input or report the failure.",
    )


_process: asyncio.subprocess.Process | None = None
_event_queue: asyncio.Queue[AuthStatus] | None = None
_last_status: AuthStatus | None = None
_stdout_task: asyncio.Task[None] | None = None
_stderr_task: asyncio.Task[None] | None = None
_pending_replies: dict[str, _PendingReply] = {}
_PENDING_REPLY_TTL_SECONDS = 600

_navigation_process: asyncio.subprocess.Process | None = None
_navigation_stdout_task: asyncio.Task[None] | None = None
_navigation_stderr_task: asyncio.Task[None] | None = None
_navigation_pending: dict[str, asyncio.Future[dict[str, object]]] = {}
_navigation_lock: asyncio.Lock | None = None
_navigation_loop: asyncio.AbstractEventLoop | None = None


def _project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "booking_agent").is_dir() and (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Could not locate the booking-agent project root")


def _kill_navigation_at_exit() -> None:
    process = _navigation_process
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


atexit.register(_kill_navigation_at_exit)


def _clean_environment(*, noninteractive: bool = True) -> dict[str, str]:
    environment = dict(os.environ)
    environment["NO_COLOR"] = "1"
    environment["TERM"] = "dumb"
    if noninteractive:
        environment["BOOKING_AGENT_NONINTERACTIVE"] = "1"
    else:
        environment.pop("BOOKING_AGENT_NONINTERACTIVE", None)
    return environment


def _navigation_call_lock() -> asyncio.Lock:
    global _navigation_lock, _navigation_loop
    global _navigation_process, _navigation_stdout_task, _navigation_stderr_task
    loop = asyncio.get_running_loop()
    if _navigation_loop is not loop:
        _kill_navigation_at_exit()
        _navigation_process = None
        _navigation_stdout_task = None
        _navigation_stderr_task = None
        _navigation_pending.clear()
        _navigation_loop = loop
        _navigation_lock = asyncio.Lock()
    assert _navigation_lock is not None
    return _navigation_lock


async def _pump_navigation_stdout(stream: asyncio.StreamReader) -> None:
    while line := await stream.readline():
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            continue
        future = _navigation_pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(payload)

    for future in list(_navigation_pending.values()):
        if not future.done():
            future.set_exception(RuntimeError("Navigation worker exited"))
    _navigation_pending.clear()


async def _drain_navigation_stderr(stream: asyncio.StreamReader) -> None:
    while await stream.readline():
        pass


async def _clear_navigation_handles() -> None:
    global _navigation_process, _navigation_stdout_task, _navigation_stderr_task
    current = asyncio.current_task()
    tasks = [
        task
        for task in (_navigation_stdout_task, _navigation_stderr_task)
        if task is not None and task is not current
    ]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for future in list(_navigation_pending.values()):
        if not future.done():
            future.cancel()
    _navigation_pending.clear()
    _navigation_process = None
    _navigation_stdout_task = None
    _navigation_stderr_task = None


async def _ensure_navigation_worker() -> None:
    global _navigation_process, _navigation_stdout_task, _navigation_stderr_task
    if _navigation_process and _navigation_process.returncode is None:
        return
    if _navigation_process is not None:
        await _clear_navigation_handles()

    root = _project_root()
    _navigation_process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "python",
        "-m",
        "booking_agent.tools.navigation_worker",
        cwd=root,
        env=_clean_environment(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert _navigation_process.stdout is not None
    assert _navigation_process.stderr is not None
    _navigation_stdout_task = asyncio.create_task(
        _pump_navigation_stdout(_navigation_process.stdout)
    )
    _navigation_stderr_task = asyncio.create_task(
        _drain_navigation_stderr(_navigation_process.stderr)
    )


async def _execute_navigation_once(
    action: str,
    arguments: dict[str, object],
    *,
    timeout_seconds: float,
) -> _NavigationAttempt:
    async with _navigation_call_lock():
        try:
            await _ensure_navigation_worker()
        except OSError:
            return _NavigationAttempt(
                ok=False,
                error="navigation_worker_failed: Navigation worker could not start",
                fatal=True,
            )

        process = _navigation_process
        if process is None or process.stdin is None or process.returncode is not None:
            return _NavigationAttempt(
                ok=False,
                error="navigation_worker_exited: Navigation worker is unavailable",
                fatal=True,
            )

        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        _navigation_pending[request_id] = future
        command = {"request_id": request_id, "action": action, **arguments}
        try:
            process.stdin.write((json.dumps(command) + "\n").encode())
            await process.stdin.drain()
            payload = await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            _navigation_pending.pop(request_id, None)
            return _NavigationAttempt(
                ok=False,
                error="navigation_timeout: Navigation request timed out",
                timed_out=True,
            )
        except (BrokenPipeError, ConnectionError, RuntimeError):
            _navigation_pending.pop(request_id, None)
            return _NavigationAttempt(
                ok=False,
                error="navigation_worker_exited: Navigation worker exited",
                fatal=True,
            )

        if bool(payload.get("ok")):
            return _NavigationAttempt(ok=True, payload=payload)
        error_code = str(payload.get("error_code") or "navigation_failed")
        message = str(payload.get("error") or "Navigation failed")
        return _NavigationAttempt(
            ok=False,
            payload=payload,
            error=f"{error_code}: {message}",
            fatal=bool(payload.get("fatal")),
        )


async def _stop_navigation_worker(*, graceful: bool = True) -> bool:
    async with _navigation_call_lock():
        process = _navigation_process
        was_active = process is not None and process.returncode is None
        if not was_active or process is None:
            if process is not None:
                await _clear_navigation_handles()
            return False

        if graceful and process.stdin is not None:
            request_id = uuid.uuid4().hex
            future: asyncio.Future[dict[str, object]] = (
                asyncio.get_running_loop().create_future()
            )
            _navigation_pending[request_id] = future
            try:
                process.stdin.write(
                    (
                        json.dumps({"request_id": request_id, "action": "close"})
                        + "\n"
                    ).encode()
                )
                await process.stdin.drain()
                await asyncio.wait_for(future, timeout=5)
                await asyncio.wait_for(process.wait(), timeout=10)
            except (TimeoutError, BrokenPipeError, ConnectionError, RuntimeError):
                graceful = False

        if not graceful and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                pass

        await _clear_navigation_handles()
        return was_active


async def _run_navigation(
    operation: str,
    action: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    policy = _retry_policy(operation)
    for attempt_number in range(1, policy.max_attempts + 1):
        attempt = await _execute_navigation_once(
            action,
            arguments or {},
            timeout_seconds=policy.timeout_seconds,
        )
        if attempt.ok and attempt.payload is not None:
            return {
                "ok": True,
                "operation": operation,
                "result": attempt.payload.get("result", {}),
                "navigation": attempt.payload.get("navigation"),
                "retry": _complete_guidance(
                    attempts=attempt_number,
                    max_attempts=policy.max_attempts,
                ).model_dump(),
            }

        failure = _classify_failure(
            operation,
            output="",
            error=attempt.error,
            timed_out=attempt.timed_out,
        )
        if attempt.fatal:
            await _stop_navigation_worker(graceful=False)
        if (
            failure.kind == "transient"
            and policy.safe_to_repeat
            and attempt_number < policy.max_attempts
        ):
            await asyncio.sleep(policy.delays_seconds[attempt_number - 1])
            continue

        return CommandResult(
            ok=False,
            operation=operation,
            error=attempt.error,
            retry=_failure_guidance(
                operation,
                failure,
                attempts=attempt_number,
                policy=policy,
            ),
        ).model_dump()

    raise AssertionError("navigation retry loop exited unexpectedly")


def _auth_retry_guidance(status: AuthStatus) -> RetryGuidance:
    if status.event == "verified":
        return RetryGuidance(
            decision="retry_original_once",
            next_action="Retry the original Booking operation once now that authentication is verified.",
        )
    if status.event in {"starting", "checking", "email_code_requested", "email_code_found"}:
        return RetryGuidance(
            decision="poll_status",
            retry_after_seconds=5,
            next_action="Call auth_status(wait_seconds=30); do not start a second auth process.",
        )
    if status.event == "pulse_approval_required":
        return RetryGuidance(
            decision="wait_for_user",
            next_action="Ask the user to approve in Pulse, then call auth_status(wait_seconds=30).",
        )
    if status.event == "sms_confirmation_required":
        return RetryGuidance(
            decision="wait_for_user",
            next_action="Ask the user before calling confirm_sms(True).",
        )
    if status.event == "sms_code_required":
        return RetryGuidance(
            decision="wait_for_user",
            next_action="Ask for one fresh SMS code and submit it once; never reuse a code.",
        )
    if status.event == "email_oauth_required":
        return RetryGuidance(
            decision="wait_for_user",
            error_code="gmail_auth_required",
            next_action="Complete connect_gmail(), then start a new email authentication flow.",
        )
    if status.event in {"pulse_failed", "email_failed"}:
        return RetryGuidance(
            decision="poll_status",
            retry_after_seconds=2,
            next_action="The active auto-auth flow is trying its next method; poll auth_status once.",
        )
    return RetryGuidance(
        decision="do_not_retry",
        error_code="authentication_failed",
        next_action="Do not restart authentication automatically; report the failure and await direction.",
    )


def _auth_payload(status: AuthStatus) -> dict[str, object]:
    return {
        **status.model_dump(),
        "retry": _auth_retry_guidance(status).model_dump(),
    }


async def _pump_stdout(stream: asyncio.StreamReader) -> None:
    global _last_status
    assert _event_queue is not None
    while line := await stream.readline():
        try:
            status = AuthStatus.model_validate_json(line)
        except ValidationError:
            continue
        _last_status = status
        await _event_queue.put(status)


async def _drain_stderr(stream: asyncio.StreamReader) -> None:
    while await stream.readline():
        pass


async def _wait_for_status(
    *,
    timeout_seconds: float,
    skip: set[str] | None = None,
) -> AuthStatus:
    skip = skip or set()
    if _event_queue is None:
        return AuthStatus(event="error", message="No authentication flow is active")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return _last_status or AuthStatus(
                event="error", message="Authentication status timed out"
            )
        try:
            status = await asyncio.wait_for(_event_queue.get(), timeout=remaining)
        except TimeoutError:
            return _last_status or AuthStatus(
                event="error", message="Authentication status timed out"
            )
        if status.event not in skip:
            return status


async def start_auth(method: AuthMethod = "auto") -> dict[str, object]:
    """Start an authentication flow and return its first actionable status."""

    global _process, _event_queue, _last_status, _stdout_task, _stderr_task
    request = _AuthRequest(method=method)
    await _stop_navigation_worker()
    if _process and _process.returncode is None:
        return _auth_payload(
            _last_status
            or AuthStatus(event="starting", message="Authentication is already running")
        )

    _event_queue = asyncio.Queue()
    _last_status = AuthStatus(event="starting", message="Starting Booking.com authentication")
    root = _project_root()
    _process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "python",
        "-m",
        "booking_agent.auth.worker",
        "--method",
        request.method,
        cwd=root,
        env=_clean_environment(noninteractive=False),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert _process.stdout is not None
    assert _process.stderr is not None
    _stdout_task = asyncio.create_task(_pump_stdout(_process.stdout))
    _stderr_task = asyncio.create_task(_drain_stderr(_process.stderr))
    status = await _wait_for_status(
        timeout_seconds=360,
        skip={"checking", "email_code_requested", "email_code_found", "pulse_failed", "email_failed"},
    )
    return _auth_payload(status)


async def auth_status(wait_seconds: float = 0) -> dict[str, object]:
    """Return current status, optionally waiting for a new event."""

    if wait_seconds <= 0:
        return _auth_payload(
            _last_status
            or AuthStatus(event="error", message="No authentication flow is active")
        )
    status = await _wait_for_status(
        timeout_seconds=min(wait_seconds, 300),
        skip={"checking", "email_code_requested", "email_code_found"},
    )
    return _auth_payload(status)


async def _send_command(payload: dict[str, object]) -> None:
    if not _process or _process.returncode is not None or _process.stdin is None:
        raise RuntimeError("No authentication flow is waiting for input")
    _process.stdin.write((json.dumps(payload) + "\n").encode())
    await _process.stdin.drain()


async def confirm_sms(approved: bool) -> dict[str, object]:
    """Continue or reject SMS fallback after explicit user confirmation."""

    await _send_command({"type": "confirm_sms", "approved": approved})
    status = await _wait_for_status(timeout_seconds=30)
    return _auth_payload(status)


async def submit_sms_code(code: str) -> dict[str, object]:
    """Submit one SMS code to the active authentication flow."""

    validated = _SmsCode(code=code)
    await _send_command(
        {"type": "sms_code", "code": validated.code.get_secret_value()}
    )
    status = await _wait_for_status(
        timeout_seconds=60,
        skip={"checking"},
    )
    return _auth_payload(status)


async def _execute_cli_once(
    arguments: list[str],
    *,
    stdin_text: str | None,
    timeout_seconds: float,
) -> _CliAttempt:
    root = _project_root()
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "booking",
            *arguments,
            cwd=root,
            env=_clean_environment(),
            stdin=(
                asyncio.subprocess.PIPE
                if stdin_text is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return _CliAttempt(127, "", f"Could not start booking CLI: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                input=stdin_text.encode() if stdin_text is not None else None
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            pass
        return _CliAttempt(
            124,
            "",
            f"Booking CLI timed out after {timeout_seconds:.0f} seconds",
            timed_out=True,
        )
    return _CliAttempt(
        int(process.returncode or 0),
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


async def _run_cli(
    operation: str,
    arguments: list[str],
    *,
    stdin_text: str | None = None,
) -> dict[str, object]:
    await _stop_navigation_worker()
    policy = _retry_policy(operation)
    for attempt_number in range(1, policy.max_attempts + 1):
        attempt = await _execute_cli_once(
            arguments,
            stdin_text=stdin_text,
            timeout_seconds=policy.timeout_seconds,
        )
        if attempt.returncode == 0:
            return CommandResult(
                ok=True,
                operation=operation,
                output=attempt.output,
                error=attempt.error,
                retry=_complete_guidance(
                    attempts=attempt_number,
                    max_attempts=policy.max_attempts,
                ),
            ).model_dump()

        failure = _classify_failure(
            operation,
            output=attempt.output,
            error=attempt.error,
            timed_out=attempt.timed_out,
        )
        if (
            failure.kind == "transient"
            and policy.safe_to_repeat
            and attempt_number < policy.max_attempts
        ):
            delay = policy.delays_seconds[attempt_number - 1]
            await asyncio.sleep(delay)
            continue

        guidance = _failure_guidance(
            operation,
            failure,
            attempts=attempt_number,
            policy=policy,
        )
        return CommandResult(
            ok=False,
            operation=operation,
            output=attempt.output,
            error=attempt.error[-4_000:],
            retry=guidance,
        ).model_dump()

    raise AssertionError("retry loop exited unexpectedly")


async def _run_json_cli(
    operation: str,
    arguments: list[str],
) -> dict[str, object]:
    result = await _run_cli(operation, arguments)
    if not result["ok"]:
        return result
    try:
        payload = json.loads(str(result["output"]))
    except json.JSONDecodeError:
        return CommandResult(
            ok=False,
            operation=operation,
            error="Booking workflow did not return valid structured output",
        ).model_dump()
    return {
        "ok": True,
        "operation": operation,
        "result": payload,
        "retry": result["retry"],
    }


async def gmail_status() -> dict[str, object]:
    return await _run_cli("gmail_status", ["auth", "gmail-status"])


async def connect_gmail() -> dict[str, object]:
    return await _run_cli("connect_gmail", ["auth", "gmail-connect"])


async def session_status() -> dict[str, object]:
    return await _run_cli("session_status", ["login", "--check"])


async def open_home() -> dict[str, object]:
    """Open the Extranet home section in the persistent read-only navigator."""

    return await _run_navigation("open_home", "open_home")


async def open_reservations(
    status: Literal["upcoming", "past", "cancelled"] = "upcoming",
) -> dict[str, object]:
    """Open a reservation list in the persistent read-only navigator."""

    request = _NavigationReservationRequest(status=status)
    return await _run_navigation(
        "open_reservations",
        "open_reservations",
        {"status": request.status},
    )


async def open_messages(unread: bool = False) -> dict[str, object]:
    """Open the sensitive messages section without performing authentication."""

    return await _run_navigation(
        "open_messages",
        "open_messages",
        {"unread": bool(unread)},
    )


async def open_calendar(month: str | None = None) -> dict[str, object]:
    """Open the availability calendar in the persistent read-only navigator."""

    request = _NavigationMonthRequest(month=month)
    arguments: dict[str, object] = {}
    if request.month:
        arguments["month"] = request.month
    return await _run_navigation("open_calendar", "open_calendar", arguments)


async def current_page() -> dict[str, object]:
    """Reconcile and re-read the navigator's current allowlisted section."""

    return await _run_navigation("current_page", "current_page")


async def go_back() -> dict[str, object]:
    """Return to the previous semantic destination in navigator history."""

    return await _run_navigation("go_back", "go_back")


async def close_navigation() -> dict[str, object]:
    """Close the persistent navigator and release the Booking browser lock."""

    was_active = await _stop_navigation_worker()
    return {
        "ok": True,
        "operation": "close_navigation",
        "result": {"closed": True, "was_active": was_active},
        "navigation": None,
        "retry": _complete_guidance().model_dump(),
    }


async def list_messages(unread: bool = False) -> dict[str, object]:
    arguments = ["messages", "list", "--json"]
    if unread:
        arguments.append("--unread")
    return await _run_cli("list_messages", arguments)


async def read_message(message_id: str) -> dict[str, object]:
    if not (
        re.fullmatch(r"\d+", message_id)
        or message_id.startswith(("data:", "href:"))
    ):
        raise ValueError("message reference is invalid")
    return await _run_cli("read_message", ["messages", "read", message_id, "--json"])


async def prepare_reply(message_ref: str) -> dict[str, object]:
    """Read a stable Booking thread so the agent can prepare a review-only draft."""

    if not message_ref.startswith(("data:", "href:")):
        raise ValueError("Choose a message with a stable thread_ref from list_messages")
    result = await _run_cli(
        "prepare_reply", ["messages", "read", message_ref, "--json"]
    )
    if not result["ok"]:
        return result
    try:
        detail = json.loads(str(result["output"]))
    except json.JSONDecodeError:
        return CommandResult(
            ok=False,
            operation="prepare_reply",
            error="Booking message detail was not valid structured output",
        ).model_dump()
    if not detail.get("stable_ref"):
        return CommandResult(
            ok=False,
            operation="prepare_reply",
            error="Booking did not expose a stable reference for this thread",
        ).model_dump()
    return {
        "ok": True,
        "operation": "prepare_reply",
        "message": detail,
        "retry": result["retry"],
    }


async def stage_reply(
    message_ref: str,
    expected_guest: str,
    text: str,
) -> dict[str, object]:
    """Stage exact reply text; this does not contact or mutate Booking.com."""

    request = _ReplyRequest(
        message_ref=message_ref,
        expected_guest=expected_guest,
        text=text,
    )
    pending_id = uuid.uuid4().hex
    _pending_replies[pending_id] = _PendingReply(
        pending_id=pending_id,
        message_ref=request.message_ref,
        expected_guest=request.expected_guest.strip(),
        text=request.text,
        created_at=time.monotonic(),
    )
    return {
        "ok": True,
        "operation": "stage_reply",
        "pending_id": pending_id,
        "message_ref": request.message_ref,
        "expected_guest": request.expected_guest.strip(),
        "expires_in_seconds": _PENDING_REPLY_TTL_SECONDS,
        "sent": False,
        "retry": _complete_guidance().model_dump(),
    }


async def confirm_reply(pending_id: str, approved: bool) -> dict[str, object]:
    """Send one staged reply only after the user explicitly approves its exact text."""

    pending = _pending_replies.pop(pending_id, None)
    if pending is None:
        return CommandResult(
            ok=False,
            operation="confirm_reply",
            error="Pending reply was not found or was already consumed",
        ).model_dump()
    if time.monotonic() - pending.created_at > _PENDING_REPLY_TTL_SECONDS:
        return CommandResult(
            ok=False,
            operation="confirm_reply",
            error="Pending reply expired; prepare and review it again",
        ).model_dump()
    if not approved:
        return {
            "ok": True,
            "operation": "confirm_reply",
            "sent": False,
            "cancelled": True,
            "retry": _complete_guidance().model_dump(),
        }
    return await _run_cli(
        "confirm_reply",
        [
            "messages",
            "reply",
            pending.message_ref,
            "--stdin",
            "--yes",
            "--expected-guest",
            pending.expected_guest,
            "--require-stable-ref",
        ],
        stdin_text=pending.text.get_secret_value(),
    )


async def list_reservations(
    status: Literal["upcoming", "past", "cancelled"] = "upcoming",
) -> dict[str, object]:
    return await _run_cli("list_reservations", ["reservations", "list", "--status", status])


async def show_reservation(booking_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z0-9-]+", booking_id):
        raise ValueError("booking_id contains unsupported characters")
    return await _run_cli("show_reservation", ["reservations", "show", booking_id])


async def list_unreplied() -> dict[str, object]:
    return await _run_cli("list_unreplied", ["reservations", "unreplied"])


async def list_arrivals(arrival_date: str | None = None) -> dict[str, object]:
    arguments = ["arrivals", "list", "--json"]
    if arrival_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", arrival_date):
            raise ValueError("arrival_date must use YYYY-MM-DD")
        arguments.extend(["--date", arrival_date])
    return await _run_cli("list_arrivals", arguments)


async def list_pending_arrival_tasks(
    arrival_date: str | None = None,
    status: ArrivalTaskStatus = "action_required",
) -> dict[str, object]:
    """List operational arrival state without email, phone, or identity values."""

    validated_date = _OptionalDateRequest(value=arrival_date).value
    arguments = ["arrivals", "pending", "--status", status, "--json"]
    if validated_date:
        arguments.extend(["--date", validated_date])
    return await _run_json_cli("list_pending_arrival_tasks", arguments)


async def refresh_gmail_matches(booking_id: str) -> dict[str, object]:
    """Refresh Gmail candidates for one stored reservation using fixed exact identifiers."""

    request = _BookingIdRequest(booking_id=booking_id)
    return await _run_json_cli(
        "refresh_gmail_matches",
        ["arrivals", "refresh-matches", request.booking_id, "--json"],
    )


async def list_gmail_matches(
    booking_id: str | None = None,
    status: GmailMatchStatus = "review_required",
) -> dict[str, object]:
    """List stored correlation metadata without Gmail message contents or identifiers."""

    arguments = ["arrivals", "matches", "--status", status, "--json"]
    if booking_id:
        request = _BookingIdRequest(booking_id=booking_id)
        arguments.extend(["--booking-id", request.booking_id])
    return await _run_json_cli("list_gmail_matches", arguments)


async def preview_gmail_match(match_id: int) -> dict[str, object]:
    """Preview one stored candidate with masked addresses and redacted, bounded text."""

    request = _MatchRequest(match_id=match_id)
    return await _run_json_cli(
        "preview_gmail_match",
        ["arrivals", "preview-match", str(request.match_id), "--json"],
    )


async def review_gmail_match(match_id: int, approved: bool) -> dict[str, object]:
    """Accept or reject one pending correlation after the user decides."""

    request = _MatchRequest(match_id=match_id)
    arguments = ["arrivals", "review-match", str(request.match_id), "--json"]
    if not approved:
        arguments.append("--reject")
    return await _run_json_cli("review_gmail_match", arguments)


async def prepare_arrival_drafts(
    reference_date: str | None = None,
) -> dict[str, object]:
    """Create only the two approved-template Gmail drafts; never send email."""

    validated_date = _OptionalDateRequest(value=reference_date).value
    arguments = ["arrivals", "run", "--json"]
    if validated_date:
        arguments.extend(["--date", validated_date])
    return await _run_json_cli("prepare_arrival_drafts", arguments)


async def identity_status(booking_id: str) -> dict[str, object]:
    """Return document workflow status without identifiers, nationality, or OCR text."""

    request = _BookingIdRequest(booking_id=booking_id)
    return await _run_json_cli(
        "identity_status",
        ["identity", "status", request.booking_id, "--json"],
    )


async def arrival_dry_run(reference_date: str | None = None) -> dict[str, object]:
    arguments = ["arrivals", "run", "--dry-run"]
    if reference_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reference_date):
            raise ValueError("reference_date must use YYYY-MM-DD")
        arguments.extend(["--date", reference_date])
    return await _run_cli("arrival_dry_run", arguments)


async def view_availability(month: str | None = None) -> dict[str, object]:
    arguments = ["availability", "view"]
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must use YYYY-MM format")
        arguments.extend(["--month", month])
    return await _run_cli("view_availability", arguments)


async def view_pricing(month: str | None = None) -> dict[str, object]:
    arguments = ["pricing", "view"]
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must use YYYY-MM format")
        arguments.extend(["--month", month])
    return await _run_cli("view_pricing", arguments)


async def get_stats() -> dict[str, object]:
    return await _run_cli("get_stats", ["stats"])


async def run(
    action: Literal["session_status", "gmail_status"] = "session_status",
) -> dict[str, object]:
    """Run a safe status check when the module is invoked as a callable."""

    return await (session_status() if action == "session_status" else gmail_status())
