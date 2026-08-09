from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from booking_agent.config import PROJECT_ROOT

DEFAULT_WORKFLOW_CONFIG = PROJECT_ROOT / "config" / "arrivals.yaml"


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("state/booking_agent.sqlite3")

    def resolved_path(self) -> Path:
        return self.path if self.path.is_absolute() else PROJECT_ROOT / self.path


class ArrivalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_timezone: str = "Europe/Athens"
    days_before: int = Field(default=4, ge=0, le=30)
    run_after: time = time(hour=9)

    @field_validator("property_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value


class GmailWorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = "menelaosfot@gmail.com"
    sent_search_days: int = Field(default=365, ge=1, le=3650)
    inbox_search_days: int = Field(default=180, ge=1, le=3650)

    @field_validator("account")
    @classmethod
    def normalize_account(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("gmail account must be an email address")
        return value


class MatchingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_match_booking_id: bool = True
    auto_match_exact_email: bool = True
    auto_match_exact_phone: bool = True
    require_review_for_name_date: bool = True


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: DatabaseConfig = DatabaseConfig()
    arrivals: ArrivalConfig = ArrivalConfig()
    gmail: GmailWorkflowConfig = GmailWorkflowConfig()
    matching: MatchingConfig = MatchingConfig()


def load_workflow_config(path: Path | str | None = None) -> WorkflowConfig:
    config_path = Path(path) if path is not None else DEFAULT_WORKFLOW_CONFIG
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Workflow config not found: {config_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Workflow config must contain a YAML mapping: {config_path}")
    return WorkflowConfig.model_validate(raw)

