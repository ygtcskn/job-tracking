from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence

import httpx

from ..models import Job

log = logging.getLogger(__name__)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

Sleep = Callable[[float], None]
IsKnown = Callable[[str], bool]


class SourceError(Exception):
    """A source could not be read. The run carries on with the other sources."""


class RateLimited(SourceError):
    pass


@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    # Problems that didn't stop the whole source (e.g. one keyword failed).
    warnings: list[str] = field(default_factory=list)


class Source(Protocol):
    name: str

    def fetch(self, keywords: Sequence[str], now: datetime, is_known: IsKnown) -> FetchResult:
        """Return postings for these keywords. `now` is aware, in the sheet's timezone.

        `is_known(source_id)` says whether a posting is already stored, so a source
        can stop paging early once it only sees old results.
        """
        ...


def get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    sleep: Sleep = time.sleep,
    attempts: int = 3,
) -> httpx.Response:
    """GET with a few retries on timeouts and 5xx. Raises RateLimited on a persistent 429."""
    delay = 5.0
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code < 400:
                return response
            last_error = f"HTTP {response.status_code}"
            if response.status_code == 429:
                if attempt == attempts:
                    raise RateLimited(f"{url}: rate limited (HTTP 429)")
            elif response.status_code < 500:
                raise SourceError(f"{url}: HTTP {response.status_code}")
        if attempt < attempts:
            log.debug("Retrying %s in %.0fs (%s)", url, delay, last_error)
            sleep(delay)
            delay *= 2
    raise SourceError(f"{url}: {last_error}")
