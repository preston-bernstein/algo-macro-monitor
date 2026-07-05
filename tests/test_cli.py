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


def test_correlate_rejects_bad_date(tmp_path, paper_db):
    runner = CliRunner()
    res = runner.invoke(
        main,
        ["--db-path", _db(tmp_path), "correlate", "--date", "2026-13-40", "--paper-db", paper_db],
    )
    assert res.exit_code != 0


def test_collect_rss_cli_exit0_on_success(tmp_path, monkeypatch, fed_feed_bytes):
    monkeypatch.setattr(collector_rss, "_fetch", lambda url: fed_feed_bytes)
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
