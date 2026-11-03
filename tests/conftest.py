from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_tracking.config import Config, load_config

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent
TZ = ZoneInfo("Europe/Istanbul")

BASE_CONFIG = """
timezone = "Europe/Istanbul"

[output]
max_age_days = 30

[search]
keywords = ["Data Analyst", "Data Scientist"]
country = "Germany"
title_include = ["data", "datenanalyst", "ai", "ki", "machine learning", "ml"]
title_exclude = ["senior", "lead"]
max_posted_age_days = 3

[sources.linkedin]
delay_seconds = 0

[sources.google]
api_key_env = "TEST_SERPAPI_KEY"
"""


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_SERPAPI_KEY", raising=False)

    def _make(extra: str = "") -> Config:
        path = tmp_path / "config.toml"
        path.write_text(BASE_CONFIG + extra, encoding="utf-8")
        return load_config(path)

    return _make


def at(text: str) -> datetime:
    """'03.11.2026 19:00' in Istanbul time."""
    return datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
