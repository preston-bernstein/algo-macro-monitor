"""FR-05/06/18/19 correlator tests (Step 6)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from macro_monitor import correlator
from macro_monitor.collector_websearch_ingest import ingest
from macro_monitor.correlator import build_query, correlate_date
from macro_monitor.validation import ValidationError

# --- build_query: the FR-05 column-name trap -----------------------------------------------


def test_build_query_uses_date_for_targets_and_marks():
    assert "WHERE date = ?" in build_query("targets")
    assert "WHERE date = ?" in build_query("marks")


def test_build_query_uses_run_date_for_gate_results():
    q = build_query("gate_results")
    assert "WHERE run_date = ?" in q
    assert "WHERE date = ?" not in q


def test_build_query_never_selects_proprietary_columns():
    for table in ("targets", "marks", "gate_results"):
        q = build_query(table).lower()
        for forbidden in ("weight", "units", "notional", "fill_quality", "sharpe", "pbo"):
            assert forbidden not in q


def test_build_query_never_interpolates_the_date():
    # Even a metacharacter-laden date must not appear in the SQL text; it fails validation first.
    with pytest.raises(ValidationError):
        build_query("targets", "2026-07-04; DROP TABLE targets;--")


# --- FR-18 fuzz: shell/SQL metacharacters rejected before any paper.db access ----------------


@pytest.mark.parametrize(
    "evil",
    [
        "2026-07-04; rm -rf /",
        "2026-07-04$(whoami)",
        "2026-07-04`id`",
        '2026-07-04" OR "1"="1',
        "'; DROP TABLE targets;--",
        "$(curl evil)",
    ],
)
def test_correlate_rejects_metacharacter_date(log_conn, paper_db, evil):
    with pytest.raises(ValidationError):
        correlate_date(log_conn, evil, paper_db_path=paper_db)


# --- FR-05: exact date matching, no bleed from other dates ----------------------------------


def test_correlate_matches_only_the_requested_date(log_conn, paper_db):
    obs_id, _ = ingest(log_conn, "2026-07-04", "websearch-wsb", "https://x/1", "SPY news", ["SPY"])
    results = correlate_date(log_conn, "2026-07-04", paper_db_path=paper_db)
    assert len(results) == 1
    res = results[0]
    # fixture has 2 targets, 2 marks, 1 gate_results on 2026-07-04 (decoys are 2026-07-01).
    assert res.tables == {"targets": 2, "marks": 2, "gate_results": 1}

    stored = log_conn.execute(
        "SELECT paper_db_table, row_json FROM correlations WHERE observation_id = ? "
        "ORDER BY paper_db_table",
        (obs_id,),
    ).fetchall()
    by_table = {r["paper_db_table"]: json.loads(r["row_json"]) for r in stored}
    # Every stored row's date is the requested date — no 2026-07-01 decoy leaked in.
    for row in by_table["targets"]:
        assert row["date"] == "2026-07-04"
    for row in by_table["marks"]:
        assert row["date"] == "2026-07-04"
    for row in by_table["gate_results"]:
        assert row["run_date"] == "2026-07-04"


def test_correlate_row_json_has_no_proprietary_fields(log_conn, paper_db):
    ingest(log_conn, "2026-07-04", "websearch-wsb", "https://x/1", "SPY news", ["SPY"])
    correlate_date(log_conn, "2026-07-04", paper_db_path=paper_db)
    for (row_json,) in log_conn.execute("SELECT row_json FROM correlations"):
        for row in json.loads(row_json):
            assert not (
                set(row) & {"weight", "units", "notional", "fill_quality", "price", "sharpe"}
            ), f"FR-19 leak: {row}"


def test_correlate_gate_results_captures_passed(log_conn, paper_db):
    ingest(log_conn, "2026-07-04", "websearch-wsb", "https://x/1", "SPY news", ["SPY"])
    correlate_date(log_conn, "2026-07-04", paper_db_path=paper_db)
    gr = log_conn.execute(
        "SELECT row_json FROM correlations WHERE paper_db_table = 'gate_results'"
    ).fetchone()
    rows = json.loads(gr["row_json"])
    assert rows[0]["passed"] == 1
    assert rows[0]["strategy"] == "strategy_a"


def test_correlate_dedup_on_rerun(log_conn, paper_db):
    ingest(log_conn, "2026-07-04", "websearch-wsb", "https://x/1", "SPY news", ["SPY"])
    r1 = correlate_date(log_conn, "2026-07-04", paper_db_path=paper_db)
    r2 = correlate_date(log_conn, "2026-07-04", paper_db_path=paper_db)
    assert r1[0].written == 3  # targets + marks + gate_results
    assert r2[0].written == 0  # UNIQUE(observation_id, paper_db_table) — no dup rows
    count = log_conn.execute("SELECT COUNT(*) c FROM correlations").fetchone()["c"]
    assert count == 3


def test_correlate_no_observation_is_empty(log_conn, paper_db):
    assert correlate_date(log_conn, "2026-07-04", paper_db_path=paper_db) == []


# --- FR-06: read-only open --------------------------------------------------------------------


def test_paper_db_opened_read_only(paper_db):
    conn = correlator._connect_ro(paper_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO marks VALUES ('2026-07-04','SPY',1.0)")
    conn.close()


def test_missing_snapshot_raises_correlation_error(log_conn, tmp_path):
    from macro_monitor.collector_websearch_ingest import ingest as _ing

    _ing(log_conn, "2026-07-04", "websearch-wsb", "https://x/1", "SPY", ["SPY"])
    with pytest.raises(correlator.CorrelationError):
        correlate_date(log_conn, "2026-07-04", paper_db_path=str(tmp_path / "nope.db"))
