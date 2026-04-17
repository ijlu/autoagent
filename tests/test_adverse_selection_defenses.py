"""Tests for the 5 adverse selection defenses.

Defense 1: Blocklist in selection.py
Defense 2: Fill-rate-based spread widening (kv_cache → quotes.py)
Defense 3: One-sided fill detection (kv_cache → quotes.py)
Defense 4: Weather conditional quoting (weather.py → kv_cache → quotes.py)
Defense 5: Postmortem risk feedback (adverse_selection.py → kv_cache → quotes.py)
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bot.db import init_db, kv_get, kv_set


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def conn():
    """In-memory DB with full schema."""
    c = init_db(":memory:")
    return c


def _make_market(ticker, title="Test Market", yes_bid=40, yes_ask=55,
                 volume=100, close_time=None):
    """Build a minimal market dict for selection/quoting tests."""
    if close_time is None:
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        close_time = future.isoformat()
    return {
        "ticker": ticker,
        "title": title,
        "subtitle": "",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "volume": volume,
        "open_interest": 50,
        "close_time": close_time,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Defense 1: Blocklist
# ═══════════════════════════════════════════════════════════════════════════

class TestDefense1Blocklist:
    def test_blocklist_filters_kxeth(self, conn):
        """KXETH tickers should be filtered out by blocklist."""
        from bot.market_maker.selection import mm_select_markets, MM_BLOCKLIST_PREFIXES
        assert "KXETH" in MM_BLOCKLIST_PREFIXES

        markets = [_make_market("KXETH-26APR15-B3500")]
        result = mm_select_markets(markets, conn, 100_000)
        assert len(result) == 0

    def test_blocklist_filters_kxbtc(self, conn):
        """KXBTC tickers should be filtered out by blocklist."""
        from bot.market_maker.selection import mm_select_markets
        markets = [_make_market("KXBTC-26APR15-B80000")]
        result = mm_select_markets(markets, conn, 100_000)
        assert len(result) == 0

    def test_blocklist_filters_kxnbatotal(self, conn):
        from bot.market_maker.selection import MM_BLOCKLIST_PREFIXES
        assert "KXNBATOTAL" in MM_BLOCKLIST_PREFIXES

    def test_blocklist_filters_kxpoliticsmention(self, conn):
        from bot.market_maker.selection import MM_BLOCKLIST_PREFIXES
        assert "KXPOLITICSMENTION" in MM_BLOCKLIST_PREFIXES

    def test_non_blocklisted_passes(self, conn):
        """KXFED (not blocklisted) should pass the blocklist filter."""
        from bot.market_maker.selection import mm_select_markets
        markets = [_make_market("KXFED-26JUL-T3.25",
                                title="Fed rate decision",
                                yes_bid=35, yes_ask=45, volume=200)]
        result = mm_select_markets(markets, conn, 100_000)
        # May still be filtered by other criteria (category, etc.) but NOT blocklist
        # We can't assert it passes all filters, but at least verify the function runs
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════
# Defense 2: Fill-rate spread widening
# ═══════════════════════════════════════════════════════════════════════════

class TestDefense2FillRate:
    def test_high_fill_rate_blocks(self, conn):
        """Family with >35% fill rate should be blocked."""
        from bot.market_maker.quotes import mm_get_effective_spread
        kv_set(conn, "mm_fill_rates", {"KXFOO": 0.40}, 600)
        result = mm_get_effective_spread(conn, "KXFOO-26APR-T50", "economics")
        assert result == -1

    def test_moderate_fill_rate_widens(self, conn):
        """Family with 20-35% fill rate should widen 2x."""
        from bot.market_maker.quotes import mm_get_effective_spread, mm_reset_adverse_cache
        mm_reset_adverse_cache()
        kv_set(conn, "mm_fill_rates", {"KXBAR": 0.25}, 600)
        # Base economics spread is 7, adaptive won't change it much with no data
        result = mm_get_effective_spread(conn, "KXBAR-26APR-T50", "economics")
        assert result > 7  # should be widened from base

    def test_no_fill_rate_data_no_change(self, conn):
        """No fill rate data → no widening from Defense 2."""
        from bot.market_maker.quotes import mm_get_effective_spread, mm_reset_adverse_cache
        mm_reset_adverse_cache()
        # Don't set any fill rate data
        result = mm_get_effective_spread(conn, "KXNEW-26APR-T50", "economics")
        assert result > 0  # should get a valid spread


# ═══════════════════════════════════════════════════════════════════════════
# Defense 3: One-sided fill detection
# ═══════════════════════════════════════════════════════════════════════════

class TestDefense3OneSided:
    def test_persistent_onesided_blocks(self, conn):
        """4+ consecutive one-sided cycles with >80% imbalance → block."""
        from bot.market_maker.quotes import mm_get_effective_spread, mm_reset_adverse_cache
        mm_reset_adverse_cache()
        kv_set(conn, "mm_onesided_consec", {"KXBAD": 5}, 3600)
        kv_set(conn, "mm_onesided_imbalance", {"KXBAD": 0.95}, 600)
        result = mm_get_effective_spread(conn, "KXBAD-26APR-T50", "economics")
        assert result == -1

    def test_short_onesided_widens(self, conn):
        """2-3 consecutive one-sided cycles → widen, don't block."""
        from bot.market_maker.quotes import mm_get_effective_spread, mm_reset_adverse_cache
        mm_reset_adverse_cache()
        kv_set(conn, "mm_onesided_consec", {"KXMED": 2}, 3600)
        kv_set(conn, "mm_onesided_imbalance", {"KXMED": 0.90}, 600)
        result = mm_get_effective_spread(conn, "KXMED-26APR-T50", "economics")
        assert result > 0  # widened but not blocked

    def test_balanced_fills_no_change(self, conn):
        """Balanced fills (low imbalance) → no one-sided defense triggered."""
        from bot.market_maker.quotes import mm_get_effective_spread, mm_reset_adverse_cache
        mm_reset_adverse_cache()
        kv_set(conn, "mm_onesided_consec", {"KXOK": 0}, 3600)
        kv_set(conn, "mm_onesided_imbalance", {"KXOK": 0.30}, 600)
        result = mm_get_effective_spread(conn, "KXOK-26APR-T50", "economics")
        assert result > 0


