"""Tests for the 2026-04-22 avoid-filter strategy partitioning fix.

The audit finding: ``compute_avoid_filters`` bucketed win-rate by
``ticker[:6]`` prefix with no strategy context. MM losses on KXHIGH*
(weather) therefore caused every directional KXHIGH* candidate to be
vetoed — even though Phase 0 showed directional signal on those
families beating baseline by 4–8×.

The fix introduces ``avoided_by_strategy: dict[str, set[str]]`` so
``passes_filters`` gates each candidate against the rejection bucket
for *its own* strategy. ``avoided_prefixes`` is retained as a derived
union for backward compatibility with any un-migrated caller.

Two copies exist: ``bot/scoring/filters.py`` (canonical module) and
``trade.py`` (local copy that ``trade.main`` actually calls). Both are
exercised here so they don't silently drift.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot.db import init_db

# `trade.py` pulls in `cryptography` at import time, which fails on some
# local dev machines (arch mismatch in the installed wheel). The deploy
# battery (`deploy/04_redeploy.sh`) runs in a clean env where the import
# succeeds, so we skip the parity tests locally rather than leaving them
# red. Tests that only exercise the canonical module still run.
try:
    import trade as _trade  # noqa: F401
    _TRADE_IMPORT_OK = True
    _TRADE_SKIP_REASON = ""
except Exception as _e:  # pragma: no cover — env-specific
    _TRADE_IMPORT_OK = False
    _TRADE_SKIP_REASON = f"trade.py import failed in this env: {_e}"


# ---------------------------------------------------------------------------
# Fixture — seed settlements so the two avoidance rules disagree
# ---------------------------------------------------------------------------

def _seed_settlements(conn: sqlite3.Connection) -> None:
    """Seed 5 MM losses on KXHIGHNY + 5 directional wins on KXHIGHNY.

    The prefix is shared ("KXHIGH") across both strategies. Before the
    fix, the global ``avoided_prefixes`` loop would average 5 losses +
    5 wins = 50% win-rate and *not* flag it (depending on MIN_WIN_RATE).
    To force a regression-visible split we make the MM side 100% losses
    and the directional side 100% wins — the legacy code might still
    blend these, but the per-strategy view gives a crisp pass/fail.
    """
    rows = []
    # MM losses on KXHIGHNY — should flag KXHIGH prefix for MM only.
    for i in range(8):
        rows.append((
            f"mm-order-{i}", f"KXHIGHNY-26APR-B{i}", "yes", 50, 10,
            -500, -500, 0,       # revenue, profit, won=0 (loss)
            100.0, 5.0, "mm_weather",
        ))
    # Directional wins on KXHIGHNY — signal passes Phase 0.
    for i in range(8):
        rows.append((
            f"dir-order-{i}", f"KXHIGHNY-26APR-B{i}", "yes", 40, 5,
            500, 500, 1,         # won=1
            100.0, 5.0, "directional",
        ))
    conn.executemany(
        """INSERT INTO settlements
           (order_id, ticker, side, price_cents, contracts,
            revenue_cents, profit_cents, won,
            volume, spread_cents, strategy)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    # recorded_at defaults to NULL. The trade.py copy filters on
    # `recorded_at > datetime('now', '-30 days')`, so backfill with now.
    conn.execute(
        "UPDATE settlements SET recorded_at = datetime('now')"
    )
    conn.commit()


@pytest.fixture()
def conn():
    return init_db(":memory:")


# ---------------------------------------------------------------------------
# bot/scoring/filters.py — canonical copy
# ---------------------------------------------------------------------------

class TestCanonicalComputeAvoidFilters:
    def test_returns_avoided_by_strategy_key(self, conn):
        from bot.scoring.filters import compute_avoid_filters
        _seed_settlements(conn)
        out = compute_avoid_filters(conn)
        assert "avoided_by_strategy" in out
        assert isinstance(out["avoided_by_strategy"], dict)

    def test_mm_prefix_flagged_for_mm_only(self, conn):
        from bot.scoring.filters import compute_avoid_filters
        _seed_settlements(conn)
        out = compute_avoid_filters(conn)
        by_strat = out["avoided_by_strategy"]
        assert "KXHIGH" in by_strat.get("mm_weather", set())
        # Directional won 100% — must NOT be flagged.
        assert "KXHIGH" not in by_strat.get("directional", set())

    def test_avoided_prefixes_is_derived_union(self, conn):
        from bot.scoring.filters import compute_avoid_filters
        _seed_settlements(conn)
        out = compute_avoid_filters(conn)
        union = {
            pfx for pfxs in out["avoided_by_strategy"].values() for pfx in pfxs
        }
        assert out["avoided_prefixes"] == union

    def test_empty_settlements_returns_empty_partitions(self, conn):
        from bot.scoring.filters import compute_avoid_filters
        out = compute_avoid_filters(conn)
        assert out["avoided_by_strategy"] == {}
        assert out["avoided_prefixes"] == set()


