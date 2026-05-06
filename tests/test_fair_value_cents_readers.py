"""Regression guard for CLAUDE.md Known Bug Pattern #13.

``mm_orders.fair_value_cents`` is stored as P(YES) on BOTH the YES and NO
rows of a paired quote (this is how the now-deleted MM writer left it; the
same convention must be preserved by future writers). The Apr 17 v1 backtest
broke (Brier 0.50) because it assumed per-side storage and read the column
raw — picking up YES-prob from NO rows and treating it as NO-prob.

The fix at the read layer is to either:
  (a) filter to a single side: ``WHERE side = 'yes'`` / ``WHERE side = 'no'``
  (b) normalize on read:
      ``CASE WHEN side = 'yes' THEN fair_value_cents ELSE 100 - fair_value_cents END``
      to recover P(our-side).

This test scans every production ``.py`` file for SQL strings that touch
``mm_orders.fair_value_cents`` and forces each one to declare its handling
explicitly. Single-ticker selects without aggregation pass; aggregations or
mixed-side reads must show ``CASE WHEN side`` or ``side =`` in the same
string. New ambiguous queries fail until the author commits to an
interpretation.

If you legitimately want P(YES) under the same-on-both convention (as the
calibration write at trade.py:~3612 does), pin it with an inline marker:

    # fv-mixed-side-ok: P(YES) under same-on-both storage
    conn.execute("SELECT AVG(fair_value_cents) FROM mm_orders WHERE ticker=?")

The marker has to be on a comment line within 3 lines preceding the call so a
future reviewer can see the choice was deliberate.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PRODUCTION_FILES: list[Path] = [
    REPO_ROOT / "trade.py",
    REPO_ROOT / "backtest_comprehensive.py",
    REPO_ROOT / "backtest_replay.py",
]
PRODUCTION_FILES += sorted((REPO_ROOT / "bot").rglob("*.py"))
PRODUCTION_FILES = [p for p in PRODUCTION_FILES if p.exists()]

# Match SQL strings that read ``fair_value_cents`` from ``mm_orders`` (in
# either order, e.g. SELECT fair_value_cents FROM mm_orders or SELECT ... FROM
# mm_orders WHERE fair_value_cents ...).
MM_ORDERS_FV_RE = re.compile(
    r"\bmm_orders\b.*?\bfair_value_cents\b|\bfair_value_cents\b.*?\bmm_orders\b",
    re.IGNORECASE | re.DOTALL,
)

# DDL — schema definition, not a read. Skip.
DDL_RE = re.compile(r"\bCREATE\s+(TABLE|INDEX|VIEW)\b", re.IGNORECASE)

# Aggregation functions on fair_value_cents — these collapse across rows and
# are the actual danger zone. Non-aggregate SELECTs that return ``side`` in
# the row let the caller disambiguate per-row, so they're safe by default.
AGG_FV_RE = re.compile(
    r"\b(AVG|SUM|MIN|MAX|TOTAL|GROUP_CONCAT)\s*\(\s*[\w.]*fair_value_cents\b",
    re.IGNORECASE,
)

# Acceptable normalisations / disambiguations within the SQL string itself.
SAFE_SQL_PATTERNS = [
    re.compile(r"CASE\s+WHEN\s+\w*\.?side\s*=\s*'(yes|no)'", re.IGNORECASE),
    re.compile(r"\bside\s*=\s*'(yes|no)'", re.IGNORECASE),
    re.compile(r"\bside\s+IN\s*\(", re.IGNORECASE),
]

# Inline-comment marker authors can use to acknowledge the storage convention.
ACK_MARKER_RE = re.compile(r"#\s*fv-mixed-side-ok\b")


def _has_safe_sql(sql: str) -> bool:
    return any(p.search(sql) for p in SAFE_SQL_PATTERNS)


def _has_ack_marker(src_lines: list[str], call_lineno: int) -> bool:
    # Look at the 3 source lines immediately preceding the call (and the call's own line).
    start = max(0, call_lineno - 4)
    end = min(len(src_lines), call_lineno)
    return any(ACK_MARKER_RE.search(line) for line in src_lines[start:end])


def _iter_fv_reads(tree: ast.AST, src_lines: list[str]):
    """Yield (lineno, sql_text, has_ack) for every Call that takes a literal
    SQL string with an AGGREGATION over mm_orders.fair_value_cents.

    Only aggregations are flagged — non-aggregate SELECTs that return ``side``
    in the row let the caller disambiguate per-row and are safe by default.
    DDL (CREATE TABLE/INDEX/VIEW) is skipped."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                sql = arg.value
                if not MM_ORDERS_FV_RE.search(sql):
                    continue
                if DDL_RE.search(sql):
                    continue
                if not AGG_FV_RE.search(sql):
                    continue
                yield node.lineno, sql, _has_ack_marker(src_lines, node.lineno)


@pytest.mark.parametrize("path", PRODUCTION_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_fair_value_cents_reads_are_explicit(path: Path) -> None:
    src = path.read_text()
    src_lines = src.splitlines()
    tree = ast.parse(src, filename=str(path))
    offenders: list[tuple[int, str]] = []
    for lineno, sql, acked in _iter_fv_reads(tree, src_lines):
        if _has_safe_sql(sql):
            continue
        if acked:
            continue
        offenders.append((lineno, sql.strip()[:200]))
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)}: SQL reading mm_orders.fair_value_cents "
        f"without single-side filter, CASE WHEN side normalisation, or "
        f"`# fv-mixed-side-ok` ack marker. mm_orders.fair_value_cents is stored "
        f"as P(YES) on both YES and NO rows — readers MUST declare their "
        f"interpretation (CLAUDE.md Known Bug Pattern #13). Offenders: {offenders}"
    )
