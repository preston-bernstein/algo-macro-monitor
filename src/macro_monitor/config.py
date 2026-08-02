"""Runtime configuration loader.

Loads ``config.yaml`` (gitignored) if present, otherwise falls back to defaults. CLI flags
override config values. Kept deliberately small — no schema framework for a handful of flat keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    data = yaml.safe_load(cfg_path.read_text()) or {}
    cfg = Config()
    for key in (
        "paper_db_path",
        "db_path",
        "reports_dir",
        "review_min_days",
        "correlate_lookback_days",
        "symbol_universe",
    ):
        if key in data and data[key] is not None:
            setattr(cfg, key, data[key])
    return cfg
