# internal-monitor-service

[![CI](https://github.com/preston-bernstein/internal-monitor-service/actions/workflows/ci.yml/badge.svg)](https://github.com/preston-bernstein/internal-monitor-service/actions/workflows/ci.yml)

internal-monitor-service watches financial news and market context, and suggests research ideas for
a related personal trading project. It never trades and never touches a trading
decision — see "Boundary: this tool never trades" below.

It does three things:

- Collects macro and market news from RSS feeds (news feeds that update automatically) and
  scoped web searches.
- Compares that news, read-only, against how a separate paper-trading (simulated, no real
  money) system's strategies actually behaved, using a local read-only snapshot of that system's
  trade database.
- Proposes candidate hypotheses (testable research ideas) for a human to review, at most once a
  week. A human then decides whether any idea is worth pursuing through that other project's own
  planning/evaluation process.

## Boundary: this tool never trades

This is a load-bearing requirement of this repo's design, and it's enforced structurally, not
just by policy:

- It never makes, influences, or automates a live or paper trading decision.
- It never writes to the trade database.
- It never invokes any strategy-evaluation pipeline.
- Its only output is a written report.

How that's enforced: this package has no dependency on any trading package, contains no
shell-out mechanism anywhere, and only ever opens the trade-database snapshot read-only
(`mode=ro`), in `correlator.py`. `tests/test_no_forbidden_imports.py` checks all of this
automatically. `correlator.py` also enforces a minimal-column allowlist (symbol/date/strategy
name/pass-fail only) with a belt-and-suspenders check against a forbidden-column set (position
size, notional, and strategy-internal validation-metric columns) — it never reads, stores, or
exposes proprietary trading internals, only whether a strategy passed or failed its own
evaluation gate on a given day.

## How it's built

Two code paths that never share a process, a call stack, or a host environment. That separation
is what makes "the AI review is never invoked from the data collector" true by construction, not
just by convention.

1. **Deterministic path** (runs on a systemd timer, no AI involved). `collect-rss` fetches an
   allowed list of RSS/Atom feeds over plain HTTP, parses the
   XML, and appends the results to the `raw_observations` table. `correlate` reads a local
   read-only snapshot of the trade database and writes minimal correlation records.
2. **AI path** (a Claude Code agent run through the `schedule` skill). A daily scoped web-search
   collection routine, and a weekly hypothesis-review routine. Each routine has an explicit
   allowed-tools list that excludes any command that could kick off a backtest, an evaluation
   gate, or a strategy-planning pipeline.

## Install (for development)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # unit + guard + live-feed smoke (skips if network unreachable)
ruff check src tests
```

`fleet-logging` and `feed-commons` (below) are pinned as public `git+https://` dependencies at an
exact commit — no credentials are needed to install this repo.

## Command-line commands

```bash
macro-monitor sources add --name fed-press --kind rss \
  --url-or-query https://www.federalreserve.gov/feeds/press_all.xml --checked-on 2026-07-05
macro-monitor collect-rss                       # no AI involved
macro-monitor ingest --source websearch-wsb --observed-date 2026-07-04 \
  --url https://... --title-or-snippet "..." --symbols SPY
macro-monitor correlate                         # read-only, trailing window
macro-monitor correlate --date 2026-07-04       # exact date (manual/back-fill)
macro-monitor correlate --since 2026-07-01      # every observed_date >= this value
macro-monitor review --hypotheses-json h.json   # cadence-gated
```

## How it reads the trade database

`correlate` does a plain local file read. It never uses SSH or sudo.

- The source of truth is a transaction-consistent, read-only snapshot file. Where that file lives
  and who can read it are entirely a deployment decision — see `config.example.yaml`'s
  `paper_db_path` (overridable via the `MACRO_MONITOR_PAPER_DB_PATH` env var) and `ops/` for one
  worked example: a periodic timer that uses SQLite's own backup API to refresh the snapshot, and
  a dedicated, unprivileged service account that reads it via group membership — no SSH, no
  sudo, no ambient access to the source system.
- `correlate` only reads four fields per row: `symbol`, `date`, `strategy`, and a gate pass/fail
  bit. It never reads or stores anything about position size, notional, or the internal
  statistics behind that pass/fail decision.

## Deploy

```bash
sudo scripts/deploy.sh --dry-run    # preview
sudo scripts/deploy.sh              # deploy under a dedicated service account and enable the daily timer
sudo -u macro-monitor bash scripts/smoke_e2e.sh   # verify the live read path
```

Deploy behavior (service-account name, install directory, snapshot path, and the read-only
group) is configured via env vars with generic defaults — see `.env.example` and the comments at
the top of `scripts/deploy.sh`.

## Observability

This package follows a small, self-contained logging/metrics contract (a JSON-line-per-event
convention on stderr, plus a node-exporter textfile export for a `Type=oneshot` job) — not a
fleet-wide standard published here, just a discipline this repo holds itself to.

**Logs.** `collect-rss`, `correlate`, and `review` each write one JSON log line per event to
stderr. Each line carries `schema_version`, `ts` (timestamp), `level`, `service`, `event`, `msg`,
and `run_id`, plus an `outcome` and a work-quantity field on the closing line of each run. See
`src/macro_monitor/log.py` — a thin wrapper around the shared `fleet-logging` package (a public
sibling repo) that keeps this repo's call sites and stderr channel unchanged. Separately,
`click.echo` output to stdout is unchanged — that's the human-facing, interactive output.

**Metrics.** This job runs once and exits (`Type=oneshot` in systemd terms) instead of running as
a long-lived server, so nothing can scrape a `/metrics` endpoint from it. Instead, `collect-rss`
and `correlate` each write their metrics as plain text to a file that node-exporter's textfile
collector reads (path configurable via `MACRO_MONITOR_TEXTFILE_DIR`, defaulting to the upstream
node_exporter convention). See `src/macro_monitor/metrics.py`. The metrics are
`macro_monitor_last_run_timestamp_seconds`, `macro_monitor_last_run_success`,
`macro_monitor_work_quantity`, and `macro_monitor_work_available`, each labeled
`phase="collect"` or `phase="correlate"`.

The work-quantity and work-available fields exist to catch one specific failure mode: a job that
runs successfully but silently does nothing. `(0, 0)` means there was no work to do — that's
fine. `work_available > 0` with `work_quantity == 0` means there was work and none of it got
done — that's a bug.

**A bug we found and fixed this way.** The daily systemd timer used to run
`correlate --date "$(date -u +%Y-%m-%d)"` — "today's exact date." That almost never matched,
because the timer fires early in the UTC day, and `raw_observations` is stamped with each RSS
entry's own published date, which for a US-business-hours news source is still "yesterday" at
that hour. We confirmed this against a live deployment: the `correlations` table held almost no
rows across the service's entire history from the scheduled timer — the scheduled correlation
step had done nothing since the day it was deployed.

The fix: `correlate`, run with neither `--date` nor `--since`, now checks every distinct
`observed_date` in a trailing window (`correlate_lookback_days` in the config, 3 days by default)
instead of one exact day. `--date` (an exact date, for manual runs or backfilling) and `--since`
(an explicit start date) still work as before. See the docstring for `observed_dates_since` in
`src/macro_monitor/correlator.py` for the full account.

## What this deliberately does not do

- No signal, feature, or sentiment-score output.
- No automatic hand-off past writing `reports/<slug>.md`.
- No import of any trading package, and no authority over live trading decisions of any kind.
- Raw scraped content, the log database, and generated reports are all excluded from git
  (`.gitignore`).

## License

MIT. See [`LICENSE`](LICENSE).
