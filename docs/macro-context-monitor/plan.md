# Plan: Macro Context Monitor

## Approach

Build this as a new sibling repo, `algo-macro-monitor` (confirmed unclaimed under `~/dev/`), following the
`algo-corpus` precedent exactly: separate git hygiene wall, separate service user, separate deploy, zero
package dependency on `algo_factory`. Two mechanically distinct execution paths do the work: a headless
systemd-timer Python process handles the cheap, deterministic RSS/Atom collection and the read-only
`paper.db` correlation (no LLM, no Claude Code session involved at all — same shape as `paper-track.timer`);
a Claude-Code-hosted scheduled agent (via the `schedule` skill) handles everything that structurally requires
an LLM-capable host — the `WebSearch`-based message-board collection (FR-02, since `WebSearch` is a Claude
Code tool, not an installable library, and paid search APIs are out of scope) and the weekly hypothesis
review (FR-07). This split isn't a stylistic choice — it is the cleanest way to make FR-07's "no code path
invokes the LLM review synchronously from the deterministic collector" true by construction: the two paths
don't share a process, a call stack, or even a host environment.

## Architecture

```
 desktop-agent (systemd, service user algo-macro, mirrors algo-factory's paper-track pattern)
 ┌────────────────────────────────────────────────────────────────────────┐
 │ macro-monitor-collect.timer  (daily, e.g. 06:30 UTC, Persistent=true)   │
 │        │                                                                │
 │        v                                                                │
 │ macro-monitor collect-rss   (FR-01 — plain HTTP + XML parse, no LLM)    │
 │   Fed press RSS / WSJ Markets RSS / Yahoo Finance RSS                  │
 │        │  writes                                                       │
 │        v                                                                │
 │ macro_monitor.db :: raw_observations  (append-only, FR-04/FR-14)       │
 │        │                                                                │
 │        v                                                                │
 │ macro-monitor correlate --date <observed_date>  (FR-05/FR-06/FR-17/18/19)│
 │   ssh (dedicated, narrowly-scoped identity -- NOT the existing agent    │
 │   NOPASSWD-ALL grant, per FR-17); argv-list subprocess, parameterized   │
 │   SQL, validated observed_date (FR-18); SELECTs only symbol/date/       │
 │   strategy/pass-fail, never weight/units/notional (FR-19), against     │
 │   /home/algo-factory/app/data/paper.db  (mode=ro, never writes)        │
 │        │  writes                                                       │
 │        v                                                                │
 │ macro_monitor.db :: correlations                                        │
 └────────────────────────────────────────────────────────────────────────┘

 Claude Code (schedule skill — cron-like recurring agent invocation, no bare systemd process)
 ┌────────────────────────────────────────────────────────────────────────┐
 │ scheduled routine: /macro-monitor-collect-websearch   (daily-ish)       │
 │   scoped WebSearch queries (site:reddit.com, site:news.ycombinator.com)│
 │   per tracked symbol — NEVER a direct WebFetch to reddit.com (FR-02)   │
 │        │  shells out to                                                │
 │        v                                                                │
 │   macro-monitor ingest --source websearch ...  → raw_observations      │
 │        │                                                                │
 │        v                                                                │
 │   macro-monitor correlate --date <observed_date>  (same as above)      │
 └────────────────────────────────────────────────────────────────────────┘
 ┌────────────────────────────────────────────────────────────────────────┐
 │ scheduled routine: /macro-monitor-review   (weekly, >= 7 days, FR-07)  │
 │   reads raw_observations + correlations since last review_runs row     │
 │   ONE offline-lane LLM turn (this Claude Code agent invocation itself  │
 │   IS the "LLM call" — no separate API hop needed)                      │
 │        │  writes (only if FR-08/FR-09 fields all present — else no-op) │
 │        v                                                                │
 │   macro_monitor.db :: candidate_hypotheses  +  reports/<slug>.md       │
 └────────────────────────────────────────────────────────────────────────┘
                          │
                          │  TERMINAL ARTIFACT — no automation past here (FR-10/FR-11)
                          v
        Preston (or an agent, on his instruction) reads reports/<slug>.md,
        manually runs, inside algo-factory:  /spec-gather <slug>
                          │
                          v
        docs/{slug}/ → /spec-challenge → backtest → gates → docs/DECISIONS.md
        (existing pipeline, unmodified, sole evaluation path)
```

