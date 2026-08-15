"""CLI plumbing tests (Step 10) using click's CliRunner."""

from __future__ import annotations

from click.testing import CliRunner

from macro_monitor import collector_rss
from macro_monitor.cli import main


def _db(tmp_path):
    return str(tmp_path / "macro_monitor.db")


def test_help_lists_all_subcommands():
    res = CliRunner().invoke(main, ["--help"])
    assert res.exit_code == 0
    for cmd in ("collect-rss", "correlate", "sources", "ingest", "review"):
        assert cmd in res.output


def test_sources_add_requires_checked_on(tmp_path):
    runner = CliRunner()
    dbp = _db(tmp_path)
    res = runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "x", "--kind", "rss",
         "--url-or-query", "https://x.test/f.xml"],
    )
    assert res.exit_code != 0  # missing --checked-on


def test_sources_add_and_list(tmp_path):
    runner = CliRunner()
    dbp = _db(tmp_path)
    res = runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "fed", "--kind", "rss",
         "--url-or-query", "https://x.test/f.xml", "--checked-on", "2026-07-05"],
    )
    assert res.exit_code == 0
    res2 = runner.invoke(main, ["--db-path", dbp, "sources", "list"])
    assert "fed" in res2.output


def test_ingest_and_correlate_flow(tmp_path, paper_db):
    runner = CliRunner()
    dbp = _db(tmp_path)
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "websearch-wsb", "--kind", "websearch",
         "--url-or-query", "q", "--checked-on", "2026-07-05"],
    )
    res = runner.invoke(
        main,
        ["--db-path", dbp, "ingest", "--source", "websearch-wsb", "--observed-date", "2026-07-04",
         "--url", "https://x/1", "--title-or-snippet", "SPY squeeze", "--symbols", "SPY"],
    )
    assert res.exit_code == 0
    assert "inserted=True" in res.output

    res2 = runner.invoke(
        main,
        ["--db-path", dbp, "correlate", "--date", "2026-07-04", "--paper-db", paper_db],
    )
    assert res2.exit_code == 0
    assert "correlations written: 3" in res2.output


def test_correlate_date_and_since_are_mutually_exclusive(tmp_path, paper_db):
    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "--db-path", _db(tmp_path), "correlate",
            "--date", "2026-07-04", "--since", "2026-07-01", "--paper-db", paper_db,
        ],
    )
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_correlate_since_walks_a_window_not_one_exact_date(tmp_path, paper_db):
    """The fix for the production no-op: `--since` (and the no-flags default it backs) considers
    every distinct observed_date in range, not one exact calendar date."""
    runner = CliRunner()
    dbp = _db(tmp_path)
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "websearch-wsb", "--kind", "websearch",
         "--url-or-query", "q", "--checked-on", "2026-07-05"],
    )
    runner.invoke(
        main,
        ["--db-path", dbp, "ingest", "--source", "websearch-wsb", "--observed-date", "2026-07-04",
         "--url", "https://x/1", "--title-or-snippet", "SPY squeeze", "--symbols", "SPY"],
    )
    # A "today" of 2026-07-06 would miss the 2026-07-04 observation under the old exact-date
    # behavior; --since 2026-07-01 finds it, reproducing the fix that replaces the systemd unit's
    # `--date "$(date -u +%Y-%m-%d)"` default.
    res = runner.invoke(
        main,
        ["--db-path", dbp, "correlate", "--since", "2026-07-01", "--paper-db", paper_db],
    )
    assert res.exit_code == 0, res.output
    assert "correlations written: 3" in res.output


