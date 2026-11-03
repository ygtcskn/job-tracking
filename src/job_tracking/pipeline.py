"""One run: ask each source for postings, keep the new relevant ones, rewrite the sheet."""

from __future__ import annotations

import fcntl
import logging
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

import httpx

from .config import Config
from .excel import PublishResult, publish
from .models import Job
from .normalize import TitleFilter, dedupe_key
from .sources import SourceError, SourceSpec, build_sources
from .store import SEEN_DAYS, Store

log = logging.getLogger(__name__)

# A posting with the same title and company as one stored in the last N days is a duplicate.
DEDUPE_DAYS = 30
# Runs fire on the hour but can start a little late; don't let that skip an "every N hours" source.
SCHEDULE_SLACK = timedelta(minutes=10)


class AlreadyRunning(Exception):
    pass


@dataclass
class SourceReport:
    name: str
    status: str  # "ok", "partial", "skipped", "failed"
    found: int = 0
    added: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    message: str = ""


@dataclass
class RunReport:
    started_at: datetime
    run_id: int | None
    sources: list[SourceReport]
    new_jobs: list[Job]
    publish: PublishResult | None = None

    @property
    def all_failed(self) -> bool:
        tried = [s for s in self.sources if s.status != "skipped"]
        return bool(tried) and all(s.status == "failed" for s in tried)


def db_path(config: Config) -> Path:
    return config.output.data_dir / "jobs.db"


@contextmanager
def run_lock(config: Config) -> Iterator[None]:
    """Stop two runs from overlapping (e.g. a slow run still going when the next hour starts)."""
    config.output.data_dir.mkdir(parents=True, exist_ok=True)
    with open(config.output.data_dir / "run.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("Another run is still in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run(
    config: Config,
    *,
    only: set[str] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    client: httpx.Client | None = None,
    specs: list[SourceSpec] | None = None,
) -> RunReport:
    now = (now or datetime.now(config.timezone)).astimezone(config.timezone).replace(second=0, microsecond=0)
    own_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        with run_lock(config), Store(db_path(config)) as store:
            specs = specs if specs is not None else build_sources(config, client)
            run_id = None if dry_run else store.start_run(now)
            report = RunReport(started_at=now, run_id=run_id, sources=[], new_jobs=[])
            title_filter = TitleFilter(config.search.title_include, config.search.title_exclude)
            seen_keys: set[str] = set()

            for spec in specs:
                source_report = _run_source(
                    spec, config, store, now, only, title_filter, seen_keys, run_id, report.new_jobs
                )
                report.sources.append(source_report)
                log.info(
                    "%s: %s, %d found, %d new%s",
                    spec.name,
                    source_report.status,
                    source_report.found,
                    source_report.added,
                    f" ({source_report.message})" if source_report.message else "",
                )

            if run_id is not None:
                store.finish_run(run_id, datetime.now(config.timezone), _summary(report))
                store.forget_seen_before(now - timedelta(days=SEEN_DAYS))
                report.publish = publish(store, config, now)
                log.info(report.publish.message)
            return report
    finally:
        if own_client:
            client.close()


def _run_source(
    spec: SourceSpec,
    config: Config,
    store: Store,
    now: datetime,
    only: set[str] | None,
    title_filter: TitleFilter,
    seen_keys: set[str],
    run_id: int | None,
    new_jobs: list[Job],
) -> SourceReport:
    name = spec.name
    if only is not None and name.casefold() not in {o.casefold() for o in only}:
        return SourceReport(name, "skipped", message="not selected")
    if spec.source is None:
        return SourceReport(name, "skipped", message=spec.reason)
    if spec.every_hours and only is None:
        last = store.last_success(name)
        if last is not None:
            due = last + timedelta(hours=spec.every_hours) - SCHEDULE_SLACK
            if now < due:
                local_due = (due + SCHEDULE_SLACK).astimezone(config.timezone)
                return SourceReport(name, "skipped", message=f"next due {local_due:%d.%m. %H:%M}")

    try:
        result = spec.source.fetch(
            config.search.keywords, now, lambda source_id: store.was_seen(name, source_id)
        )
    except SourceError as exc:
        log.error("%s failed: %s", name, exc)
        return SourceReport(name, "failed", message=str(exc))
    except Exception as exc:  # a bug in one source shouldn't stop the others
        log.exception("%s crashed", name)
        return SourceReport(name, "failed", message=f"{type(exc).__name__}: {exc}")

    report = SourceReport(name, "partial" if result.warnings else "ok", found=len(result.jobs))
    for warning in result.warnings:
        log.warning("%s: %s", name, warning)
    if result.warnings:
        report.message = f"{len(result.warnings)} warning(s), see log"

    oldest_allowed = now.date() - timedelta(days=config.search.max_posted_age_days)
    dedupe_since = now - timedelta(days=DEDUPE_DAYS)
    fresh: list[Job] = []
    for job in result.jobs:
        reason = _drop_reason(job, title_filter, oldest_allowed, store, seen_keys, dedupe_since)
        if reason:
            report.dropped[reason] += 1
            log.debug("Skip %s %s %r: %s", name, job.source_id, job.title, reason)
            continue
        seen_keys.add(dedupe_key(job.title, job.company))
        fresh.append(job)

    if run_id is not None:
        store.add_jobs(run_id, fresh)
        store.mark_seen(name, [j.source_id for j in result.jobs], now)
        store.mark_success(name, now)
    report.added = len(fresh)
    new_jobs.extend(fresh)
    return report


def _drop_reason(
    job: Job,
    title_filter: TitleFilter,
    oldest_allowed,
    store: Store,
    seen_keys: set[str],
    dedupe_since: datetime,
) -> str | None:
    if store.is_known(job.source, job.source_id):
        return "already stored"
    if title_filter.reason_to_drop(job.title):
        return "title filter"
    if job.posted_date and job.posted_date < oldest_allowed:
        return "posted too long ago"
    key = dedupe_key(job.title, job.company)
    if key in seen_keys or store.has_recent_duplicate(key, dedupe_since):
        return "duplicate"
    return None


def _summary(report: RunReport) -> dict:
    return {
        s.name: {
            "status": s.status,
            "found": s.found,
            "added": s.added,
            "dropped": dict(s.dropped),
            "message": s.message,
        }
        for s in report.sources
    }
