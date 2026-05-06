"""Regression guard for CLAUDE.md Known Bug Pattern #4.

Original offender: ``mm_liquidate_expiring()`` zeroed ``mm_inventory.net_position``
without confirming the position had actually settled. That code path was
deleted during the daemon refactor. This test pins the invariant so it can't
be reintroduced.

Invariant: the only production site that writes ``mm_inventory SET net_position``
is the canonical settlement path inside ``record_settlements()`` in trade.py.

Any other call site (a future exit policy, a "cleanup" job, a "force-close" CLI)
that wants to zero inventory must funnel through ``record_settlements()`` so the
P&L is computed against the actual settlement outcome.

The check is AST-based: walk every ``Call`` to ``conn.execute`` (or any callable
named ``execute``) in production code, look at the SQL string argument, and flag
any UPDATE of ``mm_inventory.net_position`` that doesn't live inside the
``record_settlements`` function body.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files allowed to contain inventory-zero writes. Tools / backtests / migrations
# are explicitly excluded — those run offline and don't affect live state.
PRODUCTION_FILES: list[Path] = [REPO_ROOT / "trade.py"]
PRODUCTION_FILES += sorted((REPO_ROOT / "bot").rglob("*.py"))

# SQL pattern: "UPDATE mm_inventory ... SET ... net_position ..."
INVENTORY_ZERO_RE = re.compile(
    r"UPDATE\s+mm_inventory\s+SET[^;]*\bnet_position\b",
    re.IGNORECASE | re.DOTALL,
)

# The single function in which the canonical settlement zero is allowed to
# live. If you rename it, update this list too.
ALLOWED_FUNCTIONS = {"record_settlements"}


def _enclosing_function(tree: ast.AST, target_lineno: int) -> str | None:
    """Find the innermost FunctionDef containing ``target_lineno``."""
    best: tuple[int, str] | None = None  # (start_lineno, name)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            if node.lineno <= target_lineno <= end:
                if best is None or node.lineno > best[0]:
                    best = (node.lineno, node.name)
    return best[1] if best else None


def _iter_sql_strings(tree: ast.AST):
    """Yield (lineno, sql_text) for every constant string passed to a call.

    We don't try to be too clever about which call — if a literal SQL string
    appears as a Call arg in production code, it's worth checking."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if INVENTORY_ZERO_RE.search(arg.value):
                        yield node.lineno, arg.value


@pytest.mark.parametrize("path", PRODUCTION_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_inventory_zero_only_in_settlement_path(path: Path) -> None:
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    bad: list[tuple[int, str | None, str]] = []
    for lineno, sql in _iter_sql_strings(tree):
        fn = _enclosing_function(tree, lineno)
        if fn not in ALLOWED_FUNCTIONS:
            bad.append((lineno, fn, sql.strip()[:120]))
    assert not bad, (
        f"{path.relative_to(REPO_ROOT)}: UPDATE of mm_inventory.net_position outside "
        f"record_settlements(). Inventory MUST only be zeroed when settlement is "
        f"confirmed (CLAUDE.md Known Bug Pattern #4). Offenders: {bad}"
    )
