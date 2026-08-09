from booking_agent.config import Settings


def test_deepseek_api_key_is_accepted_and_redacted() -> None:
    settings = Settings(
        _env_file=None,
        booking_email="test@example.com",
        booking_password="test-password",
        deepseek_api_key="test-deepseek-key",
    )

    assert settings.deepseek_api_key.get_secret_value() == "test-deepseek-key"
    assert "test-deepseek-key" not in repr(settings)
    assert settings.gmail_account == "menelaosfot@gmail.com"
    assert settings.auth_assurance_method == "auto"
    assert settings.auth_assurance_timeout_seconds == 300
    assert settings.email_otp_timeout_seconds == 60
