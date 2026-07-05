"""Runtime configuration loader.

Loads ``config.yaml`` (gitignored) if present, otherwise falls back to defaults. CLI flags
override config values. Kept deliberately small — no schema framework for a handful of flat keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"


@dataclass
class Config:
    paper_db_path: str = "/srv/paper-share/paper.db"
    db_path: str = "data/macro_monitor.db"
    reports_dir: str = "reports"
    review_min_days: int = 7
    symbol_universe: list[str] = field(default_factory=list)


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
        return Config()
    data = yaml.safe_load(cfg_path.read_text()) or {}
    cfg = Config()
    for key in ("paper_db_path", "db_path", "reports_dir", "review_min_days", "symbol_universe"):
        if key in data and data[key] is not None:
            setattr(cfg, key, data[key])
    return cfg
