"""Write the jobs sheet in the layout of jobs_mockup.xlsx.

Rows are newest run first. Each of today's runs gets its own pastel color (the newest run is
always peach); rows fetched on earlier days are grey.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .config import Config
from .models import StoredJob
from .store import Store

log = logging.getLogger(__name__)

SHEET_TITLE = "Jobs"
LINK_TEXT = "Open job ↗"

# (header, width) in mockup order.
COLUMNS = [
    ("Date", 13),
    ("Time", 9),
    ("Title", 46),
    ("Company", 28),
    ("Location", 20),
    ("Country", 12),
    ("Work Type", 13),
    ("Date posted", 15),
    ("Time posted since", 20),
    ("Source", 17),
    ("Link", 14),
]
TITLE_COL, SOURCE_COL, LINK_COL = 3, 10, 11

RUN_COLORS = ["FBE2D5", "DAF2D0", "DCEAF7", "FFF2CC", "F2CEEF", "C0E6F5"]
OLDER_COLOR = "E8E8E8"
HEADER_COLOR = "1F2A44"

HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10, color="222222")
TITLE_FONT = Font(name="Arial", size=10, bold=True, color="111111")
SOURCE_FONT = Font(name="Arial", size=10, bold=True, color="333333")
LINK_FONT = Font(name="Arial", size=10, color="1155CC", underline="single")
LEFT = Alignment(horizontal="left", vertical="center")
CENTER = Alignment(horizontal="center", vertical="center")
_white = Side(style="thin", color="FFFFFF")
CELL_BORDER = Border(left=_white, right=_white, top=_white, bottom=_white)

DATE_FORMAT = "DD.MM.YYYY"
TIME_FORMAT = "HH:MM"


def sort_rows(rows: list[StoredJob]) -> list[StoredJob]:
    """Newest run first; inside a run, most recently posted first."""

    def posted(r: StoredJob) -> float:
        return r.job.posted_at.timestamp() if r.job.posted_at else float("-inf")

    return sorted(
        rows,
        key=lambda r: (r.run_started_at, r.run_id, posted(r), r.job.title.casefold()),
        reverse=True,
    )


def row_colors(rows: list[StoredJob], tz: ZoneInfo, today: date) -> dict[int, str]:
    """Color per run id. Expects rows already sorted newest run first."""
    colors: dict[int, str] = {}
    todays_runs = 0
    for r in rows:
        if r.run_id in colors:
            continue
        if r.run_started_at.astimezone(tz).date() == today:
            colors[r.run_id] = RUN_COLORS[todays_runs % len(RUN_COLORS)]
            todays_runs += 1
        else:
            colors[r.run_id] = OLDER_COLOR
    return colors


def build_workbook(rows: list[StoredJob], tz: ZoneInfo, today: date) -> Workbook:
    rows = sort_rows(rows)
    colors = row_colors(rows, tz, today)

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE
    ws.sheet_view.showGridLines = False

    header_fill = PatternFill("solid", fgColor=HEADER_COLOR)
    for col, (name, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = HEADER_FONT
        cell.fill = header_fill
        cell.alignment = LEFT
        ws.column_dimensions[cell.column_letter].width = width
    ws.row_dimensions[1].height = 26

    fills = {color: PatternFill("solid", fgColor=color) for color in {*colors.values()}}
    for row_idx, r in enumerate(rows, start=2):
        job = r.job
        fetched = r.run_started_at.astimezone(tz).replace(tzinfo=None)
        values = [
            fetched.date(),
            fetched.time().replace(second=0, microsecond=0),
            job.title,
            job.company,
            job.location,
            job.country,
            job.work_type or None,
            job.posted_date,
            job.posted_text or None,
            job.source,
            LINK_TEXT if job.url else None,
        ]
        fill = fills[colors[r.run_id]]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col, value=value if value != "" else None)
            cell.fill = fill
            cell.border = CELL_BORDER
            cell.font = BODY_FONT
            cell.alignment = LEFT
        ws.cell(row=row_idx, column=1).number_format = DATE_FORMAT
        ws.cell(row=row_idx, column=2).number_format = TIME_FORMAT
        ws.cell(row=row_idx, column=8).number_format = DATE_FORMAT
        ws.cell(row=row_idx, column=TITLE_COL).font = TITLE_FONT
        source_cell = ws.cell(row=row_idx, column=SOURCE_COL)
        source_cell.font = SOURCE_FONT
        source_cell.alignment = CENTER
        if job.url:
            link = ws.cell(row=row_idx, column=LINK_COL)
            link.hyperlink = job.url
            link.font = LINK_FONT
        ws.row_dimensions[row_idx].height = 20

    ws.freeze_panes = "A2"
    last_row = max(len(rows) + 1, 1)
    ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(COLUMNS)).column_letter}{last_row}"
    return wb


def is_open_elsewhere(path: Path) -> bool:
    """True if Excel or LibreOffice has the file open (they leave a lock file beside it)."""
    return (path.parent / f"~${path.name}").exists() or (path.parent / f".~lock.{path.name}#").exists()


def save_atomic(wb: Workbook, path: Path) -> None:
    """Save to a temp file, then swap it in, so a crash never leaves a half-written sheet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".xlsx.tmp", dir=path.parent)
    os.close(fd)
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@dataclass
class PublishResult:
    path: Path | None  # file written, or None if nothing could be written
    rows: int
    pending: bool  # True if new rows went to the pending file because jobs.xlsx was open
    message: str


def publish(store: Store, config: Config, now: datetime) -> PublishResult:
    """Write jobs.xlsx, or jobs_pending.xlsx when jobs.xlsx is open in Excel."""
    out = config.output
    tz = config.timezone
    today = now.astimezone(tz).date()
    # Window starts at local midnight so whole days drop off together.
    start_day = today - timedelta(days=out.max_age_days - 1)
    since = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)

    if not is_open_elsewhere(out.workbook):
        rows = store.jobs_since(since)
        try:
            save_atomic(build_workbook(rows, tz, today), out.workbook)
        except PermissionError as exc:
            log.warning("Could not write %s (%s); using the pending file", out.workbook, exc)
        else:
            store.mark_in_workbook([r.id for r in rows if not r.in_workbook])
            _remove_pending(out.pending_workbook)
            return PublishResult(out.workbook, len(rows), False, f"Wrote {len(rows)} rows to {out.workbook.name}")

    new_rows = store.jobs_since(since, only_new=True)
    if is_open_elsewhere(out.pending_workbook):
        return PublishResult(
            None, 0, True,
            f"{out.workbook.name} and {out.pending_workbook.name} are both open; "
            f"{len(new_rows)} new rows will be written next run",
        )
    if not new_rows:
        _remove_pending(out.pending_workbook)
        return PublishResult(None, 0, True, f"{out.workbook.name} is open; no new rows to add")
    save_atomic(build_workbook(new_rows, tz, today), out.pending_workbook)
    return PublishResult(
        out.pending_workbook, len(new_rows), True,
        f"{out.workbook.name} is open; wrote {len(new_rows)} new rows to {out.pending_workbook.name}",
    )


def _remove_pending(path: Path) -> None:
    if path.exists() and not is_open_elsewhere(path):
        path.unlink()
