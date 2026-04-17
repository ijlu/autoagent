"""Tests for the fast weather requote engine (bot/daemon/weather_quoter.py).

Covers:
  - Fair value computation for bracket and threshold markets
  - _blended_mu helper (early day, late day, day over)
  - Price clamping, spread floor, inventory skew
  - _post_quotes with DRY_RUN (mocked api_post)
  - _cancel_stale_orders (mocked api_get, api_delete)
  - End-to-end requote_city with fully mocked API
"""
from __future__ import annotations

import math
import sqlite3
from unittest.mock import MagicMock, patch, call

import pytest

from bot.daemon.weather_quoter import (
    WeatherMarket,
    WeatherQuoter,
    RequoteResult,
    _blended_mu,
    _logistic_cdf,
    _sigma_for_hours,
    _fee_floor_half_spread,
    _safe_cents,
    _parse_threshold,
    clear_market_cache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear module-level caches between tests."""
    clear_market_cache()
    yield
    clear_market_cache()


@pytest.fixture()
def mock_conn():
    """In-memory SQLite connection with minimal mm_inventory table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mm_inventory (
            ticker TEXT PRIMARY KEY,
            net_position INTEGER DEFAULT 0,
            avg_entry_cents REAL DEFAULT 0,
            total_bought INTEGER DEFAULT 0,
            total_sold INTEGER DEFAULT 0,
            realized_pnl_cents INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ticker TEXT,
            action TEXT,
            data TEXT
        )
    """)
    conn.commit()
    return conn


def _make_bracket_market(
    ticker="KXHIGHNY-26APR16-B74",
    floor_val=74.0,
    cap_val=76.0,
    series="KXHIGHNY",
    yes_bid=30,
    yes_ask=35,
    volume=100,
) -> WeatherMarket:
    return WeatherMarket(
        ticker=ticker,
        title=f"NYC high {floor_val} to {cap_val}",
        series=series,
        bracket_floor=floor_val,
        bracket_cap=cap_val,
        threshold=None,
        is_bracket=True,
        is_above=True,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=volume,
        close_time="2026-04-17T04:00:00Z",
    )


def _make_threshold_market(
    ticker="KXHIGHNY-26APR16-T75",
    threshold=75.0,
    is_above=True,
    series="KXHIGHNY",
    yes_bid=40,
    yes_ask=45,
    volume=80,
) -> WeatherMarket:
    return WeatherMarket(
        ticker=ticker,
        title=f"NYC high 75 or above" if is_above else f"NYC high 75 or below",
        series=series,
        bracket_floor=None,
        bracket_cap=None,
        threshold=threshold,
        is_bracket=False,
        is_above=is_above,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=volume,
        close_time="2026-04-17T04:00:00Z",
    )


# ═══════════════════════════════════════════════════════════════════════════
# _blended_mu
# ═══════════════════════════════════════════════════════════════════════════

class TestBlendedMu:
    """Test the expected-eventual-high blending function."""

    def test_early_day_forecast_dominates(self):
        """With many hours left, forecast weight should be high."""
        # 20 hours left -> day_fraction_elapsed = 1 - 20/24 ~ 0.167
        # forecast_weight = max(0.1, 1 - 0.167) ~ 0.833
        mu = _blended_mu(running_high_f=60.0, forecast_high_f=80.0, hours_left=20.0)
        # forecast_weight * max(80,60) + obs_weight * 60
        # = 0.833 * 80 + 0.167 * 60 = 66.64 + 10.0 = 76.64
        assert mu > 75.0, f"Early day mu should be close to forecast, got {mu}"
        assert mu < 80.0, f"Early day mu should not exceed forecast, got {mu}"

    def test_late_day_observation_dominates(self):
        """With few hours left, observation weight should be high."""
        # 2 hours left -> day_fraction_elapsed = 1 - 2/24 ~ 0.917
        # forecast_weight = max(0.1, 1 - 0.917) = max(0.1, 0.083) = 0.1
        mu = _blended_mu(running_high_f=70.0, forecast_high_f=80.0, hours_left=2.0)
        # 0.1 * max(80,70) + 0.9 * 70 = 8.0 + 63.0 = 71.0
        assert 70.0 <= mu <= 72.0, f"Late day mu should be near running_high, got {mu}"

    def test_day_over_returns_running_high(self):
        """When hours_left <= 0, the running high IS the final high."""
        mu = _blended_mu(running_high_f=73.0, forecast_high_f=80.0, hours_left=0.0)
        assert mu == 73.0

    def test_running_high_exceeds_forecast(self):
        """When running_high > forecast, max(forecast, running_high) is used."""
        mu = _blended_mu(running_high_f=85.0, forecast_high_f=80.0, hours_left=12.0)
        # forecast_weight = max(0.1, 1 - 0.5) = 0.5
        # 0.5 * max(80, 85) + 0.5 * 85 = 0.5*85 + 0.5*85 = 85.0
        assert mu == 85.0

    def test_forecast_weight_floor(self):
        """Forecast weight never drops below 0.1 even very late in the day."""
        mu = _blended_mu(running_high_f=70.0, forecast_high_f=90.0, hours_left=0.5)
        # day_fraction_elapsed = 1 - 0.5/24 ~ 0.979
        # forecast_weight = max(0.1, 1 - 0.979) = 0.1
        # 0.1 * 90 + 0.9 * 70 = 9 + 63 = 72.0
        assert abs(mu - 72.0) < 0.5, f"Expected ~72.0, got {mu}"


# ═══════════════════════════════════════════════════════════════════════════
# _compute_fair_value — bracket markets
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeFairValueBracket:
    """Fair value for bracket markets: P(floor <= high < cap)."""

    def test_bracket_normal_below(self, mock_conn):
        """Running high is below bracket -- probability depends on forecast."""
        quoter = WeatherQuoter(mock_conn)
        # Use a bracket closer to the forecast high for meaningful probability
        market = _make_bracket_market(floor_val=75.0, cap_val=78.0)
        # running=70, forecast=78, hours_left=10 -> good chance of hitting 75-78
        fv = quoter._compute_fair_value(market, running_high_f=70.0, forecast_high_f=78.0, hours_left=10.0)
        assert 2 <= fv <= 98
        # The bracket includes the forecast high, so there should be non-trivial probability
        assert fv > 5, f"Expected non-trivial probability for near-forecast bracket, got {fv}"

    def test_bracket_blown_past(self, mock_conn):
        """Running high already exceeds bracket cap -- probability ~2%."""
        quoter = WeatherQuoter(mock_conn)
        market = _make_bracket_market(floor_val=70.0, cap_val=72.0)
        fv = quoter._compute_fair_value(market, running_high_f=75.0, forecast_high_f=78.0, hours_left=8.0)
        assert fv == 2, f"Blown bracket should be 2c, got {fv}"

    def test_bracket_inside(self, mock_conn):
        """Running high is inside bracket -- probability depends on staying inside."""
        quoter = WeatherQuoter(mock_conn)
        market = _make_bracket_market(floor_val=70.0, cap_val=75.0)
        # running=72, forecast=74, hours_left=4 -> currently inside, likely to stay
        fv = quoter._compute_fair_value(market, running_high_f=72.0, forecast_high_f=74.0, hours_left=4.0)
        assert fv > 20, f"Inside bracket with favorable forecast should have decent prob, got {fv}"
        assert fv <= 98

    def test_bracket_clamped_to_range(self, mock_conn):
        """Fair value is always within [2, 98]."""
        quoter = WeatherQuoter(mock_conn)
        # Very unlikely bracket
        market = _make_bracket_market(floor_val=120.0, cap_val=122.0)
        fv = quoter._compute_fair_value(market, running_high_f=70.0, forecast_high_f=75.0, hours_left=10.0)
        assert fv >= 2
        assert fv <= 98


# ═══════════════════════════════════════════════════════════════════════════
# _compute_fair_value — threshold markets
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeFairValueThreshold:
    """Fair value for threshold markets."""

    def test_above_already_exceeded(self, mock_conn):
        """Running high >= threshold (above market) -> near certainty."""
        quoter = WeatherQuoter(mock_conn)
        market = _make_threshold_market(threshold=70.0, is_above=True)
        fv = quoter._compute_fair_value(market, running_high_f=73.0, forecast_high_f=75.0, hours_left=8.0)
        # margin = 3.0 -> prob = 0.98 -> 98c
        assert fv == 98, f"Already exceeded by 3F should be 98c, got {fv}"

    def test_above_barely_exceeded(self, mock_conn):
        """Running high barely exceeds threshold -> 95c."""
        quoter = WeatherQuoter(mock_conn)
        market = _make_threshold_market(threshold=72.0, is_above=True)
        fv = quoter._compute_fair_value(market, running_high_f=72.5, forecast_high_f=75.0, hours_left=8.0)
        assert fv == 95, f"Barely exceeded should be 95c, got {fv}"

    def test_above_not_yet_reached(self, mock_conn):
        """Running high below threshold -- probability from CDF model."""
        quoter = WeatherQuoter(mock_conn)
        # Use threshold near blended mu for meaningful probability
        # running=72, forecast=78, hours_left=6 -> mu ~ 76, threshold=75
        market = _make_threshold_market(threshold=75.0, is_above=True)
        fv = quoter._compute_fair_value(market, running_high_f=72.0, forecast_high_f=78.0, hours_left=6.0)
        assert 2 <= fv <= 98
        # Threshold below blended mu should give decent probability
        assert fv > 10, f"Threshold below blended mu should give non-trivial prob, got {fv}"

    def test_below_market(self, mock_conn):
        """Below market: P(YES) = 1 - P(above)."""
        quoter = WeatherQuoter(mock_conn)
        market_above = _make_threshold_market(threshold=75.0, is_above=True)
        market_below = _make_threshold_market(threshold=75.0, is_above=False)
        fv_above = quoter._compute_fair_value(market_above, 70.0, 78.0, 10.0)
        fv_below = quoter._compute_fair_value(market_below, 70.0, 78.0, 10.0)
        # They should sum to approximately 100 (within rounding)
        assert abs(fv_above + fv_below - 100) <= 2, (
            f"above({fv_above}) + below({fv_below}) should sum to ~100"
        )

    def test_below_already_exceeded(self, mock_conn):
        """If running_high >= threshold and market is 'below', prob should be very low."""
        quoter = WeatherQuoter(mock_conn)
        market = _make_threshold_market(threshold=70.0, is_above=False)
        fv = quoter._compute_fair_value(market, running_high_f=75.0, forecast_high_f=78.0, hours_left=8.0)
        assert fv <= 5, f"Below market with threshold exceeded should be near 2c, got {fv}"


# ═══════════════════════════════════════════════════════════════════════════
# Price clamping, spread floor, inventory skew
# ═══════════════════════════════════════════════════════════════════════════

class TestPriceConstraints:
    """Verify bid/ask clamping, fee floor, and inventory skew."""

    def test_fee_floor_at_50c(self):
        """At 50c, maker fee per contract is meaningful -> floor should be > 0."""
        floor = _fee_floor_half_spread(50)
        assert floor >= 2, f"Fee floor at 50c should be >= 2, got {floor}"

    def test_fee_floor_at_extreme(self):
        """At extreme prices, fee is lower because P*(1-P) is small."""
        floor_low = _fee_floor_half_spread(5)
        floor_mid = _fee_floor_half_spread(50)
        assert floor_low <= floor_mid, "Fee floor should be lower at extreme prices"

    def test_fee_floor_minimum(self):
        """Fee floor is always >= 1."""
        assert _fee_floor_half_spread(2) >= 1
        assert _fee_floor_half_spread(98) >= 1

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    def test_inventory_skew_shifts_quotes(self, mock_conn):
        """Positive inventory should lower both bid and ask (encourage selling)."""
        quoter = WeatherQuoter(mock_conn)

        # Set up inventory = +20 (long YES)
        mock_conn.execute(
            "INSERT INTO mm_inventory (ticker, net_position) VALUES (?, ?)",
            ("TEST-T75", 20),
        )
        mock_conn.commit()

        # Skew = -(20/10)*2 = -4 -> bid and ask shift down by 4
        # With fair=50, hs=8: bid = 50 - 8 - 4 = 38, ask = 50 + 8 - 4 = 54
        n = quoter._post_quotes("TEST-T75", fair_value_cents=50, half_spread=8, inventory=20)
        assert n == 2  # both bid and ask posted (dry run)

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 15)
    def test_inventory_cap_blocks_bid(self, mock_conn):
        """At inventory cap, should not post more in same direction."""
        quoter = WeatherQuoter(mock_conn)
        # inventory=10, order_size=10 -> abs(10+10)=20 > 15 -> can't buy
        n = quoter._post_quotes("TEST-T75", fair_value_cents=50, half_spread=8, inventory=10)
        # Can still sell (abs(10-10)=0 <= 15) but can't buy
        assert n == 1  # only ask posted


# ═══════════════════════════════════════════════════════════════════════════
# _post_quotes with DRY_RUN
# ═══════════════════════════════════════════════════════════════════════════

class TestPostQuotesDryRun:
    """Verify quote posting in DRY_RUN mode (no actual API calls)."""

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.api_post")
    def test_dry_run_does_not_call_api(self, mock_api_post, mock_conn):
        """In DRY_RUN, api_post should never be called."""
        quoter = WeatherQuoter(mock_conn)
        n = quoter._post_quotes("KXHIGHNY-T75", fair_value_cents=50, half_spread=8, inventory=0)
        assert n == 2
        mock_api_post.assert_not_called()

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", False)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.api_post")
    def test_live_mode_calls_api(self, mock_api_post, mock_conn):
        """In live mode, api_post is called for each quote."""
        mock_api_post.return_value = {"order": {"order_id": "abc123"}}
        quoter = WeatherQuoter(mock_conn)
        n = quoter._post_quotes("KXHIGHNY-T75", fair_value_cents=50, half_spread=8, inventory=0)
        assert n == 2
        assert mock_api_post.call_count == 2

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", False)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.api_post")
    def test_client_order_id_no_periods(self, mock_api_post, mock_conn):
        """client_order_id must not contain periods (Kalshi rejects them)."""
        mock_api_post.return_value = {"order": {"order_id": "abc123"}}
        quoter = WeatherQuoter(mock_conn)
        quoter._post_quotes("KXHIGH.NY-T75", fair_value_cents=50, half_spread=8, inventory=0)
        for c in mock_api_post.call_args_list:
            body = c[0][1]  # positional arg: (path, body)
            cid = body["client_order_id"]
            assert "." not in cid, f"client_order_id contains period: {cid}"

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", False)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.api_post")
    def test_post_only_flag_set(self, mock_api_post, mock_conn):
        """All orders must have post_only=True."""
        mock_api_post.return_value = {"order": {"order_id": "abc123"}}
        quoter = WeatherQuoter(mock_conn)
        quoter._post_quotes("KXHIGHNY-T75", fair_value_cents=50, half_spread=8, inventory=0)
        for c in mock_api_post.call_args_list:
            body = c[0][1]
            assert body["post_only"] is True

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", False)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.api_post")
    def test_expiration_is_90_seconds(self, mock_api_post, mock_conn):
        """Expiration should be ~90 seconds from now (not 110)."""
        mock_api_post.return_value = {"order": {"order_id": "abc123"}}
        quoter = WeatherQuoter(mock_conn)
        import time
        before = int(time.time())
        quoter._post_quotes("KXHIGHNY-T75", fair_value_cents=50, half_spread=8, inventory=0)
        after = int(time.time())
        for c in mock_api_post.call_args_list:
            body = c[0][1]
            exp = body["expiration_ts"]
            # Should be between before+90 and after+90
            assert before + 89 <= exp <= after + 91, f"Expiration {exp} not ~90s from now"


# ═══════════════════════════════════════════════════════════════════════════
# _cancel_stale_orders
# ═══════════════════════════════════════════════════════════════════════════

class TestCancelStaleOrders:
    """Verify stale order cancellation logic."""

    @patch("bot.daemon.weather_quoter.api_delete")
    @patch("bot.daemon.weather_quoter.api_get")
    def test_cancels_all_resting_orders(self, mock_get, mock_delete, mock_conn):
        """Should cancel every resting order for the ticker."""
        mock_get.return_value = {
            "orders": [
                {"order_id": "order_1"},
                {"order_id": "order_2"},
                {"order_id": "order_3"},
            ]
        }
        quoter = WeatherQuoter(mock_conn)
        n = quoter._cancel_stale_orders("KXHIGHNY-T75")
        assert n == 3
        assert mock_delete.call_count == 3
        mock_delete.assert_any_call("/portfolio/orders/order_1")
        mock_delete.assert_any_call("/portfolio/orders/order_2")
        mock_delete.assert_any_call("/portfolio/orders/order_3")

    @patch("bot.daemon.weather_quoter.api_delete")
    @patch("bot.daemon.weather_quoter.api_get")
    def test_no_resting_orders(self, mock_get, mock_delete, mock_conn):
        """No orders to cancel -> returns 0."""
        mock_get.return_value = {"orders": []}
        quoter = WeatherQuoter(mock_conn)
        n = quoter._cancel_stale_orders("KXHIGHNY-T75")
        assert n == 0
        mock_delete.assert_not_called()

    @patch("bot.daemon.weather_quoter.api_delete")
    @patch("bot.daemon.weather_quoter.api_get")
    def test_api_get_failure_returns_zero(self, mock_get, mock_delete, mock_conn):
        """If fetching resting orders fails, return 0 (don't crash)."""
        mock_get.side_effect = Exception("network error")
        quoter = WeatherQuoter(mock_conn)
        n = quoter._cancel_stale_orders("KXHIGHNY-T75")
        assert n == 0
        mock_delete.assert_not_called()

    @patch("bot.daemon.weather_quoter.api_delete")
    @patch("bot.daemon.weather_quoter.api_get")
    def test_partial_cancel_failure(self, mock_get, mock_delete, mock_conn):
        """If one cancel fails, others should still proceed."""
        mock_get.return_value = {
            "orders": [
                {"order_id": "order_1"},
                {"order_id": "order_2"},
            ]
        }
        mock_delete.side_effect = [None, Exception("cancel failed")]
        quoter = WeatherQuoter(mock_conn)
        n = quoter._cancel_stale_orders("KXHIGHNY-T75")
        assert n == 1  # first succeeded, second failed
        assert mock_delete.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end requote_city
# ═══════════════════════════════════════════════════════════════════════════

class TestRequoteCity:
    """End-to-end test of the requote pipeline with mocked API."""

    def _mock_markets_response(self):
        """Return a realistic Kalshi API response for weather markets."""
        return {
            "markets": [
                {
                    "ticker": "KXHIGHNY-26APR16-B72",
                    "title": "NYC high 72 to 74",
                    "series_ticker": "KXHIGHNY",
                    "floor_strike": 72,
                    "cap_strike": 74,
                    "yes_bid": 25,
                    "yes_ask": 30,
                    "volume": 150,
                    "close_time": "2026-04-17T04:00:00Z",
                    "status": "open",
                },
                {
                    "ticker": "KXHIGHNY-26APR16-B74",
                    "title": "NYC high 74 to 76",
                    "series_ticker": "KXHIGHNY",
                    "floor_strike": 74,
                    "cap_strike": 76,
                    "yes_bid": 40,
                    "yes_ask": 45,
                    "volume": 200,
                    "close_time": "2026-04-17T04:00:00Z",
                    "status": "open",
                },
                {
                    "ticker": "KXHIGHNY-26APR16-B76",
                    "title": "NYC high 76 to 78",
                    "series_ticker": "KXHIGHNY",
                    "floor_strike": 76,
                    "cap_strike": 78,
                    "yes_bid": 20,
                    "yes_ask": 25,
                    "volume": 120,
                    "close_time": "2026-04-17T04:00:00Z",
                    "status": "open",
                },
            ]
        }

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_requote_processes_all_markets(self, mock_api_get, mock_conn):
        """Should process all 3 bracket markets in the series."""
        mock_api_get.return_value = self._mock_markets_response()
        quoter = WeatherQuoter(mock_conn)
        results = quoter.requote_city(
            series="KXHIGHNY",
            station="KNYC",
            running_high_f=73.0,
            forecast_high_f=76.0,
            hours_left=8.5,
            trajectory_f_per_hr=1.2,
        )
        assert len(results) == 3
        # All should have been processed (not skipped, unless extreme fv)
        tickers = [r.ticker for r in results]
        assert "KXHIGHNY-26APR16-B72" in tickers
        assert "KXHIGHNY-26APR16-B74" in tickers
        assert "KXHIGHNY-26APR16-B76" in tickers

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_requote_fair_values_are_reasonable(self, mock_api_get, mock_conn):
        """Fair values should be within [2, 98] and vary across brackets."""
        mock_api_get.return_value = self._mock_markets_response()
        quoter = WeatherQuoter(mock_conn)
        results = quoter.requote_city(
            series="KXHIGHNY",
            station="KNYC",
            running_high_f=73.0,
            forecast_high_f=76.0,
            hours_left=8.5,
        )
        fvs = {r.ticker: r.fair_value_cents for r in results if not r.skipped}
        for ticker, fv in fvs.items():
            assert 2 <= fv <= 98, f"{ticker} has out-of-range fair value {fv}"

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_smart_gate_can_skip_markets(self, mock_api_get, mock_conn):
        """Smart gate returning (False, reason, 1.0) should skip that market."""
        mock_api_get.return_value = self._mock_markets_response()

        # Gate that blocks all bracket markets with floor < 74
        def gate(station, floor, cap, running, forecast, hours, traj):
            if floor is not None and floor < 74:
                return False, "bracket too low", 1.0
            return True, "ok", 1.0

        quoter = WeatherQuoter(mock_conn)
        results = quoter.requote_city(
            series="KXHIGHNY",
            station="KNYC",
            running_high_f=73.0,
            forecast_high_f=76.0,
            hours_left=8.5,
            smart_gates=gate,
        )
        assert len(results) == 3
        skipped = [r for r in results if r.skipped]
        not_skipped = [r for r in results if not r.skipped]
        assert len(skipped) == 1  # B72 bracket skipped
        assert skipped[0].ticker == "KXHIGHNY-26APR16-B72"
        assert skipped[0].skip_reason == "bracket too low"
        # The other two should have been processed
        assert len(not_skipped) >= 1

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_requote_has_latency(self, mock_api_get, mock_conn):
        """Each result should report non-negative latency."""
        mock_api_get.return_value = self._mock_markets_response()
        quoter = WeatherQuoter(mock_conn)
        results = quoter.requote_city(
            series="KXHIGHNY",
            station="KNYC",
            running_high_f=73.0,
            forecast_high_f=76.0,
            hours_left=8.5,
        )
        for r in results:
            assert r.latency_ms >= 0

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_requote_empty_series(self, mock_api_get, mock_conn):
        """No open markets -> returns empty list."""
        mock_api_get.return_value = {"markets": []}
        quoter = WeatherQuoter(mock_conn)
        results = quoter.requote_city(
            series="KXHIGHNY",
            station="KNYC",
            running_high_f=73.0,
            forecast_high_f=76.0,
            hours_left=8.5,
        )
        assert results == []

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_requote_api_failure_returns_empty(self, mock_api_get, mock_conn):
        """API failure when fetching markets -> returns empty list, no crash."""
        mock_api_get.side_effect = Exception("API down")
        quoter = WeatherQuoter(mock_conn)
        results = quoter.requote_city(
            series="KXHIGHNY",
            station="KNYC",
            running_high_f=73.0,
            forecast_high_f=76.0,
            hours_left=8.5,
        )
        assert results == []

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_requote_with_cancel_and_post(self, mock_api_get, mock_conn):
        """Full cycle: fetch markets, cancel returns resting, then post new."""
        # First call: fetch markets. Subsequent calls: fetch resting orders per ticker.
        call_count = [0]

        def side_effect(path):
            call_count[0] += 1
            if "series_ticker" in path:
                return self._mock_markets_response()
            elif "status=resting" in path:
                return {"orders": [{"order_id": f"old_order_{call_count[0]}"}]}
            return {"orders": []}

        mock_api_get.side_effect = side_effect

        with patch("bot.daemon.weather_quoter.api_delete") as mock_delete:
            quoter = WeatherQuoter(mock_conn)
            results = quoter.requote_city(
                series="KXHIGHNY",
                station="KNYC",
                running_high_f=73.0,
                forecast_high_f=76.0,
                hours_left=8.5,
            )

        # Should have processed all 3 markets
        assert len(results) == 3
        # Each market should have cancelled 1 order
        for r in results:
            if not r.skipped:
                assert r.orders_cancelled == 1

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    @patch("bot.daemon.weather_quoter.api_get")
    def test_market_cache_prevents_refetch(self, mock_api_get, mock_conn):
        """Second call within 60s should use cached markets, not re-fetch."""
        mock_api_get.return_value = self._mock_markets_response()
        quoter = WeatherQuoter(mock_conn)
        r1 = quoter.requote_city("KXHIGHNY", "KNYC", 73.0, 76.0, 8.5)
        r2 = quoter.requote_city("KXHIGHNY", "KNYC", 74.0, 76.0, 8.0)
        # api_get for market fetch should have been called only once
        market_fetch_calls = [
            c for c in mock_api_get.call_args_list
            if "series_ticker" in str(c)
        ]
        assert len(market_fetch_calls) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestHelpers:
    """Test module-level helper functions."""

    def test_safe_cents_none(self):
        assert _safe_cents(None) == 0

    def test_safe_cents_dollar_float(self):
        assert _safe_cents(0.50) == 50
        assert _safe_cents(0.01) == 1
        assert _safe_cents(0.99) == 99

    def test_safe_cents_integer(self):
        assert _safe_cents(50) == 50
        # Note: 1 is in the (0, 1.0] range so treated as $1.00 = 100c
        assert _safe_cents(1) == 100
        assert _safe_cents(2) == 2  # > 1.0, treated as cents

    def test_safe_cents_string(self):
        assert _safe_cents("0.50") == 50
        assert _safe_cents("50") == 50

    def test_safe_cents_invalid(self):
        assert _safe_cents("abc") == 0

    def test_parse_threshold_above(self):
        t, above = _parse_threshold("KXHIGHNY-T75", "nyc high at or above 75")
        assert t == 75.0
        assert above is True

    def test_parse_threshold_below(self):
        t, above = _parse_threshold("KXHIGHNY-T72", "nyc high 72 or below")
        assert t == 72.0
        assert above is False

    def test_parse_threshold_ticker_fallback(self):
        t, above = _parse_threshold("KXHIGHNY-T80", "some title without temp")
        assert t == 80.0
        assert above is True

    def test_parse_threshold_no_match(self):
        t, above = _parse_threshold("RANDOM", "no temperature info here")
        assert t is None

    def test_sigma_decreases_over_day(self):
        """Sigma should monotonically decrease as hours_left decreases."""
        sigmas = [_sigma_for_hours(h) for h in [20, 12, 6, 2, 1, 0.5, 0]]
        for i in range(len(sigmas) - 1):
            assert sigmas[i] >= sigmas[i + 1], (
                f"sigma({20 - i}) = {sigmas[i]} < sigma({20 - i - 1}) = {sigmas[i + 1]}"
            )

    def test_logistic_cdf_midpoint(self):
        """At x == mu, logistic CDF should be 0.5."""
        assert abs(_logistic_cdf(50.0, 50.0, 1.0) - 0.5) < 0.001

    def test_logistic_cdf_far_above(self):
        """Far above mu -> CDF near 1.0."""
        assert _logistic_cdf(100.0, 50.0, 1.0) > 0.99

    def test_logistic_cdf_far_below(self):
        """Far below mu -> CDF near 0.0."""
        assert _logistic_cdf(0.0, 50.0, 1.0) < 0.01

    def test_logistic_cdf_overflow_protection(self):
        """Extreme inputs should not raise OverflowError."""
        assert _logistic_cdf(-1000.0, 0.0, 0.1) == 0.0
        assert _logistic_cdf(1000.0, 0.0, 0.1) == 1.0
