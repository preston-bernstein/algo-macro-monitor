"""§18 canonical-log-line tests for macro_monitor.log."""

from __future__ import annotations

import json

from macro_monitor.log import log_event, new_run_id


def _last_line(capsys) -> dict:
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines, "expected at least one line on stderr"
    return json.loads(lines[-1])


def test_log_event_goes_to_stderr_not_stdout(capsys):
    log_event("info", "test.event")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() != ""


def test_log_event_has_required_canonical_fields(capsys):
    log_event("info", "batch.completed", run_id="run-1", outcome="ok", items_processed=3)
    line = _last_line(capsys)
    assert line["schema_version"] == 1
    assert "ts" in line and line["ts"].endswith("Z")
    assert line["level"] == "info"
    assert line["service"] == "macro-monitor"
    assert line["event"] == "batch.completed"
    assert line["msg"] == "batch.completed"  # default msg falls back to event
    assert line["run_id"] == "run-1"
    assert line["outcome"] == "ok"
    assert line["items_processed"] == 3


def test_log_event_msg_override(capsys):
    log_event("warn", "feed.retry", msg="retrying after timeout")
    line = _last_line(capsys)
    assert line["msg"] == "retrying after timeout"


def test_log_event_is_valid_json_one_line_per_call(capsys):
    log_event("info", "a.one")
    log_event("info", "a.two")
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # must not raise


def test_log_event_redacts_denylisted_field_names(capsys):
    log_event("error", "auth.failed", token="super-secret-value", api_key="also-secret")
    line = _last_line(capsys)
    assert line["token"] == "[REDACTED]"
    assert line["api_key"] == "[REDACTED]"
    assert "super-secret-value" not in json.dumps(line)
    assert "also-secret" not in json.dumps(line)


def test_log_event_native_level_spelling_not_precanonicalized(capsys):
    # §18: application code emits its own native spelling; the shared Loki pipeline
    # canonicalizes it. This module must NOT rewrite "warn" to anything else.
    log_event("warn", "collect.entry_rejected")
    line = _last_line(capsys)
    assert line["level"] == "warn"


def test_new_run_id_is_unique_and_stringy():
    a = new_run_id()
    b = new_run_id()
    assert isinstance(a, str) and isinstance(b, str)
    assert a != b
    assert a.startswith("run-")
