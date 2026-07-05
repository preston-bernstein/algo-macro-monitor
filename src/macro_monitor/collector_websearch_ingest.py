"""The ``ingest`` write path (FR-04/FR-14/FR-20).

This is what the WebSearch-driven slash-command shells out to, once per result. It contains NO
WebSearch call itself — WebSearch is a Claude Code tool that lives in the slash-command, not in
installable Python. This module only validates and appends a single observation.
"""

from __future__ import annotations

import sqlite3

from .observations import insert_observation


def ingest(
    conn: sqlite3.Connection,
    observed_date: str,
    source: str,
    url: str,
    title_or_snippet: str,
    symbols: list[str] | None = None,
) -> tuple[int | None, bool]:
    """Validate + append one observation. Returns (row_id, inserted).

    Re-ingesting an identical (source, url) is a no-op (dedup), never an overwrite (FR-14).
    Uses the exact same ``dedup_key`` formula as collect-rss (shared in db.py).
    """
    return insert_observation(
        conn,
        observed_date=observed_date,
        source=source,
        url=url,
        title_or_snippet=title_or_snippet,
        tagged_symbols=symbols,
    )
