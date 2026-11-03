from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import schedule
from .config import Config, ConfigError, load_config
from .excel import publish
from .pipeline import AlreadyRunning, RunReport, db_path, run
from .sources import SOURCE_NAMES
from .store import Store

log = logging.getLogger("job_tracking")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="job-tracking",
        description="Collect new job postings and write them to an Excel sheet.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml (default: ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="fetch new jobs and update the sheet")
    run_cmd.add_argument(
        "--source",
        action="append",
        choices=SOURCE_NAMES,
        help="only this source (repeatable); also ignores its every_hours limit",
    )
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and list what would be added, without saving anything",
    )

    commands.add_parser("render", help="rewrite the sheet from saved jobs, without fetching")

    sched = commands.add_parser("schedule", help="run every hour with macOS launchd")
    sched.add_argument("action", choices=["install", "uninstall", "status"])

    # Let `--config`/`-v` also work after the subcommand.
    for sub in (run_cmd, commands.choices["render"], sched):
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sub.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    if args.command == "schedule" and args.action != "install":
        print(schedule.uninstall() if args.action == "uninstall" else schedule.status())
        return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config, args.verbose)

    if args.command == "schedule":
        if (config.root / ".git").is_file():  # a linked git worktree has a .git file, not a folder
            print(
                f"Warning: {config.root} is a git worktree. Install the schedule from the "
                "main checkout instead, or it will stop working when the worktree is removed.",
                file=sys.stderr,
            )
        try:
            print(schedule.install(config))
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    if args.command == "render":
        with Store(db_path(config)) as store:
            result = publish(store, config, datetime.now(config.timezone))
        print(result.message)
        return 0

    try:
        report = run(config, only=set(args.source) if args.source else None, dry_run=args.dry_run)
    except AlreadyRunning as exc:
        log.warning("%s; skipping this run", exc)
        print(exc, file=sys.stderr)
        return 1
    print_report(report, dry_run=args.dry_run)
    return 1 if report.all_failed else 0


def setup_logging(config: Config, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)

    config.output.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.output.log_dir / "job-tracking.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    # httpx logs full request URLs at INFO, which would put the SerpApi key in the log.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def print_report(report: RunReport, *, dry_run: bool) -> None:
    when = f"{report.started_at:%d.%m.%Y %H:%M}"
    print(f"{'Dry run' if dry_run else 'Run'} at {when}")
    for s in report.sources:
        if s.status == "skipped":
            print(f"  {s.name:<15} skipped: {s.message}")
            continue
        line = f"  {s.name:<15} {s.found} found, {s.added} new"
        if s.dropped:
            line += " (dropped: " + ", ".join(f"{n} {why}" for why, n in s.dropped.most_common()) + ")"
        if s.status == "failed":
            line = f"  {s.name:<15} FAILED: {s.message}"
        elif s.message:
            line += f" [{s.message}]"
        print(line)
    if dry_run:
        for job in report.new_jobs:
            where = ", ".join(x for x in (job.location, job.country) if x)
            print(f"    + [{job.source}] {job.title} | {job.company} | {where} | {job.posted_text}")
    elif report.publish:
        print(report.publish.message)
