# Decisions

## Repo + service account facts (resolved 2026-07-05)

- **Repo.** `github.com/preston-bernstein/internal-monitor-service` (private), cloned at
  `/home/<user>/repos/internal-monitor-service` on desktop. Sibling to `internal-research-service`, no package
  dependency on it, per plan.md's "internal-corpus-service precedent."
- **Service user.** `internal-monitor-service` (uid 981, nologin, home `/home/internal-monitor-service`). Mirrors
  `internal-research-service`'s own service-user pattern. Deployed code should land under `/home/internal-monitor-service/app`
  via a sync-deploy script mirroring `internal-research-service-desktop-sync.service`.
- **FR-17 (paper.db read access) — RESOLVED, supersedes plan.md's SSH-forced-command assumption.**
  Preston chose group-read over SSH-forced-command / sudoers-Cmnd_Alias. Concretely:
  - The live `paper.db` is at `/home/internal-research-service/app/data/paper.db` (NOT
    `/home/internal-research-service/data/paper.db` — that path is a stale pre-`app/`-layout copy, ignore it).
  - `paper-db-snapshot.timer` (systemd, runs as `internal-research-service`, every 15 min) uses SQLite's own
    backup API (`sqlite3.connect(...).backup(...)`, transaction-consistent even against concurrent
    writers — see `/usr/local/lib/internal-monitor-service/paper_db_snapshot.py`) to copy it into
    `/srv/paper-share/paper.db`, mode `0640`, owner `internal-research-service:paper-readers`.
  - `internal-monitor-service` is a member of the `paper-readers` group and can read (never write)
    `/srv/paper-share/paper.db` directly — **no SSH, no sudoers, no traversal into
    `/home/internal-research-service` at all** (verified: `internal-monitor-service` gets Permission denied on
    `/home/internal-research-service/` itself). This is a same-host read, plain `sqlite3.connect(path, uri=...,
    mode=ro)` or equivalent — building an SSH hop for a same-machine file read would be needless
    complexity plan.md didn't anticipate because it assumed a stricter isolation mechanism.
  - The correlator's FR-18 (argv-list/parameterized SQL) and FR-19 (minimal-field-only:
    symbol/date/strategy/gate-pass-fail, never weight/units/notional) requirements are UNCHANGED —
    only the transport (direct local file read of the snapshot vs. SSH) differs from plan.md's text.
  - The snapshot is up to 15 minutes stale relative to the live DB — acceptable for this tool's
    observation-and-hypothesis-proposal purpose (FR-12: never a trading/execution/risk authority).
- **Campaign queue sizing (for the ongoing research/implementation loop, once this initial
  implementation is merged).** Target ~3 concurrent research threads and ~3 concurrent
  implementation threads — smaller than `internal-research-service`'s ~6/~6, given this repo's narrower scope.

## Initial implementation pass — built + merged (2026-07-05)

Unattended session turned the hardened spec (FR-01..FR-20) into merged, tested code. Branch
`feat/initial-implementation` → PR → squash-merge to `main`.

**Built (real, tested code):**
- `src/macro_monitor/` — `db.py` (schema: sources/raw_observations/correlations/review_runs/
  candidate_hypotheses; WAL + `foreign_keys=ON` + `busy_timeout` + SQLITE_BUSY retry; single
  canonical `dedup_key = sha256(source||url)`), `validation.py` (FR-18/FR-20 field validation),
  `sources.py` (FR-03 spot-check gate), `observations.py` (shared append-only insert + symbol
  tagging), `collector_rss.py` (FR-01: httpx fetch with redirects disabled for SSRF, feedparser
  on downloaded bytes, FR-13a failure handling), `collector_websearch_ingest.py` (the `ingest`
  write path), `correlator.py` (FR-05/06/17/18/19), `reviewer.py` (FR-07/08/09 cadence gate +
  required-field validation + terminal report rendering), `cli.py` (click:
  sources/collect-rss/ingest/correlate/review), `config.py`.
