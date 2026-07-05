"""FR-16 / acceptance criterion 13: the scheduled slash-commands declare an explicit tool
allowlist that excludes /spec-gather, /spec-challenge, and any backtest/gate/deploy command."""

from __future__ import annotations

from pathlib import Path

import pytest

COMMANDS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "commands"
COMMAND_FILES = [
    "macro-monitor-collect-websearch.md",
    "macro-monitor-review.md",
]

FORBIDDEN_IN_ALLOWLIST = (
    "spec-gather",
    "spec-challenge",
    "new-story",
    "backtest",
    "gates",
    "deploy",
)


def _frontmatter(text: str) -> str:
    assert text.startswith("---"), "command file must start with YAML frontmatter"
    return text.split("---", 2)[1]


def _allowlist_entries(fm: str) -> list[str]:
    """The `- ...` list items under `allowed-tools:` — ignoring comments and other keys.

    We check the ENTRIES, not the whole frontmatter: the frontmatter comments legitimately name
    the excluded commands to document why they're absent, which is the opposite of a violation.
    """
    entries: list[str] = []
    in_list = False
    for raw in fm.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("allowed-tools:"):
            in_list = True
            continue
        if in_list:
            if line.startswith("- "):
                entries.append(line[2:].strip())
            elif line and not line.startswith("#"):
                # a new top-level key ends the list
                in_list = False
    return entries


@pytest.mark.parametrize("fname", COMMAND_FILES)
def test_command_declares_allowlist(fname):
    fm = _frontmatter((COMMANDS_DIR / fname).read_text())
    entries = _allowlist_entries(fm)
    assert "allowed-tools:" in fm, f"{fname} must declare an explicit allowed-tools list (FR-16)"
    assert entries, f"{fname} allowed-tools list must be non-empty (FR-16)"


@pytest.mark.parametrize("fname", COMMAND_FILES)
def test_allowlist_excludes_pipeline_commands(fname):
    entries = " ".join(_allowlist_entries(_frontmatter((COMMANDS_DIR / fname).read_text()))).lower()
    offenders = [tok for tok in FORBIDDEN_IN_ALLOWLIST if tok in entries]
    assert not offenders, f"{fname} allowlist must not include pipeline commands: {offenders}"
