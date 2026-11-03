"""Run the pipeline every hour on the hour.

macOS: a launchd agent. Windows: a Task Scheduler task. Both catch up once after sleep.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config

LABEL = "local.job-tracking"  # macOS launchd label
TASK_NAME = "job-tracking"  # Windows Task Scheduler name


def install(config: Config, *, platform: str | None = None, task_name: str = TASK_NAME) -> str:
    platform = platform or sys.platform
    config.output.log_dir.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        return _mac_install(config)
    if platform == "win32":
        return _windows_install(config, task_name)
    raise RuntimeError(
        "Scheduling is set up for macOS and Windows only. Elsewhere, add an hourly cron job "
        f"that runs: {' '.join(run_command(config))}"
    )


def uninstall(*, platform: str | None = None, task_name: str = TASK_NAME) -> str:
    platform = platform or sys.platform
    if platform == "darwin":
        return _mac_uninstall()
    if platform == "win32":
        return _windows_uninstall(task_name)
    return "Nothing to remove: scheduling is only set up for macOS and Windows."


def status(*, platform: str | None = None, task_name: str = TASK_NAME) -> str:
    platform = platform or sys.platform
    if platform == "darwin":
        return _mac_status()
    if platform == "win32":
        return _windows_status(task_name)
    return "Scheduling is only set up for macOS and Windows."


def run_command(config: Config, python: str = sys.executable) -> list[str]:
    return [python, "-m", "job_tracking", "run", "--config", str(config.path)]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    # errors="replace": schtasks prints in the console's code page, which may not be UTF-8.
    return subprocess.run(args, capture_output=True, text=True, errors="replace")


# --- macOS (launchd) ----------------------------------------------------------


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(config: Config, python: str = sys.executable) -> dict:
    log_file = config.output.log_dir / "launchd.log"
    return {
        "Label": LABEL,
        "ProgramArguments": run_command(config, python),
        "WorkingDirectory": str(config.root),
        # Every hour at minute 0. If the Mac was asleep, launchd runs it once on wake.
        "StartCalendarInterval": {"Minute": 0},
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
        "ProcessType": "Background",
    }


def _mac_install(config: Config) -> str:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{LABEL}"])  # replace an older copy; fails harmlessly if none
    with open(path, "wb") as handle:
        plistlib.dump(build_plist(config), handle)
    result = _run(["launchctl", "bootstrap", domain, str(path)])
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
    return f"Installed {path}. Runs every hour on the hour from {config.root}."


def _mac_uninstall() -> str:
    path = plist_path()
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    if path.exists():
        path.unlink()
        return f"Removed {path}."
    return "No schedule was installed."


def _mac_status() -> str:
    path = plist_path()
    if not path.exists():
        return "Not installed."
    result = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    loaded = "loaded" if result.returncode == 0 else "NOT loaded (run `job-tracking schedule install` again)"
    with open(path, "rb") as handle:
        args = plistlib.load(handle).get("ProgramArguments", [])
    return f"Installed at {path}, {loaded}.\nCommand: {' '.join(args)}"


# --- Windows (Task Scheduler) -------------------------------------------------


def windowless_python(python: str = sys.executable) -> str:
    """pythonw.exe runs without opening a console window every hour."""
    candidate = Path(python).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else python


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def build_task_xml(config: Config, python: str, start: datetime) -> str:
    """Task definition: hourly from `start`, only while you're signed in (no password stored).

    Unlike Task Scheduler's defaults, it also runs on battery and catches up after sleep.
    """
    command, *args = run_command(config, python)
    arguments = " ".join(_quote(a) for a in args)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Job Tracking: fetch new job postings every hour</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start:%Y-%m-%dT%H:%M:%S}</StartBoundary>
      <Repetition>
        <Interval>PT1H</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(config.root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _next_full_hour(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _windows_install(config: Config, task_name: str) -> str:
    xml = build_task_xml(config, windowless_python(), _next_full_hour())
    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        # schtasks wants the file in the encoding the XML header names (UTF-16, with BOM).
        Path(xml_path).write_text(xml, encoding="utf-16")
        result = _run(["schtasks", "/Create", "/TN", task_name, "/XML", xml_path, "/F"])
    finally:
        os.remove(xml_path)
    if result.returncode != 0:
        raise RuntimeError(f"schtasks /Create failed: {result.stderr.strip() or result.stdout.strip()}")
    return (
        f"Installed Task Scheduler task '{task_name}'. Runs every hour on the hour from "
        f"{config.root} while you're signed in. Output goes to logs\\job-tracking.log."
    )


def _windows_uninstall(task_name: str) -> str:
    result = _run(["schtasks", "/Delete", "/TN", task_name, "/F"])
    if result.returncode == 0:
        return f"Removed Task Scheduler task '{task_name}'."
    return "No schedule was installed."


def _windows_status(task_name: str) -> str:
    result = _run(["schtasks", "/Query", "/TN", task_name, "/FO", "LIST"])
    if result.returncode != 0:
        return "Not installed."
    return result.stdout.strip()
