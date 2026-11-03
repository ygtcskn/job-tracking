from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

REMOTE = "Remote"
HYBRID = "Hybrid"
ON_SITE = "On-site"


@dataclass(frozen=True)
class Job:
    """One posting as read from a source, before it is stored."""

    source: str  # display name: "LinkedIn", "Arbeitsagentur", "Google"
    source_id: str  # the source's own id; unique per source
    title: str
    company: str
    location: str
    country: str
    work_type: str  # REMOTE, HYBRID, ON_SITE, or "" when the source doesn't say
    posted_date: date | None
    posted_text: str  # the source's own wording, e.g. "14 minutes ago"
    posted_at: datetime | None  # best estimate of when it was posted (aware), for sorting
    url: str


@dataclass(frozen=True)
class StoredJob:
    """A job as saved in the database, with the run that found it."""

    id: int
    job: Job
    run_id: int
    run_started_at: datetime  # aware, UTC
    in_workbook: bool
