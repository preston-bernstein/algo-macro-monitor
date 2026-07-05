"""Shared append-only insert for raw_observations (FR-04/FR-14/FR-20).

Both collectors — ``collect-rss`` and ``ingest`` — funnel through ``insert_observation`` so the
validation rules, the dedup_key formula, and the append-only (never-overwrite) semantics are
defined exactly once.
"""

from __future__ import annotations

import json
import sqlite3

from . import db
from .validation import (
    validate_observed_date,
    validate_symbols,
    validate_title,
    validate_url,
)


def insert_observation(
    conn: sqlite3.Connection,
    *,
    observed_date: str,
    source: str,
    url: str,
    title_or_snippet: str,
    tagged_symbols: list[str] | None = None,
    collected_at: str | None = None,
) -> tuple[int | None, bool]:
    """Validate and append one observation. Returns (row_id, inserted).

    ``inserted`` is False when the item was already logged (dedup hit) — a no-op, never an
    overwrite of the existing row's title/url (FR-14). All fields are validated (FR-20) before
    any write; a failure raises ValidationError and writes nothing.
    """
    observed_date = validate_observed_date(observed_date)
    url = validate_url(url)
    title_or_snippet = validate_title(title_or_snippet)
    symbols = validate_symbols(tagged_symbols)
    if not source:
        from .validation import ValidationError

        raise ValidationError("source is required")

    key = db.dedup_key(source, url)
    collected_at = collected_at or db.now_iso()

    cur = db.execute_write(
        conn,
        "INSERT OR IGNORE INTO raw_observations"
        "(observed_date, source, url, title_or_snippet, tagged_symbols, collected_at, dedup_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            observed_date,
            source,
            url,
            title_or_snippet,
            json.dumps(symbols),
            collected_at,
            key,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        # Dedup hit — the row already exists and is left exactly as it was.
        existing = conn.execute(
            "SELECT id FROM raw_observations WHERE dedup_key = ?", (key,)
        ).fetchone()
        return (existing["id"] if existing else None), False
    return cur.lastrowid, True


def tag_symbols(text: str, universe: list[str]) -> list[str]:
    """Match a title/snippet against the configured symbol universe (FR-04 tagged_symbols).

    Deliberately simple word-boundary matching — a quiet empty result on an unknown instrument is
    the accepted failure mode (plan.md symbol-universe-drift risk), never a crash.
    """
    import re

    found: list[str] = []
    upper = text.upper()
    for sym in universe:
        if re.search(rf"(?<![A-Z0-9]){re.escape(sym.upper())}(?![A-Z0-9])", upper):
            found.append(sym.upper())
    return found
