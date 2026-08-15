# Plan: feed-commons Collector Retrofit

## Approach

Replace `collector_rss.py`'s hand-rolled `_fetch()` (httpx) + `feedparser.parse()` + `_entry_date()`
path with a single call to `feed_commons.poll(url, excerpt_max_length=300, timeout_seconds=15)`,
imported in-process (both packages are Python, same environment — no subprocess boundary is
needed, unlike fashion-monitor's TypeScript case). `poll()` already returns fully-normalized,
title/link-filtered `NormalizedItem`s and raises a bounded `PollError` on every failure mode, so
`fetch_and_log_rss` shrinks to: call `poll()`, catch `PollError`, loop over items calling the
same `tag_symbols()`/`insert_observation()` domain logic as today. Nothing else in the file
(`FeedResult`, `CollectSummary`, `collect_rss`, the public signatures) changes.

## Architecture

```
collect_rss(conn, ...)
    │  for each row in sources.pollable_rss_sources(conn)
    ▼
fetch_and_log_rss(conn, source, url, ...)
    │
    ├─► feed_commons.poll(url, excerpt_max_length=300, timeout_seconds=15)   [NEW — replaces
    │       │                                                                  _fetch()+feedparser]
    │       ├─ fetch_feed_bytes()   (HTTPS-only, no redirects, 10MB cap, 15s timeout)
    │       ├─ parse_feed()
    │       ├─ classify_parse_outcome()  → raises PollError(code) on hard failure
    │       └─ normalize_entry() per entry → list[NormalizedItem] (title/link presence filter
    │            already applied here — this is why FR-11 moves "seen" semantics into poll())
    │
    ├─ on PollError: log collect.fetch_failed{err_type=code}, return FeedResult(ok=False)  [unchanged
    │                                                                                        outcome shape]
    └─ on success: for each NormalizedItem
           ├─ tag_symbols(item["title"], symbol_universe)          [UNCHANGED — stays in this repo]
           ├─ observed_date = item["pub_date"][:10] or default_date
           └─ insert_observation(...)                              [UNCHANGED — stays in this repo]
                  ├─ raises → rejected += 1, log collect.entry_rejected, continue
                  └─ ok → inserted += (1 if was_new else 0)
```

feed-commons is consumed as a library, not a service — no new process, port, or network hop is
introduced; it is a Python import resolved at `pip install` time via the git+ssh commit pin.

## Design decisions

- **In-process integration, not a subprocess boundary.** Unlike fashion-monitor's TypeScript case
  (where a subprocess boundary existed for language interop), `feed_commons.poll()` here parses
  fully untrusted, adversary-influenced third-party XML/HTML content in-process, inside the same
  service that holds a live write connection to production `raw_observations` — a different, real
  concern from the interop reason subprocess isolation existed elsewhere. In-process is chosen
  anyway for simplicity, resting on the judgment that feed-commons' own resource caps (10MB
  response size, 15s timeout, HTTPS-only, no-redirect) are enforced robustly enough to be trusted
  in-process. This is not "no boundary is needed" — it is "the boundary feed-commons itself
  enforces is judged sufficient here."

## Data model

No data model changes. `raw_observations` and `sources` schemas, rows, and query shapes
(`sources.pollable_rss_sources(conn)`) are untouched. `insert_observation()` is called with the
same argument shape as today (`observed_date`, `source`, `url`, `title_or_snippet`,
`tagged_symbols`).

`observed_date`'s existing `validate_observed_date()` app-level validation (already gating every
write inside `insert_observation()`, confirmed via direct read of `validation.py`) already makes
any date-derivation edge case fail safely — the entry is rejected, not written as a corrupted row —
so no new DB constraint is needed here; this is already correctly handled at the application layer.

## API / interface contract

Public signatures are unchanged:

```python
def fetch_and_log_rss(
    conn: sqlite3.Connection,
    source: str,
    url: str,
    *,
    symbol_universe: list[str] | None = None,
    fallback_date: str | None = None,
    run_id: str | None = None,
) -> FeedResult: ...

def collect_rss(
    conn: sqlite3.Connection,
    *,
    symbol_universe: list[str] | None = None,
    run_id: str | None = None,
) -> CollectSummary: ...
```

