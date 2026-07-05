"""FR-04/FR-14/FR-20 ingest validation + append-only dedup (Step 8)."""

from __future__ import annotations

import pytest

from macro_monitor.collector_websearch_ingest import ingest
from macro_monitor.validation import ValidationError


def test_ingest_happy_path(log_conn):
    row_id, inserted = ingest(
        log_conn, "2026-07-04", "websearch-wsb", "https://x.test/1", "SPY squeeze", ["SPY"]
    )
    assert inserted is True
    assert row_id is not None
    stored = log_conn.execute(
        "SELECT tagged_symbols FROM raw_observations WHERE id = ?", (row_id,)
    ).fetchone()
    assert stored["tagged_symbols"] == '["SPY"]'


def test_ingest_dedup_is_noop_never_overwrite(log_conn):
    id1, ins1 = ingest(log_conn, "2026-07-04", "websearch-wsb", "https://x.test/1", "original", ["SPY"])
    # Re-ingest same (source,url) with different title — must NOT overwrite (FR-14).
    id2, ins2 = ingest(log_conn, "2026-07-04", "websearch-wsb", "https://x.test/1", "TAMPERED", [])
    assert ins1 is True and ins2 is False
    assert id1 == id2
    title = log_conn.execute(
        "SELECT title_or_snippet FROM raw_observations WHERE id = ?", (id1,)
    ).fetchone()["title_or_snippet"]
    assert title == "original"
    count = log_conn.execute("SELECT COUNT(*) c FROM raw_observations").fetchone()["c"]
    assert count == 1


@pytest.mark.parametrize(
    "date,url,title,symbols",
    [
        ("2026-13-04", "https://x.test/1", "t", None),        # bad month
        ("2026-07-32", "https://x.test/1", "t", None),        # bad day
        ("07/04/2026", "https://x.test/1", "t", None),        # wrong format
        ("2026-07-04", "file:///etc/passwd", "t", None),      # non-http scheme
        ("2026-07-04", "javascript:alert(1)", "t", None),     # js scheme
        ("2026-07-04", "https://x.test/1", "", None),         # empty title
        ("2026-07-04", "https://x.test/1", "t", ["SPY; DROP"]),  # non-ticker symbol
        ("2026-07-04", "https://x.test/1", "t", ["../../x"]),    # path-y symbol
    ],
)
def test_ingest_rejects_malformed_before_write(log_conn, date, url, title, symbols):
    with pytest.raises(ValidationError):
        ingest(log_conn, date, "websearch-wsb", url, title, symbols)
    count = log_conn.execute("SELECT COUNT(*) c FROM raw_observations").fetchone()["c"]
    assert count == 0  # nothing written


def test_ingest_oversized_title_rejected(log_conn):
    with pytest.raises(ValidationError):
        ingest(log_conn, "2026-07-04", "websearch-wsb", "https://x.test/1", "x" * 5000, None)