Nothing in either path imports, calls, or writes to `algo_factory.execution`, `algo_factory.risk`,
`backtest/gates.py`, or `targets`/`marks` in `paper.db`. The correlation step opens `paper.db` read-only
over a DEDICATED, narrowly-scoped credential (FR-17) — never the existing `agent` identity's
NOPASSWD-for-all-commands sudo grant, since extending that grant to a new tool that ingests untrusted
RSS/WebSearch content is a real privilege-escalation surface (identified during spec-challenge: a bug
or compromise in this tool would be one step from full root on the same machine running live
paper-trading infrastructure). Concretely, provision either (a) a forced-command SSH key
(`command="/home/algo-macro/app/bin/ro_query.py"` in that identity's `authorized_keys`, so the key can
run nothing else), or (b) a `Cmnd_Alias`-scoped sudoers entry limited to the exact read-only query
script with no wildcards. The command chain (SSH → remote script → SQL) is built entirely via
argv-list `subprocess.run(..., shell=False)` and parameterized SQL placeholders — never string
interpolation of any externally-derived value — and `observed_date` is validated against a strict
`YYYY-MM-DD` pattern before it touches any layer (FR-18). The correlator SELECTs only the minimal
fields needed for correlation (symbol, date, strategy, gate pass/fail) — never full `targets`/`marks`
rows, which carry proprietary `weight`/`units`/`notional` data this tool has no reason to copy off the
hardened desktop host into a less-audited sibling repo's local database and, later, into an LLM
prompt (FR-19).

Both the scheduled WebSearch-collection routine and the weekly-review routine run with an explicit
tool/skill allowlist (FR-16) excluding `/spec-gather`, `/spec-challenge`, and any backtest/gate
command — this is what actually makes FR-10's "no automatic hand-off" true, since without it nothing
would stop an LLM-hosted scheduled agent turn from invoking those commands itself mid-turn.

## Data model

