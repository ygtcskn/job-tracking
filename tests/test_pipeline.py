from datetime import date, timedelta

import openpyxl
import pytest

from conftest import at
from job_tracking.models import Job
from job_tracking.pipeline import AlreadyRunning, db_path, run, run_lock
from job_tracking.sources import SourceSpec
from job_tracking.sources.base import FetchResult, SourceError
from job_tracking.store import Store


class FakeSource:
    def __init__(self, name: str, jobs=(), error: Exception | None = None):
        self.name = name
        self.jobs = list(jobs)
        self.error = error
        self.calls = 0

    def fetch(self, keywords, now, is_known):
        self.calls += 1
        if self.error:
            raise self.error
        return FetchResult(jobs=list(self.jobs))


def job(source: str, source_id: str, title="Data Analyst", company="Acme GmbH", posted=date(2026, 11, 3)):
    return Job(
        source=source,
        source_id=source_id,
        title=title,
        company=company,
        location="Berlin",
        country="Germany",
        work_type="",
        posted_date=posted,
        posted_text="Today",
        posted_at=None,
        url=f"https://example.com/{source}/{source_id}",
    )


def sheet_titles(path):
    ws = openpyxl.load_workbook(path)["Jobs"]
    return [ws.cell(row=r, column=3).value for r in range(2, ws.max_row + 1)]


def test_run_filters_dedupes_and_writes_sheet(make_config):
    config = make_config()
    linkedin = FakeSource(
        "LinkedIn",
        [
            job("LinkedIn", "1", "Junior Data Analyst (m/w/d)", "Nordwerk Analytics GmbH"),
            job("LinkedIn", "2", "Senior Data Scientist", "Spreequell"),  # excluded word
            job("LinkedIn", "3", "Office Manager", "Spreequell"),  # not a data/ML title
            job("LinkedIn", "4", "Data Analyst", "Old Corp", posted=date(2026, 10, 20)),  # too old
        ],
    )
    agentur = FakeSource(
        "Arbeitsagentur",
        [
            # Same posting as LinkedIn #1, different id and gender tag -> duplicate.
            job("Arbeitsagentur", "A1", "Junior Data Analyst (w/m/d)", "Nordwerk Analytics"),
            job("Arbeitsagentur", "A2", "Datenanalyst (m/w/d)", "Stadtwerke Elbtal"),
        ],
    )
    specs = [SourceSpec("LinkedIn", linkedin), SourceSpec("Arbeitsagentur", agentur)]

    report = run(config, now=at("03.11.2026 19:00"), specs=specs)

    by_name = {s.name: s for s in report.sources}
    assert by_name["LinkedIn"].added == 1
    assert by_name["LinkedIn"].dropped == {"title filter": 2, "posted too long ago": 1}
    assert by_name["Arbeitsagentur"].added == 1
    assert by_name["Arbeitsagentur"].dropped == {"duplicate": 1}
    assert sheet_titles(config.output.workbook) == ["Junior Data Analyst (m/w/d)", "Datenanalyst (m/w/d)"]

    # Next hour: same results again -> nothing new, sheet unchanged.
    report = run(config, now=at("03.11.2026 20:00"), specs=specs)
    assert sum(s.added for s in report.sources) == 0
    assert by_name["LinkedIn"].found == 4
    assert len(sheet_titles(config.output.workbook)) == 2


def test_sources_are_told_about_every_id_already_looked_at(make_config):
    """Filtered-out jobs aren't stored, but still count as known so paging can stop early."""
    config = make_config()
    answers = []

    class Recorder(FakeSource):
        def fetch(self, keywords, now, is_known):
            answers.append((is_known("kept"), is_known("filtered"), is_known("never")))
            return super().fetch(keywords, now, is_known)

    source = Recorder(
        "LinkedIn", [job("LinkedIn", "kept"), job("LinkedIn", "filtered", "Senior Data Analyst")]
    )
    run(config, now=at("03.11.2026 19:00"), specs=[SourceSpec("LinkedIn", source)])
    run(config, now=at("03.11.2026 20:00"), specs=[SourceSpec("LinkedIn", source)])
    assert answers == [(False, False, False), (True, True, False)]


