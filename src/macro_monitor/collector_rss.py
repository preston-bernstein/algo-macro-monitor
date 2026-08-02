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
from .log import log_event
from .observations import insert_observation, tag_symbols

USER_AGENT = "macro-monitor/0.1 (+https://github.com/preston-bernstein/internal-monitor-service)"
FETCH_TIMEOUT = 15.0


@dataclass
class FeedResult:
    source: str
    ok: bool
    inserted: int = 0
    seen: int = 0
    rejected: int = 0
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
    run_id: str | None = None,
) -> FeedResult:
    """Fetch one feed and append its entries. Never raises on fetch/parse failure (FR-13a).

    ``run_id`` (optional, §18 Correlation) is stamped onto every log line this call emits so a
    feed's fetch/parse/reject events are all findable under the one run that produced them; it is
    optional because the direct unit tests in tests/test_collect_rss.py call this without a CLI
    run_id in scope.
    """
    symbol_universe = symbol_universe or []
    default_date = fallback_date or db.now_iso()[:10]
    try:
        raw = _fetch(url)
    except Exception as exc:  # noqa: BLE001 - FR-13a: any fetch failure is logged + skipped
        error = f"fetch: {type(exc).__name__}: {exc}"
        log_event(
            "error", "collect.fetch_failed", run_id=run_id, source=source,
            err_type=type(exc).__name__, err_msg=str(exc), outcome="failed",
        )
        return FeedResult(source=source, ok=False, error=error)

    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        error = f"parse: {parsed.bozo_exception!r}"
        log_event(
            "error", "collect.parse_failed", run_id=run_id, source=source,
            err_type=type(parsed.bozo_exception).__name__, err_msg=str(parsed.bozo_exception),
            outcome="failed",
        )
        return FeedResult(source=source, ok=False, error=error)
    if parsed.bozo and parsed.entries:
        # Malformed-but-still-parseable (e.g. a bad XML namespace) previously returned ok=True
        # with zero trace of the malformation. Not fatal — the entries are usable — but worth a
        # WARN so a feed drifting toward "fully broken" is visible before it gets there.
        log_event(
            "warn", "collect.feed_malformed_but_usable", run_id=run_id, source=source,
            err_type=type(parsed.bozo_exception).__name__, err_msg=str(parsed.bozo_exception),
        )

    inserted = 0
    seen = 0
    rejected = 0
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
        except Exception as exc:  # noqa: BLE001 - one malformed entry must not sink the whole feed
            rejected += 1
            # Never log the entry's own title/url verbatim here — it is untrusted third-party RSS
            # content and §18 already treats a raw third-party payload as a logging risk; the
            # exception type + count is enough to know a source has started violating validation.
            log_event(
                "warn", "collect.entry_rejected", run_id=run_id, source=source,
                err_type=type(exc).__name__,
            )
            continue
        if was_new:
            inserted += 1
    return FeedResult(source=source, ok=True, inserted=inserted, seen=seen, rejected=rejected)


def collect_rss(
    conn: sqlite3.Connection,
    *,
    symbol_universe: list[str] | None = None,
    run_id: str | None = None,
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
                run_id=run_id,
            )
        )
    return summary
