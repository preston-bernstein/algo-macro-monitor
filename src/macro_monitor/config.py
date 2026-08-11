"""Runtime configuration loader.

Loads ``config.yaml`` (gitignored) if present, otherwise falls back to defaults. CLI flags
override config values. Kept deliberately small — no schema framework for a handful of flat keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .log import log_event

DEFAULT_CONFIG_PATH = "config.yaml"


@dataclass
class Config:
    paper_db_path: str = "/srv/paper-share/paper.db"
    db_path: str = "data/macro_monitor.db"
    reports_dir: str = "reports"
    review_min_days: int = 7
    # Trailing window (days) the default `correlate` invocation (no --date/--since) walks back
    # from today, in observed_date terms. Exists to fix the entry-publish-date lag documented in
    # correlator.observed_dates_since's docstring — collection and correlation happen on the same
    # timer fire, but an observation's own date routinely lags the run's UTC calendar date.
    correlate_lookback_days: int = 3
    symbol_universe: list[str] = field(default_factory=list)


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
        # Silent degradation here previously meant an absent/mis-pathed config.yaml gave an empty
        # symbol_universe forever, with nothing to explain why every observation came back
        # untagged. Not fatal (a fresh checkout legitimately has no config.yaml yet), but never
        # silent — log the exact resolved path that was tried.
        log_event(
            "warn",
            "config.missing",
            "config file not found, falling back to defaults",
            path=str(cfg_path.resolve()),
        )
        return Config()
    try:
        data = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as exc:
        # Same "never silent" reasoning as the missing-file branch above, for the sibling failure
        # mode: a config.yaml that exists but doesn't parse. Still re-raised — a malformed config
        # is a genuine operator error, not something to silently fall back to defaults for — but
        # now it's queryable in the same JSON stream as every other failure class first.
        log_event(
            "error",
            "config.parse_failed",
            path=str(cfg_path.resolve()),
            err_type=type(exc).__name__,
            err_msg=str(exc),
        )
        raise
    cfg = Config()
    for f in fields(Config):
        if f.name in data and data[f.name] is not None:
            setattr(cfg, f.name, data[f.name])
    return cfg