def test_correlate_no_flags_uses_config_lookback_window(tmp_path, paper_db, monkeypatch):
    """With neither --date nor --since (the systemd unit's actual invocation), correlate must
    still find an observation dated a few days back -- this is the no-op fix end to end."""
    import macro_monitor.cli as cli_mod

    # correlate_cmd sources "today" from db.now_iso() (UTC), not date.today() (local time) -- see
    # docs/DECISIONS.md and cli.py's other now-sourced fields for why they must all agree.
    monkeypatch.setattr(cli_mod.db, "now_iso", lambda: "2026-07-06T00:00:00+00:00")

    runner = CliRunner()
    dbp = _db(tmp_path)
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "websearch-wsb", "--kind", "websearch",
         "--url-or-query", "q", "--checked-on", "2026-07-05"],
    )
    runner.invoke(
        main,
        ["--db-path", dbp, "ingest", "--source", "websearch-wsb", "--observed-date", "2026-07-04",
         "--url", "https://x/1", "--title-or-snippet", "SPY squeeze", "--symbols", "SPY"],
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {dbp}\ncorrelate_lookback_days: 3\n")
    res = runner.invoke(main, ["--config", str(cfg), "correlate", "--paper-db", paper_db])
    assert res.exit_code == 0, res.output
    assert "correlations written: 3" in res.output


def test_correlate_writes_did_nothing_metrics_when_no_observations(tmp_path, paper_db, monkeypatch):
    directory = tmp_path / "textfiles"
    directory.mkdir()
    monkeypatch.setenv("MACRO_MONITOR_TEXTFILE_DIR", str(directory))
    runner = CliRunner()
    res = runner.invoke(
        main,
        ["--db-path", _db(tmp_path), "correlate", "--since", "2026-07-04", "--paper-db", paper_db],
    )
    assert res.exit_code == 0
    content = (directory / "macro_monitor.prom").read_text()
    assert 'macro_monitor_work_quantity{phase="correlate"} 0' in content
    assert 'macro_monitor_work_available{phase="correlate"} 0' in content
    assert 'macro_monitor_last_run_success{phase="correlate"} 1' in content


def test_collect_rss_writes_metrics_reflecting_work_done(tmp_path, monkeypatch):
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: [
            {
                "title": "FOMC holds rates; SPY reaction watched",
                "link": "https://example.test/press/1",
                "guid": "https://example.test/press/1",
                "pub_date": "2026-07-04T14:00:00+00:00",
                "description_excerpt": "test",
            },
            {
                "title": "Treasury yields drift, TLT in focus",
                "link": "https://example.test/press/2",
                "guid": "https://example.test/press/2",
                "pub_date": "2026-07-04T15:30:00+00:00",
                "description_excerpt": "test",
            },
        ],
    )
    directory = tmp_path / "textfiles"
    directory.mkdir()
    monkeypatch.setenv("MACRO_MONITOR_TEXTFILE_DIR", str(directory))
    runner = CliRunner()
    dbp = _db(tmp_path)
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "fed", "--kind", "rss",
         "--url-or-query", "https://x.test/f.xml", "--checked-on", "2026-07-05"],
    )
    res = runner.invoke(main, ["--db-path", dbp, "collect-rss"])
    assert res.exit_code == 0
    content = (directory / "macro_monitor.prom").read_text()
    assert 'macro_monitor_work_quantity{phase="collect"} 2' in content
    assert 'macro_monitor_last_run_success{phase="collect"} 1' in content


def test_collect_rss_logs_a_structured_event_with_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: [
            {
                "title": "FOMC holds rates; SPY reaction watched",
                "link": "https://example.test/press/1",
                "guid": "https://example.test/press/1",
                "pub_date": "2026-07-04T14:00:00+00:00",
                "description_excerpt": "test",
            },
            {
                "title": "Treasury yields drift, TLT in focus",
                "link": "https://example.test/press/2",
                "guid": "https://example.test/press/2",
                "pub_date": "2026-07-04T15:30:00+00:00",
                "description_excerpt": "test",
            },
        ],
    )
    runner = CliRunner()
    dbp = _db(tmp_path)
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "fed", "--kind", "rss",
         "--url-or-query", "https://x.test/f.xml", "--checked-on", "2026-07-05"],
    )
    res = runner.invoke(main, ["--db-path", dbp, "collect-rss"])
    assert res.exit_code == 0
    import json

    events = [json.loads(line) for line in res.stderr.splitlines() if line.strip().startswith("{")]
    completed = [e for e in events if e["event"] == "collect.completed"]
    assert len(completed) == 1
    assert completed[0]["outcome"] == "ok"
    assert completed[0]["items_processed"] == 2
    assert "run_id" in completed[0] and completed[0]["run_id"]


def test_correlate_rejects_bad_date(tmp_path, paper_db):
    runner = CliRunner()
    res = runner.invoke(
        main,
        ["--db-path", _db(tmp_path), "correlate", "--date", "2026-13-40", "--paper-db", paper_db],
    )
    assert res.exit_code != 0


