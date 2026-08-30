"""Public-readiness regression guard.

This repo is published publicly. This test scans every git-tracked file for three classes of
content that must never reappear once removed:

1. A literal RFC1918 private-IP address (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) — this repo's
   home-lab deployment lives on a real LAN and no example/config/doc/fixture should ever encode
   its address. A fixture that genuinely needs *a* private-IP-shaped example should use something
   in ``172.16.0.0/12`` (which the real fleet never uses) and add it to ``_ALLOWED_IPS`` below,
   explicitly, rather than being silently exempted.
2. The literal string ``desktop-agent`` — the real fleet's SSH alias for the deploy host. Docs and
   scripts must refer to "the deploy host" / an env-var-configured hostname instead.
3. Any of the real algo-factory paper-trading strategy sleeve names. These are the operator's
   actual live research taxonomy and must never be named in this repo, in code, tests, or docs —
   only generic placeholders (``strategy-a``, ``strategy-b``, ...) belong here.

Run standalone: ``pytest tests/test_no_public_disclosure.py -q``
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# This file's own path — its docstring/body legitimately discusses these patterns without
# encoding a real instance of any of them; still exclude it defensively so a future edit to the
# prose above can't trip the very guard it defines.
_SELF = Path(__file__).resolve()

# RFC1918: 10.0.0.0/8, 172.16.0.0/12 (172.16.x - 172.31.x), 192.168.0.0/16.
_RFC1918_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)

# Explicit allowlist for a private-IP-shaped literal that is a deliberate, non-real-LAN example
# fixture (see module docstring). Empty today — nothing in this repo needs one.
_ALLOWED_IPS: frozenset[str] = frozenset()

_DESKTOP_AGENT_RE = re.compile(r"desktop-agent")

# The real live algo-factory paper-trading strategy sleeve names (per the public-readiness audit).
# None of these may appear anywhere in this repo's tracked source.
_REAL_STRATEGY_NAMES = (
    "cross_asset_trend",
    "cs_momentum_wide",
    "cs_momentum",
    "cs_trend_cross_asset",
    "equity_momentum_mtum",
    "individual_momentum",
    "industry_momentum",
    "portfolio_ensemble",
    "vrp_spy_timing",
    "yield_curve_carry",
)
_STRATEGY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in _REAL_STRATEGY_NAMES) + r")\b"
)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def _iter_text_files():
    for path in _tracked_files():
        if path.resolve() == _SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — not a text-disclosure vector
        yield path, text


def test_no_rfc1918_ip_literal_in_tracked_source():
    violations = []
    for path, text in _iter_text_files():
        for match in _RFC1918_RE.finditer(text):
            ip = match.group(0)
            if ip in _ALLOWED_IPS:
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {ip}")
    assert not violations, (
        "Private-IP literal(s) found in tracked source (RFC1918 addresses must never be "
        "committed to this public repo):\n" + "\n".join(violations)
    )


def test_no_desktop_agent_alias_in_tracked_source():
    violations = []
    for path, text in _iter_text_files():
        for match in _DESKTOP_AGENT_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line_no}")
    assert not violations, (
        "The real fleet's 'desktop-agent' SSH alias was found in tracked source — this must "
        "never be named in a public repo:\n" + "\n".join(violations)
    )


def test_no_real_strategy_sleeve_names_in_tracked_source():
    violations = []
    for path, text in _iter_text_files():
        for match in _STRATEGY_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)}")
    assert not violations, (
        "A real algo-factory strategy sleeve name was found in tracked source — only generic "
        "placeholder names belong in this public repo:\n" + "\n".join(violations)
    )