New SQLite database, `data/macro_monitor.db`, owned by `algo-macro-monitor` (gitignored, WAL mode — same
pattern as `algo_factory`'s `research.db`/`paper.db`, but this repo's copy is explicitly **not** versioned,
unlike algo-factory's deliberately-committed data files, because this is raw scraped content per FR-15).

```sql
-- Source allowlist. FR-03: a row lacking checked_on is rejected by the app-level
-- validator before INSERT (the CHECK below is defense-in-depth, not the primary gate).
CREATE TABLE sources(
    name         TEXT PRIMARY KEY,             -- 'fed-press', 'wsj-markets', 'yahoo-finance', 'websearch-wsb'
    kind         TEXT NOT NULL CHECK (kind IN ('rss','websearch')),
    url_or_query TEXT NOT NULL,                 -- feed URL, or a WebSearch query template
    fetchable    INTEGER NOT NULL CHECK (fetchable IN (0,1)),
    checked_on   TEXT NOT NULL,                 -- YYYY-MM-DD; NOT NULL is the FR-03 spot-check gate
    notes        TEXT
);

-- Raw, append-only observation log. FR-04, FR-14.
CREATE TABLE raw_observations(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_date    TEXT NOT NULL,              -- YYYY-MM-DD
    source           TEXT NOT NULL REFERENCES sources(name),
    url              TEXT NOT NULL,
    title_or_snippet TEXT NOT NULL,
    tagged_symbols   TEXT NOT NULL DEFAULT '[]', -- JSON array; may be empty, never null
    collected_at     TEXT NOT NULL,              -- ISO8601 UTC, collector run timestamp
    dedup_key        TEXT NOT NULL UNIQUE        -- sha256(source||url); re-collecting is a no-op, never an overwrite
);
CREATE INDEX ix_raw_obs_date ON raw_observations(observed_date);

-- Read-only paper.db correlation results, one row per (observation, table) match. FR-05/FR-19.
-- row_json holds ONLY symbol/date/strategy/gate-pass-fail fields -- never weight/units/notional
-- (those are proprietary strategy internals; FR-19 forbids selecting or storing them here).
CREATE TABLE correlations(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES raw_observations(id),
    paper_db_table TEXT NOT NULL CHECK (paper_db_table IN ('targets','marks','gate_results')),
    row_json       TEXT NOT NULL CHECK (json_valid(row_json)), -- JSON array of MINIMAL fields only, see above
    fetched_at     TEXT NOT NULL,
    UNIQUE(observation_id, paper_db_table)       -- re-running correlate for an already-correlated
                                                  -- date must not duplicate rows (identified during
                                                  -- spec-challenge — silently violated AC-4 otherwise)
);
CREATE INDEX ix_corr_obs ON correlations(observation_id);
-- NB: targets.date / marks.date use column "date"; gate_results uses "run_date" — the
-- correlator's query builder keys off paper_db_table to pick the right column name,
-- never assumes they're interchangeable (this is the FR-05 test's specific trap).
--
-- db.py must set `PRAGMA foreign_keys=ON` on every connection in every writer path (systemd-timer
-- process AND schedule-skill-hosted process) — SQLite does not enforce FK constraints by default,
-- so the REFERENCES clauses above are decorative until this is set explicitly (identified during
-- spec-challenge). db.py must also set `PRAGMA busy_timeout=5000` (or larger) plus an app-level
-- retry-with-backoff on SQLITE_BUSY: WAL mode allows concurrent readers but still serializes writers,
-- and the systemd-timer process and the schedule-skill process are two independent OS processes that
-- can genuinely overlap — without a busy_timeout, one writer's INSERT can fail outright when it
-- collides with the other's transaction, silently breaking the FR-14 append-only guarantee for that
-- item (identified during spec-challenge).
--
-- tagged_symbols/cited_observation_ids/cited_paper_db_summary (below) are also JSON-in-TEXT and
-- should carry the same CHECK(json_valid(...)) defense-in-depth as row_json above.
--
-- Migration note: `sources.kind`, `correlations.paper_db_table`, and `candidate_hypotheses.status`
-- are CHECK-constrained enums. SQLite has no ALTER-COLUMN for CHECK constraints, so adding a value
-- later (e.g. an 'api' source kind for FRED/policyuncertainty.com, or a new paper.db table) requires
-- the rename-recreate-copy-drop dance against a live WAL file with multiple writer processes —
-- budget for this explicitly rather than treating it as a trivial migration when it comes up
-- (identified during spec-challenge; not fixed now — accepted as a known, documented limitation
-- rather than solved with a premature schema-versioning system this v1 doesn't need).

-- One row per weekly review invocation. Bookkeeping for cadence + cost tracking.
-- The 7-day cadence gate (FR-07) keys off the last SUCCESSFUL run (status='ok'),
-- not the last STARTED run -- a crashed run (status='failed') must not poison the
-- gate for a week (identified during spec-challenge).
CREATE TABLE review_runs(
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at               TEXT NOT NULL,
    completed_at             TEXT,                -- NULL until the run finishes
    status                   TEXT NOT NULL DEFAULT 'started'
                             CHECK (status IN ('started','ok','failed','dry-run')),
    window_start             TEXT NOT NULL,      -- oldest observed_date considered
    window_end               TEXT NOT NULL,
    observations_considered  INTEGER NOT NULL,
    llm_model                TEXT NOT NULL,
    llm_tokens_in            INTEGER,
    llm_tokens_out           INTEGER
);

-- Candidate hypotheses. FR-08/FR-09 required fields; a report missing any of
-- these is rejected by the app validator before INSERT and never written to disk.
CREATE TABLE candidate_hypotheses(
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    review_run_id          INTEGER NOT NULL REFERENCES review_runs(id),
    slug                   TEXT NOT NULL UNIQUE, -- must match ^[a-z0-9]+(-[a-z0-9]+)*$
    mechanism_description  TEXT NOT NULL,        -- one paragraph
    cited_observation_ids  TEXT NOT NULL,        -- JSON array of raw_observations.id, >= 1 element
    cited_paper_db_summary TEXT NOT NULL,        -- JSON: which targets/marks/gate_results rows grounded this
    overfitting_disclosure TEXT NOT NULL,         -- must contain the literal FR-09 disclosure string
    status                 TEXT NOT NULL DEFAULT 'proposed'
                           CHECK (status IN ('proposed','spec-gathered','rejected','deferred')),
    created_at             TEXT NOT NULL
);
```

`status` is hand-updated by Preston (or an agent on his instruction) after he runs `/spec-gather` and the
pipeline resolves — this is bookkeeping only, never read by any automated code path, and never used to
trigger anything.

## API / interface contract

No HTTP endpoints, no UI. CLI-only, plus two Claude Code slash-commands invoked by the `schedule` skill.

- `macro-monitor sources add --name NAME --kind rss|websearch --url-or-query URL --checked-on YYYY-MM-DD [--fetchable true|false] [--notes TEXT]`
  — fails validation (nonzero exit, no row written) if `--checked-on` is omitted (FR-03, acceptance criterion 3).
- `macro-monitor sources check NAME` — performs one live fetch/query against an existing source, updates
  `fetchable`/`checked_on`. Does not add new sources.
- `macro-monitor collect-rss` — runs FR-01 collection against all `sources` rows where `kind='rss' AND fetchable=1`.
  Exit codes: 0 (>=1 feed succeeded), 1 (all feeds failed — logged, no crash-loop). Zero LLM calls; static-checkable.
- `macro-monitor ingest --source NAME --observed-date DATE --url URL --title-or-snippet TEXT [--symbols CSV]`
  — the write path the WebSearch-driven slash-command shells out to per result; enforces FR-04's required
  fields, FR-20's input validation, and FR-14's append-only/dedup behavior. Uses the exact same
  `dedup_key = sha256(source||url)` formula as `collect-rss` (both defined once in `db.py`, never
  reimplemented independently per-collector) — a divergent formula between the two collectors would
  silently defeat cross-source dedup while each collector's own unit test still passes (identified
  during spec-challenge).
