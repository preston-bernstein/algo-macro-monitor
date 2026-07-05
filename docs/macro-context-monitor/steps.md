# Steps: Macro Context Monitor

## Prerequisites

None. This implementation creates a new sibling repository (`algo-macro-monitor`) from scratch, following the `algo-corpus` pattern. No existing work must be done first; however, the implementer must have:
- SSH key configured for `desktop-agent` access (per home lab setup in CLAUDE.md)
- Ability to run `sudo -n` on desktop-agent without password prompt
- Claude Code session with `schedule` skill available for slash-commands
- Access to confirmed fetchable RSS feeds (Fed press, WSJ Markets, Yahoo Finance) for validation

## Implementation steps

### Step 1: Create algo-macro-monitor repo skeleton
**What**: Initialize new git repository with directory structure, .gitignore, README, pyproject.toml, and empty module directories matching the layout described in plan.md.
**Files**: .gitignore, README.md, pyproject.toml, src/macro_monitor/__init__.py, tests/__init__.py, .claude/, systemd/, scripts/
**Test**: `cd ~/dev/algo-macro-monitor && git status` shows clean repo; `grep algo.factory pyproject.toml` returns nothing (no algo_factory dependency).
**Depends on**: none
**Parallelizable**: No

### Step 2: Write SQLite schema and db.py helpers
**What**: Define macro_monitor.db schema (sources, raw_observations, correlations, review_runs, candidate_hypotheses tables per plan's data model) and connection helpers (WAL mode, read-only mode for paper.db access).
**Files**: src/macro_monitor/db.py
**Test**: `python -c "import sys; sys.path.insert(0, 'src'); from macro_monitor.db import init_db; init_db(':memory:')"` creates all tables without error; `pytest -xvs tests/ -k schema` passes if a schema validation test exists.
**Depends on**: Step 1
**Parallelizable**: No

### Step 3: Write standing guard test (test_no_forbidden_imports.py)
**What**: Create test that asserts zero imports of algo_factory.execution, algo_factory.risk, backtest.gates, and zero write-mode opens to paper.db outside correlator.py's read-only path (FR-12/FR-13 compliance gate).
**Files**: tests/test_no_forbidden_imports.py
**Test**: `pytest tests/test_no_forbidden_imports.py -xvs` passes; test uses ast/grep to scan src/ for forbidden imports and confirms only correlator.py may reference paper.db.
**Depends on**: Step 1, Step 2
**Parallelizable**: Yes

### Step 4: Write CLI infrastructure (cli.py)
**What**: Set up Click CLI app with subcommand routing skeleton; define --help, --version; establish import structure for subcommands to be wired in later (Step 10).
**Files**: src/macro_monitor/cli.py
**Test**: `python -m macro_monitor.cli --help` displays help text; `python -m macro_monitor.cli --version` shows version string.
**Depends on**: Step 1, Step 2
**Parallelizable**: No

### Step 5: Implement collect-rss subcommand and collector_rss module
**What**: Implement FR-01 RSS/Atom feed collection logic (feedparser integration, date parsing, dedup detection via sha256 hashing, append-only observation logging to raw_observations table).
**Files**: src/macro_monitor/collector_rss.py
**Test**: `python -c "import sys; sys.path.insert(0, 'src'); from macro_monitor.collector_rss import fetch_and_log_rss; import tempfile; db = tempfile.mktemp(); fetch_and_log_rss(db, 'test-source', 'https://www.federalreserve.gov/feeds/press_all.xml')"` returns 0 (success) or 1 (all feeds failed, expected for test env); inspect db file contains new raw_observations rows.
**Depends on**: Step 2, Step 4
**Parallelizable**: Yes

### Step 6: Implement correlate subcommand and correlator module
**What**: Implement FR-05/FR-06 read-only correlation logic; open paper.db via SSH subprocess (`ssh desktop-agent sudo -n python3 -c "import sqlite3; ..."`); fetch targets/marks/gate_results rows for each observed_date (using correct column names: targets.date, marks.date, gate_results.run_date); store results as JSON in correlations table.
**Files**: src/macro_monitor/correlator.py
**Test**: Mock test: `python -c "from macro_monitor.correlator import build_query; q = build_query('targets', '2026-07-04'); assert 'date' in q"` and `q2 = build_query('gate_results', '2026-07-04'); assert 'run_date' in q2` (no actual SSH needed for unit test); integration test requires desktop-agent access and will be verified during deploy.
**Depends on**: Step 2, Step 4
**Parallelizable**: Yes

### Step 7: Implement sources management module and subcommands
**What**: Implement FR-03 sources add/check/list commands; enforce checked_on field presence before INSERT (validation in Python, plus CHECK constraint in schema); verify URL is fetchable via test fetch; store fetchable status (true/false, checked_on date).
**Files**: src/macro_monitor/sources.py or inline command handlers in cli.py
**Test**: `python -c "from macro_monitor.sources import add_source; add_source(db, 'test', 'rss', 'http://example.com', None, {})` raises ValueError (missing checked_on); with checked_on='2026-07-04' succeeds and row is written.
**Depends on**: Step 2, Step 4
**Parallelizable**: Yes

### Step 8: Implement ingest subcommand and collector_websearch_ingest module
**What**: Implement FR-04 observation ingestion from WebSearch results; enforce all required fields (observed_date, source, url, title_or_snippet, tagged_symbols); implement dedup via sha256(source||url) UNIQUE constraint; append-only insert (never overwrite).
**Files**: src/macro_monitor/collector_websearch_ingest.py
**Test**: `python -c "from macro_monitor.collector_websearch_ingest import ingest; ingest(db, '2026-07-04', 'websearch-wsb', 'http://example.com/1', 'SPY up', ['SPY'])"` returns success; re-running identical ingest is no-op or returns "already exists"; attempt without title_or_snippet raises error.
**Depends on**: Step 2, Step 4
**Parallelizable**: Yes

### Step 9: Implement review subcommand and reviewer module
**What**: Implement FR-07/FR-08/FR-09 hypothesis review validation; read raw_observations and correlations since last review_runs row; validate slug format (^[a-z0-9]+(-[a-z0-9]+)*$), require mechanism_description, cited_observation_ids (JSON array, >=1 element), overfitting_disclosure (must contain exact FR-09 disclosure string); refuse to run if <7 days since last review_runs.started_at (default cadence, configurable to larger only); reject any report missing required fields before DB write or report file write.
**Files**: src/macro_monitor/reviewer.py
**Test**: `python -c "from macro_monitor.reviewer import validate_hypothesis; validate_hypothesis({'slug': 'test-slug', 'mechanism_description': '...', 'cited_observation_ids': [1], 'overfitting_disclosure': 'REQUIRED_STRING_HERE'})"` passes; missing slug raises ValueError; slug='bad slug' (spaces) raises ValueError; overfitting_disclosure without exact disclosure string raises ValueError.
**Depends on**: Step 2, Step 4
**Parallelizable**: Yes

### Step 10: Wire all subcommands into cli.py
**What**: Update cli.py to import collect_rss, correlate, sources, ingest, and review subcommand handlers from their respective modules; register as Click commands/groups; ensure --help displays all five subcommands.
**Files**: src/macro_monitor/cli.py (modify Step 4)
**Test**: `python -m macro_monitor.cli --help` lists: collect-rss, correlate, sources (with add/check/list), ingest, review; `python -m macro_monitor.cli collect-rss --help` shows options for RSS collection.
**Depends on**: Step 4, Steps 5-9
**Parallelizable**: No

### Step 11: Write systemd service and timer files
**What**: Create macro-monitor-collect.service (Type=oneshot, runs `~algo-macro/bin/macro-monitor correlate --date <TODAY>` after RSS collection) and macro-monitor-collect.timer (daily, 06:30 UTC, Persistent=true, matches paper-track.timer pattern). Service assumes algo-macro service user with home directory /home/algo-macro/.
**Files**: systemd/macro-monitor-collect.service, systemd/macro-monitor-collect.timer
**Test**: `systemctl list-unit-files | grep macro-monitor` (after deploy) shows timer and service; `systemctl status macro-monitor-collect.timer` shows enabled and next run time; `systemctl show macro-monitor-collect.timer` confirms Persistent=true.
**Depends on**: Step 1, Step 10 (to know final command path)
**Parallelizable**: Yes

### Step 12: Write deploy.sh script
**What**: Create deployment script that: (a) creates algo-macro nologin service user on desktop, (b) copies pyproject.toml and src/ to /home/algo-macro/app/, (c) installs dependencies in service user's venv, (d) copies systemd files to /etc/systemd/system/, (e) enables and starts timer, (f) verifies timer runs without error on first run. Mirrors algo-corpus/scripts/deploy.sh pattern.
**Files**: scripts/deploy.sh
**Test**: `bash scripts/deploy.sh --dry-run` shows all copy/install commands without executing; `ssh desktop-agent "sudo /home/algo-macro/app/bin/macro-monitor --version"` works (after non-dry-run deploy).
**Depends on**: Step 1, Step 11
**Parallelizable**: Yes

### Step 13: Write Claude Code slash-commands (.claude/commands/)
**What**: Create two slash-commands for the `schedule` skill: (a) /macro-monitor-collect-websearch (daily-ish cadence) — runs scoped WebSearch queries (site:reddit.com, site:news.ycombinator.com per tracked symbol universe per config), then shells out to `macro-monitor ingest --source websearch ...` and `macro-monitor correlate --date <TODAY>`; (b) /macro-monitor-review (weekly, >=7 days) — reads accumulated raw_observations and correlations, runs one LLM turn (this Claude Code session is the LLM call), validates hypotheses per Step 9, writes to candidate_hypotheses table and reports/ folder.
**Files**: .claude/commands/macro-monitor-collect-websearch.md, .claude/commands/macro-monitor-review.md
**Test**: Listed in Claude Code (e.g., `/help` or via Skill tool); can be invoked manually; `/macro-monitor-review --dry-run` reads observations but does not write to DB (if that flag is implemented).
**Depends on**: Step 10 (to know CLI contract)
**Parallelizable**: No

### Step 14: Update algo-factory docs/DECISIONS.md integration point
**What**: Append one entry to docs/DECISIONS.md chronicling macro-context-monitor implementation and its positioning relative to the three killed lanes (social-sentiment-as-signal, tariff-rotation, open-ended-discovery); note that candidate hypotheses this tool surfaces are logged to docs/DECISIONS.md by Preston/agent after pipeline evaluation (matching existing convention).
**Files**: docs/DECISIONS.md (in algo-factory repo — this worktree's own root, not a parent directory)
**Test**: `git diff docs/DECISIONS.md` shows new entry; entry contains "macro-context-monitor" slug, explains read-only observation/correlation role, and references FR-12/FR-13 guardrails; `git status` shows the file modified.
**Depends on**: Step 13 (ideally, but can be done independently)
**Parallelizable**: Yes

## Rollback plan

- **Steps 1-4 (skeleton, schema, test, CLI)**: Delete ~/dev/algo-macro-monitor directory; revert any changes to algo-factory docs/ if already made.
- **Steps 5-9 (modules)**: Revert git commits or delete the module files; Step 3's guard test will still pass (it verifies constraints, not successful implementation).
- **Step 10 (cli.py wiring)**: Revert cli.py to Step 4 state (before imports of 5-9 modules).
- **Steps 11-13 (systemd, deploy, slash-commands)**: Delete systemd/ directory and scripts/deploy.sh; delete .claude/commands/ files; on desktop, run `sudo systemctl disable macro-monitor-collect.timer && sudo rm -rf /etc/systemd/system/macro-monitor-* /home/algo-macro/`.
- **Step 14 (DECISIONS.md)**: Revert the single entry from algo-factory docs/DECISIONS.md.

If any step fails before Step 14, the algo-factory repo is unmodified; rollback is isolated to the new algo-macro-monitor repo (which can be deleted entirely). If Step 14 is committed before later steps fail, revert the DECISIONS.md commit separately.
