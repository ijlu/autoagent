"""Regression guard for CLAUDE.md Known Bug Pattern #5.

Kalshi returns ``*_fp`` and ``*_dollars`` fields as strings. They MUST be
parsed with ``round(float(...))``, never ``int(float(...))`` — the latter
truncates and produces an off-by-one whenever the float is a hair below an
integer (e.g. 99.99999999... → 99 instead of 100).

We've eaten this bug in production once. This test scans every production
``.py`` file under ``bot/`` and the top-level scripts (``trade.py``, etc.)
for the antipattern via AST.

Pattern matched (and rejected):

    int(float(some_fp_value))
    int(float(market["yes_bid_fp"]))
    int(float(...["...dollars"]))

If a future contributor reaches for ``int(float(x))`` on a Kalshi field, this
test fails closed at CI time and forces a switch to ``round(float(x))``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PRODUCTION_FILES: list[Path] = [
    REPO_ROOT / "trade.py",
]
PRODUCTION_FILES += sorted((REPO_ROOT / "bot").rglob("*.py"))

SUSPECT_SUFFIXES = ("_fp", "_dollars")


def _walks_a_kalshi_field(node: ast.AST) -> bool:
    """True if ``node`` looks like an access into a Kalshi ``*_fp`` /
    ``*_dollars`` field — i.e. the inner expression is a string literal,
    Subscript with a constant key, or attribute reference whose name ends
    in one of the suspect suffixes."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if sub.value.endswith(SUSPECT_SUFFIXES):
                return True
        if isinstance(sub, ast.Attribute) and sub.attr.endswith(SUSPECT_SUFFIXES):
            return True
        if isinstance(sub, ast.Name) and sub.id.endswith(SUSPECT_SUFFIXES):
            return True
    return False


def _iter_int_float_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Outer call: int(...)
        if not (isinstance(node.func, ast.Name) and node.func.id == "int"):
            continue
        if len(node.args) != 1:
            continue
        inner = node.args[0]
        # Inner call: float(...)
        if not isinstance(inner, ast.Call):
            continue
        if not (isinstance(inner.func, ast.Name) and inner.func.id == "float"):
            continue
        yield node


@pytest.mark.parametrize("path", PRODUCTION_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_int_float_on_kalshi_fixed_point_fields(path: Path) -> None:
    """Reject ``int(float(x))`` where ``x`` references a ``*_fp`` /
    ``*_dollars`` field — must be ``round(float(x))`` instead."""
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    offenders: list[tuple[int, str]] = []
    src_lines = src.splitlines()
    for call in _iter_int_float_calls(tree):
        if _walks_a_kalshi_field(call.args[0]):
            line = src_lines[call.lineno - 1].strip() if 0 < call.lineno <= len(src_lines) else "<unknown>"
            offenders.append((call.lineno, line))
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)}: int(float(...)) on a Kalshi *_fp / *_dollars field. "
        f"Use round(float(...)) instead — int() truncates and causes off-by-one. "
        f"Sites: {offenders}"
    )