- `macro-monitor correlate --date YYYY-MM-DD` — read-only `paper.db` query per FR-05/FR-06/FR-17/FR-18/
  FR-19. When invoked by the systemd-timer process (already running ON desktop-agent), this is a LOCAL
  `sudo -n <ro_query_script>` call — no SSH self-loopback. When invoked from the schedule-skill-hosted
  routines (running on the Mac), this is a genuine `ssh desktop-agent` hop to the same local call on the
  remote side. Either way, the identity used is the dedicated scoped credential from FR-17, never the
  `agent` user's NOPASSWD-ALL grant, and the command chain is argv-list + parameterized SQL per FR-18.
  Errors (unreachable host, credential denied, schema mismatch) are logged and leave `correlations`
  untouched for that date — never a partial/garbled write.
- `macro-monitor review [--since DATE] [--dry-run]` — refuses to run if fewer than 7 days have elapsed
  since the last SUCCESSFUL `review_runs` row (`status='ok'`, keyed off `completed_at` — a crashed or
  in-progress run never blocks the next attempt; identified during spec-challenge) (config default,
  overridable only to a *larger* value, never smaller, per FR-07/NFR). Invoked only from the
  `/macro-monitor-review` scheduled slash-command, never from `collect-rss` or `ingest`.
- Report artifact: `reports/<slug>.md` — human-readable rendering of one `candidate_hypotheses` row,
  written alongside the DB insert. This file plus the row are the terminal output; nothing consumes them
  automatically (FR-10).
