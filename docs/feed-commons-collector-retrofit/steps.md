# Steps: feed-commons Collector Retrofit

## Prerequisites

- Local clone of algo-macro-monitor with venv activated: `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`
- feed-commons repo cloned locally (not strictly required, but useful for checking the commit hash `78fd77470345fe0fd63b961eb57e383cf60a1197` exists)
- Read-only access to the production desktop host's deployed SQLite DB, or a recent copy of it for Steps 2-5 verification

## Implementation steps

### Step 1: Add feed-commons dependency to pyproject.toml
**What**: Pin feed-commons to the specified commit via git+ssh, matching the fleet-logging pattern.
**Files**: `pyproject.toml`
**Test**: `pip install -e ".[dev]"` succeeds and `python -c "from feed_commons import poll, PollError; print('OK')"` runs without error.
**Depends on**: none
**Parallelizable**: No

### Step 2: Pre-deploy spot-check — confirm HTTPS in production sources
**What**: Query the production desktop-deployed SQLite DB (or a recent copy) for all rows in the `sources` table where `fetchable=1`. Verify every `url_or_query` value starts with `https://` (no `http://`, `ftp://`, or other schemes). This is AC-12; any `http://` source is a blocker and must be re-pointed to HTTPS or explicitly removed before deploy.
**Files**: none (read-only query against production DB)
**Test**: Run `ssh desktop-agent "sudo -u algo-macro sqlite3 /home/algo-macro/app/data/macro_monitor.db 'SELECT name, url_or_query, fetchable FROM sources;'"` and verify all `url_or_query` results start with `https://`. If any are `http://`, capture them in a note to Preston and do not proceed.
**Depends on**: Step 1
**Parallelizable**: Yes

### Step 3: Pre-deploy spot-check — poll() against production feed URLs
**What**: For each URL returned in Step 2, call `feed_commons.poll(url, excerpt_max_length=300, timeout_seconds=15)` and verify the result is either (a) a list of NormalizedItem dicts, or (b) raises `PollError` with one of the 5 expected codes but NOT because of a "malformed but usable" scenario. This is AC-13; any source that silently goes from partial data to zero data due to the stricter parse-error threshold (bozo=1 with non-empty entries) is a blocker and must be flagged to Preston before deploy. Log each result (source, success/failure, error code if applicable).
**Files**: none (read-only poll calls against production feed URLs)
**Test**: For each production source URL, run a small Python script that calls poll() and prints results. No PollError with code `parse_error` should occur for currently working feeds; if one does, trace it against feed_commons' `classify_parse_outcome()` logic and confirm it's not a "malformed but usable" regression. Example: `python -c "from feed_commons import poll; url='https://www.federalreserve.gov/feeds/press_all.xml'; items=poll(url, 300, 15); print(f'{url}: {len(items)} items')"` should print a count > 0.
**Depends on**: Step 1
**Parallelizable**: Yes

### Step 4: Pre-deploy spot-check — title/summary-fallback regression
**What**: For the current production feed URL(s) (from Step 2), fetch and inspect raw entries to confirm no entry currently relies on a summary-fallback (i.e., every entry has a real, non-empty `<title>` element). Use `feed_commons.poll()` plus manual inspection of the raw feed XML or the returned NormalizedItem dicts' `title` fields.
**Files**: none (read-only inspection of production feeds)
**Test**: For each production feed, verify that calling `poll()` returns items where every item has a non-empty `title`. If any entry has an empty title and would need to fall back to `description_excerpt`, flag to Preston as a pre-deploy blocker — the refactor assumes every entry has a real title.
**Depends on**: Step 1
**Parallelizable**: Yes

### Step 5: Pre-deploy spot-check — dedup-key-drift risk
**What**: For a sample of already-seen entries in the production `raw_observations` table, compare their stored `url` values against what `feed_commons.poll()` returns for the same feed today (after `.strip()`), confirming no whitespace-only mismatches exist that would produce a different `dedup_key` and cause a duplicate insert on re-poll.
**Files**: none (read-only inspection of production DB and live feeds)
**Test**: For 5-10 recent `raw_observations` rows (e.g., `SELECT url FROM raw_observations ORDER BY collected_at DESC LIMIT 10`), fetch the current feed and confirm each stored `url` matches (after `.strip()`) a current feed entry's `url`. No mismatches should exist. If any are found, flag to Preston before deploy.
**Depends on**: Step 1
**Parallelizable**: Yes

