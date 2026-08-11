"""SQLite schema + connection helpers for the local macro_monitor.db log.

Mirrors the plain-stdlib-sqlite3 + WAL pattern used by algo_factory.research.ledger (read as
prior art only — this repo has zero package dependency on algo_factory). This module owns:

* the append-only schema (FR-04/FR-14),
* the single canonical ``dedup_key`` formula shared by every collector (plan.md: defining it
  twice would silently defeat cross-source dedup while each collector's own test still passed),
* connection setup that actually turns on the guarantees the schema's REFERENCES clauses and the
  two-writer topology depend on: ``PRAGMA foreign_keys=ON`` and ``PRAGMA busy_timeout`` plus an
  app-level retry-with-backoff on SQLITE_BUSY.

Note: this module concerns ONLY the local log DB. paper.db is opened read-only elsewhere, in
correlator.py, and never through this module.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .log import log_event

# --- schema ---------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    name         TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('rss','websearch')),
    url_or_query TEXT NOT NULL,
    fetchable    INTEGER NOT NULL CHECK (fetchable IN (0,1)),
    checked_on   TEXT NOT NULL,                 -- YYYY-MM-DD; NOT NULL is the FR-03 spot-check gate
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS raw_observations(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_date    TEXT NOT NULL,
    source           TEXT NOT NULL REFERENCES sources(name),
    url              TEXT NOT NULL,
    title_or_snippet TEXT NOT NULL,
    tagged_symbols   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tagged_symbols)),
    collected_at     TEXT NOT NULL,
    dedup_key        TEXT NOT NULL UNIQUE        -- sha256(source||url); re-collecting is a no-op
);
CREATE INDEX IF NOT EXISTS ix_raw_obs_date ON raw_observations(observed_date);

CREATE TABLE IF NOT EXISTS correlations(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES raw_observations(id),
    paper_db_table TEXT NOT NULL CHECK (paper_db_table IN ('targets','marks','gate_results')),
    row_json       TEXT NOT NULL CHECK (json_valid(row_json)),  -- MINIMAL fields only (FR-19)
    fetched_at     TEXT NOT NULL,
    UNIQUE(observation_id, paper_db_table)
);
CREATE INDEX IF NOT EXISTS ix_corr_obs ON correlations(observation_id);

CREATE TABLE IF NOT EXISTS review_runs(
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at               TEXT NOT NULL,
    completed_at             TEXT,
    status                   TEXT NOT NULL DEFAULT 'started'
                             CHECK (status IN ('started','ok','failed','dry-run')),
    window_start             TEXT NOT NULL,
    window_end               TEXT NOT NULL,
    observations_considered  INTEGER NOT NULL,
    llm_model                TEXT NOT NULL,
    llm_tokens_in            INTEGER,
    llm_tokens_out           INTEGER
);

CREATE TABLE IF NOT EXISTS candidate_hypotheses(
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    review_run_id          INTEGER NOT NULL REFERENCES review_runs(id),
    slug                   TEXT NOT NULL UNIQUE,
    mechanism_description  TEXT NOT NULL,
    cited_observation_ids  TEXT NOT NULL CHECK (json_valid(cited_observation_ids)),
    cited_paper_db_summary TEXT NOT NULL CHECK (json_valid(cited_paper_db_summary)),
    overfitting_disclosure TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'proposed'
                           CHECK (status IN ('proposed','spec-gathered','rejected','deferred')),
    created_at             TEXT NOT NULL
);
"""


# --- time / hashing helpers -----------------------------------------------------------------


def now_iso() -> str:
    """Current UTC timestamp, ISO8601, second precision. Single source so tests can monkeypatch."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dedup_key(source: str, url: str) -> str:
    """Canonical dedup key. sha256(source||url).

    Defined ONCE, here — every collector (collect-rss and ingest) imports this. A divergent
    per-collector formula would silently defeat cross-source dedup (plan.md).
    """
    return hashlib.sha256(f"{source}{url}".encode()).hexdigest()


# --- connection setup -----------------------------------------------------------------------


BUSY_RETRIES = 5


def connect(path: str) -> sqlite3.Connection:
    """Open the local log DB with the pragmas the schema's guarantees actually depend on.

    * ``foreign_keys=ON`` — SQLite ignores REFERENCES clauses otherwise.
    * ``journal_mode=WAL`` — concurrent readers alongside the two independent writer processes.
    * ``busy_timeout`` — WAL still serializes writers; the systemd-timer process and the
      schedule-skill process are two OS processes that can genuinely overlap. Without this an
      INSERT can fail outright on collision and silently break the FR-14 append guarantee.
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: str) -> sqlite3.Connection:
    """Create all tables if absent and return an open connection."""
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def execute_write(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Run a write statement with retry-with-backoff on SQLITE_BUSY (two-writer topology)."""
    retries = BUSY_RETRIES
    delay = 0.05
    last: sqlite3.OperationalError | None = None
    for _ in range(retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:  # pragma: no cover - timing dependent
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            last = exc
            time.sleep(delay)
            delay *= 2
    assert last is not None  # pragma: no cover
    # Retry exhaustion on the documented two-writer-process overlap (module docstring) — loud via
    # the re-raise below (non-zero exit), but also surfaced as its own queryable §18 event so it's
    # filterable in the same JSON stream as every other failure class, not just a bare traceback.
    log_event("error", "db.write_busy_exhausted", retries=BUSY_RETRIES, err_msg=str(last))
    raise last