- Hand-off contract (manual, by design): Preston reads `reports/<slug>.md`, then in an algo-factory Claude
  Code session runs `/spec-gather <slug>`. No code in `algo-macro-monitor` names or invokes `/spec-gather`,
  `/spec-challenge`, the backtest runner, or any gate-evaluation function (FR-10/FR-11, acceptance criteria 6 & 10).

## Integration points

algo-factory side (spec-only — no `src/` changes):
- `docs/macro-context-monitor/plan.md` — this file.
- `docs/macro-context-monitor/steps.md` — sequencing for implementation (written next, not by this plan).
- `docs/DECISIONS.md` — append-only: (a) an entry when this tool itself is scoped/built (per repo
  convention every non-trivial addition gets a chronicle entry), and (b) one entry per candidate hypothesis
  this tool surfaces that Preston evaluates and rejects/defers, matching the existing convention already
  used for the three killed lanes this feature must stay distinguishable from.
- `docs/MODEL_POLICY.md` — read-only reference; no edit required. The weekly review step's model tier
  (offline/slow lane) is already covered by the existing "research, strategy design, validation reasoning,
  monitoring, post-trade analysis" row — nothing new to add there.
- Explicitly **not touched**: anything under `src/algo_factory/` — no new module, no edit, per FR-12/FR-13.
  The only algo-factory artifact this tool reads is `data/paper.db` on the desktop, read-only, over SSH.

algo-macro-monitor side (new sibling repo, proposed layout, paths relative to its own root):
- `README.md` — project overview and quickstart guide for contributors/deployers.
- `pyproject.toml` — own dependency set; **must not** list `algo-factory`/`algo_factory` as a dependency —
  this is the structural enforcement of FR-12's "zero imports" requirement, not just a code-review rule.
- `.env.example` / `.env` (gitignored) — `DESKTOP_AGENT_HOST`, `LLM_GATEWAY_URL` (fallback path only, see
  Technology choices), no scraping API keys needed for the RSS/WebSearch-only source set in scope.
- `.gitignore` — `data/macro_monitor.db`, `data/*.db-wal`, `data/*.db-shm`, `reports/*.md`, `.env` (FR-15).
- `config.example.yaml` / `config.yaml` (gitignored) — desktop-agent SSH target, cadence overrides, symbol
  universe snapshot (manually re-synced from `paper.db`'s `targets`/`marks` distinct symbols; not a live
  import of `algo_factory`).
- `src/macro_monitor/collector_rss.py` — FR-01 implementation.
- `src/macro_monitor/collector_websearch_ingest.py` — the `ingest` CLI subcommand the WebSearch
  slash-command shells out to; contains no `WebSearch` call itself (that lives in the Claude Code
  slash-command, not in installable Python); owns FR-20's field validation (date format, URL scheme,
  length caps, ticker-charset check) before any row is written.
- `src/macro_monitor/correlator.py` — FR-05/FR-06/FR-17/FR-18/FR-19; owns the `targets.date` vs
  `marks.date` vs `gate_results.run_date` column-name mapping, the minimal-field SELECT (FR-19), and
  the argv-list-subprocess + parameterized-SQL read path (FR-18) over the dedicated scoped credential
  (FR-17).
- `src/macro_monitor/reviewer.py` — FR-07/FR-08/FR-09 validation (rejects any report missing the slug
  pattern, a cited `observed_date`, or the overfitting-disclosure string, before it reaches the DB or disk).
- `src/macro_monitor/db.py` — schema + connection helpers (mirrors `algo_factory.research.ledger` style:
  plain stdlib `sqlite3`, `PRAGMA journal_mode=WAL`, `CREATE TABLE IF NOT EXISTS`).
- `src/macro_monitor/cli.py` — `sources`/`collect-rss`/`ingest`/`correlate`/`review` subcommands.
- `.claude/commands/macro-monitor-collect-websearch.md` — the slash-command the `schedule` skill invokes
  daily; scoped `WebSearch` queries only, per FR-02; declares the FR-16 tool/skill allowlist excluding
  `/spec-gather`/`/spec-challenge`/`/new-story`/any backtest or deploy command.
