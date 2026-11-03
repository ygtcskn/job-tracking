import dataclasses
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from job_tracking import schedule

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def parse_task(xml: str) -> ET.Element:
    # ElementTree refuses a str that declares an encoding, so feed it bytes.
    return ET.fromstring(xml.encode("utf-16"))


def test_task_xml(make_config):
    config = make_config()
    python = r"C:\Users\Me\My Projects\job-tracking\.venv\Scripts\pythonw.exe"
    root = parse_task(schedule.build_task_xml(config, python, datetime(2026, 11, 3, 20, 0)))

    def text(path: str) -> str:
        return root.find(path, NS).text

    assert text("t:Triggers/t:TimeTrigger/t:StartBoundary") == "2026-11-03T20:00:00"
    assert text("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT1H"
    # Task Scheduler defaults would skip runs on battery or while asleep.
    assert text("t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert text("t:Settings/t:StopIfGoingOnBatteries") == "false"
    assert text("t:Settings/t:StartWhenAvailable") == "true"
    assert text("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert text("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"

    assert text("t:Actions/t:Exec/t:Command") == python
    assert text("t:Actions/t:Exec/t:Arguments").startswith("-m job_tracking run --config ")
    assert str(config.path) in text("t:Actions/t:Exec/t:Arguments")
    assert text("t:Actions/t:Exec/t:WorkingDirectory") == str(config.root)


def test_task_xml_quotes_and_escapes_paths(make_config):
    config = make_config()
    weird = Path("C:/Jobs & Stuff/config.toml")
    config = dataclasses.replace(config, path=weird)
    root = parse_task(schedule.build_task_xml(config, "python.exe", datetime(2026, 1, 1)))
    arguments = root.find("t:Actions/t:Exec/t:Arguments", NS).text
    assert arguments.endswith(f'--config "{weird}"')


def test_windows_install_runs_schtasks_with_utf16_xml(make_config, monkeypatch):
    config = make_config()
    calls = []

    def fake_run(args):
        xml_bytes = Path(args[args.index("/XML") + 1]).read_bytes()
        calls.append((args, xml_bytes))
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")

    monkeypatch.setattr(schedule, "_run", fake_run)
    message = schedule.install(config, platform="win32")

    (args, xml_bytes), = calls
    assert args[:3] == ["schtasks", "/Create", "/TN"]
    assert args[3] == "job-tracking"
    assert args[-1] == "/F"
    assert xml_bytes[:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 byte order mark
    parse_task(xml_bytes.decode("utf-16"))
    assert not Path(args[args.index("/XML") + 1]).exists()  # temp file cleaned up
    assert "every hour" in message


def test_windows_install_reports_schtasks_errors(make_config, monkeypatch):
    monkeypatch.setattr(
        schedule, "_run", lambda args: subprocess.CompletedProcess(args, 1, "", "ERROR: Access is denied.")
    )
    with pytest.raises(RuntimeError, match="Access is denied"):
        schedule.install(make_config(), platform="win32")


def test_windows_uninstall_and_status(monkeypatch):
    results = iter([1, 0, 1, 0])
    monkeypatch.setattr(
        schedule, "_run", lambda args: subprocess.CompletedProcess(args, next(results), "TaskName: x", "")
    )
    assert schedule.status(platform="win32") == "Not installed."
    assert "TaskName" in schedule.status(platform="win32")
    assert schedule.uninstall(platform="win32") == "No schedule was installed."
    assert "Removed" in schedule.uninstall(platform="win32")


def test_mac_plist(make_config):
    config = make_config()
    plist = schedule.build_plist(config, python="/venv/bin/python")
    assert plist["ProgramArguments"] == [
        "/venv/bin/python", "-m", "job_tracking", "run", "--config", str(config.path)
    ]
    assert plist["StartCalendarInterval"] == {"Minute": 0}


def test_other_platforms_get_a_cron_hint(make_config):
    with pytest.raises(RuntimeError, match="cron"):
        schedule.install(make_config(), platform="linux")


@pytest.mark.skipif(sys.platform != "win32", reason="needs real Windows Task Scheduler")
def test_real_task_scheduler_accepts_the_task(make_config):
    name = f"job-tracking-test-{uuid.uuid4().hex[:8]}"
    try:
        assert "Installed" in schedule.install(make_config(), task_name=name)
        assert name in schedule.status(task_name=name)
    finally:
        schedule.uninstall(task_name=name)
    assert schedule.status(task_name=name) == "Not installed."
