"""Text clean-up shared by all sources: titles, companies, places, work type and posting times."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Iterable

from .models import HYBRID, ON_SITE, REMOTE

# German city names that LinkedIn and Google show in English.
CITY_EXONYMS = {
    "münchen": "Munich",
    "köln": "Cologne",
    "nürnberg": "Nuremberg",
}

COUNTRY_NAMES = {
    "deutschland": "Germany",
    "österreich": "Austria",
    "oesterreich": "Austria",
    "schweiz": "Switzerland",
    "niederlande": "Netherlands",
}

_GENDER_TAG = re.compile(
    r"\(\s*(?:[mwfdxi]\s*[/|,]\s*)+[mwfdxi]\s*\)|\(\s*(?:all genders?|gn\*?|d)\s*\)",
    re.IGNORECASE,
)
_LEGAL_SUFFIXES = {
    "gmbh", "ag", "se", "kg", "co", "mbh", "kgaa", "ug", "ev", "e", "v",
    "inc", "ltd", "llc", "plc", "bv", "nv", "sa", "sarl", "group", "gruppe",
}


def clean(text: str | None) -> str:
    """Collapse whitespace; None becomes ''."""
    return " ".join((text or "").split())


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", text).strip()


def title_key(title: str) -> str:
    return _fold(_GENDER_TAG.sub(" ", title))


def company_key(company: str) -> str:
    words = [w for w in _fold(company).split() if w not in _LEGAL_SUFFIXES]
    return " ".join(words)


def dedupe_key(title: str, company: str) -> str:
    """Key used to spot the same posting on two sources (or reposted under a new id)."""
    return f"{title_key(title)}|{company_key(company)}"


def _word_pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    terms = [t.strip() for t in terms if t.strip()]
    if not terms:
        return None
    alternatives = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


class TitleFilter:
    """Whole-word include / exclude lists for job titles."""

    def __init__(self, include: Iterable[str], exclude: Iterable[str]):
        self._include = _word_pattern(include)
        self._exclude = _word_pattern(exclude)

    def reason_to_drop(self, title: str) -> str | None:
        if self._exclude and (m := self._exclude.search(title)):
            return f"title has excluded word {m.group(0)!r}"
        if self._include and not self._include.search(title):
            return "title matches none of title_include"
        return None


def split_location(raw: str, default_country: str) -> tuple[str, str]:
    """'Munich, Bavaria, Germany' -> ('Munich', 'Germany'). 'Germany' -> ('', 'Germany')."""
    raw = clean(re.sub(r"\((?:remote|hybrid|on-site)\)", "", raw, flags=re.IGNORECASE))
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return "", default_country
    if len(parts) == 1:
        only = parts[0]
        if only.casefold() in {default_country.casefold(), *COUNTRY_NAMES} or only in COUNTRY_NAMES.values():
            return "", country_name(only)
        return city_name(only), default_country
    return city_name(parts[0]), country_name(parts[-1])


def city_name(city: str) -> str:
    city = clean(city)
    return CITY_EXONYMS.get(city.casefold(), city)


def country_name(country: str) -> str:
    country = clean(country)
    return COUNTRY_NAMES.get(country.casefold(), country.title() if country.isupper() else country)


_REMOTE_RE = re.compile(r"(?<!\w)(?:remote|fully remote|100 ?% remote|work from home)(?!\w)", re.IGNORECASE)
_HYBRID_RE = re.compile(r"(?<!\w)hybrid(?!\w)", re.IGNORECASE)
_ONSITE_RE = re.compile(r"(?<!\w)(?:on-?site|vor ort|in office)(?!\w)", re.IGNORECASE)


def work_type_from_text(*texts: str) -> str:
    """Guess Remote / Hybrid / On-site from words in the title or location; '' if none."""
    text = " ".join(t for t in texts if t)
    if _HYBRID_RE.search(text):
        return HYBRID
    if _REMOTE_RE.search(text):
        return REMOTE
    if _ONSITE_RE.search(text):
        return ON_SITE
    return ""


_RELATIVE_RE = re.compile(
    r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month)s?\s+ago", re.IGNORECASE
)
_UNIT = {
    "minute": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
}


def parse_relative(text: str, now: datetime) -> datetime | None:
    """'14 minutes ago' -> now - 14 min. Also understands 'just now', 'today', 'yesterday'."""
    text = clean(text).casefold()
    if not text:
        return None
    if text in {"just now", "now", "moments ago"}:
        return now
    if text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if m := _RELATIVE_RE.search(text):
        return now - int(m.group(1)) * _UNIT[m.group(2).casefold()]
    return None


def days_ago_text(posted: date, today: date) -> str:
    """Wording for sources that only give a date: 'Today', '1 day ago', '3 days ago'."""
    days = (today - posted).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"
