"""Load settings from config.toml (and secrets from an optional .env next to it)."""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_FILENAME = "config.toml"
CONFIG_ENV_VAR = "JOB_TRACKING_CONFIG"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class OutputConfig:
    workbook: Path
    pending_workbook: Path
    data_dir: Path
    log_dir: Path
    max_age_days: int = 30


@dataclass(frozen=True)
class SearchConfig:
    keywords: tuple[str, ...]
    country: str = "Germany"
    title_include: tuple[str, ...] = ()
    title_exclude: tuple[str, ...] = ()
    max_posted_age_days: int = 3


@dataclass(frozen=True)
class LinkedInConfig:
    enabled: bool = True
    location: str = "Germany"
    experience_levels: tuple[int, ...] = (1, 2)
    posted_within_hours: int = 24
    max_pages: int = 3
    delay_seconds: float = 2.0


@dataclass(frozen=True)
class ArbeitsagenturConfig:
    enabled: bool = True
    where: str = "Deutschland"
    offer_types: tuple[int, ...] = (1, 34)
    published_within_days: int = 1
    page_size: int = 100
    max_pages: int = 3


@dataclass(frozen=True)
class GoogleConfig:
    enabled: bool = True
    api_key_env: str = "SERPAPI_API_KEY"
    location: str = "Germany"
    every_hours: int = 24
    max_pages: int = 1


@dataclass(frozen=True)
class Config:
    path: Path
    timezone: ZoneInfo
    output: OutputConfig
    search: SearchConfig
    linkedin: LinkedInConfig
    arbeitsagentur: ArbeitsagenturConfig
    google: GoogleConfig

    @property
    def root(self) -> Path:
        return self.path.parent


def find_config(explicit: Path | None = None) -> Path:
    """Explicit path, then $JOB_TRACKING_CONFIG, then ./config.toml."""
    if explicit is not None:
        candidate = explicit
    elif os.environ.get(CONFIG_ENV_VAR):
        candidate = Path(os.environ[CONFIG_ENV_VAR])
    else:
        candidate = Path.cwd() / CONFIG_FILENAME
    candidate = candidate.expanduser().resolve()
    if not candidate.is_file():
        raise ConfigError(f"Config file not found: {candidate}")
    return candidate


def load_config(path: Path | None = None) -> Config:
    path = find_config(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    load_dotenv(path.parent / ".env")
    root = path.parent

    tz_name = raw.pop("timezone", "Europe/Istanbul")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone: {tz_name!r}") from exc

    output_raw = raw.pop("output", {})
    output = _build(
        OutputConfig,
        {
            "workbook": "jobs.xlsx",
            "pending_workbook": "jobs_pending.xlsx",
            "data_dir": "data",
            "log_dir": "logs",
            **output_raw,
        },
        "output",
    )
    output = dataclasses.replace(
        output,
        workbook=root / output.workbook,
        pending_workbook=root / output.pending_workbook,
        data_dir=root / output.data_dir,
        log_dir=root / output.log_dir,
    )

    search = _build(SearchConfig, raw.pop("search", {}), "search")
    if not search.keywords:
        raise ConfigError("[search] keywords must list at least one search term")

    sources = raw.pop("sources", {})
    config = Config(
        path=path,
        timezone=tz,
        output=output,
        search=search,
        linkedin=_build(LinkedInConfig, sources.pop("linkedin", {}), "sources.linkedin"),
        arbeitsagentur=_build(
            ArbeitsagenturConfig, sources.pop("arbeitsagentur", {}), "sources.arbeitsagentur"
        ),
        google=_build(GoogleConfig, sources.pop("google", {}), "sources.google"),
    )
    if sources:
        raise ConfigError(f"Unknown source(s) in config: {', '.join(sorted(sources))}")
    if raw:
        raise ConfigError(f"Unknown setting(s) in config: {', '.join(sorted(raw))}")
    return config


def _build(cls: type, values: dict[str, Any], section: str):
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(values) - set(fields)
    if unknown:
        raise ConfigError(f"[{section}] unknown setting(s): {', '.join(sorted(unknown))}")
    converted = {}
    for name, value in values.items():
        if isinstance(value, list):
            value = tuple(value)
        if fields[name].type in ("Path",) and isinstance(value, str):
            value = Path(value)
        converted[name] = value
    try:
        return cls(**converted)
    except TypeError as exc:
        raise ConfigError(f"[{section}] {exc}") from exc


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines into os.environ without overriding variables already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)