- `tests/` — 75 tests, all green; `ruff check src tests` clean. Includes the standing FR-12/FR-13
  guard (`test_no_forbidden_imports.py`: AST import ban on `algo_factory.*`/`backtest.*`; `mode=ro`
  only in `correlator.py`; **no shell-out mechanism anywhere in the package** — the structural
  guarantee behind FR-10/11/12/16), the FR-16 slash-command allowlist check, FR-18 metacharacter
  fuzz, FR-19 no-proprietary-field check, FR-14 append-only/dedup, and a network-gated live-feed
  smoke.
- `systemd/` daily collect timer+service (hardened unit: `ProtectSystem=strict`, `ReadOnlyPaths=
  /srv/paper-share`, `NoNewPrivileges`, etc.), `scripts/deploy.sh` (+ `--dry-run`) and
  `scripts/smoke_e2e.sh`, `.claude/commands/macro-monitor-{collect-websearch,review}.md` (FR-16
  tool allowlists excluding `/spec-gather`/`/spec-challenge`/backtest/gate/deploy).

**FR-17 as resolved (not plan.md's SSH text):** `correlator.py` does a plain local
`sqlite3.connect("file:/srv/paper-share/paper.db?mode=ro", uri=True)`. Because there is no
SSH/sudo/subprocess layer, FR-18's cross-layer-injection concern collapses to: the only
externally-derived value touching the query (`observed_date`) is strictly validated (YYYY-MM-DD +
real calendar date) and passed ONLY as a bound `?` parameter. The real `paper.db` schema was
confirmed from `.ctx.md` (`marks(date,symbol,price)`, `targets(date,strategy,symbol,weight,units,
notional,fill_quality)`, `gate_results(run_date,strategy,passed,...)`); the correlator's per-table
minimal-column allowlist selects only `date/run_date`, `strategy`, `symbol`, `passed` — verified
against a schema-faithful fixture.

**Verified end-to-end this session:** real Fed-press RSS feed (20 live items) → `ingest` →
`correlate` (against a schema-faithful `paper.db` fixture) → `review` producing a
`candidate_hypotheses`-shaped `reports/<slug>.md` with slug, cited `observed_date`, and the literal
FR-09 overfitting disclosure. All three configured feeds (Fed press, WSJ Markets, Yahoo Finance)
fetch + parse with a User-Agent.

**Deferred / left for a future pass (honest gaps):**
- **Live-snapshot correlate not run this session.** The session user (`preston`) is not in
  `paper-readers` and passwordless `sudo` was unavailable unattended, so `correlate` was exercised
  only against a schema-faithful fixture, not the real `/srv/paper-share/paper.db`. The live read
  path is covered by `scripts/smoke_e2e.sh`, to be run once post-deploy as `internal-monitor-service` (the
  identity that actually has the group-read grant). The code path is identical — only the file
  differs.
- **Deploy not executed** (needs root); `scripts/deploy.sh --dry-run` verified only. The systemd
  timer, service-user venv, and live snapshot read remain to be validated on the desktop.
- **WebSearch collection (FR-02) and the weekly LLM review (FR-07) are not scheduled** — out of
  scope for this pass by design. The `ingest`/`review` CLI entry points and the two FR-16
  allowlisted slash-commands exist and are unit-tested; wiring them onto a recurring `schedule`
  is a separate follow-up.
- **`/new-story` skill was unavailable** in this environment, so the implementation was driven
  directly against `steps.md` rather than via that skill.
- Known, accepted, not-fixed-now (from challenge-notes): no `ingest`-CLI authN (single-operator
  host), CHECK-enum migration cost, no feed re-verification/failure alerting, symbol-universe drift.

---

## Post-merge deploy + live-snapshot verification (2026-07-05, same day)

Ran what the merged PR's own DECISIONS.md entry (above) left deferred:

- **Deployed for real.** `scripts/deploy.sh` (no `--dry-run`) executed as root: `internal-monitor-service`'s venv
  built, package installed, `macro-monitor-collect.timer` installed + enabled.
- **Found and fixed a real bug in `paper_db_snapshot.py`** (the `paper-db-snapshot.timer` script,
  lives outside this repo at `/usr/local/lib/internal-monitor-service/paper_db_snapshot.py` on desktop — not
  tracked in this git history, note for future sessions). The live `paper.db` is WAL-mode;
  `sqlite3.Connection.backup()` copies that flag verbatim into the snapshot, so even a `mode=ro`
  reader tried to create `-wal`/`-shm` sidecar files in `/srv/paper-share/` — which `internal-monitor-service`
  can't write to (by design) — and every read failed with "attempt to write a readonly database."
  Fix: the snapshot script now runs `PRAGMA journal_mode=DELETE` on the destination connection
  right after `.backup()`, while it's still the writable owner-side connection, converting the
  on-disk copy to plain rollback-journal mode before handing it to the `paper-readers` group.
  Verified: `internal-monitor-service` can now open the snapshot, read `sqlite_master`, and `correlate` runs
  clean against real data (see below).
- **Live-snapshot `correlate` now actually verified**, not just the fixture: seeded
  `sources` with the real `fed-press` feed, ran `collect-rss` for real (20 items), ran
  `correlate --date 2026-07-02` (a date with real ingested observations) against the real
  `/srv/paper-share/paper.db` snapshot → `2` correlations written, no errors. `scripts/smoke_e2e.sh`
  passes clean end-to-end as the `internal-monitor-service` user.
- **All "deferred" bullets in the entry above are now resolved** except: WebSearch collection
  (FR-02) + weekly review (FR-07) scheduling, and the ongoing continuous-campaign infra (queues
  ~3/~3, mirroring internal-research-service's systemd campaign+sync timer pattern) — both still TODO, see below.

## Status snapshot + what's next (2026-07-05, end of this session — picking up later)

**Running unattended right now, no action needed:**
- `paper-db-snapshot.timer` (every 15 min, on desktop, outside this repo) — refreshes
  `/srv/paper-share/paper.db`.
- `macro-monitor-collect.timer` (installed by `deploy.sh`) — runs `collect-rss` on its own
  schedule. Only `fed-press` is configured as a source right now; `wsj-markets` / `yahoo-finance`
  mentioned in this repo's own earlier DECISIONS.md entry as "fetch + parse tested" were NOT added
  to the deployed `config.yaml`'s `sources` table this session — only `fed-press` was seeded via
  `macro-monitor sources add`. Add the others the same way if desired.

**NOT yet built — this is the real next step for a fresh session:**
- The ongoing `/spec-gather` → `/spec-challenge` → `/new-story` continuous-campaign loop for
  THIS repo, mirroring `internal-research-service`'s `internal-research-service-desktop-campaign.timer` +
  `internal-research-service-desktop-sync.timer` pattern (see that repo's `docs/desktop-campaign-prompt.md` and
  `.claude/skills/internal-research-service-continuous-campaign/SKILL.md` as the template to adapt), but with
  research/implementation queue targets of ~3/~3 instead of internal-research-service's ~6/~6. This needs:
  1. A `docs/desktop-campaign-prompt.md` in THIS repo adapted from internal-research-service's (queue sizes,
     repo path, and this repo's own non-negotiables — no `algo_factory` imports, no live-trading
     authority, FR-16 tool allowlist for any WebSearch-touching subagent work).
  2. Adapting or writing this repo's own equivalent of the `internal-research-service-continuous-campaign`
     skill (worktree isolation convention, kill-pre-spec convention, etc.) — or explicitly
     deciding it can reuse the internal-research-service one's *conventions* without literally sharing the file.
  3. A `desktop-campaign-cycle.sh` + `desktop-sync-deploy.sh` pair (mirror internal-research-service's scripts)
     and matching systemd `.service`/`.timer` units, installed and enabled the same way
     `internal-research-service-desktop-campaign.timer`/`internal-research-service-desktop-sync.timer` are.
  4. The `schedule`-skill-hosted recurring agents for FR-02 (WebSearch collection) and FR-07
     (weekly review) per `plan.md`'s architecture — these are Claude-Code-hosted scheduled
     routines (`.claude/commands/macro-monitor-collect-websearch.md` and
     `macro-monitor-review.md` already exist in this repo from the initial build; they just
     aren't wired onto an actual recurring `schedule` invocation yet).
- None of this is time-sensitive or fragile — the repo is in a fully clean, merged, deployed,
  verified state. A future session can resume by reading this entry plus internal-research-service's own
  campaign infra as the template.

## Fleet observability contract conformance + a confirmed permanent no-op (2026-08-01)

A fleet-wide `internal-infra` audit (`CONVENTIONS.md` §18) flagged this repo as zero-coverage
(no metrics, no log levels, no correlation id in output) and specifically flagged the daily
`correlate` step as a suspected permanent no-op. Both were investigated and fixed this session.

**The no-op was real, and confirmed against the live deployment, not just by reading the code.**
The systemd unit ran `correlate --date "$(date -u +%Y-%m-%d)"` at 06:30 UTC. `raw_observations`
is stamped with each RSS entry's own published date (`collector_rss._entry_date`), which at that
hour — the middle of the US business night — has essentially never advanced to "today" yet for
any US-business-hours source. Checked on the desktop: `correlations` held exactly 2 rows total,
both dated to a one-off manual verification recorded earlier in this file
(`correlate --date 2026-07-02`, a date deliberately chosen to match existing data), never from
the timer; the journal for the 2026-07-31 and 2026-08-01 runs both logged `no observations for
<that day's UTC date>` immediately after `collect-rss` had just inserted rows for the *previous*
day. Every automated run since deploy had silently correlated nothing.

**Fix:** `correlator.observed_dates_since(conn, since)` returns every distinct `observed_date` in
range; `correlate` with neither `--date` nor `--since` now walks a trailing window
(`Config.correlate_lookback_days`, default 3) instead of asserting one exact day. `--date` (exact,
for manual/back-fill use) and `--since` (explicit window start) remain available.
`systemd/macro-monitor-collect.service`'s second `ExecStart` line no longer passes `--date`.
Re-running an already-correlated date through this window is a safe no-op (`correlate_date` is
idempotent via `INSERT OR IGNORE` + `UNIQUE(observation_id, paper_db_table)`).

**Observability added**, per §18 (a `Type=oneshot` job cannot be scraped, so metrics go through
the node-exporter textfile collector, not an HTTP endpoint — see `src/macro_monitor/metrics.py`
and the README's new Observability section for the full account):
- `src/macro_monitor/log.py` — one canonical JSON line per event to stderr, `click.echo`'s stdout
  left alone as human-facing output.
- `src/macro_monitor/metrics.py` — `macro_monitor.prom`, merge-not-clobber across the two phases,
  atomic write.
- `run_id` (a new UUID per CLI invocation for `collect-rss`/`correlate`; `review_runs.id` itself
  for `review`, which existed in the DB but had never once appeared in any output line) is now on
  every structured log line.
- The did-nothing rule: every closing log line and the exported metrics carry both a
  work-quantity and a work-available field, so "ran and did nothing" is distinguishable from "ran
  and did work" in both signals, per §18.
- Two `assert`-based FR-19 guards in `correlator.py` (which `python -O` would strip entirely)
  became explicit `raise CorrelationError(...)`.
- Per-entry RSS validation failures (previously a silent `except Exception: continue`) now
  increment a `rejected` counter on `FeedResult` and log a WARN with the exception type (never the
  untrusted entry content itself).

**Deliberately not done in this pass:** Loki log shipping (a `internal-infra`-side Lane B change —
`config.alloy`'s allow-list plus `tools/config-drift/lane-b-registry.json` — not something this
repo can self-serve) and the corresponding `alert-rules.yml` entries (staleness, `absent()`, the
did-nothing gate against the new metrics) — both live in `internal-infra`, not here. Nothing in this
pass was deployed; `scripts/deploy.sh` was not run.

## First-ever review run + review-phase observability gap closed (2026-08-13)

- **`macro-monitor review` had never been invoked, automatically or manually, since deploy —
  confirmed against `review_runs` (0 rows) with `raw_observations` (35 rows, 2026-05-21 through
  2026-08-04) and `correlations` (18 rows) already accumulated.** Found while an `/advisor`
  research sweep in the sibling `internal-research-service` repo asked whether this repo's hypothesis reports
  were being consumed; the honest answer turned out to be one step further back — nothing had
  ever produced a report to consume, because the `review` command's own docstring names it "the
  schedule-skill agent turn" and no recurring invocation of that turn was ever wired up (this file,
  2026-08-01 entry, item 4, already named this as outstanding).
- **Ran the first review by hand** (`macro-monitor review --since 2026-05-21`, run_id 1, as the
  `internal-monitor-service` service user), reading the full backlog rather than the default 7-day window since
  this was the first-ever pass. **Verdict: zero candidate hypotheses.** The 35 observations are
  almost entirely routine community-bank enforcement press releases (e.g. "Federal Reserve Board
  issues enforcement action with former employee of Iuka Bancshares") with no plausible
  macro-mechanism; `tagged_symbols` is `[]` on every one. The 18 correlations are same-day
  coincidences with whatever crypto-momentum symbols (`strategy_b`) happened to be in
  `paper.db` that date — no `gate_results` correlations exist at all, and FR-19's minimal-field
  design means no return/direction data is even available to reason from. Proposing a hypothesis
  from this would be exactly the noise-chasing FR-09's `OVERFITTING_DISCLOSURE` exists to flag.
  Zero-hypotheses is a legitimate, designed outcome (the review command itself: "propose zero or
  more") — this is a real triage decision, not a skipped one.
- **The review phase was also the one command in this CLI that never reported through
  `_finish_phase`/`metrics.write_phase_metrics`**, unlike `collect-rss`/`correlate` (both
  instrumented in the 2026-08-01 entry above). That means a `review` that silently never ran again
  would have been invisible to node-exporter indefinitely — the same class of gap the 2026-08-01
  fix closed for the other two phases. Fixed: `review_cmd` in `src/macro_monitor/cli.py` now calls
  `_finish_phase("review", ...)` on both the failure and success/dry-run paths, so
  `macro_monitor_last_run_timestamp_seconds{phase="review"}` and the paired
  work_quantity/work_available/success gauges now exist and reflect this run (2 new tests:
  `test_review_writes_metrics_reflecting_work_done`, `test_review_writes_did_nothing_metrics_when_no_hypotheses`
  in `tests/test_cli.py`; full suite + ruff clean).
- **Deliberately not done in this pass:** wiring `/macro-monitor-review` onto an actual recurring
  invocation (this file's 2026-08-01 entry's item 4, still outstanding). Anthropic's cloud-hosted
  `schedule` routines were evaluated and ruled out — a cloud sandbox has no path to this desktop's
  live `/home/internal-monitor-service/app/data/macro_monitor.db` or the `/srv/paper-share/paper.db` snapshot, so
  the review would have nothing to read. The right shape is a local systemd timer + `claude -p`
  invocation as the `internal-monitor-service` service user, matching the pattern already proven in
  `internal-research-service/scripts/desktop_campaign_cycle.sh` (preflight, structured logging, fail-closed exit
  codes) — but that script earned its complexity through real production hardening, and a rushed
  copy is a worse outcome than an honestly-documented gap for unattended infra that reads untrusted
  scraped content. Now that the metric exists, the concrete next step is a `internal-infra` alert on
  `time() - macro_monitor_last_run_timestamp_seconds{phase="review"}` exceeding ~10-14 days (past
  the FR-07 7-day minimum), which at least makes the staleness visible even before the recurring
  invocation itself is built.

## Migrated to shared `fleet-logging` package (2026-08-14)

- **`src/macro_monitor/log.py` and `src/macro_monitor/config.py` are now thin wrappers around the
  new shared `fleet-logging` package** (git-pinned dependency in `pyproject.toml`, exact commit
  `fab3ce04dbd1b16479527fe4a512c6f8d1f960fb`, same `git+ssh://...@<commit>` pattern
  `internal-research-service/pyproject.toml` already uses for `scraper-commons`). `fleet-logging` was built as
  a strict superset of this module's own hand-rolled implementations, among two other repos' —
  see its README's "What it replaces" section. The old hand-rolled JSON formatter, redaction set,
  and yaml-to-dataclass loader were deleted, not just superseded; both files now delegate to
  `fleet_logging.log_event`/`new_run_id`/`load_config` under thin wrappers that preserve every
  existing call site's signature unchanged.
- **One real behavior gap found and closed during the swap, not shipped silently**:
  `fleet_logging.load_config`'s own internal `config.missing`/`config.parse_failed` log lines have
  no `stream=` override and default to stdout, but this repo's §18 contract requires every
  machine-readable log line on stderr (kept separate from `click.echo`'s human-facing stdout).
  `config.py`'s `load_config` now wraps the `fleet_logging.load_config` call in a narrow,
  restored-in-`finally` stdout→stderr redirect for that one synchronous call only (the first thing
  `cli.py`'s `main()` does, before any `click.echo` output exists). Caught by
  `tests/test_config.py`, added in this pass — `config.py` had zero dedicated test coverage
  before.
- **`env_prefix="MACRO_MONITOR_"` passed explicitly** to `load_config`: the shared package adds an
  env-var-overlay capability this repo never had before (bare uppercased field names by default,
  e.g. `DB_PATH`). Given this service runs live under systemd and those bare names are generic
  enough to collide with an unrelated future env var by accident, every override is namespaced
  under `MACRO_MONITOR_` instead. Nothing today sets any of these names, prefixed or not, so this
  changes no resolved config value right now.
- **A second real behavior gap found in code review, also closed before shipping**:
  `fleet_logging.load_config` unconditionally calls python-dotenv's `load_dotenv()` unless a
  `dotenv_path=` is given — with no path, that walks *up the filesystem tree from wherever the
  `fleet_logging` package itself is installed* (site-packages, not this repo's cwd) looking for a
  file literally named `.env`, and silently overlays whatever it finds into `os.environ`. This
  repo's original loader never touched `.env` at all. `config.py` now pins an explicit,
  guaranteed-nonexistent `dotenv_path=` (`_NO_DOTENV`) so that call is always a no-op, matching
  the original behavior exactly. Verified by a test that spies on the actual `load_dotenv` call
  arguments (`tests/test_config.py::test_load_config_never_triggers_dotenv_tree_walk`), not just an
  absence of symptoms — a real ambient `.env` above site-packages isn't something a test can force.
- **`requires-python` bumped `>=3.10` → `>=3.11`**: `fleet-logging` requires Python 3.11+. CI
  already runs 3.12 and the desktop deploy host's `python3` is 3.12.3, so this raises the declared
  floor to match what was already true in practice. The bump surfaced `ruff`'s `UP017` rule
  (`datetime.UTC` alias) against four pre-existing, unrelated call sites
  (`db.py`, `reviewer.py` x3, `test_reviewer.py` x2) — fixed via `ruff check --fix`; `datetime.UTC`
  is the same object as `datetime.timezone.utc`, not a behavior change.
- Full test suite (110 tests, including the 7 new `test_config.py` cases) and `ruff check` both
  clean after the swap. Deployed via `scripts/deploy.sh` and smoke-verified the same day.
