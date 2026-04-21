"""T0.4 — writer-owner registry.

Regression guard for "who writes this table?" Some daemon-era tables are
meant to have exactly one producer module — if a future change introduces
a second writer it's almost certainly a mistake (dual writes race under
the shared daemon connection, and readers can't reason about ordering).

Enforcement is a grep over ``INSERT INTO <table>`` patterns, filtered to
production code. Tests that set up their own fixture rows are exempt.

If you're intentionally adding a new writer, add your path to the
expected-owners set in this file along with a comment explaining why a
second writer is safe (e.g. both are under the same lock, or the table
is append-only with uncorrelated row domains).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Map table → set of modules allowed to INSERT into it.
# Keep the set minimal. New entries require human review in code-review.
EXPECTED_OWNERS: dict[str, set[str]] = {
    # Shadow MM writes — only the WeatherQuoter records what it would
    # have posted. Readers (mm_promotion, match_shadow_fills) are read-only.
    "weather_mm_shadow": {"bot/daemon/weather_quoter.py"},

    # Forecast snapshot table — only the weather-ensemble stitcher drops
    # snapshots. Anything else writing here corrupts the snapshot stream
    # used for calibration + backtests.
    "weather_forecast_snapshots": {"bot/signals/weather_ensemble.py"},

    # Four-factor decision log — single-owner by design. Other decision
    # paths log to `strategy_journal` instead.
    "decision_log": {"bot/scoring/four_factor.py"},

    # Promotion sweeps. Two writers exist — directional shadow promotion
    # and MM shadow promotion — and they write disjoint `kind` values.
    # Both are append-only, both run under the daily promotion-sweep
    # scheduler task. Audit if a third writer appears.
    "promotion_events": {
        "bot/learning/shadow_promotion.py",
        "bot/learning/mm_promotion.py",
    },

    # T3.1 canonical fills ledger — the entire purpose of the table is
    # one writer. Readers (kill-switch P&L, shadow-markout annotator,
    # settlement reconciler, bandit) derive from this table rather than
    # from the legacy three-writer mess. A second writer breaks the
    # invariant the ledger was built to enforce.
    "fills_ledger": {"bot/daemon/fills_writer.py"},
}


def _production_py_files() -> list[Path]:
    """Source files that count as 'production'. Excludes tests, backups,
    audit snapshots, utility scripts, and vendored / virtualenv code."""
    excluded_dirs = {
        ".venv", ".git", "__pycache__", ".pytest_cache", "node_modules",
        "tests", "reports",
        # .claude/worktrees holds duplicate repo copies for sub-agent
        # worktree isolation. Those files are not part of this repo's
        # production code — scanning them duplicates every match.
        ".claude",
    }
    excluded_files = {
        # Operator scripts with their own ephemeral DBs.
        "backtest.py", "diagnose_sources.py", "eval_bot.py",
        "audit_orders.py",
    }
    return [
        p for p in REPO_ROOT.rglob("*.py")
        if not any(part in excluded_dirs for part in p.parts)
        and p.name not in excluded_files
    ]


def _find_writers(table: str) -> set[str]:
    """Return the set of relative paths that contain ``INSERT INTO <table>``."""
    # Match the table name as a whole word so `mm_orders` doesn't hit
    # `mm_orders_archive` etc.
    pattern = re.compile(
        rf"INSERT\s+INTO\s+{re.escape(table)}\b", re.IGNORECASE
    )
    writers: set[str] = set()
    for path in _production_py_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if pattern.search(text):
            writers.add(path.relative_to(REPO_ROOT).as_posix())
    return writers


def test_writer_ownership_matches_registry():
    """Every tracked table has exactly the owners listed in EXPECTED_OWNERS.

    Failure means either:
    - a new writer was added (update the registry after code review), or
    - an expected writer was removed (also update the registry).
    """
    violations: list[str] = []
    for table, expected in EXPECTED_OWNERS.items():
        found = _find_writers(table)
        if found != expected:
            extra = found - expected
            missing = expected - found
            parts = [f"{table}: writers mismatch"]
            if extra:
                parts.append(f"  NEW writer(s) (audit needed): {sorted(extra)}")
            if missing:
                parts.append(f"  MISSING expected writer: {sorted(missing)}")
            violations.append("\n".join(parts))

    assert not violations, (
        "Writer-ownership registry drift detected. Either the audit table "
        "registry is stale or a new INSERT path needs review.\n\n" +
        "\n\n".join(violations)
    )


def test_registry_is_not_trivially_empty():
    """Sanity: registry + scanner aren't both broken at once (would make
    the assertion above a tautology)."""
    assert EXPECTED_OWNERS, "registry was emptied — test is a no-op"
    files = _production_py_files()
    assert len(files) > 20, f"only scanned {len(files)} files — path logic broken"
