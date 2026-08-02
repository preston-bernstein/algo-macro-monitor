# internal-monitor-service

[![CI](https://github.com/preston-bernstein/internal-monitor-service/actions/workflows/ci.yml/badge.svg)](https://github.com/preston-bernstein/internal-monitor-service/actions/workflows/ci.yml)

A read-only **observation-and-hypothesis-proposal** layer over internal-research-service's paper-traded
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
macro-monitor correlate                         # FR-05/06/17/18/19, read-only, trailing window
macro-monitor correlate --date 2026-07-04       # exact date (manual/back-fill)
macro-monitor correlate --since 2026-07-01      # every observed_date >= this value
macro-monitor review --hypotheses-json h.json   # FR-07/08/09 (cadence-gated)
```

## paper.db read path (FR-17)

Per `docs/DECISIONS.md`, correlation is a plain **local read** of the transaction-consistent
snapshot at `/srv/paper-share/paper.db` (refreshed every 15 min by `paper-db-snapshot.timer`),
which the `internal-monitor-service` service user reads via its `paper-readers` group membership — no SSH, no
sudo, no traversal into `/home/internal-research-service`. Only `symbol`/`date`/`strategy`/gate-pass-fail
fields are selected; `weight`/`units`/`notional` are never read or stored (FR-19).

## Deploy

```bash
sudo scripts/deploy.sh --dry-run    # preview
sudo scripts/deploy.sh              # deploy under internal-monitor-service + enable the daily timer
sudo -u internal-monitor-service bash scripts/smoke_e2e.sh   # verify the live read path
```

## Observability (internal-infra CONVENTIONS.md §18)

`collect-rss`/`correlate`/`review` each emit one JSON log line per event to **stderr** (schema
per §18: `schema_version`, `ts`, `level`, `service`, `event`, `msg`, `run_id`, plus `outcome` +
a work-quantity field on the closing line of each run — see `src/macro_monitor/log.py`). `click.echo`
output to stdout is unchanged and stays human-facing/interactive.

Because this is a `Type=oneshot` systemd job, not a long-lived server, metrics go out via the
node-exporter **textfile collector** (§18 — a oneshot job cannot be scraped) rather than a
`/metrics` endpoint: `collect-rss` and `correlate` each write `macro_monitor_last_run_timestamp_seconds`,
`macro_monitor_last_run_success`, `macro_monitor_work_quantity`, and `macro_monitor_work_available`
(labeled `phase="collect"`/`phase="correlate"`) to
`/opt/docker/observability/node-exporter-textfiles/macro_monitor.prom` — see
`src/macro_monitor/metrics.py`. `work_quantity`/`work_available` implement §18's did-nothing rule:
`(0, 0)` is a benign no-op, `work_available > 0` with `work_quantity == 0` is the vault-indexer
failure mode (there was work and none of it got done).

**Known no-op bug and its fix (2026-08-01).** The daily systemd timer previously ran `correlate
--date "$(date -u +%Y-%m-%d)"` — an exact calendar date that a 06:30 UTC fire could essentially
never match, because `raw_observations` is stamped with each RSS entry's own published date, which
lags "today" at that hour for any US-business-hours source. Confirmed against the live deployment:
`correlations` held exactly 2 rows total across the service's whole history, both from a one-off
manual verification, never from the timer — i.e. the scheduled correlation step had been a
permanent no-op since deploy. `correlate` with no `--date`/`--since` now walks a trailing window
(`correlate_lookback_days` in config, default 3) of distinct `observed_date` values instead of one
exact day; see `src/macro_monitor/correlator.py`'s `observed_dates_since` docstring for the full
account.

**Not yet done, and why.** Log shipping to Loki is not wired up from this repo — onboarding a host
systemd unit is a `internal-infra`-side change (`config.alloy`'s Lane B allow-list plus
`tools/config-drift/lane-b-registry.json`, per §18), not something this repo can self-serve.
Alert rules against the new metrics (staleness, `absent()`, the did-nothing gate) live in
`internal-infra`'s `alert-rules.yml`, also not added by this change.

## What is intentionally NOT here

No signal/feature/sentiment-score output (FR-13), no automatic hand-off past `reports/<slug>.md`
(FR-10/FR-11), no `algo_factory` import, no live-decision authority of any kind (FR-12). Raw
scraped content, the log DB, and generated reports are all gitignored (FR-15).

## License

Proprietary — private repository, all rights reserved. See `license` in
[`pyproject.toml`](pyproject.toml).
