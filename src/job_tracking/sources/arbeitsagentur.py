"""Bundesagentur für Arbeit job search (the API behind arbeitsagentur.de/jobsuche).

Uses the public client key the official website and app use. The older /pc/v4/ endpoint
now answers 403; /pc/v6/ is current.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Sequence

import httpx

from ..config import ArbeitsagenturConfig
from ..models import HYBRID, ON_SITE, REMOTE, Job
from ..normalize import city_name, clean, country_name, days_ago_text, work_type_from_text
from .base import FetchResult, IsKnown, SourceError, get

log = logging.getLogger(__name__)

NAME = "Arbeitsagentur"
SEARCH_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
JOB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{id}"
API_KEY = "jobboerse-jobsuche"


class ArbeitsagenturSource:
    name = NAME

    def __init__(self, config: ArbeitsagenturConfig, default_country: str, client: httpx.Client):
        self.config = config
        self.default_country = default_country
        self.client = client

    def fetch(self, keywords: Sequence[str], now: datetime, is_known: IsKnown) -> FetchResult:
        result = FetchResult()
        seen: set[str] = set()
        searches = [(kw, offer) for kw in keywords for offer in self.config.offer_types]
        for keyword, offer_type in searches:
            try:
                for job in self._search(keyword, offer_type, now):
                    if job.source_id not in seen:
                        seen.add(job.source_id)
                        result.jobs.append(job)
            except SourceError as exc:
                result.warnings.append(f"{keyword!r} (type {offer_type}): {exc}")
        if searches and len(result.warnings) == len(searches):
            raise SourceError("; ".join(result.warnings))
        return result

    def _search(self, keyword: str, offer_type: int, now: datetime):
        for page in range(1, self.config.max_pages + 1):
            params = {
                "was": keyword,
                "wo": self.config.where,
                "angebotsart": offer_type,
                "veroeffentlichtseit": self.config.published_within_days,
                "size": self.config.page_size,
                "page": page,
            }
            response = get(self.client, SEARCH_URL, params=params, headers={"X-API-Key": API_KEY})
            try:
                data = response.json()
            except ValueError as exc:
                raise SourceError(f"not JSON: {exc}") from exc
            items = data.get("ergebnisliste") or []
            for item in items:
                if job := parse_item(item, now, self.default_country):
                    yield job
            total = int(data.get("maxErgebnisse") or 0)
            if not items or page * self.config.page_size >= total:
                return


def parse_item(item: dict[str, Any], now: datetime, default_country: str) -> Job | None:
    ref = item.get("referenznummer")
    title = clean(item.get("stellenangebotsTitel") or item.get("hauptberuf"))
    if not ref or not title:
        return None

    address = {}
    if places := item.get("stellenlokationen"):
        address = (places[0] or {}).get("adresse") or {}
    city = clean(address.get("ort")) or clean(str(address.get("region") or "").replace("_", " ").title())
    country = country_name(address.get("land") or "") or default_country

    posted_date = _date(
        (item.get("veroeffentlichungszeitraum") or {}).get("von")
        or item.get("datumErsteVeroeffentlichung")
    )
    posted_at = (
        datetime.combine(posted_date, datetime.min.time(), tzinfo=now.tzinfo) if posted_date else None
    )

    return Job(
        source=NAME,
        source_id=str(ref),
        title=title,
        company=clean(item.get("firma")),
        location=city_name(city),
        country=country,
        work_type=_work_type(item, title),
        posted_date=posted_date,
        posted_text=days_ago_text(posted_date, now.date()) if posted_date else "",
        posted_at=posted_at,
        url=JOB_URL.format(id=ref),
    )


def _work_type(item: dict[str, Any], title: str) -> str:
    from_title = work_type_from_text(title)
    if from_title:
        return from_title
    home_office = item.get("homeofficemoeglich")
    kind = str(item.get("homeofficetyp") or "").upper()
    if home_office is True:
        if "AUSSCHLIESS" in kind or "VOLLSTAEND" in kind or "VOLLSTÄND" in kind:
            return REMOTE
        return HYBRID
    if home_office is False:
        return ON_SITE
    return ""


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
