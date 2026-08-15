# Requirements: feed-commons Collector Retrofit

## Problem statement

`src/macro_monitor/collector_rss.py` hand-rolls RSS/Atom fetching (`httpx`) and parsing
(`feedparser.parse()` + manual `published_parsed`/`updated_parsed` extraction) — logic that the
sibling `feed-commons` library already implements, has already hardened to 100% mutation score,
and already ships to one real consumer (internal-monitor-app, via subprocess since that consumer is
TypeScript). internal-monitor-service and feed-commons are both Python in the same dev environment, so
the hand-rolled path is duplicated maintenance with no cross-language justification. The person
who has this problem is whoever maintains internal-monitor-service's collection path (Preston) and,
indirectly, whoever maintains feed-commons — a second hand-rolled fetch/parse implementation is a
second place SSRF hardening, encoding edge cases, and malformed-feed handling can silently drift
from feed-commons' hardened baseline. It matters now because this collector is live and
unattended (`systemd/macro-monitor-collect.timer`, `internal-monitor-service` service user, desktop host) and
feeds a production SQLite DB consumed by the downstream correlation/hypothesis pipeline — any
regression here is a silent data-quality problem, not a build failure.

## Users / stakeholders

- `internal-monitor-service`'s deterministic RSS collector (`fetch_and_log_rss`, `collect_rss`) — the
  direct caller being retrofitted.
- The `internal-monitor-service` service user running `macro-monitor-collect.timer` unattended on the desktop
  host — depends on this path continuing to run without manual intervention.
- The downstream correlation/hypothesis-review pipeline that reads `raw_observations` — depends on
  `observed_date`, `title_or_snippet`, and `tagged_symbols` continuing to have the same meaning and
  shape as today.
- The FR-12 AST guard test (`tests/test_no_forbidden_imports.py`) — depends on this repo continuing
  to contain no `subprocess`/`os.system` calls and no `algo_factory`/`backtest` imports anywhere
  under `src/macro_monitor/*.py`.
- feed-commons maintainer (also Preston) — gains a second in-process consumer; any bug found here
  that traces back to `poll()` itself is feed-commons' problem, not this repo's.

## Functional requirements

1. The system shall replace `collector_rss.py`'s internal `_fetch()` (httpx GET) and
   `feedparser.parse()` call with a single in-process call to `feed_commons.poll(url,
   excerpt_max_length=300, timeout_seconds=15)`, imported directly (no subprocess).
2. The system shall pin `feed-commons` in `pyproject.toml`'s `dependencies` list as
   `feed-commons @ git+ssh://git@github.com/preston-bernstein/feed-commons.git@78fd77470345fe0fd63b961eb57e383cf60a1197`,
   matching the exact-commit-pin pattern already used for the `fleet-logging` dependency (comment
   included, per existing convention, noting this pin must be bumped by hand).
3. The system shall map each `feed_commons.NormalizedItem`'s `title` field — untouched,
   never truncated by feed-commons — to both the `tag_symbols(title, symbol_universe)` call and the
   `title_or_snippet` argument of `insert_observation()`; it shall never use
   `description_excerpt` (HTML-stripped and truncated to 300 chars) for either purpose.
4. The system shall derive `observed_date` as a `YYYY-MM-DD` string by taking the date portion of
   a `NormalizedItem`'s `pub_date` (an ISO-8601 string) when `pub_date` is not `None`, and shall
   fall back to `fallback_date` (or, if `fallback_date` is not provided, `db.now_iso()[:10]`) when
   `pub_date` is `None` — preserving the existing fallback contract of `_entry_date()` /
   `default_date` exactly.
5. The system shall call `tag_symbols()` and `insert_observation()` from
   `macro_monitor.observations` exactly as `fetch_and_log_rss` does today; this domain logic shall
   not move into feed-commons.
6. The system shall catch `feed_commons.errors.PollError` raised by `poll()` and return a
   `FeedResult(source=source, ok=False, error=...)` without raising, for every one of the 5 bounded
   `PollError` codes (`timeout`, `invalid_url`, `http_error`, `parse_error`, `network_error`) —
   preserving FR-13a (a bad feed is logged and skipped, never fatal to the run).
7. The system shall log a `collect.fetch_failed`-equivalent ERROR event on `PollError`,
   including the `PollError.code` and `run_id`/`source` as today, and shall never include the raw
   feed URL response body, a raw entry title, or a raw entry link verbatim in that log line — where
   "raw" means the entry's title/link/content field values exactly as returned by `poll()` or
   received over the network, unmodified and unredacted; these values may never appear verbatim in
   a log line's message or context fields, only derived/bounded metadata (counts, codes, types) may.
