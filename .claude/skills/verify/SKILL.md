---
name: verify
description: How to verify a change to algo-macro-monitor by actually running it, not just via pytest.
---

# Verify algo-macro-monitor

1. Activate the venv (create it first if absent): `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`. `fleet-logging` and `feed-commons` are pinned as public `git+https://` deps at an exact commit -- no credentials needed.
2. `pytest -m "not network"` for the fast suite. The one `@pytest.mark.network` test in `tests/test_smoke_e2e.py` hits the real production Fed RSS feed and skips gracefully if unreachable.
3. To exercise the real CLI end to end (the actual `macro-monitor` console script, not `CliRunner`):
   ```
   .venv/bin/macro-monitor --db-path /tmp/verify.db sources add --name fed-press --kind rss \
     --url-or-query "https://www.federalreserve.gov/feeds/press_all.xml" --checked-on <today>
   .venv/bin/macro-monitor --db-path /tmp/verify.db collect-rss
   ```
   This makes a real network call to the real Federal Reserve feed and writes real rows to a
   throwaway SQLite DB -- inspect with `sqlite3 /tmp/verify.db "select * from raw_observations"`.
   A second `collect-rss` run against the same DB should insert 0 new rows (dedup/append-only).
4. Clean up: `rm /tmp/verify.db`.

A live deployment runs under `systemd/macro-monitor-collect.timer` on the deploy host as a
dedicated service user (default account name `macro-monitor`, see `scripts/deploy.sh`), writing
to `<app-dir>/data/macro_monitor.db`. Deploy is `scripts/deploy.sh`, run as root on the deploy
host itself.
