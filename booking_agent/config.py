from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
SESSION_FILE = STATE_DIR / "session.json"
GMAIL_CREDENTIALS_FILE = STATE_DIR / "credentials.json"
GMAIL_TOKEN_FILE = STATE_DIR / "token.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
    )

    booking_email: str
    booking_password: str
    booking_hotel_id: str = "7455203"
    headless: bool = True
    slow_mo: int = 0
    gmail_otp_enabled: bool = False
    vision_login: bool = False
    hf_token: str = ""

    @property
    def extranet_base(self) -> str:
        return f"https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/home.html?ses=&hotel_id={self.booking_hotel_id}"

    @property
    def sign_in_url(self) -> str:
        return "https://account.booking.com/sign-in"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
