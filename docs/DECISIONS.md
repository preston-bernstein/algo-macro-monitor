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