def test_failing_source_does_not_stop_others(make_config):
    config = make_config()
    specs = [
        SourceSpec("LinkedIn", FakeSource("LinkedIn", error=SourceError("HTTP 429"))),
        SourceSpec("Arbeitsagentur", FakeSource("Arbeitsagentur", [job("Arbeitsagentur", "A1")])),
    ]
    report = run(config, now=at("03.11.2026 19:00"), specs=specs)
    statuses = {s.name: s.status for s in report.sources}
    assert statuses == {"LinkedIn": "failed", "Arbeitsagentur": "ok"}
    assert not report.all_failed
    assert sheet_titles(config.output.workbook) == ["Data Analyst"]


def test_unexpected_crash_in_a_source_is_contained(make_config):
    config = make_config()
    specs = [SourceSpec("LinkedIn", FakeSource("LinkedIn", error=KeyError("boom")))]
    report = run(config, now=at("03.11.2026 19:00"), specs=specs)
    assert report.sources[0].status == "failed"
    assert report.all_failed


def test_every_hours_spaces_out_a_source(make_config):
    config = make_config()
    google = FakeSource("Google", [])
    specs = [SourceSpec("Google", google, every_hours=24)]

    run(config, now=at("03.11.2026 09:00"), specs=specs)
    run(config, now=at("03.11.2026 10:00"), specs=specs)
    assert google.calls == 1
    report = run(config, now=at("04.11.2026 09:05"), specs=specs)  # a bit early is fine
    assert google.calls == 2
    assert report.sources[0].status == "ok"

    # Asking for it by name ignores the interval.
    run(config, now=at("04.11.2026 10:00"), specs=specs, only={"Google"})
    assert google.calls == 3


def test_skipped_and_unselected_sources(make_config):
    config = make_config()
    linkedin = FakeSource("LinkedIn", [job("LinkedIn", "1")])
    specs = [SourceSpec("LinkedIn", linkedin), SourceSpec("Google", None, reason="no key")]
    report = run(config, now=at("03.11.2026 19:00"), specs=specs, only={"Google"})
    assert [(s.name, s.status, s.message) for s in report.sources] == [
        ("LinkedIn", "skipped", "not selected"),
        ("Google", "skipped", "no key"),
    ]
    assert linkedin.calls == 0


def test_dry_run_saves_nothing(make_config):
    config = make_config()
    specs = [SourceSpec("LinkedIn", FakeSource("LinkedIn", [job("LinkedIn", "1")]))]
    report = run(config, now=at("03.11.2026 19:00"), specs=specs, dry_run=True)
    assert [j.source_id for j in report.new_jobs] == ["1"]
    assert report.run_id is None
    assert not config.output.workbook.exists()
    with Store(db_path(config)) as store:
        assert store.count_jobs() == 0


def test_runs_cannot_overlap(make_config):
    config = make_config()
    with run_lock(config):
        with pytest.raises(AlreadyRunning):
            run(config, now=at("03.11.2026 19:00"), specs=[])


def test_repo_config_loads():
    from conftest import REPO_ROOT
    from job_tracking.config import load_config

    config = load_config(REPO_ROOT / "config.toml")
    assert config.timezone.key == "Europe/Istanbul"
    assert config.output.workbook == REPO_ROOT / "jobs.xlsx"
    assert "Data Analyst" in config.search.keywords


def test_unknown_config_key_is_an_error(make_config):
    from job_tracking.config import ConfigError

    with pytest.raises(ConfigError, match="typo_setting"):
        make_config("\n[sources.arbeitsagentur]\ntypo_setting = 1\n")
