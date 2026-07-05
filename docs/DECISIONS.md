# Decisions

## Repo + service account facts (resolved 2026-07-05)

- **Repo.** `github.com/preston-bernstein/algo-macro-monitor` (private), cloned at
  `/home/preston/repos/algo-macro-monitor` on desktop. Sibling to `algo-factory`, no package
  dependency on it, per plan.md's "algo-corpus precedent."
- **Service user.** `algo-macro` (uid 981, nologin, home `/home/algo-macro`). Mirrors
  `algo-factory`'s own service-user pattern. Deployed code should land under `/home/algo-macro/app`
  via a sync-deploy script mirroring `algo-factory-desktop-sync.service`.
- **FR-17 (paper.db read access) — RESOLVED, supersedes plan.md's SSH-forced-command assumption.**
  Preston chose group-read over SSH-forced-command / sudoers-Cmnd_Alias. Concretely:
  - The live `paper.db` is at `/home/algo-factory/app/data/paper.db` (NOT
    `/home/algo-factory/data/paper.db` — that path is a stale pre-`app/`-layout copy, ignore it).
  - `paper-db-snapshot.timer` (systemd, runs as `algo-factory`, every 15 min) uses SQLite's own
    backup API (`sqlite3.connect(...).backup(...)`, transaction-consistent even against concurrent
    writers — see `/usr/local/lib/algo-macro/paper_db_snapshot.py`) to copy it into
    `/srv/paper-share/paper.db`, mode `0640`, owner `algo-factory:paper-readers`.
  - `algo-macro` is a member of the `paper-readers` group and can read (never write)
    `/srv/paper-share/paper.db` directly — **no SSH, no sudoers, no traversal into
    `/home/algo-factory` at all** (verified: `algo-macro` gets Permission denied on
    `/home/algo-factory/` itself). This is a same-host read, plain `sqlite3.connect(path, uri=...,
    mode=ro)` or equivalent — building an SSH hop for a same-machine file read would be needless
    complexity plan.md didn't anticipate because it assumed a stricter isolation mechanism.
  - The correlator's FR-18 (argv-list/parameterized SQL) and FR-19 (minimal-field-only:
    symbol/date/strategy/gate-pass-fail, never weight/units/notional) requirements are UNCHANGED —
    only the transport (direct local file read of the snapshot vs. SSH) differs from plan.md's text.
  - The snapshot is up to 15 minutes stale relative to the live DB — acceptable for this tool's
    observation-and-hypothesis-proposal purpose (FR-12: never a trading/execution/risk authority).
- **Campaign queue sizing (for the ongoing research/implementation loop, once this initial
  implementation is merged).** Target ~3 concurrent research threads and ~3 concurrent
  implementation threads — smaller than `algo-factory`'s ~6/~6, given this repo's narrower scope.
