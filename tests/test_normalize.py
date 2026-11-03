from datetime import date, timedelta

import pytest

from conftest import at
from job_tracking.normalize import (
    TitleFilter,
    days_ago_text,
    dedupe_key,
    parse_relative,
    split_location,
    work_type_from_text,
)


@pytest.mark.parametrize(
    "title, dropped",
    [
        ("Junior Data Analyst (m/w/d)", False),
        ("KI-Ingenieur (m/w/d) Berufseinsteiger", False),
        ("Datenanalyst (m/w/d)", False),
        ("Senior Data Scientist", True),
        ("Sr. Data Analyst", True),
        ("Lead ML Engineer", True),
        ("Trainee Sales", True),  # "ai" inside "Trainee" must not count as a match
        ("Kita-Leitung", True),
    ],
)
def test_title_filter(title, dropped):
    f = TitleFilter(
        include=["data", "datenanalyst", "ai", "ki", "ml"],
        exclude=["senior", "sr", "lead", "leitung"],
    )
    assert (f.reason_to_drop(title) is not None) is dropped


def test_empty_include_keeps_everything():
    assert TitleFilter(include=[], exclude=[]).reason_to_drop("Anything at all") is None


def test_dedupe_key_ignores_gender_tags_and_legal_form():
    assert dedupe_key("Junior Data Analyst (m/w/d)", "Nordwerk Analytics GmbH") == dedupe_key(
        "Junior Data Analyst (w/m/d)", "Nordwerk Analytics"
    )
    assert dedupe_key("Data Analyst", "A") != dedupe_key("Data Scientist", "A")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Berlin, Berlin, Germany", ("Berlin", "Germany")),
        ("Munich, Bavaria, Germany", ("Munich", "Germany")),
        ("München, Deutschland", ("Munich", "Germany")),
        ("Germany", ("", "Germany")),
        ("Berlin Metropolitan Area", ("Berlin Metropolitan Area", "Germany")),
        ("Vienna, Austria", ("Vienna", "Austria")),
        ("", ("", "Germany")),
    ],
)
def test_split_location(raw, expected):
    assert split_location(raw, "Germany") == expected


def test_work_type_from_text():
    assert work_type_from_text("ML Engineer (Hybrid)") == "Hybrid"
    assert work_type_from_text("Data Analyst", "Germany (Remote)") == "Remote"
    assert work_type_from_text("Data Analyst – on-site") == "On-site"
    assert work_type_from_text("Data Analyst") == ""


def test_parse_relative():
    now = at("03.11.2026 19:00")
    assert parse_relative("14 minutes ago", now) == now - timedelta(minutes=14)
    assert parse_relative("1 hour ago", now) == now - timedelta(hours=1)
    assert parse_relative("2 days ago", now) == now - timedelta(days=2)
    assert parse_relative("30+ days ago", now) == now - timedelta(days=30)
    assert parse_relative("Today", now) == at("03.11.2026 00:00")
    assert parse_relative("whenever", now) is None


def test_days_ago_text():
    today = date(2026, 11, 3)
    assert days_ago_text(today, today) == "Today"
    assert days_ago_text(date(2026, 11, 2), today) == "1 day ago"
    assert days_ago_text(date(2026, 10, 31), today) == "3 days ago"
