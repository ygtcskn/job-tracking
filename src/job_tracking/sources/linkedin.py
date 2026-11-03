"""LinkedIn, through the public job search that LinkedIn serves to signed-out visitors.

No account or key is used. LinkedIn rate-limits this endpoint, so requests are spaced out
and a 429 ends LinkedIn's part of the run (the next run picks up where this one left off).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Sequence

import httpx
from bs4 import BeautifulSoup, Tag

from ..config import LinkedInConfig
from ..models import Job
from ..normalize import clean, parse_relative, split_location, work_type_from_text
from .base import BROWSER_USER_AGENT, FetchResult, IsKnown, RateLimited, Sleep, SourceError, get

log = logging.getLogger(__name__)

NAME = "LinkedIn"
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_URL = "https://www.linkedin.com/jobs/view/{id}"
PAGE_SIZE = 10

_ID_FROM_URN = re.compile(r"jobPosting:(\d+)")
_ID_FROM_HREF = re.compile(r"/jobs/view/[^?]*?(\d{6,})(?:[/?]|$)")


class LinkedInSource:
    name = NAME

    def __init__(
        self,
        config: LinkedInConfig,
        default_country: str,
        client: httpx.Client,
        sleep: Sleep = time.sleep,
    ):
        self.config = config
        self.default_country = default_country
        self.client = client
        self.sleep = sleep
        self._requests = 0

    def fetch(self, keywords: Sequence[str], now: datetime, is_known: IsKnown) -> FetchResult:
        result = FetchResult()
        seen: set[str] = set()
        for keyword in keywords:
            try:
                for job in self._search(keyword, now, is_known):
                    if job.source_id not in seen:
                        seen.add(job.source_id)
                        result.jobs.append(job)
            except RateLimited:
                if not result.jobs:
                    raise
                result.warnings.append(f"rate limited at keyword {keyword!r}; stopped early")
                break
            except SourceError as exc:
                result.warnings.append(f"{keyword!r}: {exc}")
        if result.warnings and not result.jobs and len(result.warnings) == len(keywords):
            raise SourceError("; ".join(result.warnings))
        return result

    def _search(self, keyword: str, now: datetime, is_known: IsKnown):
        for page in range(self.config.max_pages):
            params = {
                "keywords": keyword,
                "location": self.config.location,
                "f_TPR": f"r{self.config.posted_within_hours * 3600}",
                "sortBy": "DD",  # newest first, so paging can stop at the first all-known page
                "start": page * PAGE_SIZE,
            }
            if self.config.experience_levels:
                params["f_E"] = ",".join(str(x) for x in self.config.experience_levels)
            if self._requests:
                self.sleep(self.config.delay_seconds)
            self._requests += 1
            response = get(
                self.client,
                SEARCH_URL,
                params=params,
                headers={"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                sleep=self.sleep,
            )
            jobs = parse_search_page(response.text, now, self.default_country)
            log.debug("LinkedIn %r page %d: %d cards", keyword, page, len(jobs))
            yield from jobs
            if len(jobs) < PAGE_SIZE or all(is_known(j.source_id) for j in jobs):
                return


def parse_search_page(html: str, now: datetime, default_country: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for card in soup.select(".base-card, .job-search-card"):
        if not isinstance(card, Tag) or card.find_parent(class_="base-card"):
            continue
        job = _parse_card(card, now, default_country)
        if job:
            jobs.append(job)
    return jobs


def _parse_card(card: Tag, now: datetime, default_country: str) -> Job | None:
    job_id = _job_id(card)
    title = _text(card, ".base-search-card__title")
    if not job_id or not title:
        return None
    company = _text(card, ".base-search-card__subtitle")
    raw_location = _text(card, ".job-search-card__location")
    location, country = split_location(raw_location, default_country)

    posted_text, posted_date = "", None
    if (time_tag := card.select_one("time")) is not None:
        posted_text = clean(time_tag.get_text())
        if dt_attr := time_tag.get("datetime"):
            try:
                posted_date = date.fromisoformat(str(dt_attr))
            except ValueError:
                pass
    posted_at = parse_relative(posted_text, now)
    if posted_at is None and posted_date is not None:
        posted_at = datetime.combine(posted_date, datetime.min.time(), tzinfo=now.tzinfo)
    if posted_date is None and posted_at is not None:
        posted_date = posted_at.date()

    return Job(
        source=NAME,
        source_id=job_id,
        title=title,
        company=company,
        location=location,
        country=country,
        work_type=work_type_from_text(title, raw_location),
        posted_date=posted_date,
        posted_text=posted_text,
        posted_at=posted_at,
        url=JOB_URL.format(id=job_id),
    )


def _job_id(card: Tag) -> str | None:
    for el in [card, *card.select("[data-entity-urn]")]:
        if m := _ID_FROM_URN.search(str(el.get("data-entity-urn", ""))):
            return m.group(1)
    link = card if card.name == "a" else card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
    if link is not None and (m := _ID_FROM_HREF.search(str(link.get("href", "")))):
        return m.group(1)
    return None


def _text(card: Tag, selector: str) -> str:
    el = card.select_one(selector)
    return clean(el.get_text(" ")) if el else ""
