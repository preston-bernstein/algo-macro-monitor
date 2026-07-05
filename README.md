# algo-macro-monitor

A read-only **observation-and-hypothesis-proposal** layer over algo-factory's paper-traded
strategies. It collects macro/market context (RSS feeds + scoped WebSearch), correlates it
read-only against recorded strategy behavior in `paper.db`, and — no more than weekly — proposes
candidate hypotheses for a human to run through the existing `/spec-gather` pipeline.

> **Load-bearing boundary (FR-12).** This tool NEVER makes, influences, or automates a live or
> paper trading decision, never writes to `paper.db`, and never invokes the evaluation pipeline.
> Its only output is a written report. This is enforced structurally: there is no `algo_factory`
> dependency, no shell-out mechanism anywhere in the package, and the `paper.db` snapshot is only
> ever opened read-only (`mode=ro`) in `correlator.py`. See `tests/test_no_forbidden_imports.py`.

## Architecture

Two mechanically distinct paths (they share no process, call stack, or host env — this is what
makes "the LLM review is never invoked from the collector" true by construction):

1. **Deterministic path (systemd timer, no LLM).** `collect-rss` fetches allowlisted RSS/Atom
   feeds (plain HTTP + XML parse), appends to `raw_observations`; `correlate` reads the local
   `/srv/paper-share/paper.db` snapshot read-only and writes minimal correlations.
2. **LLM path (schedule-skill Claude Code agent).** A daily scoped-WebSearch collection routine
   and a weekly hypothesis-review routine, each with an explicit tool allowlist (FR-16) that
   excludes `/spec-gather`, `/spec-challenge`, and any backtest/gate command.

## Install (dev)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # unit + guard + live-feed smoke (skips if network unreachable)
ruff check src tests
```

## CLI

```bash
macro-monitor sources add --name fed-press --kind rss \
  --url-or-query https://www.federalreserve.gov/feeds/press_all.xml --checked-on 2026-07-05
macro-monitor collect-rss                       # FR-01, no LLM
macro-monitor ingest --source websearch-wsb --observed-date 2026-07-04 \
  --url https://... --title-or-snippet "..." --symbols SPY   # FR-04/14/20
macro-monitor correlate --date 2026-07-04       # FR-05/06/17/18/19, read-only
macro-monitor review --hypotheses-json h.json   # FR-07/08/09 (cadence-gated)
```

## paper.db read path (FR-17)

Per `docs/DECISIONS.md`, correlation is a plain **local read** of the transaction-consistent
snapshot at `/srv/paper-share/paper.db` (refreshed every 15 min by `paper-db-snapshot.timer`),
which the `algo-macro` service user reads via its `paper-readers` group membership — no SSH, no
sudo, no traversal into `/home/algo-factory`. Only `symbol`/`date`/`strategy`/gate-pass-fail
fields are selected; `weight`/`units`/`notional` are never read or stored (FR-19).

## Deploy

```bash
sudo scripts/deploy.sh --dry-run    # preview
sudo scripts/deploy.sh              # deploy under algo-macro + enable the daily timer
sudo -u algo-macro bash scripts/smoke_e2e.sh   # verify the live read path
```

## What is intentionally NOT here

No signal/feature/sentiment-score output (FR-13), no automatic hand-off past `reports/<slug>.md`
(FR-10/FR-11), no `algo_factory` import, no live-decision authority of any kind (FR-12). Raw
scraped content, the log DB, and generated reports are all gitignored (FR-15).
