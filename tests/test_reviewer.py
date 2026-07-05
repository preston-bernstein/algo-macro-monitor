"""FR-07/08/09 reviewer tests (Step 9)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from macro_monitor import db, reviewer
from macro_monitor.reviewer import (
    OVERFITTING_DISCLOSURE,
    ReviewError,
    cadence_ok,
    persist_hypothesis,
    validate_hypothesis,
)


def _good_hypothesis():
    return {
        "slug": "fed-presser-day-cross-asset-trend-drift",
        "mechanism_description": "On FOMC press-release days, cross_asset_trend targets drifted.",
        "cited_observation_ids": [1],
        "cited_paper_db_summary": {"targets": 2, "gate_results": 1},
        "overfitting_disclosure": OVERFITTING_DISCLOSURE,
    }


# --- FR-08/FR-09 validation ------------------------------------------------------------------


def test_valid_hypothesis_passes():
    out = validate_hypothesis(_good_hypothesis())
    assert out["slug"] == "fed-presser-day-cross-asset-trend-drift"


@pytest.mark.parametrize("bad_slug", ["Bad Slug", "has_underscore", "-leading", "UPPER", "a--b", ""])
def test_bad_slug_rejected(bad_slug):
    h = _good_hypothesis()
    h["slug"] = bad_slug
    with pytest.raises(ReviewError):
        validate_hypothesis(h)


def test_missing_cited_observations_rejected():
    h = _good_hypothesis()
    h["cited_observation_ids"] = []
    with pytest.raises(ReviewError):
        validate_hypothesis(h)


def test_missing_overfitting_disclosure_rejected():
    h = _good_hypothesis()
    h["overfitting_disclosure"] = "some other text"
    with pytest.raises(ReviewError):
        validate_hypothesis(h)


def test_empty_mechanism_rejected():
    h = _good_hypothesis()
    h["mechanism_description"] = "   "
    with pytest.raises(ReviewError):
        validate_hypothesis(h)


# --- FR-07 cadence gate ----------------------------------------------------------------------


def test_cadence_ok_when_no_prior_review():
    conn = db.init_db(":memory:")
    assert cadence_ok(conn) is True


def test_cadence_blocks_within_7_days():
    conn = db.init_db(":memory:")
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0).isoformat()
    db.execute_write(
        conn,
        "INSERT INTO review_runs(started_at, completed_at, status, window_start, window_end, "
        "observations_considered, llm_model) VALUES (?,?,?,?,?,?,?)",
        (recent, recent, "ok", "2026-06-01", "2026-06-08", 10, "m"),
    )
    conn.commit()
    assert cadence_ok(conn) is False


def test_cadence_ignores_failed_runs():
    # A crashed run must not poison the gate (plan.md).
    conn = db.init_db(":memory:")
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    db.execute_write(
        conn,
        "INSERT INTO review_runs(started_at, completed_at, status, window_start, window_end, "
        "observations_considered, llm_model) VALUES (?,?,?,?,?,?,?)",
        (recent, recent, "failed", "2026-06-01", "2026-06-08", 10, "m"),
    )
    conn.commit()
    assert cadence_ok(conn) is True


def test_cadence_cannot_be_shortened():
    conn = db.init_db(":memory:")
    with pytest.raises(ReviewError):
        cadence_ok(conn, min_days=3)


# --- FR-08/FR-10 persistence + report ---------------------------------------------------------


def test_persist_writes_row_and_report(tmp_path):
    conn = db.init_db(":memory:")
    from macro_monitor import sources
    from macro_monitor.collector_websearch_ingest import ingest

    sources.add_source(conn, "websearch-wsb", "websearch", "q", "2026-07-05")
    ingest(conn, "2026-07-04", "websearch-wsb", "https://x/1", "SPY", ["SPY"])
    run_id = reviewer.open_review_run(
        conn, window_start="2026-06-28", window_end="2026-07-05", observations_considered=1
    )
    path = persist_hypothesis(conn, run_id, _good_hypothesis(), reports_dir=str(tmp_path))

    row = conn.execute("SELECT slug FROM candidate_hypotheses").fetchone()
    assert row["slug"] == "fed-presser-day-cross-asset-trend-drift"
    text = open(path).read()
    assert "fed-presser-day-cross-asset-trend-drift" in text
    assert OVERFITTING_DISCLOSURE in text
    assert "2026-07-04" in text  # cited observed_date rendered


def test_bad_hypothesis_writes_nothing(tmp_path):
    conn = db.init_db(":memory:")
    run_id = reviewer.open_review_run(
        conn, window_start="2026-06-28", window_end="2026-07-05", observations_considered=0
    )
    bad = _good_hypothesis()
    bad["overfitting_disclosure"] = "missing"
    with pytest.raises(ReviewError):
        persist_hypothesis(conn, run_id, bad, reports_dir=str(tmp_path))
    assert conn.execute("SELECT COUNT(*) c FROM candidate_hypotheses").fetchone()["c"] == 0
    assert not list(tmp_path.glob("*.md"))
