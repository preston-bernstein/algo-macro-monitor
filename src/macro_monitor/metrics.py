"""Node-exporter textfile-collector metrics — internal-infra CONVENTIONS.md §18.

This package is a systemd ``Type=oneshot`` job (two of them back to back: ``collect-rss`` then
``correlate``). A oneshot job cannot be scraped — there is no process listening at the moment a
Prometheus scrape would arrive, because the job has already exited by then. §18's mechanism for
this deployment shape is the node-exporter **textfile collector**: write a ``.prom`` file under
the shared textfile directory on the way out; node-exporter picks it up on its own next scrape.

Both phases share one file (``macro_monitor.prom``) so a single ``ls`` / dashboard panel shows the
whole service, but they run as two independent OS processes a few seconds apart on the same timer
fire. Each write therefore reads whatever is already on disk, replaces only its own phase's values,
and rewrites the merged content — never blindly overwriting the other phase's just-written metrics
(the collect-then-correlate ordering means a naive overwrite would erase collect-rss's own numbers
within seconds of every single run). The write itself is tmp-file-then-atomic-``os.replace``, the
same pattern proven in internal-infra's ``arr-stack/gluetun-health-metric.sh`` — node-exporter must
never observe a half-written file, since a malformed ``.prom`` silences its *entire* textfile
collector, not just this one service's metrics.

Exports, at minimum per §18's "the minimum metric set" — labeled ``phase="collect"`` /
``phase="correlate"``:
  * ``macro_monitor_last_run_timestamp_seconds`` — staleness.
  * ``macro_monitor_last_run_success`` — 1/0; a phase can run, do nothing, AND still fail (a
    read error, all feeds down) — success and work_quantity are independent signals.
  * ``macro_monitor_work_quantity`` — units of work this phase actually completed (§18's
    did-nothing rule: 0 is a valid, honest value here — it is never omitted).
  * ``macro_monitor_work_available`` — units of work that existed for this phase to do. The pair
    (0 available, 0 processed) is benign ("nothing to do"); (available > 0, processed 0) is the
    vault-indexer failure mode exactly — the same distinction §18 requires.
"""

from __future__ import annotations

import os
import re
import time

DEFAULT_TEXTFILE_DIR = "/opt/docker/observability/node-exporter-textfiles"
TEXTFILE_DIR_ENV = "MACRO_MONITOR_TEXTFILE_DIR"
METRIC_FILE_NAME = "macro_monitor.prom"

_HELP: dict[str, str] = {
    "macro_monitor_last_run_timestamp_seconds": (
        "Unix timestamp of the last run of this phase (collect-rss or correlate)."
    ),
    "macro_monitor_last_run_success": (
        "1 if the last run of this phase completed successfully, 0 otherwise."
    ),
    "macro_monitor_work_quantity": (
        "Units of work this phase actually completed on its last run "
        "(items inserted for collect, correlations written for correlate)."
    ),
    "macro_monitor_work_available": (
        "Units of work available to this phase on its last run "
        "(feed entries seen for collect, observations considered for correlate)."
    ),
}
_METRIC_NAMES = tuple(_HELP)
_LINE_RE = re.compile(r'^(\w+)\{phase="([^"]+)"\}\s+([0-9.eE+\-]+)\s*$')


def _textfile_path(textfile_dir: str | None) -> tuple[str, str]:
    directory = textfile_dir or os.environ.get(TEXTFILE_DIR_ENV, DEFAULT_TEXTFILE_DIR)
    return directory, os.path.join(directory, METRIC_FILE_NAME)


def _parse_existing(path: str) -> dict[str, dict[str, float]]:
    """{metric_name: {phase: value}} for every metric this module owns, read from disk.

    Any line this module would not itself have written (unknown metric name, malformed value) is
    dropped rather than preserved — this file has exactly one writer (this module, from two
    phases), so "foreign content" here almost certainly means a prior partial/corrupt write, and
    the safest recovery is to drop it and let both phases re-populate on their next run.
    """
    data: dict[str, dict[str, float]] = {name: {} for name in _METRIC_NAMES}
    if not os.path.exists(path):
        return data
    try:
        with open(path) as fh:
            for raw_line in fh:
                m = _LINE_RE.match(raw_line.strip())
                if m and m.group(1) in data:
                    try:
                        data[m.group(1)][m.group(2)] = float(m.group(3))
                    except ValueError:
                        continue
    except OSError:
        return {name: {} for name in _METRIC_NAMES}
    return data


def _render(data: dict[str, dict[str, float]]) -> str:
    out: list[str] = []
    for name in _METRIC_NAMES:
        phase_values = data[name]
        if not phase_values:
            continue
        out.append(f"# HELP {name} {_HELP[name]}")
        out.append(f"# TYPE {name} gauge")
        for phase in sorted(phase_values):
            out.append(f'{name}{{phase="{phase}"}} {phase_values[phase]}')
    return "\n".join(out) + ("\n" if out else "")


def write_phase_metrics(
    phase: str,
    *,
    success: bool,
    work_quantity: int,
    work_available: int,
    textfile_dir: str | None = None,
    now: float | None = None,
) -> str:
    """Merge this phase's metrics into the shared textfile and write it atomically.

    Returns the path written, or ``""`` if the textfile directory does not exist (a dev box or a
    test sandbox without the observability stack deployed) — a missing directory is never fatal to
    the CLI command that called this; metrics coverage is a deploy-time concern, not a reason for
    ``collect-rss``/``correlate`` to fail on a laptop.
    """
    directory, path = _textfile_path(textfile_dir)
    if not os.path.isdir(directory):
        return ""

    data = _parse_existing(path)
    data["macro_monitor_last_run_timestamp_seconds"][phase] = now if now is not None else time.time()
    data["macro_monitor_last_run_success"][phase] = 1 if success else 0
    data["macro_monitor_work_quantity"][phase] = work_quantity
    data["macro_monitor_work_available"][phase] = work_available

    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as fh:
        fh.write(_render(data))
    os.replace(tmp_path, path)
    return path
