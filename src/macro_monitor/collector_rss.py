"""FR-01 deterministic RSS/Atom collection — plain HTTP fetch + XML parse, NO LLM.

Fetch and parse are delegated to feed-commons' ``poll()`` (explicit timeout, redirects disabled,
HTTPS-only, 10MB response cap — an RSS URL that redirects to an internal/private host is an SSRF
vector, plan.md Risk areas). A feed that times out, returns non-2xx, or fails to parse raises
``PollError``, which is logged and skipped, never fatal (FR-13a).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from feed_commons import PollError, poll

from . import db, sources
from .log import log_event
from .observations import insert_observation, tag_symbols

FETCH_TIMEOUT = 15.0
EXCERPT_MAX_LENGTH = 300


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
        items = poll(url, excerpt_max_length=EXCERPT_MAX_LENGTH, timeout_seconds=FETCH_TIMEOUT)
    except PollError as exc:  # FR-13a: any poll() failure is logged + skipped, never fatal
        error = f"poll: {exc.code}"
        log_event(
            "error", "collect.fetch_failed", run_id=run_id, source=source,
            err_type=exc.code,
            err_msg=str(exc.__cause__) if exc.__cause__ is not None else str(exc),
            outcome="failed",
        )
        return FeedResult(source=source, ok=False, error=error)
    except Exception as exc:  # noqa: BLE001 - belt-and-suspenders safety net (see note below)
        # The narrow `except PollError` above gives rich, code-based handling and logging for
        # feed-commons' own anticipated failure modes, but the "never raises" invariant (FR-13a)
        # must not depend entirely on a sibling repo's exception discipline being complete — this
        # broad fallback is the belt-and-suspenders safety net that keeps one unanticipated bug in
        # feed-commons from crashing the whole unattended collection run.
        error = "poll: network_error"
        log_event(
            "error", "collect.fetch_failed", run_id=run_id, source=source,
            err_type="network_error", err_msg=str(exc), outcome="failed",
        )
        return FeedResult(source=source, ok=False, error=error)

    inserted = 0
    seen = 0
    rejected = 0
    for item in items:
        seen += 1
        title = item["title"]
        pub_date = item["pub_date"]
        observed_date = pub_date[:10] if pub_date else default_date
        symbols = tag_symbols(title, symbol_universe)
        try:
            _, was_new = insert_observation(
                conn,
                observed_date=observed_date,
                source=source,
                url=item["link"],
                title_or_snippet=title,
                tagged_symbols=symbols,
            )
        except Exception as exc:  # noqa: BLE001 - one malformed entry must not sink the whole feed
            rejected += 1
            # Never log the entry's own title/url verbatim here -- untrusted third-party RSS
            # content, same rationale as today.
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
