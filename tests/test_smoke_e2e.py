"""End-to-end smoke test: real RSS feed -> ingest -> correlate against a fixture paper.db.

The correlate leg runs against the schema-faithful ``paper_db`` fixture rather than the real
/srv/paper-share/paper.db snapshot: the snapshot is readable only by the ``internal-monitor-service`` service
user (paper-readers group), which the test runner is not. The real-snapshot leg is exercised by
scripts/smoke_e2e.sh under the deployed service user (see docs/DECISIONS.md).

The live-feed leg is network-gated: if the feed is unreachable from the sandbox it is skipped, not
failed.
"""

from __future__ import annotations

import pytest

from macro_monitor import collector_rss, db, sources
from macro_monitor.correlator import correlate_date

LIVE_FEED = "https://www.federalreserve.gov/feeds/press_all.xml"


@pytest.mark.network
def test_live_feed_collect_then_correlate(tmp_path, paper_db):
    conn = db.init_db(str(tmp_path / "macro_monitor.db"))
    sources.add_source(conn, "fed-press", "rss", LIVE_FEED, "2026-07-05")
    try:
        collector_rss._fetch(LIVE_FEED)  # reachability probe
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live feed unreachable from sandbox: {exc}")

    # Parse the real feed and log it (no LLM anywhere in this path).
    result = collector_rss.fetch_and_log_rss(
        conn, "fed-press", LIVE_FEED, symbol_universe=["SPY", "TLT", "GLD"]
    )
    assert result.ok is True
    assert result.seen >= 1  # FR-01: a parseable feed with at least one entry

    # Correlate one known fixture date end-to-end (report-shaped output can be produced).
    from macro_monitor.collector_websearch_ingest import ingest

    ingest(conn, "2026-07-04", "fed-press", "https://x/e2e", "SPY macro note", ["SPY"])
    results = correlate_date(conn, "2026-07-04", paper_db_path=paper_db)
    assert results and results[0].written == 3