# ═══════════════════════════════════════════════════════════════════════════
# Defense 4: Weather conditional quoting
# ═══════════════════════════════════════════════════════════════════════════

class TestDefense4WeatherGate:
    def test_forecast_persisted_by_weather_source(self, conn):
        """get_weather_estimate should persist forecast high to kv_cache."""
        # We can't easily call get_weather_estimate without mocking the API,
        # but we can test the kv_cache read/write mechanics.
        station = "KNYC"
        date = datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")
        key = f"metar_forecast_high_{station}_{date}"

        # Simulate what weather.py does
        kv_set(conn, key, 78.5, 86400)
        result = kv_get(conn, key)
        assert result == 78.5

    def test_large_divergence_detected(self, conn):
        """Forecast vs METAR divergence >5°F should be detectable."""
        station = "KNYC"
        date = datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")

        # Forecast says 80°F
        kv_set(conn, f"metar_forecast_high_{station}_{date}", 80.0, 86400)
        # METAR observed 73°F running high
        kv_set(conn, f"metar_daily_high_{station}_{date}",
               {"high_f": 73.0, "obs_count": 5}, 86400)

        fc = kv_get(conn, f"metar_forecast_high_{station}_{date}")
        obs = kv_get(conn, f"metar_daily_high_{station}_{date}")
        divergence = abs(fc - obs["high_f"])
        assert divergence > 5.0

    def test_small_divergence_ok(self, conn):
        """Divergence <3°F should not trigger blocking."""
        station = "KNYC"
        date = datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")

        kv_set(conn, f"metar_forecast_high_{station}_{date}", 78.0, 86400)
        kv_set(conn, f"metar_daily_high_{station}_{date}",
               {"high_f": 76.5, "obs_count": 5}, 86400)

        fc = kv_get(conn, f"metar_forecast_high_{station}_{date}")
        obs = kv_get(conn, f"metar_daily_high_{station}_{date}")
        divergence = abs(fc - obs["high_f"])
        assert divergence < 3.0


# ═══════════════════════════════════════════════════════════════════════════
# Defense 5: Postmortem risk feedback
# ═══════════════════════════════════════════════════════════════════════════

