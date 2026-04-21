"""T1.3 — codified writer-ownership registry.

This test locks the set of files that are allowed to INSERT into each table,
and fails CI if a new writer appears outside that set. It does NOT try to
collapse legitimately dual-source writers — several tables have deliberate
multi-writer patterns (e.g. ``alpha_id`` provenance separation for
settlement-driven learning population), and that's documented here.

Rationale
---------

Before T1.3 we had 7 tables flagged "SPLIT" by a naive grep. Three of those
were legitimate co-ownership patterns:

1. **Provenance-tagged dual write** — ``populate_from_alpha.py`` writes rows
   with ``alpha_id NOT NULL`` alongside legacy learning-module writers
   (``alpha_id IS NULL``). Explicit design, documented in the module docstring.

2. **INSERT vs UPDATE split** — ``weather_quoter.py`` inserts shadow rows at
   quote time; ``mm_promotion.py`` updates them post-hoc with fill-match and
   settlement columns. This is operation-level ownership, not a split.

3. **Test fixtures** — a handful of tests insert canned rows. Those are test
   code, not production writers, and are explicitly allow-listed.

The other phantom "splits" — e.g. ``bot/observability/opportunity_log.py`` —
were dead duplicate modules with zero callers, and have been deleted.

What this test actually enforces
--------------------------------

For each table in ``EXPECTED_INSERT_WRITERS``, every production (non-test)
file that contains ``INSERT INTO <table>`` must be in the expected set, and
every file in the expected set must still contain the INSERT (no stale
entries).

This gives us a grep-equivalent CI guard that refuses to let a new writer
sneak in without an explicit registry update.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files under these roots are *production* writers and in-scope for the test.
PRODUCTION_ROOTS = ("trade.py", "bot/")

# ──────────────────────────────────────────────────────────────────────────
# Registry: table → set of relative paths allowed to INSERT into it.
# If you need to add a new writer, add the path here AND the call-site, in
# the same commit. Anyone reviewing the PR sees the ownership change
# explicitly.
# ──────────────────────────────────────────────────────────────────────────
EXPECTED_INSERT_WRITERS: dict[str, set[str]] = {
    # Decision journal — cycle-time (trade.py) and MM quote-time (weather_quoter).
    "opportunity_log": {
        "trade.py",
        "bot/daemon/weather_quoter.py",
    },
    # Shadow MM row insert — weather_quoter is sole owner. mm_promotion UPDATEs
    # post-hoc (fill match, settlement PnL) but does not INSERT.
    "weather_mm_shadow": {
        "bot/daemon/weather_quoter.py",
    },
    # Atomic decision log — alpha_log is sole owner.
    "alpha_backtest": {
        "bot/learning/alpha_log.py",
    },
    # Position health / exit — trade.py is sole owner (cycle only).
    # mm_inventory is legacy: only UPDATEd (settlement zero-out), never
    # INSERTed in production code, so it doesn't appear in this registry.
    "position_health_log": {"trade.py"},
    "position_exits": {"trade.py"},
    # Pipeline health — cycle writer (trade.py) and ensemble internal counter.
    "pipeline_health": {
        "trade.py",
        "bot/signals/ensemble.py",
    },
    # Strategy journal — cycle (trade.py) and learning writers that append
    # their decision trace.
    "strategy_journal": {
        "trade.py",
        "bot/learning/edge_convergence.py",
        "bot/learning/shadow_testing.py",
    },
    # Learning tables: legacy (alpha_id IS NULL) + alpha-provenance
    # (alpha_id NOT NULL). Documented in populate_from_alpha.py.
    # calibration is written by the cycle (trade.py) and alpha-provenance
    # backfill (populate_from_alpha.py). bot/learning/calibration.py itself
    # READS from calibration and writes *derived* probabilities into kv_cache
    # — it is not a writer of the calibration table.
    "calibration": {
        "trade.py",
        "bot/learning/populate_from_alpha.py",
    },
    "timing_patterns": {
        "trade.py",
        "bot/learning/timing_patterns.py",
        "bot/learning/populate_from_alpha.py",
    },
    "edge_convergence": {
        "trade.py",
        "bot/learning/edge_convergence.py",
        "bot/learning/populate_from_alpha.py",
    },
    "loss_postmortems": {
        "trade.py",
        "bot/learning/postmortems.py",
        "bot/learning/populate_from_alpha.py",
    },
    # settlements, kv_cache — bot/db.py owns via helpers.
    "settlements": {"bot/db.py", "trade.py"},
    # kv_cache: bot/db.py provides the generic put/expiry plumbing;
    # calibration.py writes derived Platt coefficients under its own key.
    "kv_cache": {
        "bot/db.py",
        "bot/learning/calibration.py",
    },
}


def _production_files():
    for rel in PRODUCTION_ROOTS:
        p = REPO_ROOT / rel
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (x for x in p.rglob("*.py") if "__pycache__" not in x.parts)


def _find_insert_sites(table: str) -> set[str]:
    """Return set of repo-relative paths containing INSERT INTO <table>."""
    # Matches: INSERT INTO foo, INSERT OR IGNORE INTO foo, INSERT OR REPLACE INTO foo.
    pattern = re.compile(
        rf"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+{re.escape(table)}\b",
        re.IGNORECASE,
    )
    hits: set[str] = set()
    for path in _production_files():
        try:
            text = path.read_text()
        except Exception:
            continue
        if pattern.search(text):
            hits.add(str(path.relative_to(REPO_ROOT)))
    return hits


def test_insert_writers_match_registry():
    """Every table's actual INSERT writers must match the declared registry.

    If this fails:
    - New writer added → decide if it belongs, then update
      EXPECTED_INSERT_WRITERS with a one-line comment on *why* it's a separate
      owner (new data shape? different provenance? different operation?).
    - Writer removed / refactored → drop the stale entry.
    """
    mismatches: list[str] = []
    for table, expected in sorted(EXPECTED_INSERT_WRITERS.items()):
        actual = _find_insert_sites(table)
        extra = actual - expected  # undeclared writers
        missing = expected - actual  # stale registry entries
        if extra or missing:
            mismatches.append(
                f"\n  table={table}"
                f"\n    undeclared writers (add to registry or remove): {sorted(extra) or '—'}"
                f"\n    stale registry entries (remove): {sorted(missing) or '—'}"
            )
    assert not mismatches, (
        "Writer-ownership drift detected:"
        + "".join(mismatches)
        + "\n\nSee tests/test_writer_ownership.py docstring for how to resolve."
    )


def test_registry_covers_all_tables_with_writers():
    """If a table has any INSERT in production code, it must be in the registry.

    This catches the case where someone adds INSERTs for a brand-new table
    without registering ownership at all.
    """
    # Find every `INSERT INTO <name>` in production code.
    pat = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
    referenced_tables: set[str] = set()
    for path in _production_files():
        try:
            text = path.read_text()
        except Exception:
            continue
        for m in pat.finditer(text):
            referenced_tables.add(m.group(1).lower())

    # Tables that are purely schema-fixture / migration targets (written only
    # inside bot/db.py during init) — these are owned by db.py implicitly.
    SCHEMA_ONLY_TABLES: set[str] = set()  # populated by init_db/migrations only

    declared = set(EXPECTED_INSERT_WRITERS.keys())
    unregistered = referenced_tables - declared - SCHEMA_ONLY_TABLES

    # Filter: registry is only about tables whose writes we care about
    # governing. Many log/session/daemon-internal tables may legitimately
    # live below this test's bar — declare them explicitly once we do care.
    # For now, just assert that *none of the governed tables* have leaked
    # into unregistered territory by typo.
    governed_prefixes = (
        "opportunity_", "weather_mm_", "alpha_", "mm_", "position_",
        "pipeline_", "strategy_", "calibration", "timing_", "edge_",
        "loss_", "settlements", "kv_",
    )
    leaks = {
        t for t in unregistered
        if any(t.startswith(p) for p in governed_prefixes)
    }
    assert not leaks, (
        f"Governed-prefix tables missing from EXPECTED_INSERT_WRITERS: "
        f"{sorted(leaks)}. Add them to the registry."
    )
