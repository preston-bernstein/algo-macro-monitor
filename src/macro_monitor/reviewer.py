"""Weekly hypothesis-review deterministic logic (FR-07/FR-08/FR-09).

This module owns the *deterministic* parts of the review step: the cadence gate, the required-field
validation, and the storage + report-file rendering. It does NOT itself call an LLM — the "LLM
call" is the schedule-skill-hosted Claude Code agent turn that invokes the ``review`` command and
supplies candidate hypotheses. This module is what that turn's output is validated and persisted
through, so that a malformed or injection-shaped hypothesis can never reach disk or the DB (FR-08/
FR-09, and the blast-radius bound of FR-13b).

FR-10/FR-11/FR-12: nothing here invokes /spec-gather, /spec-challenge, a backtest, or any gate
code. The report file + candidate_hypotheses row are terminal artifacts.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

DEFAULT_MODEL = "offline-lane-review"

# FR-09: the literal, first-class overfitting-risk disclosure. Every emitted hypothesis must carry
# this exact statement; a report missing it fails validation and is never written.
OVERFITTING_DISCLOSURE = (
    "OVERFITTING RISK: This is a retrospective, 'I noticed a pattern in the log' hypothesis. "
    "Such a hypothesis carries HIGHER overfitting risk than a textbook anomaly found in the "
    "literature, not lower, and it must clear the same (or a stricter) gate bar before being "
    "taken seriously."
)

MIN_REVIEW_DAYS = 7  # FR-07 default; overridable ONLY to a larger value.


class ReviewError(RuntimeError):
    """Raised when a hypothesis fails required-field validation or the cadence gate blocks a run."""


# --- cadence gate (FR-07) -------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_successful_review(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Most recent review keyed off the last SUCCESSFUL run (status='ok'), not the last started.

    A crashed run (status='failed') must not poison the gate for a week (plan.md).
    """
    return conn.execute(
        "SELECT * FROM review_runs WHERE status = 'ok' AND completed_at IS NOT NULL "
        "ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()


def cadence_ok(conn: sqlite3.Connection, *, min_days: int = MIN_REVIEW_DAYS, now: datetime | None = None) -> bool:
    """True if >= ``min_days`` have elapsed since the last successful review (or none exists)."""
    if min_days < MIN_REVIEW_DAYS:
        raise ReviewError(f"review cadence cannot be shortened below {MIN_REVIEW_DAYS} days (FR-07)")
    last = last_successful_review(conn)
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now - _parse_iso(last["completed_at"]) >= timedelta(days=min_days)


# --- required-field validation (FR-08/FR-09) ------------------------------------------------


def validate_hypothesis(h: dict) -> dict:
    """Validate one candidate hypothesis. Returns a normalised dict or raises ReviewError.

    Required (FR-08/FR-09):
      * ``slug`` matching ^[a-z0-9]+(-[a-z0-9]+)*$
      * ``mechanism_description`` — non-empty
      * ``cited_observation_ids`` — JSON-serialisable list with >= 1 element
      * ``cited_paper_db_summary`` — present (may summarise which paper.db rows grounded it)
      * ``overfitting_disclosure`` — must CONTAIN the literal FR-09 disclosure string
    """
    slug = h.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ReviewError(f"slug must match ^[a-z0-9]+(-[a-z0-9]+)*$, got {slug!r}")

    mechanism = h.get("mechanism_description")
    if not isinstance(mechanism, str) or not mechanism.strip():
        raise ReviewError("mechanism_description is required and must be non-empty")

    cited = h.get("cited_observation_ids")
    if not isinstance(cited, list) or len(cited) < 1:
        raise ReviewError("cited_observation_ids must be a non-empty list")
    if not all(isinstance(x, int) for x in cited):
        raise ReviewError("cited_observation_ids must all be integers")

    summary = h.get("cited_paper_db_summary")
    if summary is None:
        raise ReviewError("cited_paper_db_summary is required")

    disclosure = h.get("overfitting_disclosure", "")
    if not isinstance(disclosure, str) or OVERFITTING_DISCLOSURE not in disclosure:
        raise ReviewError("overfitting_disclosure must contain the literal FR-09 disclosure string")

    return {
        "slug": slug,
        "mechanism_description": mechanism.strip(),
        "cited_observation_ids": cited,
        "cited_paper_db_summary": summary,
        "overfitting_disclosure": disclosure,
    }


# --- report rendering + persistence (FR-08/FR-10) -------------------------------------------


def render_report(h: dict, *, observed_dates: list[str] | None = None) -> str:
    """Render the terminal ``reports/<slug>.md`` artifact for one validated hypothesis."""
    dates = observed_dates or []
    lines = [
        f"# Candidate hypothesis: {h['slug']}",
        "",
        f"**Slug (pass directly to `/spec-gather`):** `{h['slug']}`",
        "",
        "## Mechanism",
        "",
        h["mechanism_description"],
        "",
        "## Grounding",
        "",
        f"- Cited observation ids: {', '.join(str(i) for i in h['cited_observation_ids'])}",
    ]
    if dates:
        lines.append(f"- Cited observed_date(s): {', '.join(dates)}")
    lines += [
        f"- paper.db grounding: {json.dumps(h['cited_paper_db_summary'])}",
        "",
        "## Overfitting-risk disclosure (FR-09)",
        "",
        h["overfitting_disclosure"],
        "",
        "---",
        "",
        "This report is a TERMINAL artifact. Evaluating this hypothesis requires a separate, "
        "explicit human/agent action: running `/spec-gather <slug>` in internal-research-service and taking it "
        "through the full /spec-challenge → backtest → gate pipeline unchanged. This tool does not, "
        "and must not, invoke any part of that pipeline itself (FR-10/FR-11/FR-12).",
        "",
    ]
    return "\n".join(lines)


def _observed_dates_for(conn: sqlite3.Connection, obs_ids: list[int]) -> list[str]:
    if not obs_ids:
        return []
    placeholders = ",".join("?" for _ in obs_ids)
    rows = conn.execute(
        f"SELECT DISTINCT observed_date FROM raw_observations WHERE id IN ({placeholders}) "
        "ORDER BY observed_date",
        tuple(obs_ids),
    ).fetchall()
    return [r["observed_date"] for r in rows]


def open_review_run(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
    observations_considered: int,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> int:
    cur = db.execute_write(
        conn,
        "INSERT INTO review_runs"
        "(started_at, status, window_start, window_end, observations_considered, llm_model) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (db.now_iso(), "dry-run" if dry_run else "started", window_start, window_end,
         observations_considered, model),
    )
    conn.commit()
    return cur.lastrowid


def close_review_run(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    db.execute_write(
        conn,
        "UPDATE review_runs SET status = ?, completed_at = ? WHERE id = ?",
        (status, db.now_iso(), run_id),
    )
    conn.commit()


def persist_hypothesis(
    conn: sqlite3.Connection,
    review_run_id: int,
    hypothesis: dict,
    *,
    reports_dir: str,
) -> str:
    """Validate, INSERT the candidate_hypotheses row, and write reports/<slug>.md. Returns path.

    Both side effects (DB row + report file) are the terminal output. Nothing consumes them
    automatically (FR-10). Validation happens first, so a bad hypothesis touches neither.
    """
    h = validate_hypothesis(hypothesis)
    dates = _observed_dates_for(conn, h["cited_observation_ids"])
    db.execute_write(
        conn,
        "INSERT INTO candidate_hypotheses"
        "(review_run_id, slug, mechanism_description, cited_observation_ids, "
        "cited_paper_db_summary, overfitting_disclosure, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            review_run_id,
            h["slug"],
            h["mechanism_description"],
            json.dumps(h["cited_observation_ids"]),
            json.dumps(h["cited_paper_db_summary"]),
            h["overfitting_disclosure"],
            db.now_iso(),
        ),
    )
    conn.commit()

    reports_path = Path(reports_dir)
    reports_path.mkdir(parents=True, exist_ok=True)
    out = reports_path / f"{h['slug']}.md"
    out.write_text(render_report(h, observed_dates=dates))
    return str(out)


def observations_since(conn: sqlite3.Connection, since: str | None) -> list[sqlite3.Row]:
    """Accumulated observations (with any correlations) considered by a review window."""
    if since:
        rows = conn.execute(
            "SELECT * FROM raw_observations WHERE observed_date >= ? ORDER BY observed_date, id",
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM raw_observations ORDER BY observed_date, id"
        ).fetchall()
    return list(rows)


def default_window_start(days: int = 7, *, today: date | None = None) -> str:
    today = today or datetime.now(timezone.utc).date()
    return (today - timedelta(days=days)).isoformat()
