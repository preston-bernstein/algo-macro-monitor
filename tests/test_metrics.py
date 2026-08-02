"""§18 textfile-collector metrics tests for macro_monitor.metrics.

Covers: the did-nothing rule (work_quantity/work_available both always present, 0 is honest),
the merge-not-clobber behavior across the two independent phases that share one file, and the
atomic write / missing-directory-is-a-noop behaviors that keep a dev/test box from ever failing
a CLI command just because the observability stack isn't deployed there.
"""

from __future__ import annotations

import os

from macro_monitor import metrics


def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def test_missing_textfile_dir_is_a_noop_not_a_crash(tmp_path):
    result = metrics.write_phase_metrics(
        "collect",
        success=True,
        work_quantity=0,
        work_available=0,
        textfile_dir=str(tmp_path / "does-not-exist"),
    )
    assert result == ""


def test_writes_all_four_minimum_metrics(tmp_path):
    path = metrics.write_phase_metrics(
        "collect",
        success=True,
        work_quantity=2,
        work_available=20,
        textfile_dir=str(tmp_path),
        now=1000.0,
    )
    content = _read(path)
    assert 'macro_monitor_last_run_timestamp_seconds{phase="collect"} 1000.0' in content
    assert 'macro_monitor_last_run_success{phase="collect"} 1' in content
    assert 'macro_monitor_work_quantity{phase="collect"} 2' in content
    assert 'macro_monitor_work_available{phase="collect"} 20' in content
    # every metric this module owns gets a HELP + TYPE line
    for name in (
        "macro_monitor_last_run_timestamp_seconds",
        "macro_monitor_last_run_success",
        "macro_monitor_work_quantity",
        "macro_monitor_work_available",
    ):
        assert f"# HELP {name}" in content
        assert f"# TYPE {name} gauge" in content


def test_did_nothing_rule_zero_is_an_honest_value_not_omitted(tmp_path):
    path = metrics.write_phase_metrics(
        "correlate",
        success=True,
        work_quantity=0,
        work_available=0,
        textfile_dir=str(tmp_path),
        now=2000.0,
    )
    content = _read(path)
    # A benign no-op (nothing to do) still emits explicit 0s, never drops the metric lines.
    assert 'macro_monitor_work_quantity{phase="correlate"} 0' in content
    assert 'macro_monitor_work_available{phase="correlate"} 0' in content


def test_did_nothing_rule_distinguishes_available_from_processed(tmp_path):
    # available > 0, processed 0 -- the vault-indexer failure mode exactly.
    path = metrics.write_phase_metrics(
        "correlate",
        success=True,
        work_quantity=0,
        work_available=15,
        textfile_dir=str(tmp_path),
    )
    content = _read(path)
    assert 'macro_monitor_work_quantity{phase="correlate"} 0' in content
    assert 'macro_monitor_work_available{phase="correlate"} 15' in content


def test_second_phase_write_does_not_clobber_the_first(tmp_path):
    # collect-rss and correlate run as two separate OS processes seconds apart on the same
    # systemd timer fire and share one file -- this is the regression this module exists to
    # prevent (a naive overwrite would erase collect's numbers within seconds of every run).
    metrics.write_phase_metrics(
        "collect", success=True, work_quantity=2, work_available=20,
        textfile_dir=str(tmp_path), now=1000.0,
    )
    metrics.write_phase_metrics(
        "correlate", success=True, work_quantity=3, work_available=3,
        textfile_dir=str(tmp_path), now=1001.0,
    )
    content = _read(os.path.join(str(tmp_path), metrics.METRIC_FILE_NAME))
    assert 'macro_monitor_work_quantity{phase="collect"} 2' in content
    assert 'macro_monitor_work_quantity{phase="correlate"} 3' in content
    assert 'macro_monitor_last_run_timestamp_seconds{phase="collect"} 1000.0' in content
    assert 'macro_monitor_last_run_timestamp_seconds{phase="correlate"} 1001.0' in content


def test_rewrite_of_same_phase_updates_in_place(tmp_path):
    metrics.write_phase_metrics(
        "collect", success=True, work_quantity=2, work_available=20,
        textfile_dir=str(tmp_path), now=1000.0,
    )
    metrics.write_phase_metrics(
        "collect", success=False, work_quantity=0, work_available=0,
        textfile_dir=str(tmp_path), now=2000.0,
    )
    content = _read(os.path.join(str(tmp_path), metrics.METRIC_FILE_NAME))
    assert 'macro_monitor_work_quantity{phase="collect"} 0' in content
    assert 'macro_monitor_last_run_success{phase="collect"} 0' in content
    assert 'macro_monitor_last_run_timestamp_seconds{phase="collect"} 2000.0' in content
    # stale value from the first write must be gone, not just superseded-but-present twice
    assert content.count('macro_monitor_work_quantity{phase="collect"}') == 1


def test_write_is_atomic_no_tmp_file_left_behind(tmp_path):
    metrics.write_phase_metrics(
        "collect", success=True, work_quantity=1, work_available=1, textfile_dir=str(tmp_path)
    )
    leftovers = [p for p in os.listdir(tmp_path) if ".tmp." in p]
    assert leftovers == []


def test_corrupt_existing_file_is_dropped_not_fatal(tmp_path):
    path = os.path.join(str(tmp_path), metrics.METRIC_FILE_NAME)
    with open(path, "w") as fh:
        fh.write("not a valid prometheus line at all\n")
    result = metrics.write_phase_metrics(
        "collect", success=True, work_quantity=5, work_available=5, textfile_dir=str(tmp_path)
    )
    assert result == path
    content = _read(path)
    assert 'macro_monitor_work_quantity{phase="collect"} 5' in content
