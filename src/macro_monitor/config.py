"""Runtime configuration loader.

Thin wrapper around the shared `fleet-logging` package's `load_config(dataclass_type, path)` (see
the git-pinned dependency in pyproject.toml — fleet-logging was built as a strict superset of this
module's own original hand-rolled loader, among others; see its README's "What it replaces"
section). Loads ``config.yaml`` (gitignored) if present, otherwise falls back to defaults; CLI
flags still override the returned config values afterward, in cli.py's `main()` — this module has
never handled that override itself. Kept deliberately small — no schema framework for a handful of
flat keys.

`env_prefix="MACRO_MONITOR_"` is passed explicitly here as a deliberate safety choice:
`fleet_logging.load_config` overlays a bare uppercased env var per field (`DB_PATH`,
`REPORTS_DIR`, ...) by default, which this repo's original hand-rolled loader never supported at
all — this is new capability, not a preserved behavior. This service runs live under systemd, and
those bare names are generic enough that some future unrelated env var could collide with a config
field by accident; prefixing removes that risk entirely while still giving this repo the new
env-override capability under its own namespace (`MACRO_MONITOR_DB_PATH`, etc.). Nothing in this
repo's systemd units or `.env` sets any of these names today, prefixed or not, so this changes no
resolved config value right now — it only changes what a *future* env var name would have to be to
affect this service.

`fleet_logging.load_config`'s own internal `config.missing`/`config.parse_failed` log lines have
no `stream=` override (unlike `fleet_logging.log_event`, which does) — they always resolve
`sys.stdout` at call time. That's wrong for this repo: every machine-readable §18 log line belongs
on **stderr**, kept deliberately separate from `click.echo`'s stdout (see `log.py`'s module
docstring). `_log_config_events_to_stderr` below closes that gap the only way the public API
allows: it temporarily points `sys.stdout` at the real `sys.stderr` for the duration of the single,
synchronous `load_config()` call this module makes (the very first thing `cli.py`'s `main()` does,
before any `click.echo` output exists to collide with), then restores it immediately after —
verified by `tests/test_config.py`, which asserts these lines land on stderr, not stdout.

`fleet_logging.load_config` also unconditionally calls python-dotenv's `load_dotenv()` unless a
`dotenv_path=` is given — with no path, that walks *up the filesystem tree from wherever the
`fleet_logging` package itself is installed* (not this repo's cwd) looking for any file literally
named `.env`, and silently overlays whatever it finds into `os.environ` for the rest of the
process. This repo's original hand-rolled loader never touched dotenv/`.env` at all, and nothing
here opts into internal-corpus-service's "secrets from `.env`" pattern this exists for. `_NO_DOTENV` below
pins an explicitly nonexistent path, which short-circuits `load_dotenv()` straight to its
does-nothing branch — verified by `tests/test_config.py` — so this stays a no-op exactly like
before, instead of an undocumented tree-walking side effect on every live invocation.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass, field

from fleet_logging import load_config as _fleet_load_config

from .log import SERVICE

DEFAULT_CONFIG_PATH = "config.yaml"

# See module docstring. A path guaranteed never to exist, so `fleet_logging.load_config`'s
# internal `load_dotenv(dotenv_path)` call is always a no-op instead of the tree-walking
# `load_dotenv()` (no args) default.
_NO_DOTENV = os.path.join(os.devnull, "unused.env")


@contextlib.contextmanager
def _log_config_events_to_stderr():
    """See module docstring. Narrow, restored-in-`finally` redirect around one call only."""
    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = original_stdout


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
    """Load ``config.yaml`` (or `path`) into a `Config`.

    Non-fatal on a missing file — falls back to defaults, after logging a `config.missing` warning
    with the exact resolved path that was tried (so a fresh checkout or a mis-pathed config is
    never silently empty). Fatal (re-raised, after logging `config.parse_failed`) on a file that
    exists but fails to parse — a malformed config is a genuine operator error, not something to
    silently default around. Both behaviors are `fleet_logging.load_config`'s own default
    (`required=False`) — its module docstring documents this as ported verbatim from this file's
    original implementation.
    """
    with _log_config_events_to_stderr():
        return _fleet_load_config(
            Config,
            path or DEFAULT_CONFIG_PATH,
            required=False,
            env_prefix="MACRO_MONITOR_",
            service=SERVICE,
            dotenv_path=_NO_DOTENV,
        )
