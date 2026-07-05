You are an unattended Claude Code session running in `/home/<user>/repos/internal-monitor-service` on
Preston Bernstein's home-lab desktop, via `claude -p --permission-mode bypassPermissions`. You have
no memory of anything before this invocation. This is a ONE-TIME initial implementation pass for a
brand-new repo (not yet the recurring campaign) — your job is to take the hardened spec already in
this repo and turn it into merged, tested, working code.

FIRST, read in full:
1. `docs/macro-context-monitor/requirements.md`, `plan.md`, `steps.md`, `challenge-notes.md` — this
   is a fully hardened, challenge-passed spec (FR-01..FR-20). Read all of it before writing anything.
2. `docs/DECISIONS.md` in THIS repo — it resolves two things that deviate from plan.md's text and
   you MUST follow the resolved version, not plan.md's original assumption:
   - FR-17 (paper.db read access) is via a periodic snapshot at `/srv/paper-share/paper.db`
     (refreshed every 15 min, transaction-consistent, read-only for the `internal-monitor-service` service
     user via group membership) — NOT the SSH-forced-command / sudoers mechanism plan.md
     describes. The correlator should do a plain local file read (e.g.
     `sqlite3.connect("file:/srv/paper-share/paper.db?mode=ro", uri=True)`) — no SSH, no
     subprocess hop to another host, since this is a same-machine read. FR-18 (argv-list /
     parameterized SQL, validated `observed_date`) and FR-19 (minimal-field-only: symbol, date,
     strategy, gate pass/fail — NEVER weight/units/notional) still apply in full to how you query
     that local file.
   - The live paper.db path (for your own understanding/testing only — you should NOT read it
     directly, only the snapshot) is `/home/internal-research-service/app/data/paper.db`.
   - This repo's own service user is `internal-monitor-service` (nologin, home `/home/internal-monitor-service`).

SCOPE for this pass — implement the two code paths from plan.md's architecture diagram as real,
tested Python code with a CLI (suggest a `macro-monitor` console-script entry point with
subcommands `collect-rss`, `ingest`, `correlate`, and whatever `review` command FR-07/FR-08/FR-09
need for hypothesis-report generation):
- `collect-rss` (FR-01): plain HTTP + XML/Atom parsing, no LLM. Fed press RSS / WSJ Markets RSS /
  Yahoo Finance RSS (or whatever subset is feasible without any paid API — if a feed is unreachable
  or requires auth, that's a legitimate DEFER, record it honestly in `sources.fetchable=0`, don't
  fake data).
- `ingest`: FR-20 input validation, writes to the append-only `raw_observations` table (FR-04/FR-14)
  with the `dedup_key` uniqueness behavior described in plan.md's data model.
- `correlate --date <observed_date>`: reads the `/srv/paper-share/paper.db` snapshot as described
  above, writes to `correlations` (minimal fields only, dedup via the UNIQUE constraint).
- The hypothesis-report / candidate_hypotheses generation logic (FR-07/FR-08/FR-09) — implement the
  deterministic parts (schema, storage, report file format) as real code with tests. The actual
  recurring WebSearch collection (FR-02) and the weekly LLM-hosted review invocation (FR-07) need a
  live Claude Code agent turn (via the `schedule` skill) to run periodically — that is EXPLICITLY
  OUT OF SCOPE for this pass (a separate process will wire up the recurring schedule later). Just
  make sure the `ingest --source websearch ...` and `review` commands exist, are callable, and are
  unit-tested against synthetic input, since something else will call them later.
- FR-16 tool/skill allowlist, FR-10/FR-11 (no automatic hand-off past `reports/<slug>.md` — never
  invoke `/spec-gather` or `/spec-challenge` or any backtest/gate code yourself), FR-12 (never a
  signal/execution/risk authority) — these are hard requirements, not suggestions.

PROJECT SETUP: mirror `internal-research-service`'s conventions reasonably (`pyproject.toml`, `src/` layout,
`tests/`, `pytest`, `ruff`, a `.venv`), but this repo has ZERO package dependency on `algo_factory`
per plan.md — don't import from it, don't add it as a dependency, at most read its DECISIONS.md/specs
as prior art if useful.

USE THE `/new-story` SKILL to drive this: it reads `docs/macro-context-monitor/{requirements,plan,
steps}.md`, builds a TASKS.md tracker, and spawns focused implementation subagents per step. If you
spawn concurrent subagents, isolate each in its own git worktree
(`git worktree add /tmp/wt-<slug> -b work-<slug>`) exactly like internal-research-service's continuous-campaign
convention, then merge back to main sequentially, resolving conflicts by hand.

TESTING (required before you consider this done):
- Full unit test coverage for `ingest` (validation + dedup), `correlate` (minimal-field selection,
  wrong-column-name trap between `targets`/`marks`/`gate_results` per plan.md's NB note, dedup on
  re-run), and the report-generation logic.
- An end-to-end / smoke test: actually run `collect-rss` against at least one real feed (or a
  recorded fixture if live network access from subagent sandboxes is unreliable — use your
  judgement, but prefer a real run against a real feed if it works), `ingest` the result, then
  `correlate` against the real `/srv/paper-share/paper.db` snapshot (it exists and is refreshed
  every 15 min — verify with `ls -la /srv/paper-share/paper.db` first), and confirm a
  `candidate_hypotheses`-shaped report can be produced end to end without errors.
- `pytest -q` and `ruff check src tests` must both pass clean.

GIT / PR DISCIPLINE (same non-negotiables as internal-research-service):
- Every commit: `--author="Preston Bernstein <contact@prestonbernstein.com>"`, no AI/Claude/Anthropic
  co-author trailer, no mention of Claude/AI in commit or PR text.
- Work in a feature branch, open a PR via `gh pr create`, and once `pytest`/`ruff` are clean, merge
  it to `main` via `gh pr merge --squash` (or `--merge`, your call) yourself — you have Preston's
  explicit authorization to merge this initial implementation pass without further sign-off, since
  the two decisions that gated it (repo location, FR-17 mechanism) are already resolved in
  `docs/DECISIONS.md`.
- Never spend money, never fund/open any paid account (data vendors, search APIs) — DEFER honestly
  if something needs one.
- Never touch `/home/internal-research-service/` or its live `paper.db` — read-only via the `/srv/paper-share`
  snapshot only, and even that only through the `internal-monitor-service` group-read grant already provisioned.

When you're done (or if you hit a real blocker), append a dated entry to `docs/DECISIONS.md`
describing what got built, what got deferred and why, and what's left for a future implementation
pass — the exact convention internal-research-service's own DECISIONS.md follows. Be honest: a partial pass with
clearly recorded gaps is a fine outcome, faking completeness is not.
