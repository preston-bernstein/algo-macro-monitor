"""Canonical JSON log line — internal-infra CONVENTIONS.md §18 (fleet logging contract).

One JSON object per event, printed to **stderr** — deliberately separate from ``click.echo``'s
stdout, which stays human-facing/interactive output for every subcommand (``sources``, ``ingest``,
``review``, and the per-feed/per-date summary lines in ``collect-rss``/``correlate``). This module
is the machine-readable channel: the thing a future Loki onboarding (Lane B, host systemd unit —
see §18) would actually ship and query.

No ``logging.basicConfig``/handler configuration lives here or anywhere else in this package — §18
reserves output configuration for an application's own entry point, and this package has no
long-lived process to configure: every invocation is a single oneshot CLI command, so a bare
``print(..., file=sys.stderr)`` *is* the entry point's own, and only, output configuration.

Level is emitted in this package's own native spelling (``info``/``warn``/``error``) — the shared
Loki ``loki.process`` pipeline canonicalizes it; this module does not pre-canonicalize.
"""

from __future__ import annotations

import json
import sys
import time
import uuid

SERVICE = "macro-monitor"
SCHEMA_VERSION = 1

# §18 Redaction: these field names are never logged, in any repo. The shared `loki.process`
# `stage.replace` is the enforced backstop; this is defense-in-depth so a slip here never even
# reaches stdout/stderr in the first place.
_REDACT_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "api_key",
        "apikey",
        "secret",
        "authorization",
        "access_token",
        "refresh_token",
        "ssn",
        "cookie",
        "session",
    }
)


def new_run_id() -> str:
    """A run_id for one CLI invocation — one cron fire / one `macro-monitor <cmd>` call (§18
    Correlation). Time-prefixed so lines are roughly sortable by eye in a raw journal dump, plus
    a random suffix so two invocations in the same second never collide.
    """
    return f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def _scrub(fields: dict) -> dict:
    return {
        k: ("[REDACTED]" if k.lower() in _REDACT_KEYS else v) for k, v in fields.items()
    }


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
    line = {
        "schema_version": SCHEMA_VERSION,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "service": SERVICE,
        "event": event,
        "msg": msg if msg is not None else event,
        **_scrub(fields),
    }
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)
