"""Read-only correlation of observations with paper.db (FR-05/FR-06/FR-17/FR-18/FR-19).

Read path (FR-17, per docs/DECISIONS.md — supersedes plan.md's SSH-forced-command text): the
correlator does a plain LOCAL read of the transaction-consistent snapshot at
``/srv/paper-share/paper.db``, which the ``internal-monitor-service`` service user can read via its
``paper-readers`` group membership. No SSH, no sudo, no traversal into /home/internal-research-service. Because
there is no shell/subprocess/SSH layer at all, FR-18's "never build a command layer via string
interpolation" concern collapses to a single rule that is enforced here: the only externally
derived value that touches the query — ``observed_date`` — is strictly validated (YYYY-MM-DD +
real calendar date) and then passed ONLY as a bound SQL parameter (``?``), never interpolated.

FR-06: the snapshot is opened ``mode=ro`` via SQLite URI; a write-mode open is refused. FR-19: the
SELECT lists only minimal, non-proprietary columns per table — never weight/units/notional (nor the
other strategy-internal metric columns) — enforced both by a static per-table allowlist and by a
belt-and-suspenders assertion against a forbidden-column set.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import db
from .validation import ValidationError, validate_observed_date

DEFAULT_PAPER_DB = "/srv/paper-share/paper.db"

# The date column differs per table — targets/marks use "date", gate_results uses "run_date".
# The query builder keys off the table name to pick the right one; it never assumes they match.
DATE_COLUMN = {
    "targets": "date",
    "marks": "date",
    "gate_results": "run_date",
}

# FR-19: minimal, non-proprietary fields only. Symbol/date/strategy establish the correlation;
# gate_results.passed is the pass/fail bit. NOTHING here selects weight/units/notional or the
# proprietary metric columns (sharpe/dsr/pbo/...). Column names are fixed literals, never input.
MINIMAL_COLUMNS = {
    "targets": ("date", "strategy", "symbol"),
    "marks": ("date", "symbol"),
    "gate_results": ("run_date", "strategy", "passed"),
}

# Belt-and-suspenders: even a future edit to MINIMAL_COLUMNS can never leak these.
FORBIDDEN_COLUMNS = frozenset(
    {
        "weight",
        "units",
        "notional",
        "fill_quality",
        "price",
        "sharpe",
        "sr_oos",
        "mintrl",
        "dsr",
        "pbo",
        "mc_prob_profit",
        "reasons",
    }
)

TABLES = tuple(MINIMAL_COLUMNS.keys())


class CorrelationError(RuntimeError):
    """Raised when the paper.db read cannot complete; leaves correlations untouched for that date."""


def build_query(table: str, observed_date: str | None = None) -> str:
    """Build the minimal-field, parameterized SELECT for one table.

    ``observed_date`` is accepted for signature symmetry and, when provided, is validated — but it
    is NEVER interpolated into the returned SQL: the WHERE clause uses a ``?`` placeholder. The
    returned string always references the correct per-table date column (the FR-05 trap).
    """
    if table not in MINIMAL_COLUMNS:
        raise ValidationError(f"unknown paper.db table: {table!r}")
    if observed_date is not None:
        validate_observed_date(observed_date)
    cols = MINIMAL_COLUMNS[table]
    leaked = set(cols) & FORBIDDEN_COLUMNS
    if leaked:  # pragma: no cover - guards against a future edit to MINIMAL_COLUMNS
        # An explicit raise, not `assert` — `assert` is stripped entirely under `python -O`,
        # which would silently delete this FR-19 guard from optimized bytecode.
        raise CorrelationError(f"FR-19 violation: {leaked} selected from {table}")
    date_col = DATE_COLUMN[table]
    return f"SELECT {', '.join(cols)} FROM {table} WHERE {date_col} = ?"  # noqa: S608 - fixed literals only


def _connect_ro(paper_db_path: str) -> sqlite3.Connection:
    """Open paper.db strictly read-only (FR-06). A write attempt on this handle will fail."""
    uri = f"file:{paper_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError as exc:
        raise CorrelationError(f"cannot open paper.db read-only at {paper_db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class CorrelationResult:
    observation_id: int
    written: int  # number of correlations rows inserted this call
    tables: dict[str, int]  # per-table row counts fetched from paper.db


def _fetch_table_rows(
    paper: sqlite3.Connection, table: str, observed_date: str
) -> list[dict]:
    query = build_query(table, observed_date)
    try:
        rows = paper.execute(query, (observed_date,)).fetchall()
    except sqlite3.OperationalError as exc:
        raise CorrelationError(f"query failed on {table}: {exc}") from exc
    result = []
    for row in rows:
        d = {k: row[k] for k in row.keys()}
        # Final guard: no forbidden field can appear in what we persist (FR-19). An explicit
        # raise, not `assert` — see build_query's identical fix for why.
        leaked = set(d) & FORBIDDEN_COLUMNS
        if leaked:
            raise CorrelationError(f"FR-19 leak in {table}: {leaked}")
        result.append(d)
    return result


def correlate_date(
    conn: sqlite3.Connection,
    observed_date: str,
    *,
    paper_db_path: str = DEFAULT_PAPER_DB,
) -> list[CorrelationResult]:
    """Correlate every observation on ``observed_date`` with paper.db rows for that date.

    Writes one ``correlations`` row per (observation, table) with a non-empty match. Re-running for
    an already-correlated date inserts nothing new (UNIQUE(observation_id, paper_db_table)). On any
    read failure the correlations table is left untouched for that date (never a partial write).
    """
    observed_date = validate_observed_date(observed_date)
    obs = conn.execute(
        "SELECT id FROM raw_observations WHERE observed_date = ? ORDER BY id",
        (observed_date,),
    ).fetchall()
    if not obs:
        return []

    # Fetch paper.db rows once for the date, then attach to every matching observation.
    paper = _connect_ro(paper_db_path)
    try:
        per_table = {t: _fetch_table_rows(paper, t, observed_date) for t in TABLES}
    finally:
        paper.close()

    fetched_at = db.now_iso()
    results: list[CorrelationResult] = []
    for row in obs:
        obs_id = row["id"]
        written = 0
        counts: dict[str, int] = {}
        for table in TABLES:
            table_rows = per_table[table]
            counts[table] = len(table_rows)
            if not table_rows:
                continue
            cur = db.execute_write(
                conn,
                "INSERT OR IGNORE INTO correlations"
                "(observation_id, paper_db_table, row_json, fetched_at) VALUES (?, ?, ?, ?)",
                (obs_id, table, json.dumps(table_rows), fetched_at),
            )
            written += cur.rowcount
        results.append(CorrelationResult(observation_id=obs_id, written=written, tables=counts))
    conn.commit()
    return results


def observed_dates_since(conn: sqlite3.Connection, since: str) -> list[str]:
    """Distinct ``observed_date`` values in ``raw_observations``, ``>= since``, ascending.

    Exists because the daily systemd timer's original ``correlate --date "$(date -u
    +%Y-%m-%d)"`` invocation is very likely a **permanent no-op**: ``collect-rss`` stamps every
    observation with the *feed entry's own* published/updated date (``collector_rss._entry_date``),
    not the date the job happened to run on, and the timer fires at 06:30 UTC — the middle of the
    US business night — before that UTC calendar date's own news exists anywhere. Confirmed against
    the live deployment 2026-08-01: ``raw_observations`` has rows through 2026-07-31 only, while
    both the 2026-07-31 and 2026-08-01 runs logged "no observations for <that day's date>", and
    ``correlations`` holds exactly 2 rows total across the entire deployment's history — both from
    a one-off manual DECISIONS.md verification against a date chosen to match, never from the
    timer. Every automated day has silently correlated nothing since deploy.

    The fix: the CLI's default ``correlate`` invocation (no ``--date``) now walks every distinct
    observed_date from a trailing window instead of asserting one exact date. ``correlate_date`` is
    idempotent (``INSERT OR IGNORE`` + ``UNIQUE(observation_id, paper_db_table)``), so re-covering
    an already-correlated date here is a cheap no-op, not a correctness risk.
    """
    since = validate_observed_date(since, field_name="since")
    rows = conn.execute(
        "SELECT DISTINCT observed_date FROM raw_observations WHERE observed_date >= ? "
        "ORDER BY observed_date",
        (since,),
    ).fetchall()
    return [r["observed_date"] for r in rows]