- `.claude/commands/macro-monitor-review.md` — the slash-command the `schedule` skill invokes weekly;
  same FR-16 tool/skill allowlist restriction as above (this is the more safety-critical of the two,
  since it's the step that reads accumulated untrusted content and produces the report — FR-16/FR-18
  are a defense-in-depth pair here).
- `scripts/deploy.sh` — desktop deploy under a new dedicated nologin service user (e.g. `algo-macro`),
  mirroring `algo-corpus/scripts/deploy.sh` and `algo-factory`'s existing service-user convention;
  additionally provisions the FR-17 minimal-privilege `paper.db` read path (the forced-command SSH key
  or `Cmnd_Alias`-scoped sudoers entry) — this credential does not exist until `deploy.sh` creates it,
  it is not something the implementer sets up "by hand, off the books" (identified during
  spec-challenge as a real gap in an earlier draft).
- `systemd/macro-monitor-collect.service` + `.timer` — daily, `Type=oneshot`, mirrors `paper-track.timer`.
- `tests/test_no_forbidden_imports.py` — a standing grep/AST-based test asserting the package imports
  nothing under `algo_factory.execution`, `algo_factory.risk`, `backtest.gates`, and asserting `paper.db`
  is never opened outside `correlator.py`'s read-only path (defense-in-depth for FR-12/FR-13's static-review
  acceptance criteria).

## Technology choices

- **`schedule` skill (Claude Code)** for the WebSearch collector and the weekly review — both steps
  structurally require an LLM-capable host (`WebSearch` is a Claude Code tool, not an installable library;
  the review step's "LLM call" is the agent turn itself), so a bare systemd Python job cannot do either
  without adding a paid search API, which is out of scope.
- **`feedparser`** (new dep, `algo-macro-monitor` only) for RSS/Atom parsing — handles the real-world feed
  format variance across Fed/WSJ/Yahoo more robustly than hand-rolled `xml.etree`.
- **`httpx`** (new dep) for HTTP fetch with explicit timeouts — avoids `requests`' default of no timeout,
  which matters for a scheduled job that must not hang the timer.
- **stdlib `sqlite3` + WAL**, no ORM — matches the exact pattern already proven in
  `src/algo_factory/research/ledger.py`; no new concept for anyone reading both repos.
- **SSH subprocess to `desktop-agent`** for `paper.db` reads, via a DEDICATED scoped credential (FR-17)
  — deliberately NOT the existing `agent` identity's NOPASSWD-for-all-commands channel; a new,
  untrusted-content-consuming tool gets a new, narrowly-scoped grant, not an extension of an
  unrestricted one. argv-list `subprocess.run`, parameterized SQL, validated inputs throughout
  (FR-18).
- **llm-gateway (fallback, not primary path)** — if the `schedule` skill's cloud-routine mechanism turns
  out not to support the desired unattended cadence in production, `reviewer.py` can fall back to a direct
  HTTP call to the existing `llm-gateway` (port 4000) using an offline-lane model per `MODEL_POLICY.md`;
  flagged here as a design option, not built until the primary path is confirmed insufficient (see Risk areas).

## Risk areas

- **`schedule`-skill production viability, honestly uncertain.** The whole architecture leans on the
  `schedule`/`loop` mechanism being able to run two recurring, unattended, indefinite-horizon jobs (daily
  WebSearch collection, weekly review) tied to a Claude Code account/session rather than a bare OS cron.
  Whether that holds up long-term on the desktop (vs. requiring a Mac to be awake, vs. plan/session limits)
  is not fully validated in this plan — if it doesn't, FR-02 and FR-07 need the llm-gateway fallback above,
  which adds real implementation work not currently budgeted.
- **Cost (honest order-of-magnitude, not hand-waved).** With daily RSS collection bounded to 3 feeds and a
  WebSearch pass scoped to the ~10 tracked strategies' instruments, a week's accumulated log is roughly
  150-300 short items (title+snippet) plus small `targets`/`marks`/`gate_results` correlation rows — call
  it 25k-70k input tokens per weekly review, with 0-5 hypothesis reports (roughly 500-1500 tokens each) as
  output, so 1k-7k output tokens. At Sonnet-class offline-lane pricing that's roughly **$0.20-$0.50 per
  weekly review run** (under $2/month, under ~$25/year even generously). This estimate breaks down if log
  volume balloons beyond the NFR's bounded-symbol-universe scope (e.g. per-symbol WebSearch queries firing
  far more often than daily, or feed count growing unchecked) — it is bounded by design, not by enforcement,
  so a runaway config change could silently 10x this without a hard guard rail in place today.
  **Human review time:** roughly 10-20 minutes/week to read a report and decide go/no-go; the subsequent
  `/spec-gather` → `/spec-challenge` → backtest → gate pipeline is pre-existing effort this tool doesn't add to.
