"""Config-loading tests.

No test file for `macro_monitor.config` existed before the fleet-logging migration (this repo's
config loader had zero dedicated coverage). Added here rather than left implicit, because this
service runs live under systemd and a config-loading regression is exactly the kind of thing that
fails silently in production — see `load_config`'s own docstring for why the missing-file and
malformed-file branches behave differently (non-fatal vs. fatal) and why that split matters.
"""

from __future__ import annotations

import json
from pathlib import Path

import fleet_logging.config
import pytest

from macro_monitor.config import Config, load_config


def test_missing_file_falls_back_to_defaults_and_warns(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.yaml"
    cfg = load_config(str(missing))
    assert cfg == Config()
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines, "expected a config.missing warning on stderr"
    line = json.loads(lines[-1])
    assert line["level"] == "warn"
    assert line["service"] == "macro-monitor"
    assert line["event"] == "config.missing"
    assert line["path"] == str(missing.resolve())


def test_default_path_is_config_yaml_in_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None)
    assert cfg == Config()
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["path"] == str((tmp_path / "config.yaml").resolve())


def test_yaml_overlay_sets_only_present_fields(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("db_path: /tmp/custom.db\nreview_min_days: 14\nsymbol_universe: [SPY, QQQ]\n")
    cfg = load_config(str(cfg_path))
    assert cfg.db_path == "/tmp/custom.db"
    assert cfg.review_min_days == 14
    assert cfg.symbol_universe == ["SPY", "QQQ"]
    # Fields absent from the yaml file keep the dataclass default, not None/empty.
    assert cfg.paper_db_path == Config().paper_db_path
    assert cfg.reports_dir == Config().reports_dir
    assert cfg.correlate_lookback_days == Config().correlate_lookback_days


def test_malformed_yaml_logs_then_raises(tmp_path, capsys):
    bad = tmp_path / "config.yaml"
    bad.write_text("db_path: [unterminated\n  - broken")
    with pytest.raises(Exception):  # noqa: B017 - yaml.YAMLError subclass, exact type not the point
        load_config(str(bad))
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["level"] == "error"
    assert line["service"] == "macro-monitor"
    assert line["event"] == "config.parse_failed"
    assert line["path"] == str(bad.resolve())
    assert "err_type" in line and "err_msg" in line


def test_env_override_requires_macro_monitor_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("MACRO_MONITOR_DB_PATH", raising=False)
    missing = tmp_path / "does-not-exist.yaml"

    # A bare, unprefixed DB_PATH must NOT reach this service's config -- avoids an unrelated
    # process's generic env var accidentally overriding a live config value.
    monkeypatch.setenv("DB_PATH", "/should/not/apply")
    cfg = load_config(str(missing))
    assert cfg.db_path == Config().db_path

    # The MACRO_MONITOR_-prefixed name is honored.
    monkeypatch.setenv("MACRO_MONITOR_DB_PATH", "/prefixed/works.db")
    cfg2 = load_config(str(missing))
    assert cfg2.db_path == "/prefixed/works.db"


def test_yaml_value_overridden_by_prefixed_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("review_min_days: 14\n")
    monkeypatch.setenv("MACRO_MONITOR_REVIEW_MIN_DAYS", "21")
    cfg = load_config(str(cfg_path))
    # Env overlay takes precedence over the yaml file value (fleet_logging.load_config's
    # documented precedence: env > yaml > dataclass default).
    assert cfg.review_min_days == 21
    monkeypatch.delenv("MACRO_MONITOR_REVIEW_MIN_DAYS", raising=False)


def test_load_config_never_triggers_dotenv_tree_walk(tmp_path, monkeypatch):
    """`fleet_logging.load_config` calls `load_dotenv()` internally. With no args, that walks
    *up the filesystem tree from wherever the `fleet_logging` package is installed* (not this
    repo's cwd) looking for any file named `.env` and silently overlays whatever it finds into
    `os.environ` -- a side effect this repo's original hand-rolled loader never had. `config.py`
    passes an explicit, always-nonexistent `dotenv_path=` specifically to keep that call a no-op.
    Asserts on the actual mechanism (the argument passed to `load_dotenv`), not just an absence
    of symptoms, since a real ambient `.env` living above the site-packages dir isn't something
    this test can control.
    """
    calls: list[tuple] = []
    real_load_dotenv = fleet_logging.config.load_dotenv

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_load_dotenv(*args, **kwargs)

    monkeypatch.setattr(fleet_logging.config, "load_dotenv", spy)
    missing = tmp_path / "does-not-exist.yaml"
    load_config(str(missing))

    assert len(calls) == 1
    args, kwargs = calls[0]
    dotenv_path = args[0] if args else kwargs.get("dotenv_path")
    # Must be a specific, non-None path -- the argument-less `load_dotenv()` shape (which
    # triggers `find_dotenv()`'s tree walk) must never be reached.
    assert dotenv_path is not None
    assert not Path(dotenv_path).exists()