8. The system shall continue to reject an entry (increment `rejected`, log
   `collect.entry_rejected` with `err_type` and no raw title/url) when `insert_observation()`
   raises, exactly as today, so one bad item does not sink the rest of the feed's entries.
9. The system shall preserve the `FeedResult` and `CollectSummary` dataclass shapes
   (`source`, `ok`, `inserted`, `seen`, `rejected`, `error`; `results`, `any_success`,
   `total_inserted`) unchanged, so the CLI's exit-code contract built on them requires no changes.
10. The system shall preserve the `fetch_and_log_rss(conn, source, url, *, symbol_universe=None,
    fallback_date=None, run_id=None) -> FeedResult` and `collect_rss(conn, *, symbol_universe=None,
    run_id=None) -> CollectSummary` public signatures unchanged.
11. The system shall define `seen` as the count of `NormalizedItem`s returned by `poll()` for that
    feed (i.e., entries feed-commons itself judged to have both a non-empty `title` and a
    non-empty `link`), and shall define `rejected` as the count of those items for which
    `insert_observation()` raised — matching today's `seen`/`rejected` split, adjusted for the fact
    that title/link presence filtering now happens inside `poll()` rather than in
    `fetch_and_log_rss`.
12. The system shall remove `_fetch()` and `_entry_date()` (or reduce them to wrappers that are
    completely unused by the runtime path) once `poll()` fully replaces their behavior, so there is
    exactly one fetch/parse path in this file, and shall remove the `import httpx` and
    `import feedparser` lines and the module-level `USER_AGENT` constant from `collector_rss.py`
    once nothing in that file references them anymore (this removal applies to `collector_rss.py`
    specifically, not to other files that may still need `httpx` or `feedparser`).
13. The system shall pass to `insert_observation()` the same identity-field values (source,
    url/link, and any other fields `insert_observation()` uses to compute its dedup key) that
    `_fetch()`/`feedparser` produced today, unaltered by the retrofit's item-mapping in any way that
    would change `insert_observation()`'s existing dedup/append-only behavior — the retrofit is a
    pure pass-through on the identity fields `insert_observation()` uses for deduplication;
    `insert_observation()` itself is unchanged and out of scope for this retrofit.
14. The system shall, when logging a `PollError`-sourced failure (FR-7), set `err_type` to
    `exc.code` (one of the 5 bounded values: `timeout`, `invalid_url`, `http_error`, `parse_error`,
    `network_error`), not the exception's class name — a deliberate, spec-level departure from this
    file's convention elsewhere (where `err_type` is normally a class name), made because a single
    shared exception class name (`PollError`) would be useless for filtering by failure mode.
15. The system shall accept, as a documented behavior change, that entries with no `<title>`
    element (even if a `<description>`/`<summary>` is present) are now silently excluded by
    feed-commons' own filtering inside `poll()` before reaching this repo, with no fallback to
    summary/content available in this repo — feed-commons is a frozen, out-of-scope dependency per
    Constraints, and this repo may not reintroduce the summary-fallback that today's
    `_entry_date`-adjacent code (`title = getattr(entry, "title", None) or getattr(entry, "summary",
    None)`) provides.

## Non-functional requirements

- Per-call timeout to `poll()` shall remain 15 seconds, matching the existing `FETCH_TIMEOUT =
  15.0` behavior.
