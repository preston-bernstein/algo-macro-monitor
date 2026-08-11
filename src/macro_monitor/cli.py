"""macro-monitor CLI.

Subcommands: ``sources`` (add/check/list), ``collect-rss``, ``ingest``, ``correlate``, ``review``.

Exit-code contract:
  * collect-rss: 0 if >= 1 feed succeeded, 1 if all configured feeds failed (FR-01/FR-13a).
  * every other command: 0 on success, non-zero on validation/operational error.

Nothing in this module invokes /spec-gather, /spec-challenge, a backtest, or gate code (FR-10/11/12).
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import click

from . import (
    __version__,
    collector_rss,
    collector_websearch_ingest,
    correlator,
    db,
    metrics,
    reviewer,
    sources,
)
from .config import Config, load_config
from .log import log_event, new_run_id
from .validation import ValidationError


def _conn(ctx):
    cfg: Config = ctx.obj["config"]
    return db.init_db(cfg.db_path)


def _finish_phase(
    phase: str,
    event: str,
    *,
    run_id: str,
    level: str,
    outcome: str,
    success: bool,
    work_quantity: int,
    work_available: int,
    **extra_log_fields,
) -> None:
    """Emit the closing §18 log_event + metrics.write_phase_metrics pair for one phase.

    The did-nothing-rule numbers (work_quantity/work_available) and success/outcome must agree
    across both signals — extracted so that invariant holds by construction instead of by
    copy-paste discipline at each call site. Also surfaces a metrics write that was silently
    skipped (textfile dir missing) as its own log line, so a production misconfiguration that
    would otherwise degrade metrics coverage forever without ever failing a run is queryable in
    the same JSON stream as everything else.
    """
    log_event(
        level, event, run_id=run_id, outcome=outcome,
        items_processed=work_quantity, work_available=work_available, **extra_log_fields,
    )
    written = metrics.write_phase_metrics(
        phase, success=success, work_quantity=work_quantity, work_available=work_available,
    )
    if not written:
        log_event("warn", "metrics.write_skipped", phase=phase, run_id=run_id)


@click.group()
@click.version_option(__version__, prog_name="macro-monitor")
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.option("--db-path", default=None, help="Override the log DB path")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, db_path: str | None) -> None:
    """Macro Context Monitor — read-only observation + hypothesis-proposal layer."""
    cfg = load_config(config_path)
    if db_path:
        cfg.db_path = db_path
    ctx.obj = {"config": cfg}


# --- sources --------------------------------------------------------------------------------


@main.group()
def sources_group() -> None:
    """Manage the source allowlist (FR-03)."""


main.add_command(sources_group, name="sources")


@sources_group.command("add")
@click.option("--name", required=True)
@click.option("--kind", type=click.Choice(list(sources.VALID_KINDS)), required=True)
@click.option("--url-or-query", required=True)
@click.option("--checked-on", default=None, help="YYYY-MM-DD spot-check date (FR-03, required)")
@click.option("--fetchable/--not-fetchable", default=True)
@click.option("--notes", default=None)
@click.pass_context
def sources_add(ctx, name, kind, url_or_query, checked_on, fetchable, notes) -> None:
    conn = _conn(ctx)
    try:
        sources.add_source(
            conn, name, kind, url_or_query, checked_on, fetchable=fetchable, notes=notes
        )
    except ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"added source {name!r}")


@sources_group.command("check")
@click.argument("name")
@click.pass_context
def sources_check(ctx, name) -> None:
    """Live-fetch a source and update its fetchable/checked_on (FR-03)."""
    conn = _conn(ctx)
    rows = {r["name"]: r for r in sources.list_sources(conn)}
    if name not in rows:
        raise click.ClickException(f"no such source: {name}")
    row = rows[name]
    today = db.now_iso()[:10]
    if row["kind"] != "rss":
        click.echo(f"{name}: kind={row['kind']} — live-check only implemented for rss")
        return
    result = collector_rss.fetch_and_log_rss(conn, name, row["url_or_query"])
    ok = result.ok
    sources.set_fetchable(conn, name, ok, today)
    click.echo(f"{name}: fetchable={ok} ({result.error or 'ok'})")


@sources_group.command("list")
@click.pass_context
def sources_list(ctx) -> None:
    conn = _conn(ctx)
    for r in sources.list_sources(conn):
        click.echo(f"{r['name']}\t{r['kind']}\tfetchable={r['fetchable']}\tchecked_on={r['checked_on']}")


# --- collect-rss ----------------------------------------------------------------------------


@main.command("collect-rss")
@click.pass_context
def collect_rss_cmd(ctx) -> None:
    """FR-01: fetch + parse all pollable RSS feeds, append observations. No LLM."""
    cfg: Config = ctx.obj["config"]
    conn = _conn(ctx)
    run_id = new_run_id()
    log_event("info", "collect.started", run_id=run_id)

    summary = collector_rss.collect_rss(
        conn, symbol_universe=cfg.symbol_universe, run_id=run_id
    )
    if not summary.results:
        click.echo("no pollable rss sources configured", err=True)
        _finish_phase(
            "collect", "collect.no_sources",
            run_id=run_id, level="error", outcome="failed", success=False,
            work_quantity=0, work_available=0,
        )
        sys.exit(1)

    for r in summary.results:
        status = "ok" if r.ok else f"FAILED ({r.error})"
        click.echo(f"{r.source}: {status} inserted={r.inserted} seen={r.seen}", err=not r.ok)
    click.echo(f"total inserted: {summary.total_inserted}")

    work_available = sum(r.seen for r in summary.results)
    feeds_ok = sum(1 for r in summary.results if r.ok)
    feeds_failed = len(summary.results) - feeds_ok
    outcome = "ok" if summary.any_success else "failed"
    # §18 did-nothing rule: outcome and the work-quantity field are both always present on this
    # closing line, and 0 is logged as an honest value, never omitted — a feed set that is up
    # (feeds_ok > 0) but inserted 0 new items (every feed already fully collected, or every entry
    # rejected) must be distinguishable from a feed set that is down.
    _finish_phase(
        "collect", "collect.completed",
        run_id=run_id, level="info" if summary.any_success else "error",
        outcome=outcome, success=summary.any_success,
        work_quantity=summary.total_inserted, work_available=work_available,
        feeds_ok=feeds_ok, feeds_failed=feeds_failed,
    )
    sys.exit(0 if summary.any_success else 1)


# --- ingest ---------------------------------------------------------------------------------


@main.command("ingest")
@click.option("--source", required=True)
@click.option("--observed-date", required=True)
@click.option("--url", required=True)
@click.option("--title-or-snippet", required=True)
@click.option("--symbols", default=None, help="Comma-separated ticker symbols")
@click.pass_context
def ingest_cmd(ctx, source, observed_date, url, title_or_snippet, symbols) -> None:
    """FR-04/FR-14/FR-20: validate and append one observation (the WebSearch write path)."""
    conn = _conn(ctx)
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
    try:
        row_id, inserted = collector_websearch_ingest.ingest(
            conn, observed_date, source, url, title_or_snippet, sym_list
        )
    except ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"observation id={row_id} inserted={inserted}")


# --- correlate ------------------------------------------------------------------------------


@main.command("correlate")
@click.option(
    "--date",
    "observed_date",
    default=None,
    help="YYYY-MM-DD — correlate exactly this one observed_date (manual/back-fill use). "
    "Mutually exclusive with --since.",
)
@click.option(
    "--since",
    default=None,
    help="YYYY-MM-DD — correlate every distinct observed_date >= this value, not just one exact "
    "date. Omit both --date and --since to use the config's correlate_lookback_days trailing "
    "window from today — the default for the scheduled daily run, since an observation's own "
    "observed_date (the feed entry's published date) routinely lags the day the job actually "
    "runs on. See correlator.observed_dates_since for why a single exact date was a no-op.",
)
@click.option("--paper-db", "paper_db_path", default=None, help="Override paper.db snapshot path")
@click.pass_context
def correlate_cmd(ctx, observed_date, since, paper_db_path) -> None:
    """FR-05/06/17/18/19: read-only paper.db correlation over one date or a trailing window."""
    cfg: Config = ctx.obj["config"]
    conn = _conn(ctx)
    path = paper_db_path or cfg.paper_db_path
    run_id = new_run_id()

    if observed_date is not None and since is not None:
        raise click.ClickException("--date and --since are mutually exclusive")

    try:
        if observed_date is not None:
            dates = [observed_date]
        else:
            window_start = since or (
                date.fromisoformat(db.now_iso()[:10]) - timedelta(days=cfg.correlate_lookback_days)
            ).isoformat()
            dates = correlator.observed_dates_since(conn, window_start)
    except ValidationError as exc:
        raise click.ClickException(str(exc)) from exc

    log_event("info", "correlate.started", run_id=run_id, dates_considered=len(dates))

    total_written = 0
    total_considered = 0
    any_result = False
    try:
        for d in dates:
            results = correlator.correlate_date(conn, d, paper_db_path=path)
            any_result = any_result or bool(results)
            for r in results:
                total_considered += 1
                total_written += r.written
                click.echo(f"obs {r.observation_id}: tables={json.dumps(r.tables)}")
    except ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    except correlator.CorrelationError as exc:
        # FR-05/plan.md: read failures are logged, leave correlations untouched, non-zero exit.
        click.echo(f"correlation failed: {exc}", err=True)
        _finish_phase(
            "correlate", "correlate.failed",
            run_id=run_id, level="error", outcome="failed", success=False,
            work_quantity=0, work_available=0,
            err_type=type(exc).__name__, err_msg=str(exc),
        )
        sys.exit(2)

    if not dates:
        click.echo("no observation dates in range")
    elif not any_result:
        if len(dates) == 1:
            click.echo(f"no observations for {dates[0]}")
        else:
            click.echo(f"no observations for {dates[0]}..{dates[-1]}")
    else:
        click.echo(f"correlations written: {total_written}")

    # §18 did-nothing rule: a run that correlated nothing (total_written == 0, whether because
    # there were no observations in range at all, or every observation's paper.db lookup came
    # back empty) must be distinguishable from a run that did work — both here in the closing log
    # line's outcome + work-quantity fields, and in the exported work_quantity/work_available
    # metrics below.
    _finish_phase(
        "correlate", "correlate.completed",
        run_id=run_id, level="info", outcome="ok", success=True,
        work_quantity=total_written, work_available=total_considered,
        dates_considered=len(dates),
    )


# --- review ---------------------------------------------------------------------------------


@main.command("review")
@click.option("--since", default=None, help="Oldest observed_date to consider (YYYY-MM-DD)")
@click.option("--min-days", default=None, type=int, help="Override cadence (larger only, FR-07)")
@click.option("--dry-run", is_flag=True, help="Read observations, write nothing")
@click.option(
    "--hypotheses-json",
    default=None,
    help="Path to a JSON file of candidate hypotheses (supplied by the review agent turn)",
)
@click.pass_context
def review_cmd(ctx, since, min_days, dry_run, hypotheses_json) -> None:
    """FR-07/08/09: cadence-gated hypothesis review + terminal report generation.

    The LLM reasoning itself is the schedule-skill agent turn that invokes this command; that turn
    supplies validated candidate hypotheses via --hypotheses-json. This command enforces the
    cadence gate, validates required fields, and writes the terminal artifacts. It never invokes
    any downstream pipeline (FR-10/11/12).
    """
    cfg: Config = ctx.obj["config"]
    conn = _conn(ctx)
    md = cfg.review_min_days if min_days is None else min_days
    try:
        if not dry_run and not reviewer.cadence_ok(conn, min_days=md):
            last = reviewer.last_successful_review(conn)
            # Benign refusal, not a failure: the cadence gate working as designed. No review_runs
            # row exists yet at this point, so this event carries no run_id — it precedes one.
            log_event(
                "info", "review.cadence_refused", outcome="refused",
                min_days=md, last_completed_at=last["completed_at"] if last else None,
            )
            raise click.ClickException(
                f"cadence gate: < {md} days since last successful review "
                f"(completed_at={last['completed_at'] if last else None}); refusing (FR-07)"
            )
    except reviewer.ReviewError as exc:
        raise click.ClickException(str(exc)) from exc

    since = since or reviewer.default_window_start(md)
    considered = reviewer.observations_since(conn, since)
    window_end = db.now_iso()[:10]

    run_id = reviewer.open_review_run(
        conn,
        window_start=since,
        window_end=window_end,
        observations_considered=len(considered),
        dry_run=dry_run,
    )
    # review_runs.id is the real correlation id for this unit of work (§18 Correlation) — it was
    # persisted to review_runs and echoed to stdout for a human, but never appeared in a
    # machine-parseable log line until now.
    log_event(
        "info", "review.started", run_id=str(run_id),
        window_start=since, window_end=window_end, observations_considered=len(considered),
        dry_run=dry_run,
    )

    written = []
    try:
        hypotheses = []
        if hypotheses_json:
            hypotheses = json.loads(Path(hypotheses_json).read_text())
        for h in hypotheses:
            if dry_run:
                reviewer.validate_hypothesis(h)  # validate but do not persist
                continue
            path = reviewer.persist_hypothesis(conn, run_id, h, reports_dir=cfg.reports_dir)
            written.append(path)
    except (OSError, json.JSONDecodeError, reviewer.ReviewError) as exc:
        reviewer.close_review_run(conn, run_id, "failed")
        log_event(
            "error", "review.failed", run_id=str(run_id), outcome="failed",
            err_type=type(exc).__name__, err_msg=str(exc), items_processed=len(written),
        )
        raise click.ClickException(str(exc)) from exc

    status = "dry-run" if dry_run else "ok"
    reviewer.close_review_run(conn, run_id, status)
    log_event(
        "info", "review.completed", run_id=str(run_id), outcome=status,
        items_processed=len(written), work_available=len(hypotheses),
    )
    click.echo(f"review run {run_id}: considered={len(considered)} reports={len(written)}")
    for p in written:
        click.echo(f"  wrote {p}")


if __name__ == "__main__":
    main()
