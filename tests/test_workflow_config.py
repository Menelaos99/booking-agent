from datetime import time
from pathlib import Path

import pytest

from booking_agent.workflow_config import load_workflow_config


def test_load_workflow_config(tmp_path: Path) -> None:
    config_file = tmp_path / "arrivals.yaml"
    config_file.write_text(
        """
database:
  path: state/test.sqlite3
arrivals:
  property_timezone: Europe/Athens
  days_before: 4
  run_after: "09:00"
gmail:
  account: Example@Gmail.com
  sent_search_days: 90
  inbox_search_days: 30
matching:
  auto_match_booking_id: true
  auto_match_exact_email: true
  auto_match_exact_phone: false
  require_review_for_name_date: true
""".strip()
    )

    config = load_workflow_config(config_file)

    assert config.arrivals.run_after == time(hour=9)
    assert config.gmail.account == "example@gmail.com"
    assert config.matching.auto_match_exact_phone is False


def test_rejects_unknown_config_keys(tmp_path: Path) -> None:
    config_file = tmp_path / "arrivals.yaml"
    config_file.write_text("database:\n  path: test.db\n  unexpected: true\n")

    with pytest.raises(ValueError):
        load_workflow_config(config_file)

