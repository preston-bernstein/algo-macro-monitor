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

import click

from . import (
    collector_rss,
    collector_websearch_ingest,
    correlator,
    db,
    reviewer,
    sources,
)
from .config import Config, load_config
from .validation import ValidationError

__version__ = "0.1.0"


def _conn(ctx):
    cfg: Config = ctx.obj["config"]
    return db.init_db(cfg.db_path)


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
@click.option("--kind", type=click.Choice(["rss", "websearch"]), required=True)
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
    summary = collector_rss.collect_rss(conn, symbol_universe=cfg.symbol_universe)
    if not summary.results:
        click.echo("no pollable rss sources configured", err=True)
        sys.exit(1)
    for r in summary.results:
        status = "ok" if r.ok else f"FAILED ({r.error})"
        click.echo(f"{r.source}: {status} inserted={r.inserted} seen={r.seen}")
    click.echo(f"total inserted: {summary.total_inserted}")
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
@click.option("--date", "observed_date", required=True, help="YYYY-MM-DD")
@click.option("--paper-db", "paper_db_path", default=None, help="Override paper.db snapshot path")
@click.pass_context
def correlate_cmd(ctx, observed_date, paper_db_path) -> None:
    """FR-05/06/17/18/19: read-only paper.db correlation for one observed_date."""
    cfg: Config = ctx.obj["config"]
    conn = _conn(ctx)
    path = paper_db_path or cfg.paper_db_path
    try:
        results = correlator.correlate_date(conn, observed_date, paper_db_path=path)
    except ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    except correlator.CorrelationError as exc:
        # FR-05/plan.md: read failures are logged, leave correlations untouched, non-zero exit.
        click.echo(f"correlation failed: {exc}", err=True)
        sys.exit(2)
    if not results:
        click.echo(f"no observations for {observed_date}")
        return
    total = sum(r.written for r in results)
    for r in results:
        click.echo(f"obs {r.observation_id}: tables={json.dumps(r.tables)}")
    click.echo(f"correlations written: {total}")


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

    hypotheses = []
    if hypotheses_json:
        hypotheses = json.loads(open(hypotheses_json).read())

    written = []
    try:
        for h in hypotheses:
            if dry_run:
                reviewer.validate_hypothesis(h)  # validate but do not persist
                continue
            path = reviewer.persist_hypothesis(conn, run_id, h, reports_dir=cfg.reports_dir)
            written.append(path)
    except reviewer.ReviewError as exc:
        reviewer.close_review_run(conn, run_id, "failed")
        raise click.ClickException(str(exc)) from exc

    reviewer.close_review_run(conn, run_id, "dry-run" if dry_run else "ok")
    click.echo(f"review run {run_id}: considered={len(considered)} reports={len(written)}")
    for p in written:
        click.echo(f"  wrote {p}")


if __name__ == "__main__":
    main()
