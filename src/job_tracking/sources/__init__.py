from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from ..config import Config
from . import arbeitsagentur, google, linkedin
from .base import FetchResult, Source, SourceError

SOURCE_NAMES = (linkedin.NAME, arbeitsagentur.NAME, google.NAME)


@dataclass(frozen=True)
class SourceSpec:
    """A source plus how often it may run. `source` is None when it can't run (reason says why)."""

    name: str
    source: Source | None
    every_hours: int = 0
    reason: str = ""


def build_sources(config: Config, client: httpx.Client) -> list[SourceSpec]:
    country = config.search.country
    specs: list[SourceSpec] = []

    if config.linkedin.enabled:
        specs.append(SourceSpec(linkedin.NAME, linkedin.LinkedInSource(config.linkedin, country, client)))
    else:
        specs.append(SourceSpec(linkedin.NAME, None, reason="disabled in config"))

    if config.arbeitsagentur.enabled:
        specs.append(
            SourceSpec(
                arbeitsagentur.NAME,
                arbeitsagentur.ArbeitsagenturSource(config.arbeitsagentur, country, client),
            )
        )
    else:
        specs.append(SourceSpec(arbeitsagentur.NAME, None, reason="disabled in config"))

    key = os.environ.get(config.google.api_key_env, "").strip()
    if not config.google.enabled:
        specs.append(SourceSpec(google.NAME, None, reason="disabled in config"))
    elif not key:
        specs.append(SourceSpec(google.NAME, None, reason=f"no {config.google.api_key_env} set"))
    else:
        specs.append(
            SourceSpec(
                google.NAME,
                google.GoogleJobsSource(config.google, key, country, client),
                every_hours=config.google.every_hours,
            )
        )
    return specs


__all__ = ["SOURCE_NAMES", "FetchResult", "Source", "SourceError", "SourceSpec", "build_sources"]
