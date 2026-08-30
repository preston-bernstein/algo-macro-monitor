"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3

import pytest

from macro_monitor import db, sources

# A schema-faithful paper.db fixture (a real deployment's schema shape, generic strategy names):
#   marks(date, symbol, price)
#   targets(date, strategy, symbol, weight, units, notional, fill_quality)
#   gate_results(run_date, strategy, passed, sharpe, sr_oos, mintrl, dsr, pbo, mc_prob_profit, reasons)
PAPER_DB_SCHEMA = """
CREATE TABLE marks(date TEXT, symbol TEXT, price REAL);
CREATE TABLE targets(
    date TEXT, strategy TEXT, symbol TEXT,
    weight REAL, units REAL, notional REAL, fill_quality REAL
);
CREATE TABLE gate_results(
    run_date TEXT, strategy TEXT, passed INTEGER,
    sharpe REAL, sr_oos REAL, mintrl REAL, dsr REAL, pbo REAL,
    mc_prob_profit REAL, reasons TEXT
);
"""


@pytest.fixture
def log_conn() -> sqlite3.Connection:
    """An in-memory macro_monitor.db with one usable source row."""
    conn = db.init_db(":memory:")
    sources.add_source(
        conn, "fed-press", "rss", "https://example.test/fed.xml", "2026-07-05"
    )
    sources.add_source(
        conn, "websearch-wsb", "websearch", "site:reddit.com {symbol}", "2026-07-05"
    )
    return conn


@pytest.fixture
def paper_db(tmp_path) -> str:
    """A schema-faithful paper.db fixture with rows for 2026-07-04 and a decoy 2026-07-01."""
    path = tmp_path / "paper.db"
    conn = sqlite3.connect(path)
    conn.executescript(PAPER_DB_SCHEMA)
    conn.executemany(
        "INSERT INTO targets VALUES (?,?,?,?,?,?,?)",
        [
            ("2026-07-04", "strategy-a", "SPY", 0.25, 100, 55000.0, 0.98),
            ("2026-07-04", "strategy-b", "TLT", -0.10, 40, 4000.0, 0.95),
            ("2026-07-01", "strategy-a", "SPY", 0.30, 120, 66000.0, 0.97),  # decoy date
        ],
    )
    conn.executemany(
        "INSERT INTO marks VALUES (?,?,?)",
        [
            ("2026-07-04", "SPY", 550.12),
            ("2026-07-04", "TLT", 92.30),
            ("2026-07-01", "SPY", 548.00),  # decoy date
        ],
    )
    conn.executemany(
        "INSERT INTO gate_results VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-07-04", "strategy-a", 1, 1.2, 0.9, 12, 0.8, 0.1, 0.7, "ok"),
            ("2026-07-01", "strategy-a", 0, 0.3, 0.1, 4, 0.2, 0.6, 0.4, "fail"),  # decoy
        ],
    )
    conn.commit()
    conn.close()
    # mode=ro clients require the file to already exist (it does).
    return str(path)
