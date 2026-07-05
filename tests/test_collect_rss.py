"""FR-01 / FR-13a collector tests (Step 5). No live network — feed bytes are mocked."""

from __future__ import annotations

import httpx

from macro_monitor import collector_rss
from macro_monitor.collector_rss import collect_rss, fetch_and_log_rss


def test_fetch_and_log_parses_and_tags(monkeypatch, log_conn, fed_feed_bytes):
    monkeypatch.setattr(collector_rss, "_fetch", lambda url: fed_feed_bytes)
    result = fetch_and_log_rss(
        log_conn, "fed-press", "https://example.test/fed.xml", symbol_universe=["SPY", "TLT"]
    )
    assert result.ok is True
    assert result.inserted == 2
    rows = log_conn.execute(
        "SELECT observed_date, tagged_symbols FROM raw_observations ORDER BY id"
    ).fetchall()
    assert rows[0]["observed_date"] == "2026-07-04"
    assert "SPY" in rows[0]["tagged_symbols"]
    assert "TLT" in rows[1]["tagged_symbols"]


def test_rerun_is_append_only_noop(monkeypatch, log_conn, fed_feed_bytes):
    monkeypatch.setattr(collector_rss, "_fetch", lambda url: fed_feed_bytes)
    r1 = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    r2 = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert r1.inserted == 2
    assert r2.inserted == 0  # dedup — no new rows on re-run (FR-14)
    count = log_conn.execute("SELECT COUNT(*) c FROM raw_observations").fetchone()["c"]
    assert count == 2


def test_http_error_is_logged_not_raised(monkeypatch, log_conn):
    def boom(url):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(collector_rss, "_fetch", boom)
    result = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert result.ok is False
    assert "fetch" in result.error


def test_malformed_xml_is_logged_not_raised(monkeypatch, log_conn):
    monkeypatch.setattr(collector_rss, "_fetch", lambda url: b"<<<not xml>>>")
    result = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert result.ok is False


def test_collect_rss_summary_any_success(monkeypatch, log_conn, fed_feed_bytes):
    # fed-press succeeds; add a second source that fails — summary.any_success stays True (FR-13a).
    from macro_monitor import sources

    sources.add_source(log_conn, "wsj-markets", "rss", "https://wsj.test/f.xml", "2026-07-05")

    def selective(url):
        if "wsj" in url:
            raise httpx.ConnectError("boom")
        return fed_feed_bytes

    monkeypatch.setattr(collector_rss, "_fetch", selective)
    summary = collect_rss(log_conn, symbol_universe=["SPY"])
    assert summary.any_success is True
    assert {r.source for r in summary.results} == {"fed-press", "wsj-markets"}
    assert any(not r.ok for r in summary.results)
