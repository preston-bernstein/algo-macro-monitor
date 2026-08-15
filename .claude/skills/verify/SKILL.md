---
name: verify
description: How to verify a change to internal-monitor-service by actually running it, not just via pytest.
---

# Verify internal-monitor-service

1. Activate the venv (create it first if absent): `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`. Note: `fleet-logging` and `feed-commons` are pinned as private `git+ssh://` deps — the install needs SSH access to both repos.
2. `pytest -m "not network"` for the fast suite. The one `@pytest.mark.network` test in `tests/test_smoke_e2e.py` hits the real production Fed RSS feed and skips gracefully if unreachable.
3. To exercise the real CLI end to end (the actual `macro-monitor` console script, not `CliRunner`):
   ```
   .venv/bin/macro-monitor --db-path /tmp/verify.db sources add --name fed-press --kind rss \
     --url-or-query "https://www.federalreserve.gov/feeds/press_all.xml" --checked-on <today>
   .venv/bin/macro-monitor --db-path /tmp/verify.db collect-rss
   ```
   This makes a real network call to the real Federal Reserve feed and writes real rows to a
   throwaway SQLite DB — inspect with `sqlite3 /tmp/verify.db "select * from raw_observations"`.
   A second `collect-rss` run against the same DB should insert 0 new rows (dedup/append-only).
4. Clean up: `rm /tmp/verify.db`.

The live-deployed system runs under `systemd/macro-monitor-collect.timer` on the desktop host as
the `internal-monitor-service` service user, writing to `/home/internal-monitor-service/app/data/macro_monitor.db`. Deploy is
`scripts/deploy.sh`, run as root via SSH on the desktop itself.
