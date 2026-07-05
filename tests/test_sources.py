"""FR-03 source spot-check gate (Step 7)."""

from __future__ import annotations

import pytest

from macro_monitor import db, sources
from macro_monitor.validation import ValidationError


def test_add_source_requires_checked_on():
    conn = db.init_db(":memory:")
    with pytest.raises(ValidationError):
        sources.add_source(conn, "x", "rss", "https://x.test/f.xml", None)


def test_add_source_with_checked_on_succeeds():
    conn = db.init_db(":memory:")
    sources.add_source(conn, "x", "rss", "https://x.test/f.xml", "2026-07-05")
    rows = sources.list_sources(conn)
    assert [r["name"] for r in rows] == ["x"]


def test_add_source_rejects_bad_kind():
    conn = db.init_db(":memory:")
    with pytest.raises(ValidationError):
        sources.add_source(conn, "x", "twitter", "https://x.test", "2026-07-05")


def test_add_source_rejects_non_date_checked_on():
    conn = db.init_db(":memory:")
    with pytest.raises(ValidationError):
        sources.add_source(conn, "x", "rss", "https://x.test", "not-a-date")


def test_pollable_excludes_not_fetchable(log_conn):
    sources.add_source(
        log_conn, "dead-feed", "rss", "https://dead.test/f.xml", "2026-07-05", fetchable=False
    )
    pollable = {r["name"] for r in sources.pollable_rss_sources(log_conn)}
    assert "fed-press" in pollable
    assert "dead-feed" not in pollable
    assert "websearch-wsb" not in pollable  # websearch kind is not RSS-pollable