`feed_commons.poll()`'s actual contract (read from source, not guessed):

- `poll(url: str, excerpt_max_length: int = 300, timeout_seconds: float = 15) -> list[NormalizedItem]`
- `NormalizedItem` is a `TypedDict` with keys `title: str`, `link: str`, `guid: str`,
  `pub_date: str | None` (ISO-8601, e.g. `"2026-07-04T12:00:00+00:00"`), `description_excerpt: str`
  (HTML-stripped, truncated to `excerpt_max_length` — never used per FR-3).
- `poll()` internally drops any entry missing a non-empty `title` or `link` before returning
  (`normalize_entry()` returns `None` for those, and `_run_poll` filters `None`s out). This is
  **not exactly** the `if not link or not title: continue` filter `fetch_and_log_rss` does today —
  see the "title fallback dropped by feed-commons" risk item below for the real difference — but it
  is still true that `poll()`'s output list *is* the post-filter set, so `seen = len(items)` is
  correct per FR-11.
- On failure, `poll()` raises `feed_commons.errors.PollError`, a plain `Exception` subclass whose
  **code is `err.code`** (a `Literal["timeout", "invalid_url", "http_error", "parse_error",
  "network_error"]` attribute set in `__init__`, confirmed by reading `errors.py` — not `str(err)`
  parsing, though `str(err)` also happens to equal the code since `Exception.__init__` was called
  with just `code`). All 5 codes are handled uniformly (per Out-of-scope: no code-specific policy).
  Confirmed via direct read of `feed_commons/poll.py`: at least one path does
  `raise PollError("network_error") from exc`, i.e. exception-chains the original underlying error
  — so `exc.__cause__` holds the real diagnostic detail (e.g. the actual httpx/feedparser exception
  text) when feed-commons chained it, and the code sketch below preserves that instead of
  discarding it.
- Import path confirmed correct: `from feed_commons import PollError` (top-level, not
  `from feed_commons.errors import PollError`) — `feed_commons/__init__.py` re-exports `PollError`
  at the top level (confirmed by direct read), resolving an import-path ambiguity two reviewers
  separately questioned.

Exact retrofitted `fetch_and_log_rss` body:

```python
from feed_commons import PollError, poll

from . import db, sources
from .log import log_event
from .observations import insert_observation, tag_symbols

FETCH_TIMEOUT = 15.0
EXCERPT_MAX_LENGTH = 300


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
        # poll() already filtered out entries missing title/link (normalize_entry), so every item
        # here counts as "seen" per FR-11 — no title/link presence check needed at this layer.
        seen += 1
        title = item["title"]  # never description_excerpt (FR-3) -- untouched, un-truncated
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
```

**Why both `except` layers are needed:** the narrow `except PollError` gives rich, code-based
handling and logging for feed-commons' own anticipated failure modes, but FR-13a's "never raises"
invariant must not depend entirely on a sibling repo's exception discipline being complete — the
broad `except Exception` fallback is the belt-and-suspenders safety net that keeps one unanticipated
bug in feed-commons from crashing the whole unattended collection run, mirroring the outer
catch-all fashion-monitor's `bulletin-poll.ts` added for the identical reason.

`collect_rss()` body is unchanged (still one `fetch_and_log_rss()` call per
`sources.pollable_rss_sources(conn)` row, aggregated into `CollectSummary`).

`_fetch()` and `_entry_date()` are deleted outright (FR-12/AC-14) — nothing else in the file
references them once the above lands, and no test in this repo imports them directly (confirm
during implementation by grepping `tests/` for `_fetch` / `_entry_date` before deleting; if a unit
test does import either directly, delete that test alongside per AC-14's "unused by runtime path"
allowance, not by keeping the function around).

`observed_date` derivation — exactly matches FR-4/AC-4/AC-5/AC-6:
- `pub_date` is truthy (a non-empty string) → `pub_date[:10]` (ISO-8601 string, first 10 chars is
  `YYYY-MM-DD` by construction of `datetime.isoformat()`).