### Step 6a: Refactor fetch_and_log_rss() — implement poll() call with belt-and-suspenders exception handling
**What**: In `fetch_and_log_rss()`, replace the old `_fetch()` + `feedparser.parse()` + `_entry_date()` logic with a call to feed-commons' `poll()`. Import `poll` and `PollError` with the exact pattern `from feed_commons import poll, PollError` (this exact import style is required so that test mocks can patch `collector_rss.poll`). Call `poll(url, excerpt_max_length=300, timeout_seconds=15)`. Wrap in exception handling: catch `PollError as exc` FIRST and log `err_type=exc.code`, `err_msg=str(exc.__cause__) if exc.__cause__ is not None else str(exc)`, then return `FeedResult(ok=False, ...)`. THEN add a REQUIRED second, broader `except Exception as exc` catch-all as a last-resort safety net (the current code has a broad except that today's code relies on; narrowing to only PollError would let an unanticipated exception from feed-commons crash the entire unattended collection run). Log `err_type="network_error"` for the broad catch.
**Files**: `src/macro_monitor/collector_rss.py`
**Test**: Unit test mocking `collector_rss.poll` to raise both `PollError(code="timeout")` and a plain `ValueError("bad thing")`, confirming both return `FeedResult(ok=False, ...)` without raising. Verify both error cases are logged distinctly (PollError with `err_type=code`, broad Exception with `err_type="network_error"`).
**Depends on**: Step 1
**Parallelizable**: No

### Step 6b: Refactor fetch_and_log_rss() — implement title/date field mapping
**What**: Map each item's fields correctly: use `item["title"]` (NEVER `item["description_excerpt"]`) for both `tag_symbols()` call and `insert_observation()`'s `title_or_snippet` parameter. Derive `observed_date` as `item["pub_date"][:10] if item["pub_date"] else fallback_date` (using truthiness check, not `is not None`, on `pub_date`). Handle three cases: pub_date present (use it), pub_date None but fallback_date provided (use fallback), pub_date None and no fallback (handle gracefully, either skip or use default).
**Files**: `src/macro_monitor/collector_rss.py`
**Test**: Unit test with three items: one with pub_date, one without pub_date but with fallback_date provided, one without either. Confirm observed_date derivation is correct for all three cases and title is never replaced with description_excerpt.
**Depends on**: Step 1
**Parallelizable**: No

### Step 6c: Refactor fetch_and_log_rss() — implement seen/rejected counting and entry rejection logging
**What**: Loop over `poll()`'s returned items, maintaining counters for `seen` (total count), `inserted` (successful DB inserts), and `rejected` (items where `insert_observation()` raised). Call `insert_observation()` for each item, catch rejections without logging raw title/url (log at most a count and error type), and accumulate counts for the final `FeedResult`.
**Files**: `src/macro_monitor/collector_rss.py`
**Test**: Unit test with a mix of valid items and one item that causes `insert_observation()` to raise (e.g., via monkeypatch). Confirm `seen`/`inserted`/`rejected` counts are correct and rejection is logged without exposing raw title/url.
**Depends on**: Step 1
**Parallelizable**: No

### Step 7: Identify _fetch()/_entry_date() references in tests
**What**: Grep `tests/` for direct imports or calls of `_fetch` or `_entry_date`. NOTE: This grep WILL find hits (expected, not an error); do not treat a clean result as the goal. Current known hits: `tests/test_cli.py` lines 154, 173, 203 (three tests doing `monkeypatch.setattr(collector_rss, "_fetch", ...)`), and `tests/test_smoke_e2e.py` line 27 (direct call `collector_rss._fetch(LIVE_FEED)` as reachability probe).
**Files**: `tests/test_collect_rss.py`, `tests/test_cli.py`, `tests/test_smoke_e2e.py`, and any other test files
**Test**: `grep -r "_fetch\|_entry_date" tests/` will show hits; capture these hits and verify they match expected locations noted above. This is NOT a failure; these hits indicate tests that must be rewritten in Steps 8 and 9.
**Depends on**: Step 6a (refactor must exist before we can meaningfully rewrite tests that depend on old functions)
**Parallelizable**: No

