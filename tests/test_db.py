"""Schema + connection helper tests (Step 2)."""

from __future__ import annotations

import sqlite3

import pytest

from macro_monitor import db


def test_init_db_creates_all_tables():
    conn = db.init_db(":memory:")
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "sources",
        "raw_observations",
        "correlations",
        "review_runs",
        "candidate_hypotheses",
    } <= names


def test_dedup_key_is_stable_and_source_url_derived():
    k1 = db.dedup_key("fed-press", "https://x/1")
    k2 = db.dedup_key("fed-press", "https://x/1")
    k3 = db.dedup_key("wsj", "https://x/1")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64  # sha256 hexdigest


def test_foreign_keys_enforced(log_conn):
    # raw_observations.source REFERENCES sources(name); an unknown source must be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute_write(
            log_conn,
            "INSERT INTO raw_observations"
            "(observed_date, source, url, title_or_snippet, collected_at, dedup_key) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-07-04", "no-such-source", "https://x/1", "t", db.now_iso(), "k1"),
        )


def test_json_valid_check_on_tagged_symbols(log_conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute_write(
            log_conn,
            "INSERT INTO raw_observations"
            "(observed_date, source, url, title_or_snippet, tagged_symbols, collected_at, dedup_key) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-07-04", "fed-press", "https://x/1", "t", "not-json", db.now_iso(), "k2"),
        )
