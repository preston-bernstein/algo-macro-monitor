"""FR-15 / acceptance criterion 11: raw log DB, reports, and credentials are gitignored."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_gitignore_covers_secrets_and_raw_content():
    patterns = (REPO / ".gitignore").read_text()
    for required in ("data/macro_monitor.db", "reports/*.md", ".env", "config.yaml"):
        assert required in patterns, f".gitignore missing FR-15 pattern: {required}"