### Step 8: Rewrite test_cli.py's three _fetch-mocking tests to mock poll() instead
**What**: In `tests/test_cli.py`, locate the three tests at lines 154, 173, 203 (or nearby if line numbers have drifted) that currently do `monkeypatch.setattr(collector_rss, "_fetch", lambda url: fed_feed_bytes)`. Rewrite each to instead mock `collector_rss.poll` returning appropriately-shaped `NormalizedItem` lists (or parse the feed-bytes fixture once through `feed_commons.parse` and return the parsed list, if simpler — implementer's judgment). Ensure the mock returns data that produces the same test assertion as the old _fetch mock did.
**Files**: `tests/test_cli.py`
**Test**: `pytest -q tests/test_cli.py` passes; all three rewritten tests pass their assertions.
**Depends on**: Step 7 (must identify which tests to rewrite)
**Parallelizable**: No

### Step 9: Fix test_smoke_e2e.py's reachability probe (CRITICAL)
**What**: In `tests/test_smoke_e2e.py`, the current reachability probe calls `collector_rss._fetch(LIVE_FEED)` wrapped in a broad `except Exception: pytest.skip(...)`. Once `_fetch()` is deleted, this becomes `AttributeError`, silently caught by the same broad except — meaning this live-feed smoke test will SKIP FOREVER after the retrofit, regardless of actual network reachability, with zero failure signal ever surfacing again. CRITICAL: Rewrite the probe to call `feed_commons.poll(LIVE_FEED, excerpt_max_length=300, timeout_seconds=15)` instead. Narrow the except clause so only a genuinely expected network-unreachable exception type (e.g., `PollError` with code "network_error", or a specific exception like `socket.timeout` / `ConnectError`) triggers a skip — any other exception (including a coding mistake like a missing attribute) must FAIL the test loudly, not skip silently.
**Files**: `tests/test_smoke_e2e.py`
**Test**: Manually verify (e.g., by temporarily breaking the probe on purpose, such as passing an invalid URL or missing import) that a broken probe now fails with a clear assertion/error instead of silently skipping, then revert the deliberate break. Also verify that an actual network-unreachable condition (if you can simulate one) still skips gracefully.
**Depends on**: Step 7 (must identify the probe)
**Parallelizable**: No

### Step 10: Delete _fetch() and _entry_date() functions; update docstring references
**What**: Remove the two helper functions `_fetch()` and `_entry_date()` from `src/macro_monitor/collector_rss.py` that are no longer used after Steps 6a-c. Also update the stale docstring reference in `src/macro_monitor/correlator.py` line 185 that mentions `collector_rss._entry_date` by name (cosmetic fix, low priority, but must be done now that the function is deleted).
**Files**: `src/macro_monitor/collector_rss.py`, `src/macro_monitor/correlator.py`
**Test**: The file still parses (no syntax errors); `grep -n "_entry_date" src/macro_monitor/correlator.py` shows no remaining references (or only in comments unrelated to the deleted function).
**Depends on**: Step 8 (test_cli mocks must be rewritten first)
**Parallelizable**: No

### Step 11: Remove unused imports from collector_rss.py
**What**: Delete `import feedparser`, `import httpx`, and the `USER_AGENT` constant if present from `src/macro_monitor/collector_rss.py` (verify by reading the file that they are not used anywhere else in the module). feed_commons owns its own User-Agent internally.
**Files**: `src/macro_monitor/collector_rss.py`
**Test**: `python -c "import src.macro_monitor.collector_rss"` succeeds; `grep -n "feedparser\|httpx\|USER_AGENT" src/macro_monitor/collector_rss.py` shows no references (or only from other files, not this one).
**Depends on**: Step 10
**Parallelizable**: No

### Step 12a: Rewrite unit tests in test_collect_rss.py — title/description mapping and observed_date fallback tests
**What**: For all acceptance criteria AC-3 through AC-6 (title vs. description_excerpt separation, and observed_date fallback cases), rewrite existing tests or add new tests that mock `macro_monitor.collector_rss.poll` (not httpx or feedparser) returning `NormalizedItem` dicts. Each test should verify: (1) title is used, not description_excerpt, (2) observed_date is derived correctly from pub_date or fallback, (3) observed_date with no pub_date and no fallback is handled gracefully.
**Files**: `tests/test_collect_rss.py`
**Test**: `pytest -q tests/test_collect_rss.py -k "title or observed_date"` passes, covering AC-3..AC-6.
**Depends on**: Step 6a-c (must exist before test-writing makes sense)
**Parallelizable**: No

### Step 12b: Rewrite unit tests in test_collect_rss.py — PollError/rejection/dedup tests
**What**: For all acceptance criteria AC-7, AC-8, and AC-11 (PollError codes, rejected-entry logging, dedup/append-only), rewrite existing tests or add new tests that mock `collector_rss.poll` raising `PollError` with each of the 5 expected codes (`timeout`, `invalid_url`, `http_error`, `parse_error`, `network_error`), and tests where `insert_observation()` raises to simulate rejection. Verify each `PollError` code returns `FeedResult(ok=False)` with appropriate logging. Verify rejected entries are logged without exposing raw title/url.
**Files**: `tests/test_collect_rss.py`
**Test**: `pytest -q tests/test_collect_rss.py -k "PollError or rejected or dedup"` passes, covering AC-7, AC-8, AC-11. Add at least one assertion per test verifying the `excerpt_max_length=300, timeout_seconds=15` arguments passed to poll().
**Depends on**: Step 6a-c
**Parallelizable**: No

### Step 13: Write real integration test with network marker
**What**: Add one integration test marked with this repo's `network` pytest marker (skips gracefully if unreachable, per the existing convention in `pyproject.toml`) that calls the REAL `feed_commons.poll()` against the real production feed URL(s) from Step 2, confirming a real end-to-end fetch+parse succeeds. Since every other test mocks `poll()` entirely, this closes a real gap in test signal against feed-commons' actual behavior surviving future pin bumps.
**Files**: Appropriate test file (check existing naming conventions; could go in a new test file or alongside `test_smoke_e2e.py`)
**Test**: `pytest -q tests/ -m network` runs this test and passes (or gracefully skips if network is unavailable). The test calls `poll()` with real production feed(s) and asserts `len(items) > 0` or similar, confirming parse succeeds.
**Depends on**: Step 6a-c (refactor must exist)
**Parallelizable**: No

### Step 14: Run full test suite and verify imports
**What**: Run `pytest -q` to confirm all tests pass (including any new tests from Steps 12a-b and 13). Run the AST guard (`test_no_forbidden_imports.py` if it exists, or a manual grep check) to confirm no subprocess, os.system, algo_factory, or backtest imports are present.
**Files**: none (read-only test run)
**Test**: `pytest -q` exits with status 0; `grep -E "subprocess|os\.system|algo_factory|backtest" src/macro_monitor/collector_rss.py` returns no results.
**Depends on**: Steps 8, 9, 10, 11, 12a-b, 13 (all code changes must be done)
**Parallelizable**: Yes

### Step 15: Final verification — optional dependency cleanup
**What**: Check whether `httpx` and `feedparser` are used anywhere else in the codebase (particularly `collector_websearch_ingest.py`). If they are used only by collector_rss.py and have been removed from there, consider removing them from `pyproject.toml` as well. If they are used elsewhere, leave them in `dependencies`. This is not a blocker, just cleanup.
**Files**: `pyproject.toml` (read, potentially modify)
**Test**: `grep -r "import httpx\|import feedparser" src/` returns no results (or results only from other collectors that still need them). If cleanup is done, `pip install -e ".[dev]"` still succeeds and no import errors occur.
**Depends on**: Step 11
**Parallelizable**: Yes

## Rollback plan

- **Step 1 (dependency)**: Remove the `feed-commons` line from `pyproject.toml` and run `pip install -e ".[dev]"` again.
- **Steps 6a-c (refactor)**: Revert to the original `collector_rss.py` via `git checkout src/macro_monitor/collector_rss.py`.
- **Steps 8-11 (test rewrites + deletions)**: Revert to the original test files and collector_rss.py via `git checkout tests/test_cli.py tests/test_smoke_e2e.py tests/test_collect_rss.py src/macro_monitor/collector_rss.py src/macro_monitor/correlator.py`.
- **Steps 12a-b (test rewrites)**: Revert to the original `tests/test_collect_rss.py` via `git checkout tests/test_collect_rss.py`.
- **Step 13 (integration test)**: Revert the new integration test file via `git checkout tests/` (or delete the file if it was newly created).
- **Steps 2-5, 14-15 (verifications/cleanup)**: No code was modified; no rollback needed.
- **All steps reversible via git**: If the entire retrofit needs to be undone before merge, `git reset --hard` to the pre-retrofit commit.
