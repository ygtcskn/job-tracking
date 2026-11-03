"""Run the pipeline every hour on the hour with a macOS launchd agent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import Config

LABEL = "local.job-tracking"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(config: Config, python: str = sys.executable) -> dict:
    log_file = config.output.log_dir / "launchd.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "job_tracking", "run", "--config", str(config.path)],
        "WorkingDirectory": str(config.root),
        # Every hour at minute 0. If the Mac was asleep, launchd runs it once on wake.
        "StartCalendarInterval": {"Minute": 0},
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(config: Config) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("Scheduling is only set up for macOS (launchd). Use cron elsewhere.")
    config.output.log_dir.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # replace an older copy; fails harmlessly if none
    with open(path, "wb") as handle:
        plistlib.dump(build_plist(config), handle)
    result = _launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
    return f"Installed {path}. Runs every hour on the hour from {config.root}."


def uninstall() -> str:
    path = plist_path()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if path.exists():
        path.unlink()
        return f"Removed {path}."
    return "No schedule was installed."


def status() -> str:
    path = plist_path()
    if not path.exists():
        return "Not installed."
    result = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    loaded = "loaded" if result.returncode == 0 else "NOT loaded (run `job-tracking schedule install` again)"
    with open(path, "rb") as handle:
        args = plistlib.load(handle).get("ProgramArguments", [])
    return f"Installed at {path}, {loaded}.\nCommand: {' '.join(args)}"