- `pub_date` is falsy (`None` or, hypothetically, `""`) and `fallback_date` was passed →
  `fallback_date`.
- `pub_date` is falsy and `fallback_date` was not passed → `db.now_iso()[:10]`.
- This is the same `default_date` variable and same fallback order as today's `_entry_date(entry)
  or default_date`, just with `_entry_date`'s struct_time formatting replaced by a slice on
  `poll()`'s already-ISO-8601 string. Using truthiness (`if pub_date:`) rather than `is not None`
  is a cosmetic clarity improvement, not a bug fix — a hypothetical future empty-string `pub_date`
  from feed-commons now falls back to `fallback_date`/today the same way `None` does, instead of
  being passed through to validation as a malformed date string (today's code already fails safe
  either way via `validate_observed_date()`, see Data model below).

## Integration points

- `src/macro_monitor/collector_rss.py` — replace `_fetch()`/`feedparser.parse()`/`_entry_date()`
  with `feed_commons.poll()` + `PollError` handling inside `fetch_and_log_rss`; delete `_fetch()`
  and `_entry_date()`; remove the `import feedparser` / `import httpx` lines (no longer used in
  this file) and the module-level `USER_AGENT` constant (no longer used — `poll()`/
  `fetch_feed_bytes()` owns its own User-Agent internally); keep `FETCH_TIMEOUT` (renamed usage:
  now the `timeout_seconds` arg to `poll()`) and add `EXCERPT_MAX_LENGTH = 300`. `FeedResult`,
  `CollectSummary`, and `collect_rss()` are untouched.
- `pyproject.toml` — add `feed-commons @
  git+ssh://git@github.com/preston-bernstein/feed-commons.git@78fd77470345fe0fd63b961eb57e383cf60a1197`
  to `dependencies`, formatted identically to the existing `fleet-logging` entry (inline comment
  explaining the pin, matching that entry's tone): note this is a second git-SSH-pinned dependency,
  so the comment should say "same pattern as fleet-logging above" rather than duplicating the whole
  rationale. `httpx` and `feedparser` direct dependencies can stay in `dependencies` only if
  something else in this repo still imports them directly (check `collector_websearch_ingest.py`
  and elsewhere before removing either — out of scope for this retrofit to touch those, so default
  to leaving both listed unless a grep confirms `collector_rss.py` was their only consumer).
- `tests/test_collect_rss.py` (or wherever the existing RSS collector unit tests live) — every
  test that currently mocks `httpx.get`/`feedparser.parse` must be rewritten to mock
  `macro_monitor.collector_rss.poll` (patch at the import site in `collector_rss.py`, i.e.
  `monkeypatch.setattr(collector_rss, "poll", ...)` or `unittest.mock.patch("macro_monitor.collector_rss.poll")`)
  returning `NormalizedItem` dicts or raising `PollError(code)`, covering AC-3 through AC-8 and
  AC-11. This is implied by every functional requirement being test-verifiable (AC-3..AC-11) and by
  FR-12 removing `_fetch()`/`_entry_date()`, which are today's mock seams.
- `tests/test_no_forbidden_imports.py` — no code change; re-run as-is post-retrofit per AC-10 to
  confirm the AST guard still passes (the new `import feed_commons` is not `subprocess`/
  `algo_factory`/`backtest`, so this should pass without modification — verify, don't assume).
- `tests/test_cli.py` (lines 154, 173, 203) — three tests
  (`test_collect_rss_writes_metrics_reflecting_work_done`,
  `test_collect_rss_logs_a_structured_event_with_run_id`, `test_collect_rss_cli_exit0_on_success`)
  currently do `monkeypatch.setattr(collector_rss, "_fetch", lambda url: fed_feed_bytes)`. After
  `_fetch()` is deleted these become dead patches on an unused attribute — the tests would attempt
  a real network call to a `.test` domain and fail. Rewrite all three to mock
  `collector_rss.poll` instead, same pattern as `test_collect_rss.py`.
- `tests/test_smoke_e2e.py` (line 27) — `collector_rss._fetch(LIVE_FEED)  # reachability probe` is
  a direct, unmocked call to the private function used to decide whether to `pytest.skip()` the
  rest of a live-feed end-to-end test. Once `_fetch()` is deleted this becomes `AttributeError`,
  which the test's own broad `except Exception: pytest.skip(...)` would catch — meaning this smoke
  test would silently skip forever after the retrofit, regardless of actual network reachability,
  with no failure signal ever surfacing. Rewrite the reachability probe to call
  `feed_commons.poll()` (or a lighter existence/import check) instead of the deleted `_fetch()`,
  and ensure a genuinely broken probe fails the test loudly rather than silently skipping — only
  skip on a specific, expected "network unreachable" exception type, not on `AttributeError` or any
  other unexpected exception.
- `src/macro_monitor/correlator.py` (line 185) — a docstring references `collector_rss._entry_date`
  by name in prose. Not a runtime break, but becomes a stale/dangling reference once that function
  is deleted. Update the docstring.
- **New feed-commons contract test.** Add at least one test, marked with this repo's existing
  `network` pytest marker (per `pyproject.toml`'s marker convention, so it skips gracefully when
  unreachable), that calls the real `feed_commons.poll()` against the real production feed URL and
  confirms the properties this repo's threat model depends on (successful fetch+parse, HTTPS-only,
  no-redirect). Every other test in this plan mocks `poll()` entirely, so without this the repo
  would have zero test signal on feed-commons' real behavior surviving future feed-commons pin
  bumps.

## Technology choices

- `feed_commons.poll()` (already a dependency of the sibling repo, now imported directly) —
  replaces `httpx` + `feedparser` as the fetch/parse path per the requirements' core mandate; no
  new technology is introduced, an existing hardened one is reused.
- No other new library, pattern, or abstraction. `tag_symbols()`/`insert_observation()`,
  `log_event()`, and the dataclass shapes all stay exactly as they are — this is a narrow swap of
  the fetch/parse internals, not a redesign.

## Risk areas

- **Parse-failure narrowing (real behavior change, but resolved for current prod config).**
  feed-commons' `classify_parse_outcome()` hard-fails (`parse_error`) on any `bozo` exception
  outside two benign types, where today's code tolerates `bozo=1` with non-empty entries as a
  degraded-but-usable feed. This was flagged blocking in requirements.md but is now empirically
  clear: `collect.feed_malformed_but_usable` has 0 occurrences across this service's entire
  journalctl history, and a live `poll()` call against the real production `fed-press` URL
  (https://www.federalreserve.gov/feeds/press_all.xml) just returned 20 clean items with zero
  parse errors. **Still spot-check this again immediately before deploy** (AC-13) — feeds can
  change shape between now and ship, and the check is cheap (one live `poll()` call per
  `pollable_rss_sources()` row).
- **HTTPS-only scheme enforcement (real constraint, resolved for current prod config).**
  `validate_https_url()` rejects non-`https://` URLs with `invalid_url`. Flagged blocking in
  requirements.md, now empirically clear: a live query against the production `sources` table
  confirmed the only real production source (`fed-press`) is already `https://`. **Still spot-check
  this again immediately before deploy** (AC-12) — a `select url_or_query from sources where
  fetchable=1` (or equivalent) is a one-line check against the live desktop DB, cheap insurance
  against a source added between now and ship.
- **Mock-seam churn in existing tests.** `_fetch()` and `_entry_date()` are today's test seams
  (mocked via `httpx`/`feedparser` in unit tests); removing them means every existing
  `test_collect_rss.py` test that patches those needs rewriting to patch `poll()` instead. This is
  mechanical but touches every AC-3..AC-11 test case — get the mock target right
  (`macro_monitor.collector_rss.poll`, the name as imported into this module, not
  `feed_commons.poll.poll`) or the mocks silently no-op and tests hit the real network.
  `EXCERPT_MAX_LENGTH`/`FETCH_TIMEOUT` argument values passed to `poll()` should also be asserted
  in at least one test, since AC-1 requires the exact `excerpt_max_length=300, timeout_seconds=15`
  call shape.
- **`err_type=exc.code` deviates from the existing convention of `err_type=type(exc).__name__`.**
  Every other `err_type` in this file is an exception class name; for `PollError` all 5 instances
  share one class name (`PollError`), which would be useless for filtering/alerting on a specific
  failure mode. Using `exc.code` (`"timeout"`, `"invalid_url"`, etc.) as `err_type` instead is more
  informative and satisfies FR-7's "including PollError.code" explicitly, but is a deliberate
  divergence from the class-name convention elsewhere in the file — worth flagging in code review
  rather than silently drifting the log schema's meaning of `err_type` between event types.
- **`httpx`/`feedparser` becoming unused-but-still-declared dependencies.** If no other module in
  this repo imports them after the retrofit, they become dead weight in `pyproject.toml`
  (harmless, but worth a grep to confirm before deciding whether to prune them — out of scope to
  guess without checking `collector_websearch_ingest.py` and any other collector modules first).
- **Deploy-key access to feed-commons is unverified — converged finding from 3 independent
  reviewers.** Nothing in this plan verifies that the deploy-host service user (`algo-macro`, on
  the desktop) actually has working SSH deploy-key access to clone the private `feed-commons` repo
  specifically. This exact failure class — a new service-user git+ssh dependency on a private
  sibling repo with no deploy key provisioned — has already happened before in this environment,
  including in this repo's own git history for the `fleet-logging` dependency ("Fix CI: load a
  dedicated deploy key for the fleet-logging git+ssh dependency"). Copying "the fleet-logging
  pattern" doesn't guarantee the credential scope also covers feed-commons. **Must be verified
  before merge/deploy**: confirm the CI environment and the `algo-macro` service user on the
  desktop host both have deploy-key access to feed-commons specifically, adding a dedicated deploy
  key mirroring the fleet-logging fix if one doesn't already exist.
- **Title fallback dropped by feed-commons — real, accepted behavior change.** Today's title
  extraction is `title = getattr(entry, "title", None) or getattr(entry, "summary", None)` (falls
  back to summary), while feed-commons' `normalize_entry()` (confirmed via direct read of
  `feed_commons/normalize.py`) does `title = entry.get("title", "").strip()` with no summary
  fallback — entries with no title but a populated summary are now silently dropped by `poll()`
  itself (not even counted, since `poll()` discards its own internal skipped_count before
  returning). This is not "exactly" the same filter as today's (see the API/interface contract
  section above). Fixing this locally isn't possible without either forking feed-commons
  (out of scope) or reimplementing summary-fallback logic in this repo (which would defeat the
  retrofit's purpose of eliminating duplicated fetch/parse logic). Disposition: accept as a
  documented behavior change, verify empirically for current sources (same "spot-check before
  deploy" discipline as the parse-failure and HTTPS-only items above), and flag for any future new
  source before it's added.
- **Dedup-key drift risk from link whitespace-stripping.** feed-commons' `normalize_entry()` does
  `link = entry.get("link", "").strip()` (strips whitespace), where today's code does not strip.
  Since this repo's `dedup_key` (in `db.py`) is `sha256(source + url)`, a historical row whose
  stored `url` had incidental whitespace would hash differently once re-polled post-retrofit,
  silently breaking `INSERT OR IGNORE` dedup and producing a live duplicate row. **Spot-check
  before deploy**: diff feed-commons' normalized `link` against the current production `url`
  column for a sample of already-seen entries.
- **`USER_AGENT` constant removal is an accepted trade-off.** Removing it loses a local lever to
  override the outbound User-Agent string if a future feed source starts UA-filtering. Already
  empirically low-risk: the live `poll()` spot-check against the one real production source
  succeeded using feed-commons' own default User-Agent.
- **XXE exposure in feedparser's XML parsing is pre-existing, not new.** Today's code already calls
  `feedparser.parse()` directly, so any XXE surface in that parsing path predates this retrofit and
  is not a new risk it introduces — noted here only so a future review doesn't mistake this for a
  new gap.
