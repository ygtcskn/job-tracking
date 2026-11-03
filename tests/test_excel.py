from datetime import date, datetime, time, timedelta

import openpyxl
import pytest

from conftest import REPO_ROOT, TZ, at
from job_tracking.excel import OLDER_COLOR, RUN_COLORS, build_workbook, publish
from job_tracking.models import Job
from job_tracking.store import Store


def make_job(n: int, source="LinkedIn", posted_at=None, **kw) -> Job:
    defaults = dict(
        source=source,
        source_id=str(n),
        title=f"Data Analyst {n}",
        company=f"Company {n}",
        location="Berlin",
        country="Germany",
        work_type="Hybrid",
        posted_date=date(2026, 11, 3),
        posted_text="1 hour ago",
        posted_at=posted_at,
        url=f"https://www.linkedin.com/jobs/view/{n}",
    )
    defaults.update(kw)
    return Job(**defaults)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "jobs.db") as s:
        yield s


def add_run(store: Store, when: datetime, jobs: list[Job]) -> int:
    run_id = store.start_run(when)
    store.add_jobs(run_id, jobs)
    return run_id


def fill(cell) -> str:
    return cell.fill.fgColor.rgb[-6:]


def test_layout_matches_mockup(store):
    add_run(store, at("03.11.2026 19:00"), [make_job(1, work_type="")])
    wb = build_workbook(store.jobs_since(at("01.11.2026 00:00")), TZ, date(2026, 11, 3))
    ws = wb["Jobs"]
    mockup = openpyxl.load_workbook(REPO_ROOT / "jobs_mockup.xlsx")["Jobs"]

    headers = [c.value for c in ws[1]]
    assert headers == [c.value for c in mockup[1]]
    for col in "ABCDEFGHIJK":
        assert ws.column_dimensions[col].width == mockup.column_dimensions[col].width
        assert ws[f"{col}1"].font.color.rgb == mockup[f"{col}1"].font.color.rgb
        assert fill(ws[f"{col}1"]) == fill(mockup[f"{col}1"])
        assert ws[f"{col}2"].font.b == mockup[f"{col}2"].font.b
        assert ws[f"{col}2"].alignment.horizontal == mockup[f"{col}2"].alignment.horizontal
    assert ws.freeze_panes == mockup.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:K2"
    assert ws.sheet_view.showGridLines is False
    assert ws.row_dimensions[1].height == 26
    assert ws.row_dimensions[2].height == 20

    row = [c.value for c in ws[2]]
    assert row[0] == date(2026, 11, 3)
    assert ws["A2"].number_format == "DD.MM.YYYY"
    assert row[1] == time(19, 0)
    assert ws["B2"].number_format == "HH:MM"
    assert row[2:7] == ["Data Analyst 1", "Company 1", "Berlin", "Germany", None]
    assert row[9] == "LinkedIn"
    assert row[10] == "Open job ↗"
    assert ws["K2"].hyperlink.target == "https://www.linkedin.com/jobs/view/1"
    assert ws["K2"].font.color.rgb == mockup["K2"].font.color.rgb


def test_rows_ordered_and_colored_by_run(store):
    add_run(store, at("02.11.2026 22:00"), [make_job(1)])
    add_run(store, at("03.11.2026 17:00"), [make_job(2)])
    add_run(
        store,
        at("03.11.2026 19:00"),
        [
            make_job(3, posted_at=at("03.11.2026 18:18")),
            make_job(4, posted_at=at("03.11.2026 18:46")),
            make_job(5, source="Arbeitsagentur", posted_at=at("03.11.2026 00:00")),
        ],
    )
    ws = build_workbook(store.jobs_since(at("01.11.2026 00:00")), TZ, date(2026, 11, 3))["Jobs"]

    titles = [ws.cell(row=r, column=3).value for r in range(2, ws.max_row + 1)]
    assert titles == ["Data Analyst 4", "Data Analyst 3", "Data Analyst 5", "Data Analyst 2", "Data Analyst 1"]

    colors = [fill(ws.cell(row=r, column=1)) for r in range(2, ws.max_row + 1)]
    newest, previous, older = RUN_COLORS[0], RUN_COLORS[1], OLDER_COLOR
    assert colors == [newest, newest, newest, previous, older]


def test_publish_writes_workbook_and_marks_rows(make_config, store):
    config = make_config()
    add_run(store, at("03.11.2026 19:00"), [make_job(1), make_job(2)])
    result = publish(store, config, at("03.11.2026 19:01"))

    assert result.path == config.output.workbook
    assert not result.pending
    assert openpyxl.load_workbook(config.output.workbook)["Jobs"].max_row == 3
    assert all(r.in_workbook for r in store.jobs_since(at("01.11.2026 00:00")))


def test_publish_uses_pending_file_while_workbook_is_open(make_config, store):
    config = make_config()
    workbook, pending = config.output.workbook, config.output.pending_workbook

    add_run(store, at("03.11.2026 18:00"), [make_job(1)])
    publish(store, config, at("03.11.2026 18:01"))

    # User opens jobs.xlsx in Excel (it creates a ~$ lock file), then a new run finds a job.
    lock = workbook.parent / f"~${workbook.name}"
    lock.write_text("")
    before = workbook.read_bytes()
    add_run(store, at("03.11.2026 19:00"), [make_job(2)])
    result = publish(store, config, at("03.11.2026 19:01"))

    assert result.pending and result.path == pending
    assert workbook.read_bytes() == before  # untouched while open
    pending_ws = openpyxl.load_workbook(pending)["Jobs"]
    assert [pending_ws.cell(row=r, column=3).value for r in range(2, pending_ws.max_row + 1)] == [
        "Data Analyst 2"
    ]

    # Excel closed: next publish merges everything into jobs.xlsx and removes the pending file.
    lock.unlink()
    result = publish(store, config, at("03.11.2026 20:01"))
    assert result.path == workbook and not result.pending
    assert not pending.exists()
    assert openpyxl.load_workbook(workbook)["Jobs"].max_row == 3


def test_publish_leaves_out_rows_older_than_max_age(make_config, store):
    config = make_config()  # max_age_days = 30
    now = at("03.11.2026 19:00")
    add_run(store, now - timedelta(days=30), [make_job(1)])
    add_run(store, now - timedelta(days=29), [make_job(2)])
    publish(store, config, now)
    ws = openpyxl.load_workbook(config.output.workbook)["Jobs"]
    assert [ws.cell(row=r, column=3).value for r in range(2, ws.max_row + 1)] == ["Data Analyst 2"]
