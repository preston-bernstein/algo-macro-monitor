# algo-macro-monitor

[![CI](https://github.com/preston-bernstein/algo-macro-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/preston-bernstein/algo-macro-monitor/actions/workflows/ci.yml)

algo-macro-monitor watches financial news and market context, and suggests research ideas for
a related trading project called algo-factory. It never trades and never touches a trading
decision — see "Boundary: this tool never trades" below.

It does three things:

- Collects macro and market news from RSS feeds (news feeds that update automatically) and
  scoped web searches.
- Compares that news, read-only, against how algo-factory's paper-traded (simulated, no real
  money) strategies actually behaved, using a local copy of algo-factory's `paper.db` database.
- Proposes candidate hypotheses (testable research ideas) for a human to review, at most once a
  week. A human then runs any idea worth pursuing through algo-factory's own `/spec-gather`
  planning pipeline.

## Boundary: this tool never trades

This is requirement FR-12 from this repo's spec, and it's enforced structurally, not just by
policy:

- It never makes, influences, or automates a live or paper trading decision.
- It never writes to `paper.db`.
- It never invokes algo-factory's evaluation pipeline.
- Its only output is a written report.

How that's enforced: this package has no dependency on `algo_factory`, contains no shell-out
mechanism anywhere, and only ever opens the `paper.db` snapshot read-only (`mode=ro`), in
`correlator.py`. `tests/test_no_forbidden_imports.py` checks all of this automatically.

## How it's built

Two code paths that never share a process, a call stack, or a host environment. That separation
is what makes "the AI review is never invoked from the data collector" true by construction, not
just by convention.

1. **Deterministic path** (runs on a systemd timer — a scheduled background job — no AI
   involved). `collect-rss` fetches an allowed list of RSS/Atom feeds over plain HTTP, parses the
   XML, and appends the results to the `raw_observations` table. `correlate` reads a local
   read-only snapshot of `/srv/paper-share/paper.db` and writes minimal correlation records.
2. **AI path** (a Claude Code agent run through the `schedule` skill). A daily scoped web-search
   collection routine, and a weekly hypothesis-review routine. Each routine has an explicit
   allowed-tools list (FR-16) that excludes `/spec-gather`, `/spec-challenge`, and any backtest
   or gate command.

## Install (for development)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # unit + guard + live-feed smoke (skips if network unreachable)
ruff check src tests
```

## Command-line commands

```bash
macro-monitor sources add --name fed-press --kind rss \
  --url-or-query https://www.federalreserve.gov/feeds/press_all.xml --checked-on 2026-07-05
macro-monitor collect-rss                       # FR-01, no AI involved
macro-monitor ingest --source websearch-wsb --observed-date 2026-07-04 \
  --url https://... --title-or-snippet "..." --symbols SPY   # FR-04/14/20
macro-monitor correlate                         # FR-05/06/17/18/19, read-only, trailing window
macro-monitor correlate --date 2026-07-04       # exact date (manual/back-fill)
macro-monitor correlate --since 2026-07-01      # every observed_date >= this value
macro-monitor review --hypotheses-json h.json   # FR-07/08/09 (cadence-gated)
```

## How it reads paper.db (FR-17)

Per `docs/DECISIONS.md`, `correlate` does a plain local file read. It never uses SSH or sudo.

- The source of truth is a transaction-consistent snapshot at `/srv/paper-share/paper.db`. A
  separate timer, `paper-db-snapshot.timer`, refreshes this snapshot every 15 minutes.
- This repo's service account, `algo-macro`, can read that snapshot because it belongs to the
  `paper-readers` group. It cannot reach `/home/algo-factory` at all.
- `correlate` only reads four fields: `symbol`, `date`, `strategy`, and gate pass/fail. It never
  reads or stores `weight`, `units`, or `notional` (FR-19).

## Deploy

```bash
sudo scripts/deploy.sh --dry-run    # preview
sudo scripts/deploy.sh              # deploy under the algo-macro service account and enable the daily timer
sudo -u algo-macro bash scripts/smoke_e2e.sh   # verify the live read path
```

## Observability

This follows section 18 of home-infra's CONVENTIONS.md file (home-infra is the shared
infrastructure repo that sets fleet-wide logging rules).

**Logs.** `collect-rss`, `correlate`, and `review` each write one JSON log line per event to
stderr. Each line carries `schema_version`, `ts` (timestamp), `level`, `service`, `event`, `msg`,
and `run_id`, plus an `outcome` and a work-quantity field on the closing line of each run. See
`src/macro_monitor/log.py`. Separately, `click.echo` output to stdout is unchanged — that's the
human-facing, interactive output.

**Metrics.** This job runs once and exits (`Type=oneshot` in systemd terms) instead of running as
a long-lived server, so nothing can scrape a `/metrics` endpoint from it. Instead, `collect-rss`
and `correlate` each write their metrics as plain text to a file that node-exporter (a monitoring
agent) reads: `/opt/docker/observability/node-exporter-textfiles/macro_monitor.prom`. See
`src/macro_monitor/metrics.py`. The metrics are `macro_monitor_last_run_timestamp_seconds`,
`macro_monitor_last_run_success`, `macro_monitor_work_quantity`, and
`macro_monitor_work_available`, each labeled `phase="collect"` or `phase="correlate"`.

The work-quantity and work-available fields exist to catch one specific failure mode: a job that
runs successfully but silently does nothing. `(0, 0)` means there was no work to do — that's
fine. `work_available > 0` with `work_quantity == 0` means there was work and none of it got
done — that's a bug.

**A bug we found and fixed this way (2026-08-01).** The daily systemd timer used to run
`correlate --date "$(date -u +%Y-%m-%d)"` — "today's exact date." That almost never matched,
because the timer fires at 06:30 UTC, and `raw_observations` is stamped with each RSS entry's own
published date, which for a US-business-hours news source is still "yesterday" at that hour. We
confirmed this against the live deployment: the `correlations` table held exactly 2 rows in the
service's entire history, both from a one-off manual check, never from the scheduled timer. In
other words, the scheduled correlation step had done nothing since the day it was deployed.

The fix: `correlate`, run with neither `--date` nor `--since`, now checks every distinct
`observed_date` in a trailing window (`correlate_lookback_days` in the config, 3 days by default)
instead of one exact day. `--date` (an exact date, for manual runs or backfilling) and `--since`
(an explicit start date) still work as before. See the docstring for `observed_dates_since` in
`src/macro_monitor/correlator.py` for the full account.

**Not done yet, and why.** This repo doesn't ship its logs to Loki (the fleet's log aggregator)
yet — that needs a change on the home-infra side (adding this service to `config.alloy`'s
allow-list and to `tools/config-drift/lane-b-registry.json`), which this repo can't do on its
own. Alert rules for the new metrics — staleness, an `absent()` check, and the did-nothing gate —
also live in home-infra's `alert-rules.yml` and haven't been added yet.

## What this deliberately does not do

- No signal, feature, or sentiment-score output (FR-13).
- No automatic hand-off past writing `reports/<slug>.md` (FR-10/FR-11).
- No import of `algo_factory`, and no authority over live trading decisions of any kind (FR-12).
- Raw scraped content, the log database, and generated reports are all excluded from git
  (`.gitignore`, FR-15).

## License

Proprietary — private repository, all rights reserved. See `license` in
[`pyproject.toml`](pyproject.toml).