- **`paper.db` access is a fragile external dependency.** Every correlation/review run depends on
  `desktop-agent` being reachable, the dedicated scoped credential (FR-17) continuing to work
  non-interactively, and the `algo-factory` service user's ownership/permissions on `paper.db` staying
  exactly as confirmed this session. Any change on the desktop side breaks this tool silently — the
  failure mode is "produces no correlation for that date," not corruption (good), but there's no current
  alerting on that failure, so it could go unnoticed for weeks.
- **Reused-NOPASSWD-sudo privilege escalation and shell/SQL injection — identified during
  spec-challenge, now closed by FR-17/FR-18/FR-19.** The original draft reused the existing `agent`
  identity's unrestricted sudo grant and built the SSH→sudo→python3→SQL chain via string
  interpolation, which a Security Auditor reviewer correctly flagged as a real vulnerability class: a
  bug or compromise in this new, untrusted-content-consuming tool would have been one step from full
  root on the machine also running live paper-trading infrastructure, and any externally-derived value
  (an `observed_date` or symbol ultimately sourced from RSS/WebSearch content) spliced into any of the
  three nested layers could break out via shell metacharacters. Fixed via a dedicated scoped credential,
  argv-list subprocess invocation, and parameterized SQL — see Architecture and Data model above.
- **`feedparser`/`httpx` dependency risk (identified during spec-challenge).** `feedparser` has
  historically been vulnerable to XXE/entity-expansion when parsing untrusted XML; since RSS sources
  are attacker-reachable (a compromised or newly-added feed), pin a modern version and confirm no
  external entity resolution before Step 5 ships. `httpx`'s redirect-following could resolve to an
  internal/private IP (SSRF) if a feed URL redirects there — disable redirect-following or validate the
  final resolved host is public before Step 5 ships.
- **FR-12/FR-13 boundary is enforced by absence-of-dependency today, not by a standing test yet.** The
  `pyproject.toml`-has-no-`algo_factory`-dependency design makes an accidental import fail immediately at
  install/import time, which is strong — but nothing in this plan has yet written the
  `tests/test_no_forbidden_imports.py` grep/AST check that would make this verifiable in CI rather than by
  manual code review at each `/spec-challenge`. Listed in Integration points; must actually get built, not
  just planned.
- **Symbol-universe drift.** The tracked-strategy instrument list is a manually-synced snapshot in
  `algo-macro-monitor`'s own config, not a live read of `paper.db`'s `targets`/`marks` distinct symbols
  (deliberately, to avoid a live dependency edge into algo-factory). If Preston adds a new sleeve/instrument
  in `paper.db` and forgets to re-sync the snapshot, new observations for that instrument simply won't get
  `tagged_symbols` — a quiet gap, not a crash.
- **Prompt-injection surface, low blast radius but worth naming.** The weekly review feeds untrusted
  scraped text (RSS titles, WebSearch snippets) into an LLM prompt. A crafted snippet could attempt to
  manipulate the review agent's output. Blast radius is bounded by design — the review step's only possible
  effect is a report file + a DB row, never a tool call with side effects beyond that — but it's a real
  input-trust boundary worth a sentence rather than silence.
