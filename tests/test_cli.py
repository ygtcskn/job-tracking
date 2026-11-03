import logging

import openpyxl
import pytest

from job_tracking import cli


@pytest.fixture(autouse=True)
def restore_logging():
    """cli.main adds handlers to the root logger; remove and close them after each test."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in root.handlers[:]:
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def test_render_writes_an_empty_sheet(make_config, capsys):
    config = make_config()
    assert cli.main(["--config", str(config.path), "render"]) == 0
    assert "Wrote 0 rows" in capsys.readouterr().out
    ws = openpyxl.load_workbook(config.output.workbook)["Jobs"]
    assert ws["A1"].value == "Date"


def test_crash_during_run_goes_to_the_log_file(make_config, monkeypatch):
    config = make_config()

    def boom(*args, **kwargs):
        raise RuntimeError("network fell over")

    monkeypatch.setattr(cli, "run", boom)
    assert cli.main(["--config", str(config.path), "run"]) == 1
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_text = (config.output.log_dir / "job-tracking.log").read_text(encoding="utf-8")
    assert "run failed" in log_text
    assert "network fell over" in log_text


def test_runs_without_a_console(make_config, monkeypatch):
    """Task Scheduler starts pythonw.exe, where sys.stdout and sys.stderr are None."""
    config = make_config()
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)
    assert cli.main(["--config", str(config.path), "render"]) == 0
    assert config.output.workbook.exists()
