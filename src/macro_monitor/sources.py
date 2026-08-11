"""Source allowlist management (FR-03).

A source is not polled until it has been spot-checked once and recorded with a ``checked_on``
date. ``add_source`` refuses to write a row without one — the app-level gate; the schema's
NOT NULL on ``checked_on`` is defense-in-depth behind it.
"""

from __future__ import annotations

import sqlite3

from . import db
from .validation import ValidationError, validate_observed_date

VALID_KINDS = ("rss", "websearch")


def add_source(
    conn: sqlite3.Connection,
    name: str,
    kind: str,
    url_or_query: str,
    checked_on: str | None,
    *,
    fetchable: bool = True,
    notes: str | None = None,
) -> None:
    """Insert (or replace) a source row. Raises ValidationError if the FR-03 gate is not met."""
    if not name:
        raise ValidationError("source name is required")
    if kind not in VALID_KINDS:
        raise ValidationError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    if not url_or_query:
        raise ValidationError("url_or_query is required")
    if not checked_on:
        raise ValidationError("checked_on is required (FR-03 spot-check gate)")
    # A checked_on must itself be a real YYYY-MM-DD date.
    checked_on = validate_observed_date(checked_on, field_name="checked_on")
    db.execute_write(
        conn,
        "INSERT OR REPLACE INTO sources(name, kind, url_or_query, fetchable, checked_on, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, kind, url_or_query, 1 if fetchable else 0, checked_on, notes),
    )
    conn.commit()


def set_fetchable(
    conn: sqlite3.Connection, name: str, fetchable: bool, checked_on: str
) -> None:
    """Update a source's fetchability + spot-check date after a live re-check (`sources check`)."""
    checked_on = validate_observed_date(checked_on, field_name="checked_on")
    cur = db.execute_write(
        conn,
        "UPDATE sources SET fetchable = ?, checked_on = ? WHERE name = ?",
        (1 if fetchable else 0, checked_on, name),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValidationError(f"no such source: {name!r}")


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM sources ORDER BY name").fetchall())


def pollable_rss_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """RSS sources cleared for polling: kind='rss' AND fetchable=1 (FR-01/FR-03)."""
    return list(
        conn.execute(
            "SELECT * FROM sources WHERE kind = 'rss' AND fetchable = 1 ORDER BY name"
        ).fetchall()
    )