# ---------------------------------------------------------------------------
# trade.py — duplicated local copy called by trade.main
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _TRADE_IMPORT_OK, reason=_TRADE_SKIP_REASON)
class TestTradePyComputeAvoidFilters:
    def test_parity_with_canonical_module(self, conn):
        """The two copies MUST produce structurally identical dicts.

        If they drift, ``trade.main`` will behave differently from the
        module-level helpers. Pin parity so the drift is caught in CI.
        """
        import trade
        from bot.scoring.filters import compute_avoid_filters as canonical
        _seed_settlements(conn)
        a = trade.compute_avoid_filters(conn)
        b = canonical(conn)
        assert a["avoided_by_strategy"] == b["avoided_by_strategy"]
        assert a["avoided_prefixes"] == b["avoided_prefixes"]
        assert a["avoided_strategies"] == b["avoided_strategies"]


# ---------------------------------------------------------------------------
# passes_filters — partitioned gate
# ---------------------------------------------------------------------------

class TestPassesFiltersPartitioned:
    def test_prefix_blocks_only_owning_strategy(self):
        from bot.scoring.market_scorer import passes_filters
        af = {
            "low_volume_threshold": None,
            "avoided_strategies": set(),
            "avoided_by_strategy": {"mm_weather": {"KXHIGH"}},
            "avoided_prefixes": {"KXHIGH"},  # derived union
        }
        # MM on a KXHIGH ticker → blocked
        ok, reason = passes_filters(
            "KXHIGHNY-26APR-B74", "mm_weather", 100, 5, af
        )
        assert ok is False
        assert "KXHIGH" in reason
        # Directional on the same ticker → passes (fix #1)
        ok2, reason2 = passes_filters(
            "KXHIGHNY-26APR-B74", "directional", 100, 5, af
        )
        assert ok2 is True
        assert reason2 == ""

    def test_falls_back_to_global_prefixes_when_partition_missing(self):
        """Back-compat: old persisted state may not have
        ``avoided_by_strategy``. Fall through to the legacy union."""
        from bot.scoring.market_scorer import passes_filters
        af = {
            "low_volume_threshold": None,
            "avoided_strategies": set(),
            "avoided_prefixes": {"KXHIGH"},
            # avoided_by_strategy intentionally absent
        }
        ok, reason = passes_filters(
            "KXHIGHNY-26APR-B74", "directional", 100, 5, af
        )
        assert ok is False
        assert "KXHIGH" in reason

    def test_volume_gate_still_fires(self):
        from bot.scoring.market_scorer import passes_filters
        af = {
            "low_volume_threshold": 500,
            "avoided_strategies": set(),
            "avoided_by_strategy": {},
            "avoided_prefixes": set(),
        }
        ok, reason = passes_filters(
            "KXANYTHING", "directional", 100, 5, af
        )
        assert ok is False
        assert "vol" in reason

    def test_strategy_gate_still_fires(self):
        from bot.scoring.market_scorer import passes_filters
        af = {
            "low_volume_threshold": None,
            "avoided_strategies": {"directional"},
            "avoided_by_strategy": {},
            "avoided_prefixes": set(),
        }
        ok, reason = passes_filters(
            "KXANYTHING", "directional", 5000, 5, af
        )
        assert ok is False
        assert "strat 'directional'" in reason

    @pytest.mark.skipif(not _TRADE_IMPORT_OK, reason=_TRADE_SKIP_REASON)
    def test_trade_py_copy_matches_module_copy(self):
        """Pin the in-file ``trade.passes_filters`` to the canonical
        behaviour so the two copies can't drift silently."""
        import trade
        from bot.scoring.market_scorer import passes_filters as mod_pf
        af = {
            "low_volume_threshold": None,
            "avoided_strategies": set(),
            "avoided_by_strategy": {"mm_weather": {"KXHIGH"}},
            "avoided_prefixes": {"KXHIGH"},
        }
        cases = [
            ("KXHIGHNY-A", "mm_weather", 100, 5),
            ("KXHIGHNY-A", "directional", 100, 5),
            ("KXFED-T2", "directional", 100, 5),
        ]
        for t, s, v, sp in cases:
            assert trade.passes_filters(t, s, v, sp, af) == mod_pf(t, s, v, sp, af)
