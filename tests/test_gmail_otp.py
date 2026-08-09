from pathlib import Path

from booking_agent.auth.gmail_otp import (
    _extract_otp,
    _is_booking_sender,
    _secure_write,
)


def test_extract_otp_requires_code_context_and_a_digit() -> None:
    assert _extract_otp("Booking.com – 9RKUQF is your verification code") == "9RKUQF"
    assert _extract_otp("Your security code is: 483921") == "483921"
    assert _extract_otp("BOOKING ACCOUNT SECURITY") is None
    assert _extract_otp("Unrelated token ABCDEF") is None


def test_booking_sender_validation() -> None:
    assert _is_booking_sender("Booking.com <noreply-iam@booking.com>")
    assert _is_booking_sender("security@mail.booking.com")
    assert not _is_booking_sender("attacker@booking.com.example.org")


def test_secure_write_uses_owner_only_permissions(tmp_path: Path) -> None:
    destination = tmp_path / "state" / "token.json"
    _secure_write(destination, '{"token":"secret"}')

    assert destination.read_text().endswith("\n")
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
