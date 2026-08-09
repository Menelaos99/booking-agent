from booking_agent.modules.smart_reply import redact_identity_data


def test_reply_prompt_redacts_identity_and_contact_data() -> None:
    raw = """Passport number AB1234567
Email alice@example.com or call +30 690 123 4567
Reference 123456789"""

    redacted = redact_identity_data(raw)

    assert "AB1234567" not in redacted
    assert "alice@example.com" not in redacted
    assert "690 123" not in redacted
    assert "123456789" not in redacted
