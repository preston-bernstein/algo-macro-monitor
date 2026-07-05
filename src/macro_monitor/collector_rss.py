"""FR-01 deterministic RSS/Atom collection — plain HTTP fetch + XML parse, NO LLM.

Fetch is done with httpx (explicit timeout, redirects disabled — an RSS URL that redirects to an
internal/private host is an SSRF vector, plan.md Risk areas) and parsing with feedparser on the
already-downloaded bytes (so feedparser never does its own network fetch). A feed that times out,
returns non-2xx, or fails to parse is logged and skipped, never fatal (FR-13a).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import feedparser
import httpx

from . import db, sources
from .observations import insert_observation, tag_symbols

USER_AGENT = "macro-monitor/0.1 (+https://github.com/preston-bernstein/algo-macro-monitor)"
FETCH_TIMEOUT = 15.0


@dataclass
class FeedResult:
    source: str
    ok: bool
    inserted: int = 0
    seen: int = 0
    error: str | None = None


@dataclass
class CollectSummary:
    results: list[FeedResult] = field(default_factory=list)

    @property
    def any_success(self) -> bool:
        return any(r.ok for r in self.results)

    @property
    def total_inserted(self) -> int:
        return sum(r.inserted for r in self.results)


def _fetch(url: str) -> bytes:
    """Fetch feed bytes. Redirects disabled to avoid SSRF into private hosts (plan.md Risk areas)."""
    resp = httpx.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,
    )
    resp.raise_for_status()
    return resp.content


def _entry_date(entry) -> str | None:
    """Best-effort YYYY-MM-DD from a feed entry's published/updated struct_time."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return f"{parsed.tm_year:04d}-{parsed.tm_mon:02d}-{parsed.tm_mday:02d}"
    return None


def fetch_and_log_rss(
    conn: sqlite3.Connection,
    source: str,
    url: str,
    *,
    symbol_universe: list[str] | None = None,
    fallback_date: str | None = None,
) -> FeedResult:
    """Fetch one feed and append its entries. Never raises on fetch/parse failure (FR-13a)."""
    symbol_universe = symbol_universe or []
    default_date = fallback_date or db.now_iso()[:10]
    try:
        raw = _fetch(url)
    except Exception as exc:  # noqa: BLE001 - FR-13a: any fetch failure is logged + skipped
        return FeedResult(source=source, ok=False, error=f"fetch: {type(exc).__name__}: {exc}")

    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        return FeedResult(source=source, ok=False, error=f"parse: {parsed.bozo_exception!r}")

    inserted = 0
    seen = 0
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        title = getattr(entry, "title", None) or getattr(entry, "summary", None)
        if not link or not title:
            continue
        seen += 1
        observed_date = _entry_date(entry) or default_date
        symbols = tag_symbols(title, symbol_universe)
        try:
            _, was_new = insert_observation(
                conn,
                observed_date=observed_date,
                source=source,
                url=link,
                title_or_snippet=title,
                tagged_symbols=symbols,
            )
        except Exception:  # noqa: BLE001 - one malformed entry must not sink the whole feed
            continue
        if was_new:
            inserted += 1
    return FeedResult(source=source, ok=True, inserted=inserted, seen=seen)


def collect_rss(
    conn: sqlite3.Connection, *, symbol_universe: list[str] | None = None
) -> CollectSummary:
    """Run FR-01 collection over every pollable RSS source. Exit-code policy lives in the CLI."""
    summary = CollectSummary()
    for row in sources.pollable_rss_sources(conn):
        summary.results.append(
            fetch_and_log_rss(
                conn,
                row["name"],
                row["url_or_query"],
                symbol_universe=symbol_universe,
            )
        )
    return summary