def test_collect_rss_cli_exit0_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: [
            {
                "title": "FOMC holds rates; SPY reaction watched",
                "link": "https://example.test/press/1",
                "guid": "https://example.test/press/1",
                "pub_date": "2026-07-04T14:00:00+00:00",
                "description_excerpt": "test",
            },
            {
                "title": "Treasury yields drift, TLT in focus",
                "link": "https://example.test/press/2",
                "guid": "https://example.test/press/2",
                "pub_date": "2026-07-04T15:30:00+00:00",
                "description_excerpt": "test",
            },
        ],
    )
    runner = CliRunner()
    dbp = _db(tmp_path)
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "fed", "--kind", "rss",
         "--url-or-query", "https://x.test/f.xml", "--checked-on", "2026-07-05"],
    )
    res = runner.invoke(main, ["--db-path", dbp, "collect-rss"])
    assert res.exit_code == 0
    assert "total inserted: 2" in res.output


def test_review_writes_report_via_json(tmp_path):
    import json

    runner = CliRunner()
    dbp = _db(tmp_path)
    reports = tmp_path / "reports"
    # seed a source + observation so cited id 1 exists
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "websearch-wsb", "--kind", "websearch",
         "--url-or-query", "q", "--checked-on", "2026-07-05"],
    )
    runner.invoke(
        main,
        ["--db-path", dbp, "ingest", "--source", "websearch-wsb", "--observed-date", "2026-07-04",
         "--url", "https://x/1", "--title-or-snippet", "SPY", "--symbols", "SPY"],
    )
    from macro_monitor.reviewer import OVERFITTING_DISCLOSURE

    hyp = [{
        "slug": "test-hypothesis-slug",
        "mechanism_description": "desc",
        "cited_observation_ids": [1],
        "cited_paper_db_summary": {"targets": 1},
        "overfitting_disclosure": OVERFITTING_DISCLOSURE,
    }]
    hjson = tmp_path / "h.json"
    hjson.write_text(json.dumps(hyp))
    # config with reports_dir under tmp
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {dbp}\nreports_dir: {reports}\n")
    res = runner.invoke(
        main,
        ["--config", str(cfg), "review", "--hypotheses-json", str(hjson)],
    )
    assert res.exit_code == 0, res.output
    assert (reports / "test-hypothesis-slug.md").exists()


def test_review_writes_metrics_reflecting_work_done(tmp_path, monkeypatch):
    import json

    directory = tmp_path / "textfiles"
    directory.mkdir()
    monkeypatch.setenv("MACRO_MONITOR_TEXTFILE_DIR", str(directory))
    runner = CliRunner()
    dbp = _db(tmp_path)
    reports = tmp_path / "reports"
    runner.invoke(
        main,
        ["--db-path", dbp, "sources", "add", "--name", "websearch-wsb", "--kind", "websearch",
         "--url-or-query", "q", "--checked-on", "2026-07-05"],
    )
    runner.invoke(
        main,
        ["--db-path", dbp, "ingest", "--source", "websearch-wsb", "--observed-date", "2026-07-04",
         "--url", "https://x/1", "--title-or-snippet", "SPY", "--symbols", "SPY"],
    )
    from macro_monitor.reviewer import OVERFITTING_DISCLOSURE

    hyp = [{
        "slug": "test-hypothesis-slug",
        "mechanism_description": "desc",
        "cited_observation_ids": [1],
        "cited_paper_db_summary": {"targets": 1},
        "overfitting_disclosure": OVERFITTING_DISCLOSURE,
    }]
    hjson = tmp_path / "h.json"
    hjson.write_text(json.dumps(hyp))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {dbp}\nreports_dir: {reports}\n")
    res = runner.invoke(
        main,
        ["--config", str(cfg), "review", "--hypotheses-json", str(hjson)],
    )
    assert res.exit_code == 0, res.output
    content = (directory / "macro_monitor.prom").read_text()
    assert 'macro_monitor_work_quantity{phase="review"} 1' in content
    assert 'macro_monitor_work_available{phase="review"} 1' in content
    assert 'macro_monitor_last_run_success{phase="review"} 1' in content


def test_review_writes_did_nothing_metrics_when_no_hypotheses(tmp_path, monkeypatch):
    directory = tmp_path / "textfiles"
    directory.mkdir()
    monkeypatch.setenv("MACRO_MONITOR_TEXTFILE_DIR", str(directory))
    runner = CliRunner()
    dbp = _db(tmp_path)
    res = runner.invoke(main, ["--db-path", dbp, "review", "--since", "2026-07-01"])
    assert res.exit_code == 0, res.output
    content = (directory / "macro_monitor.prom").read_text()
    assert 'macro_monitor_work_quantity{phase="review"} 0' in content
    assert 'macro_monitor_work_available{phase="review"} 0' in content
    assert 'macro_monitor_last_run_success{phase="review"} 1' in content
