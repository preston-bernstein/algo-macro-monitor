"""Canonical JSON log line — internal-infra CONVENTIONS.md §18 (fleet logging contract).

Thin wrapper around the shared `fleet-logging` package (see the git-pinned dependency in
pyproject.toml). `fleet-logging` was built as a strict superset of this module's own original
hand-rolled implementation (among others) — see its README's "What it replaces" section — so this
wrapper exists only to keep every existing call site in this repo (`log_event(level, event, msg,
**fields)`, no `service`/`stream` args) working unchanged, and to pin this package's channel to
**stderr** — deliberately separate from `click.echo`'s stdout, which stays human-facing/interactive
output for every subcommand (`sources`, `ingest`, `review`, and the per-feed/per-date summary lines
in `collect-rss`/`correlate`). This module is the machine-readable channel: the thing a future Loki
onboarding (Lane B, host systemd unit — see §18) would actually ship and query.

No ``logging.basicConfig``/handler configuration lives here or anywhere else in this package — §18
reserves output configuration for an application's own entry point, and this package has no
long-lived process to configure: every invocation is a single oneshot CLI command, so
`fleet_logging.log_event(...)` printing straight to stderr *is* the entry point's own, and only,
output configuration.

Level is emitted in this package's own native spelling (``info``/``warn``/``error``) — the shared
Loki ``loki.process`` pipeline canonicalizes it; this module does not pre-canonicalize, and neither
does `fleet_logging.log_event`.
"""

from __future__ import annotations

import sys

from fleet_logging import log_event as _fleet_log_event
from fleet_logging import new_run_id as _fleet_new_run_id

SERVICE = "macro-monitor"


def new_run_id() -> str:
    """A run_id for one CLI invocation — one cron fire / one `macro-monitor <cmd>` call (§18
    Correlation). Same time-prefixed-plus-random-suffix shape as before (`fleet_logging.new_run_id`
    additionally reuses a pre-existing `$RUN_ID` from a parent process if one is set — this repo
    has no orchestrating shell script that sets one, so that extra behavior is a no-op here today).
    """
    return _fleet_new_run_id(prefix="run")


def log_event(level: str, event: str, msg: str | None = None, **fields) -> None:
    """Emit one canonical JSON log line to stderr.

    ``level`` — this package's own spelling (``debug``/``info``/``warn``/``error``/``critical``);
    the shared pipeline maps it to the fleet's canonical enum, never this module.
    ``event`` — a short, stable, dot-namespaced string (``collect.completed``, ``correlate.failed``)
    — the thing a dashboard panel or ``absent()`` alert actually filters on; ``msg`` is prose for a
    human in Grafana Explore and must never be what anything alerts on.
    ``fields`` — everything else (``run_id``, ``outcome``, work-quantity, ``err_type``/``err_msg``,
    ``duration_ms``, ...) per §18's canonical log line shape.
    """
    _fleet_log_event(level, event, msg, service=SERVICE, stream=sys.stderr, **fields)