- No new subprocess, shell-out, or `os.system` call may be introduced anywhere in
  `src/macro_monitor/*.py` — the retrofit is a plain in-process Python import and function call
  (confirmed compatible with `tests/test_no_forbidden_imports.py`'s existing AST guard).
- No new import of `algo_factory`, `backtest`, or `backtest.gates` may be introduced anywhere in
  `src/macro_monitor/*.py` (FR-12 guard).
- The retrofit shall not change the on-disk schema or existing rows of `raw_observations` or
  `sources`.
- feed-commons' fetch path enforces a 10 MB response-size cap and HTTPS-only URLs
  (`validate_https_url` rejects any non-`https://` scheme) — this repo's collector currently
  imposes no size cap and no scheme restriction; the retrofit accepts feed-commons' stricter
  behavior as-is rather than working around it (see Constraints).

## Constraints

- Must integrate with the existing `sources` table / `sources.pollable_rss_sources(conn)` query —
  unchanged, this retrofit only changes what happens inside `fetch_and_log_rss`.
- Must integrate with the existing `macro_monitor.log.log_event()` logging wrapper and its
  `run_id`/`source`/`err_type`/`err_msg`/`outcome` field conventions.
- Must integrate with the existing `macro_monitor.observations.tag_symbols()` and
  `insert_observation()` — these stay in this repo, are not moved to feed-commons, and are called
  with the same argument shapes as today.
- Must follow the existing `fleet-logging` git-SSH-pinned-to-commit dependency pattern exactly for
  the new `feed-commons` dependency — no version ranges, no branch pins.
- Access to the production desktop deploy host's SQLite DB for the AC-12/AC-13/AC-15/AC-16
  pre-deploy spot-checks happens via SSH to the desktop deploy host as the `agent` user (with sudo,
  per this environment's established convention), querying
  `/home/internal-monitor-service/app/data/macro_monitor.db` read-only via `sudo -u internal-monitor-service sqlite3 ...`.
- feed-commons is pinned at commit `78fd77470345fe0fd63b961eb57e383cf60a1197` on its `main` branch,
  is 100% mutation-score hardened, and its `poll()` contract (signature, `NormalizedItem` shape,
  5-code `PollError`) is fixed for this integration — this repo may not fork or vendor
  feed-commons code as a workaround for any of the behavior differences below.
- feed-commons' `fetch_feed_bytes()` sends requests with `allow_redirects=False` and calls
  `validate_https_url()` first — this is confirmed equivalent-or-stronger than this repo's current
  `httpx(..., follow_redirects=False)` SSRF hardening (feed-commons additionally enforces
  HTTPS-only scheme and a 10 MB response cap, neither of which the current `_fetch()` has). No
  redirect-following gap exists; this is not a blocking finding.
- **Blocking finding — parse-failure behavior narrows.** Today, a feed with `parsed.bozo == 1` but
  a non-empty `parsed.entries` (a malformed-but-still-parseable feed, e.g. bad XML namespace) is
  treated as a soft failure: `collect.feed_malformed_but_usable` is logged at WARN and the entries
  are still processed (`ok=True`). feed-commons' `classify_parse_outcome()` treats any `bozo`
  exception outside two specific benign types (`CharacterEncodingOverride`, `NonXMLContentType`)
  as a hard `parse_error` regardless of whether entries are present, so `poll()` raises and the
  entire feed yields zero entries where today it would yield a degraded set. This is a real
  behavior change, not a wash — any currently-configured feed relying on today's "malformed but
  usable" tolerance will silently go from partial data to zero data on this retrofit. Before this
  retrofit ships, the currently-configured production RSS sources must be checked against this new
  failure mode (see Acceptance Criteria).
- **Blocking finding — HTTPS-only.** `validate_https_url()` rejects any feed URL that is not
  `https://`. Any currently-configured production source using a plain `http://` URL will start
  failing with `invalid_url` after this retrofit. Before this retrofit ships, all currently
  pollable sources in the production `sources` table must be confirmed `https://`.
- **Blocking finding — link-stripping dedup risk.** feed-commons' `normalize_entry()` does
  `link = entry.get("link", "").strip()` (strips whitespace); today's code uses
  `getattr(entry, "link", None)` with no stripping. Since `dedup_key` in this repo's `db.py` is
  derived as `sha256(source + url)`, any historical row whose stored `url` had incidental
  leading/trailing whitespace would produce a different `dedup_key` when that same article is
  re-polled post-retrofit (now stripped) — silently defeating `INSERT OR IGNORE` dedup and
  producing a live duplicate row for an already-seen article, undermining AC-11's append-only
  guarantee. Before this retrofit ships, feed-commons' normalized `link` values must be diffed
  against the current production `url` column for a sample of already-seen entries in
  `raw_observations` to confirm no whitespace-only mismatches exist (see Acceptance Criteria).

## Out of scope

- Any change to `feed_commons.poll()`, `fetch.py`, `normalize.py`, or `parse.py` — feed-commons is
  a frozen, already-hardened dependency for this task; behavior differences are worked around or
  flagged in this repo, not patched upstream as part of this retrofit.
- Any change to `tag_symbols()` or `insert_observation()` internals in `macro_monitor.observations`.
- Any change to the `sources` table schema, `add_source()`, `set_fetchable()`, or
  `pollable_rss_sources()`.
- Any change to the CLI's exit-code logic or `CollectSummary`/`FeedResult` consumers outside
  `collector_rss.py`.
- The web-search collection path (`collector_websearch_ingest.py` / FR-02-class collection) — this
  retrofit is scoped to the RSS collector only.
- Adding a retry/backoff layer on top of `poll()` — none exists today and none is requested here.
- Migrating internal-monitor-app's subprocess-based feed-commons integration to anything — that
  consumer is out of scope and already shipped.
- Adding new `PollError`-specific handling logic beyond catching-and-logging (e.g., no
  code-specific retry policy, no alerting integration) — all 5 codes are handled uniformly as a
  failed `FeedResult`, matching today's uniform handling of any fetch/parse exception.

## Acceptance criteria

1. `fetch_and_log_rss()` calls `feed_commons.poll(url, excerpt_max_length=300,
   timeout_seconds=15)` and no longer calls `httpx.get()` or `feedparser.parse()` directly.
2. `pyproject.toml` lists `feed-commons` pinned to commit
   `78fd77470345fe0fd63b961eb57e383cf60a1197` via `git+ssh://`, formatted identically to the
   existing `fleet-logging` entry (inline comment included).
3. Given a mocked `poll()` returning items with distinct `title` and `description_excerpt` values,
   `insert_observation()` is called with `title_or_snippet` equal to the item's `title`, never its
   `description_excerpt`, and `tag_symbols()` is called with that same `title`.
4. Given a mocked `poll()` returning an item with a non-`None` ISO-8601 `pub_date` (e.g.
   `"2026-07-04T12:00:00+00:00"`), the inserted row's `observed_date` is `"2026-07-04"`.
5. Given a mocked `poll()` returning an item with `pub_date=None`, and `fallback_date="2026-07-01"`
   passed to `fetch_and_log_rss`, the inserted row's `observed_date` is `"2026-07-01"`.
6. Given a mocked `poll()` returning an item with `pub_date=None` and no `fallback_date` argument,
   the inserted row's `observed_date` equals `db.now_iso()[:10]`.
7. Given a mocked `poll()` that raises `PollError("timeout")` (and, separately, each of
   `invalid_url`, `http_error`, `parse_error`, `network_error`), `fetch_and_log_rss()` returns
   `FeedResult(ok=False, ...)` without raising, and a log event is emitted containing the
   `PollError.code` and no raw feed URL body or entry content.
8. Given a mocked `poll()` returning 3 items where `insert_observation()` raises for exactly 1 of
   them, `fetch_and_log_rss()` returns `seen=3`, `inserted=2`, `rejected=1`, `ok=True`, and the
   `collect.entry_rejected` log line contains no raw title or url.
9. `collect_rss()` continues to call `fetch_and_log_rss()` once per row from
   `sources.pollable_rss_sources(conn)` and aggregate results into `CollectSummary` unchanged;
   `summary.any_success` is `True` when at least one source's `FeedResult.ok` is `True`.
10. `tests/test_no_forbidden_imports.py`'s AST guard passes unchanged against the retrofitted
    `collector_rss.py` (no `subprocess`/`os.system`, no `algo_factory`/`backtest` imports).
11. Re-running `fetch_and_log_rss()` against the same feed content twice is still append-only —
    the second run inserts 0 new rows (dedup / FR-13 preserved).
12. Every currently-configured pollable RSS source in the production `sources` table is confirmed
    `https://` before deploy; any `http://` source is flagged to Preston as a pre-deploy blocker —
    do not proceed with deploy in that case (re-pointing a source URL requires `sources.py` /
    `set_fetchable()` / the `sources` table, which is out of scope for this retrofit; Preston may
    re-point it himself via existing out-of-scope tooling).
13. Every currently-configured pollable RSS source is spot-checked (or its recent raw feed bytes
    replayed) against `feed_commons.parse.classify_parse_outcome()` to confirm it does not rely on
    today's "malformed but usable" (`bozo=1`, non-empty entries) tolerance; any source that does is
    flagged to Preston as a pre-deploy blocker, not silently deployed to start returning zero
    entries.
14. `_fetch()` and `_entry_date()` no longer exist in `collector_rss.py` (or, if retained for
    backward compatibility of direct unit tests, are unused by `fetch_and_log_rss`'s runtime path);
    `collector_rss.py` no longer contains `import httpx`, `import feedparser`, or a module-level
    `USER_AGENT` constant.
15. A pre-deploy spot-check of the current production feed's entries (or a recent sample) confirms
    none currently rely on the summary-fallback (i.e., no entry has an empty/missing `<title>` but
    a populated `<description>`/`<summary>`), using the same discipline as AC-12/AC-13; any source
    found to rely on it is flagged to Preston as a pre-deploy blocker, not silently deployed.
16. feed-commons' normalized `link` values are diffed against the current production `url` column
    for a sample of already-seen entries in `raw_observations`, confirming no whitespace-only
    mismatches exist that would cause a duplicate insert on re-poll; any mismatch found is flagged
    to Preston as a pre-deploy blocker, not silently deployed.
