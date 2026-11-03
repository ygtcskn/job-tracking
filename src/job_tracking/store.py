"""SQLite store: every job ever seen (for de-duplication) and the runs that found them."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .models import Job, StoredJob
from .normalize import dedupe_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,          -- UTC ISO-8601
    finished_at TEXT,
    summary     TEXT                    -- JSON: per-source counts and errors
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT NOT NULL,
    country     TEXT NOT NULL,
    work_type   TEXT NOT NULL,
    posted_date TEXT,
    posted_text TEXT NOT NULL,
    posted_at   TEXT,
    url         TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    in_workbook INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS jobs_dedupe_key ON jobs (dedupe_key);
CREATE INDEX IF NOT EXISTS jobs_run_id ON jobs (run_id);
CREATE TABLE IF NOT EXISTS source_state (
    source          TEXT PRIMARY KEY,
    last_success_at TEXT NOT NULL       -- UTC ISO-8601
);
-- Every id a source has returned, kept or not, so paging can stop at results already looked at.
CREATE TABLE IF NOT EXISTS seen (
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,        -- UTC ISO-8601
    PRIMARY KEY (source, source_id)
) WITHOUT ROWID;
"""

# How long to remember ids that were looked at but not kept.
SEEN_DAYS = 60


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- runs -------------------------------------------------------------

    def start_run(self, started_at: datetime) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_utc(started_at),))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished_at: datetime, summary: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                (_utc(finished_at), json.dumps(summary, ensure_ascii=False), run_id),
            )

    # --- per-source schedule ----------------------------------------------

    def last_success(self, source: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT last_success_at FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def mark_success(self, source: str, at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_state (source, last_success_at) VALUES (?, ?) "
                "ON CONFLICT (source) DO UPDATE SET last_success_at = excluded.last_success_at",
                (source, _utc(at)),
            )

    # --- ids already looked at ---------------------------------------------

    def was_seen(self, source: str, source_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        return row is not None

    def mark_seen(self, source: str, source_ids: list[str], at: datetime) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen (source, source_id, first_seen_at) VALUES (?, ?, ?)",
                [(source, sid, _utc(at)) for sid in source_ids],
            )

    def forget_seen_before(self, cutoff: datetime) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM seen WHERE first_seen_at < ?", (_utc(cutoff),))

    # --- jobs -------------------------------------------------------------

    def is_known(self, source: str, source_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        return row is not None

    def has_recent_duplicate(self, key: str, since: datetime) -> bool:
        """Same title + company stored by any source in a run that started after `since`."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs JOIN runs ON runs.id = jobs.run_id "
            "WHERE jobs.dedupe_key = ? AND runs.started_at >= ? LIMIT 1",
            (key, _utc(since)),
        ).fetchone()
        return row is not None

    def add_jobs(self, run_id: int, jobs: list[Job]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO jobs (source, source_id, run_id, title, company, location, country, "
                "work_type, posted_date, posted_text, posted_at, url, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        j.source,
                        j.source_id,
                        run_id,
                        j.title,
                        j.company,
                        j.location,
                        j.country,
                        j.work_type,
                        j.posted_date.isoformat() if j.posted_date else None,
                        j.posted_text,
                        j.posted_at.isoformat() if j.posted_at else None,
                        j.url,
                        dedupe_key(j.title, j.company),
                    )
                    for j in jobs
                ],
            )

    def jobs_since(self, since: datetime, *, only_new: bool = False) -> list[StoredJob]:
        """Jobs from runs started after `since`. `only_new` limits to rows not yet in jobs.xlsx."""
        query = (
            "SELECT jobs.*, runs.started_at AS run_started_at FROM jobs "
            "JOIN runs ON runs.id = jobs.run_id WHERE runs.started_at >= ?"
        )
        if only_new:
            query += " AND jobs.in_workbook = 0"
        rows = self.conn.execute(query, (_utc(since),)).fetchall()
        return [_stored_job(r) for r in rows]

    def mark_in_workbook(self, job_ids: list[int]) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE jobs SET in_workbook = 1 WHERE id = ?", [(i,) for i in job_ids]
            )

    def count_jobs(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


def _stored_job(row: sqlite3.Row) -> StoredJob:
    job = Job(
        source=row["source"],
        source_id=row["source_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        country=row["country"],
        work_type=row["work_type"],
        posted_date=date.fromisoformat(row["posted_date"]) if row["posted_date"] else None,
        posted_text=row["posted_text"],
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        url=row["url"],
    )
    return StoredJob(
        id=row["id"],
        job=job,
        run_id=row["run_id"],
        run_started_at=datetime.fromisoformat(row["run_started_at"]),
        in_workbook=bool(row["in_workbook"]),
    )