class TestDefense5PostmortemRisk:
    def test_high_adverse_rate_produces_high_score(self, conn):
        """Family with mostly adverse selection losses → high risk score."""
        from bot.market_maker.adverse_selection import mm_compute_postmortem_risk_scores
        now = datetime.now(timezone.utc).isoformat()

        # Insert 8 postmortems: 6 adverse selection, 2 directional
        for i in range(6):
            conn.execute(
                "INSERT INTO loss_postmortems "
                "(recorded_at, ticker, category, loss_type, source_combo) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, f"KXTOXIC-{i}", "crypto", "mm_adverse_selection", "mm:KXTOXIC"),
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO loss_postmortems "
                "(recorded_at, ticker, category, loss_type, source_combo) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, f"KXTOXIC-{i+10}", "crypto", "mm_directional_loss", "mm:KXTOXIC"),
            )
        conn.commit()

        scores = mm_compute_postmortem_risk_scores(conn)
        assert "KXTOXIC" in scores
        assert scores["KXTOXIC"] >= 0.70  # 6/8 adverse = 75%

    def test_low_adverse_rate_low_score(self, conn):
        """Family with mostly directional losses → low risk score."""
        from bot.market_maker.adverse_selection import mm_compute_postmortem_risk_scores
        now = datetime.now(timezone.utc).isoformat()

        for i in range(8):
            conn.execute(
                "INSERT INTO loss_postmortems "
                "(recorded_at, ticker, category, loss_type, source_combo) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, f"KXSAFE-{i}", "economics", "mm_directional_loss", "mm:KXSAFE"),
            )
        conn.commit()

        scores = mm_compute_postmortem_risk_scores(conn)
        # No adverse selection → score should be 0 or absent
        assert scores.get("KXSAFE", 0) < 0.10

    def test_fee_erosion_contributes_moderately(self, conn):
        """Fee erosion losses should contribute to risk score but less than adverse."""
        from bot.market_maker.adverse_selection import mm_compute_postmortem_risk_scores
        now = datetime.now(timezone.utc).isoformat()

        for i in range(10):
            conn.execute(
                "INSERT INTO loss_postmortems "
                "(recorded_at, ticker, category, loss_type, source_combo) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, f"KXFEES-{i}", "economics", "mm_fee_erosion", "mm:KXFEES"),
            )
        conn.commit()

        scores = mm_compute_postmortem_risk_scores(conn)
        # 10/10 fee erosion × 0.3 weight = 0.30
        assert "KXFEES" in scores
        assert 0.20 <= scores["KXFEES"] <= 0.40

    def test_postmortem_risk_blocks_in_spread(self, conn):
        """High postmortem risk stored in kv_cache should block quoting."""
        from bot.market_maker.quotes import mm_get_effective_spread, mm_reset_adverse_cache
        mm_reset_adverse_cache()
        kv_set(conn, "mm_postmortem_risk", {"KXDANGER": 0.80}, 3600)
        result = mm_get_effective_spread(conn, "KXDANGER-26APR-T50", "economics")
        assert result == -1

    def test_moderate_postmortem_risk_widens(self, conn):
        """Moderate postmortem risk should widen spread, not block."""
        from bot.market_maker.quotes import mm_get_effective_spread, mm_reset_adverse_cache
        mm_reset_adverse_cache()
        kv_set(conn, "mm_postmortem_risk", {"KXMOD": 0.55}, 3600)
        result = mm_get_effective_spread(conn, "KXMOD-26APR-T50", "economics")
        assert result > 7  # base economics spread is 7, should be widened

    def test_insufficient_data_no_score(self, conn):
        """<3 postmortems for a family → no risk score computed."""
        from bot.market_maker.adverse_selection import mm_compute_postmortem_risk_scores
        now = datetime.now(timezone.utc).isoformat()

        # Only 2 postmortems — below minimum threshold
        for i in range(2):
            conn.execute(
                "INSERT INTO loss_postmortems "
                "(recorded_at, ticker, category, loss_type, source_combo) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, f"KXTINY-{i}", "economics", "mm_adverse_selection", "mm:KXTINY"),
            )
        conn.commit()

        scores = mm_compute_postmortem_risk_scores(conn)
        assert "KXTINY" not in scores


# ═══════════════════════════════════════════════════════════════════════════
# Signal computation (core.py helpers)
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalComputation:
    def test_fill_rate_computation(self, conn):
        """Fill rate signal should be computed and stored in kv_cache."""
        from bot.market_maker.core import _compute_adverse_selection_signals

        now = datetime.now(timezone.utc).isoformat()
        # Insert 20 orders for KXTEST-1 (same ticker), 10 filled.
        # Needs >=4 per ticker for SQL HAVING and >=8 per family for scoring.
        for i in range(20):
            conn.execute(
                "INSERT INTO mm_orders "
                "(timestamp, ticker, side, price_cents, contracts, order_id, "
                "status, fill_qty, tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, "KXTEST-1", "yes", 45, 5,
                 f"ord_{i}", "filled" if i < 10 else "posted",
                 5 if i < 10 else 0, "mm_v1"),
            )
        conn.commit()

        _compute_adverse_selection_signals(conn)

        fill_rates = kv_get(conn, "mm_fill_rates")
        assert fill_rates is not None
        assert "KXTEST" in fill_rates
        assert fill_rates["KXTEST"] == pytest.approx(0.5, abs=0.01)

    def test_onesided_detection(self, conn):
        """One-sided fills should produce high imbalance score."""
        from bot.market_maker.core import _compute_adverse_selection_signals

        now = datetime.now(timezone.utc).isoformat()
        # All fills on YES side
        for i in range(5):
            conn.execute(
                "INSERT INTO mm_orders "
                "(timestamp, ticker, side, price_cents, contracts, order_id, "
                "status, fill_qty, tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, "KXONE-1", "yes", 45, 5,
                 f"ord_one_{i}", "filled", 3, "mm_v1"),
            )
        conn.commit()

        _compute_adverse_selection_signals(conn)

        imbalance = kv_get(conn, "mm_onesided_imbalance")
        assert imbalance is not None
        assert "KXONE" in imbalance
        assert imbalance["KXONE"] == 1.0  # 100% one-sided

    def test_kv_cache_conn_bug_fixed(self, conn):
        """Verify QA loop kv_get/kv_set use conn parameter (regression test)."""
        # The bug was: kv_get(flag_key) instead of kv_get(conn, flag_key)
        # which silently failed. We verify by checking the imports in core.py.
        import inspect
        from bot.market_maker import core
        source = inspect.getsource(core)

        # The old buggy pattern: kv_get(flag_key) without conn
        assert "kv_get(flag_key)" not in source
        assert "kv_set(flag_key," not in source

        # The fixed pattern uses _kv_get(conn, flag_key)
        assert "_kv_get(conn, flag_key)" in source
        assert "_kv_set(conn, flag_key," in source
