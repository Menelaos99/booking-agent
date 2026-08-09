import asyncio
import json
from pathlib import Path

import booking_agent.browser as browser_module


class FakeContext:
    def __init__(self) -> None:
        self.indexed_db: bool | None = None

    async def storage_state(self, *, indexed_db: bool = False) -> dict:
        self.indexed_db = indexed_db
        return {
            "cookies": [{"name": "session", "value": "sensitive"}],
            "origins": [{"origin": "https://account.booking.com", "localStorage": []}],
        }


def test_save_session_is_atomic_secure_and_includes_indexed_db(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "state"
    session_file = state_dir / "session.json"
    monkeypatch.setattr(browser_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(browser_module, "SESSION_FILE", session_file)
    context = FakeContext()

    asyncio.run(browser_module.save_session(context))

    assert context.indexed_db is True
    assert json.loads(session_file.read_text())["cookies"][0]["name"] == "session"
    assert state_dir.stat().st_mode & 0o777 == 0o700
    assert session_file.stat().st_mode & 0o777 == 0o600
    assert not list(state_dir.glob(".session-*.json"))


def test_prime_noninteractive_mode_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv(browser_module.NONINTERACTIVE_ENV, raising=False)
    assert browser_module.is_noninteractive_mode() is False

    monkeypatch.setenv(browser_module.NONINTERACTIVE_ENV, "true")
    assert browser_module.is_noninteractive_mode() is True
