# Codebase polish pass — 2026-08-11

`/codebase-polish` run against this repo (~1730 LOC, 13 modules under `src/macro_monitor/`).
Grounded in a live best-practices sweep (see Standards Brief below), three parallel judgment
lenses (elegance/architecture/DRY, dead-code/duplication triage, observability), then applied
directly and verified against the existing test suite + an independent bug-focused regression
review of the diff.

## Standards Brief sources

- Vault doc `Development/Research/codebase-quality-polish-skill.md` (refreshed same-day for this
  run) — Google eng-practices reviewer standard, Fowler's speculative-generality/YAGNI boundary,
  hybrid static+LLM review pattern, and (added this run) a repo-specific note that this repo's
  `log.py`/`metrics.py` already implement the household's fleet logging contract
  (home-infra `CONVENTIONS.md` §18) — the observability lens audited *coverage* of that existing
  pattern rather than proposing a replacement.
- Deterministic tools: `ruff` (already configured, clean before and after) + `vulture
  --min-confidence 60` (dead-code signal). No architecture-boundary tool run — 13 modules is too
  small to justify Import Linter/Tach setup cost.

## Deterministic signal vs. what survived triage

`vulture --min-confidence 60` raised 11 hits. 10 were false positives (7 click-decorated CLI
commands vulture can't see are invoked via Click's dispatch; 2 `sqlite3.Connection.row_factory`
attribute assignments, a side-effect vulture can't trace; confirmed by direct code read). 1 was
real: `reviewer.ReviewOutcome`, a dataclass with zero references anywhere in the repo.

## Applied (all findings — no escalations; nothing touched a public interface or looked like
deliberate dead code)

### Dead code
- `src/macro_monitor/reviewer.py` — deleted unused `ReviewOutcome` dataclass (confirmed dead by
  repo-wide grep across `.py` and non-`.py` files); also dropped the now-unused `dataclass` import.

### Elegance / architecture / DRY
- `src/macro_monitor/cli.py` — removed the third copy of the version string (`__version__ =
  "0.1.0"` in `cli.py`, duplicating `pyproject.toml` and `__init__.py`); `@click.version_option`
  now reads the package's one copy via `from . import __version__`.
- `src/macro_monitor/config.py` — `load_config`'s loadable-key allowlist now derives from
  `dataclasses.fields(Config)` instead of a hand-maintained tuple that had to be kept in sync by
  hand with every new `Config` field.
- `src/macro_monitor/cli.py:228` (was) — `correlate_cmd`'s default lookback window used local-time
  `date.today()` while every other "today" in the package (`cli.py`, `db.py`) goes through UTC
  `db.now_iso()`; on a non-UTC host this could start the trailing window on the wrong calendar
  day relative to the `observed_date` values it's matched against. Now sources from
  `db.now_iso()[:10]` like everything else.
- `src/macro_monitor/cli.py` — extracted `_finish_phase(...)` to replace 4 sites that hand-paired
  a closing `log_event(...)` with `metrics.write_phase_metrics(...)` (collect-rss and correlate,
  success and failure paths); the §18 did-nothing-rule numbers now agree across both signals by
  construction instead of by copy-paste discipline.
- `src/macro_monitor/cli.py` — `sources add --kind` now reads its choices from
  `sources.VALID_KINDS` instead of restating `["rss", "websearch"]` as a second literal.
- `src/macro_monitor/validation.py` — `validate_observed_date` takes a `field_name` kwarg
  (default `"observed_date"`) so its error messages name the field the caller actually validated;
  `sources.py` passes `"checked_on"`, `correlator.py`'s `since` call passes `"since"`. Previously
  a bad `--checked-on` value surfaced as "observed_date must match YYYY-MM-DD", naming a field the
  user never typed.
- `src/macro_monitor/observations.py` — hoisted two function-local imports (`ValidationError`,
  `re`) to module level; both were the odd one out next to the module's other top-level imports
  with no cycle/optional-dependency reason for the local scoping.
- `src/macro_monitor/cli.py` — replaced a hard-to-parse multi-line conditional expression with a
  plain `if`/`else`; replaced `open(hypotheses_json).read()` with `Path(hypotheses_json).read_text()`
  (the only un-contexted file read in the package; every other file read already used `pathlib`).

