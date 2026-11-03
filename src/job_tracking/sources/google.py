"""Google Jobs, read through SerpApi (https://serpapi.com/google-jobs-api).

Google has no public jobs API, so this needs a SerpApi key. Each keyword (and each extra
page) uses one SerpApi search from your plan's quota.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence

import httpx

from ..config import GoogleConfig
from ..models import REMOTE, Job
from ..normalize import clean, parse_relative, split_location, work_type_from_text
from .base import FetchResult, IsKnown, SourceError, get

log = logging.getLogger(__name__)

NAME = "Google"
SEARCH_URL = "https://serpapi.com/search.json"


class GoogleJobsSource:
    name = NAME

    def __init__(self, config: GoogleConfig, api_key: str, default_country: str, client: httpx.Client):
        self.config = config
        self.api_key = api_key
        self.default_country = default_country
        self.client = client

    def fetch(self, keywords: Sequence[str], now: datetime, is_known: IsKnown) -> FetchResult:
        result = FetchResult()
        seen: set[str] = set()
        for keyword in keywords:
            try:
                for job in self._search(keyword, now):
                    if job.source_id not in seen:
                        seen.add(job.source_id)
                        result.jobs.append(job)
            except SourceError as exc:
                result.warnings.append(f"{keyword!r}: {exc}")
        if keywords and len(result.warnings) == len(keywords):
            raise SourceError("; ".join(result.warnings))
        return result

    def _search(self, keyword: str, now: datetime):
        params: dict[str, Any] = {
            "engine": "google_jobs",
            "q": keyword,
            "location": self.config.location,
            "hl": "en",
            "api_key": self.api_key,
        }
        for _ in range(self.config.max_pages):
            response = get(self.client, SEARCH_URL, params=params)
            data = response.json()
            if error := data.get("error"):
                # SerpApi reports "no results" as an error too.
                if "hasn't returned any results" in error:
                    return
                raise SourceError(error)
            for item in data.get("jobs_results") or []:
                if job := parse_item(item, now, self.default_country):
                    yield job
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            if not token:
                return
            params["next_page_token"] = token


def parse_item(item: dict[str, Any], now: datetime, default_country: str) -> Job | None:
    job_id = item.get("job_id")
    title = clean(item.get("title"))
    if not job_id or not title:
        return None
    raw_location = clean(item.get("location"))
    location, country = split_location(raw_location, default_country)
    extensions = item.get("detected_extensions") or {}
    posted_text = clean(extensions.get("posted_at"))
    posted_at = parse_relative(posted_text, now)

    work_type = work_type_from_text(title, raw_location, *(item.get("extensions") or []))
    if not work_type and (extensions.get("work_from_home") or raw_location.casefold() == "anywhere"):
        work_type = REMOTE
    if raw_location.casefold() == "anywhere":
        location = ""

    link = item.get("share_link") or next(
        (o.get("link") for o in item.get("apply_options") or [] if o.get("link")), ""
    )
    return Job(
        source=NAME,
        source_id=str(job_id),
        title=title,
        company=clean(item.get("company_name")),
        location=location,
        country=country,
        work_type=work_type,
        posted_date=posted_at.date() if posted_at else None,
        posted_text=posted_text,
        posted_at=posted_at,
        url=link,
    )
