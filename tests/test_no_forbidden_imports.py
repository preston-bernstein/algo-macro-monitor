"""Standing guard test (Step 3, FR-12/FR-13).

Asserts by AST scan that the package never imports algo_factory.execution / algo_factory.risk /
backtest.gates, and that paper.db is only ever referenced from correlator.py's read-only path.
This is the structural, in-CI enforcement of the FR-12/FR-13 static-review acceptance criteria —
not just "the code doesn't happen to call it."
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "macro_monitor"

FORBIDDEN_IMPORT_PREFIXES = (
    "algo_factory",
    "backtest.gates",
    "backtest",
)


def _module_files():
    return sorted(SRC.glob("*.py"))


def test_no_forbidden_imports():
    offenders = []
    for path in _module_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_IMPORT_PREFIXES):
                    offenders.append((path.name, name))
    assert not offenders, f"forbidden imports found: {offenders}"


def _strip_prose(path) -> str:
    """Return module source with docstrings and comments removed.

    The guards below assert properties of *executable code*, not of explanatory prose — several
    modules legitimately name paper.db and /spec-gather in docstrings precisely to document what
    the tool must never do (FR-10/FR-12). Scanning raw text would flag that documentation.
    """
    import io
    import tokenize

    src = path.read_text()
    out: list[str] = []
    prev_type = tokenize.INDENT
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:  # pragma: no cover
        return src
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev_type in (
            tokenize.INDENT,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.DEDENT,
        ):
            # a bare string expression statement == a docstring
            continue
        out.append(tok.string)
        if tok.type not in (tokenize.NL, tokenize.NEWLINE):
            prev_type = tok.type
    return " ".join(out)


def test_snapshot_opened_read_only_only_in_correlator():
    """The paper.db snapshot is opened (mode=ro) ONLY in correlator.py, in executable code (FR-06)."""
    offenders = []
    for path in _module_files():
        code = _strip_prose(path)
        if "mode=ro" in code and path.name != "correlator.py":
            offenders.append(path.name)
    assert not offenders, f"paper.db opened outside correlator.py: {offenders}"
    assert "mode=ro" in _strip_prose(SRC / "correlator.py")


def test_no_write_mode_paper_db_open():
    """No module ever opens a write-mode sqlite URI to the snapshot (FR-06)."""
    for path in _module_files():
        code = _strip_prose(path)
        assert "mode=rwc" not in code and "mode=rw" not in code, path.name


def test_no_shellout_at_all():
    """The package never shells out — no subprocess / os.system in executable code.

    This is the structural guarantee behind FR-10/FR-11/FR-12/FR-16: with no shell-out mechanism
    anywhere in the package, no code path can invoke /spec-gather, /spec-challenge, a backtest
    runner, or any gate command, regardless of what untrusted content it processes. (The correlator
    reads the snapshot as a local file per docs/DECISIONS.md — no SSH/subprocess hop.)
    """
    offenders = []
    for path in _module_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                a.name == "subprocess" for a in node.names
            ):
                offenders.append((path.name, "import subprocess"))
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                offenders.append((path.name, "from subprocess"))
            if isinstance(node, ast.Attribute) and node.attr == "system":
                offenders.append((path.name, "os.system"))
    assert not offenders, f"shell-out mechanism present: {offenders}"