### Observability
- `src/macro_monitor/config.py` — `load_config` previously handled a *missing* `config.yaml`
  gracefully (logged + defaulted) but let a *malformed* one raise an unhandled `yaml.YAMLError`
  with no `log_event`. Now logs `config.parse_failed` (path, error type/message) before
  re-raising — still a hard failure (a malformed config is a real operator error), now queryable
  in the same JSON stream as everything else.
- `src/macro_monitor/cli.py` (`review_cmd`) — the `--hypotheses-json` file read happened *after*
  `open_review_run` had already inserted a `review_runs` row and logged `review.started`, but
  outside the surrounding try/except. A missing/malformed file crashed uncaught and left that row
  permanently stuck at `status='started'` — `review.failed` never fired. The read now happens
  inside the existing try block, whose `except` was widened to `(OSError, json.JSONDecodeError,
  reviewer.ReviewError)`, so any of these failure modes now closes the run and logs
  `review.failed` like every other failure path in this command already does.
- `src/macro_monitor/cli.py` / `_finish_phase` — `metrics.write_phase_metrics`'s `""` return
  (textfile directory missing — deliberately non-fatal, e.g. a dev box) was previously discarded
  at all 4 call sites. A production textfile-dir misconfiguration would have kept every run
  exiting 0 and logging `"outcome":"ok"` while `macro_monitor.prom` silently stopped updating
  forever — indistinguishable from healthy except by manually checking the file's mtime.
  `_finish_phase` now logs `metrics.write_skipped` (phase, run_id) whenever that happens.
- `src/macro_monitor/db.py` (`execute_write`) — SQLITE_BUSY retry-exhaustion (the two-writer-process
  overlap the module's own docstring calls out as a real, anticipated scenario) re-raised with no
  `log_event` anywhere in the chain. Failure was already loud (non-zero exit via the raised
  exception), but now also logs `db.write_busy_exhausted` (retries, error message) before the
  raise, so it's filterable in the same stream instead of only visible as a bare traceback.

## Not flagged (checked, judged intentional — no action)

- `correlator.py`'s `DATE_COLUMN`/`MINIMAL_COLUMNS` key overlap — deriving one from the other
  would trade the explicit FR-05 trap documentation for brittleness.
- `collector_websearch_ingest.py`'s thin pass-through shape — it's the named FR-04 write path
  per its own docstring, not accidental duplication.
- `log.py`/`metrics.py`'s design — already the result of a deliberate 2026-08-01 §18-conformance
  pass (`docs/DECISIONS.md`); the observability lens audited coverage of this pattern, not its
  design.
- `ops/paper_db_snapshot.py`'s lack of logging — runs outside this package (as the `algo-factory`
  user, per `docs/DECISIONS.md`), can't trivially call `log_event` without a cross-service-user
  package dependency; systemd's default journal capture already makes an uncaught failure loud.
  Informational only — no action taken.
- The FR-NN/`docs/DECISIONS.md` traceability-comment volume — house style, not noise.

## Regression safety net

- `ruff check src tests` — clean before and after.
- `pytest` — 101 passed before; one test
  (`test_correlate_no_flags_uses_config_lookback_window`) needed updating after the
  `date.today()` → `db.now_iso()` fix (it monkeypatched the wrong source of "today"); updated to
  monkeypatch `db.now_iso` instead. 101 passed after.
- Independent bug-focused regression review of the full diff (correctness only, not style):
  **PASS** — no bugs found across all 6 checked risk areas (`_finish_phase` behavioral parity,
  `db.now_iso()` purity, `config.py` field-derivation equivalence, `review_cmd` except-widening
  scope, `validate_observed_date` default-argument backward compatibility, full-suite +
  strict-log-shape re-verification).

## Not applicable to this run

- Fallow (TS/JS dead-code/architecture tool) — this is a pure Python repo, not run.
- Import Linter / Tach (Python architecture-boundary tools) — 13 modules is too small to justify
  the setup cost; no boundary violations surfaced by the judgment lens either.
