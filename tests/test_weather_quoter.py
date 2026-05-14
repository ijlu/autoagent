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
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest


def _today_close_time() -> str:
    """Return a close_time string that is 'today' in KJFK-LST (EST, UTC-5).

    Must anchor to LST, not UTC: the quoter compares close_time converted
    to LST against LST-today. Using UTC-today here fails at night UTC
    (00:00-05:00) when LST is still on the previous date — that's what
    caused the integration test to flake nightly.
    """
    lst_tz = timezone(timedelta(hours=-5))
    today_lst = datetime.now(lst_tz).date()
    noon_lst = datetime(
        today_lst.year, today_lst.month, today_lst.day,
        12, 0, 0, tzinfo=lst_tz,
    )
    return noon_lst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
    """In-memory SQLite connection initialised via the real ``init_db``.

    Previously this fixture hand-rolled a stale
    ``opportunity_log(timestamp, ticker, action, data)`` schema that
    matched the weather_quoter writer — which meant CI was happily
    validating against a contract that hadn't existed in production
    since the MM-deletion pivot. Writer was broken for the entire
    daemon era; test fixture was the reason no one caught it.

    Running against ``init_db`` guarantees the writer is tested against
    the exact same schema the production DB uses.
    """
    from bot.db import init_db

    return init_db(":memory:")


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
        close_time=_today_close_time(),
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
        close_time=_today_close_time(),
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
# A6: WEATHER_ENSEMBLE_V2 FV branch
# ═══════════════════════════════════════════════════════════════════════════

class TestEnsembleV2FairValueBranch:
    """`_compute_fair_value` delegates to `predict_v2` when flag is on and
    `market.raw` is populated; falls back to v1 on missing raw, flag off,
    v2 returning None, or v2 raising."""

    def test_flag_off_ignores_v2(self, mock_conn, monkeypatch):
        # Even with raw payload present, flag off → v1 path.
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", False)
        quoter = WeatherQuoter(mock_conn)
        market = _make_bracket_market(floor_val=70.0, cap_val=72.0)
        market.raw = {"ticker": market.ticker, "floor_strike": 70, "cap_strike": 72}
        called = {"n": 0}

        def fake_predict(ticker, market_data):
            called["n"] += 1
            return 0.77, "v2_fake"

        monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
        fv = quoter._compute_fair_value(
            market, running_high_f=70.0, forecast_high_f=72.0, hours_left=4.0,
        )
        assert called["n"] == 0, "v2 should not be called when flag is off"
        assert 2 <= fv <= 98

    def test_flag_on_empty_raw_falls_to_v1(self, mock_conn, monkeypatch):
        # Flag on, but legacy test fixture leaves raw={} → v1 path.
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        quoter = WeatherQuoter(mock_conn)
        market = _make_bracket_market(floor_val=70.0, cap_val=72.0)
        assert market.raw == {}
        called = {"n": 0}

        def fake_predict(ticker, market_data):
            called["n"] += 1
            return 0.5, "v2_fake"

        monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
        fv = quoter._compute_fair_value(
            market, running_high_f=70.0, forecast_high_f=72.0, hours_left=4.0,
        )
        assert called["n"] == 0, "v2 should not be called with empty raw"
        assert 2 <= fv <= 98

    def test_flag_on_uses_v2_prob_as_fv(self, mock_conn, monkeypatch):
        # Happy path: v2 returns prob, FV = round(prob * 100).
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        quoter = WeatherQuoter(mock_conn)
        market = _make_bracket_market(floor_val=70.0, cap_val=72.0)
        market.raw = {"ticker": market.ticker, "floor_strike": 70, "cap_strike": 72}

        def fake_predict(ticker, market_data):
            assert ticker == market.ticker
            assert market_data is market.raw
            return 0.734, "weather_ensemble_v2:fake"

        monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
        fv = quoter._compute_fair_value(
            market, running_high_f=70.0, forecast_high_f=72.0, hours_left=4.0,
        )
        assert fv == 73, f"expected 73c from prob 0.734, got {fv}"

    def test_flag_on_v2_returns_none_falls_to_v1(self, mock_conn, monkeypatch):
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        quoter = WeatherQuoter(mock_conn)
        market = _make_bracket_market(floor_val=70.0, cap_val=72.0)
        market.raw = {"ticker": market.ticker, "floor_strike": 70, "cap_strike": 72}

        def fake_predict(ticker, market_data):
            return None, None

        monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
        # Produce a matching v1 result by running with the flag off, then
        # compare.
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", False)
        fv_v1 = quoter._compute_fair_value(market, 70.0, 72.0, 4.0)
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        fv_with_fallback = quoter._compute_fair_value(market, 70.0, 72.0, 4.0)
        assert fv_with_fallback == fv_v1

    def test_flag_on_v2_raises_falls_to_v1(self, mock_conn, monkeypatch):
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        quoter = WeatherQuoter(mock_conn)
        market = _make_threshold_market(threshold=75.0, is_above=True)
        market.raw = {"ticker": market.ticker, "floor_strike": 75}

        def fake_predict(ticker, market_data):
            raise RuntimeError("simulated v2 failure")

        monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
        fv = quoter._compute_fair_value(market, 72.0, 76.0, 6.0)
        # v1 produces a non-extreme value for this setup; just confirm we
        # got a valid cents FV rather than an exception.
        assert 2 <= fv <= 98

    def test_live_requote_v2_none_fail_closes_before_orders(
        self, mock_conn, monkeypatch,
    ):
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        quoter = WeatherQuoter(mock_conn)
        market = _make_threshold_market(threshold=75.0, is_above=True)
        market.raw = {"ticker": market.ticker, "floor_strike": 75}

        def fake_predict(ticker, market_data):
            return None, None

        monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
        cancel = MagicMock(return_value=0)
        post = MagicMock(return_value=(2, "bid-oid", "ask-oid", 40, 60))
        monkeypatch.setattr(quoter, "_cancel_stale_orders", cancel)
        monkeypatch.setattr(quoter, "_post_quotes", post)

        result = quoter._requote_single(
            market=market,
            station="KJFK",
            running_high_f=72.0,
            forecast_high_f=76.0,
            hours_left=6.0,
            trajectory_f_per_hr=0.0,
            smart_gates=None,
        )

        assert result.skipped is True
        assert result.skip_reason == "v2_fair_value_unavailable"
        assert result.orders_posted == 0
        assert result.orders_cancelled == 0
        cancel.assert_not_called()
        post.assert_not_called()

    def test_live_requote_v2_exception_fail_closes_before_orders(
        self, mock_conn, monkeypatch,
    ):
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        quoter = WeatherQuoter(mock_conn)
        market = _make_threshold_market(threshold=75.0, is_above=True)
        market.raw = {"ticker": market.ticker, "floor_strike": 75}

        def fake_predict(ticker, market_data):
            raise RuntimeError("simulated v2 failure")

        monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
        cancel = MagicMock(return_value=0)
        post = MagicMock(return_value=(2, "bid-oid", "ask-oid", 40, 60))
        monkeypatch.setattr(quoter, "_cancel_stale_orders", cancel)
        monkeypatch.setattr(quoter, "_post_quotes", post)

        result = quoter._requote_single(
            market=market,
            station="KJFK",
            running_high_f=72.0,
            forecast_high_f=76.0,
            hours_left=6.0,
            trajectory_f_per_hr=0.0,
            smart_gates=None,
        )

        assert result.skipped is True
        assert result.skip_reason == "v2_fair_value_unavailable"
        assert result.orders_posted == 0
        assert result.orders_cancelled == 0
        cancel.assert_not_called()
        post.assert_not_called()

    def test_v2_prob_clamped_into_2_98_cents(self, mock_conn, monkeypatch):
        monkeypatch.setattr("bot.daemon.weather_quoter.WEATHER_ENSEMBLE_V2", True)
        quoter = WeatherQuoter(mock_conn)
        market = _make_threshold_market(threshold=80.0, is_above=True)
        market.raw = {"ticker": market.ticker, "floor_strike": 80}

        for prob, want in [(0.0001, 2), (0.01, 2), (0.9999, 98), (0.5, 50)]:
            def fake_predict(t, md, _p=prob):
                return _p, "v2"
            monkeypatch.setattr("bot.signals.weather_ensemble_v2.predict_v2", fake_predict)
            fv = quoter._compute_fair_value(market, 70.0, 82.0, 8.0)
            assert fv == want, f"prob={prob} → expected {want}c, got {fv}"


class TestParseMarketAttachesRaw:
    """`_parse_market` must store the raw API dict on the `WeatherMarket`
    so v2 can consume it without re-fetching."""

    def test_bracket_market_keeps_raw(self):
        raw = {
            "ticker": "KXHIGHNY-26APR16-B74",
            "title": "NYC high 74 to 76",
            "floor_strike": 74.0,
            "cap_strike": 76.0,
            "yes_bid_dollars": "0.30",
            "yes_ask_dollars": "0.35",
            "volume": 100,
            "close_time": _today_close_time(),
        }
        m = WeatherQuoter._parse_market(raw, "KXHIGHNY")
        assert m is not None
        assert m.raw is raw, "raw payload must be attached by reference"

    def test_threshold_market_keeps_raw(self):
        raw = {
            "ticker": "KXHIGHNY-26APR16-T75",
            "title": "NYC high > 75",
            "floor_strike": 75.0,
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.45",
            "volume": 80,
            "close_time": _today_close_time(),
        }
        m = WeatherQuoter._parse_market(raw, "KXHIGHNY")
        assert m is not None
        assert m.raw is raw


class TestEnsembleV2FlagWiring:
    """Drift-guard: the feature flag exists, defaults False, and is exported
    from bot.config."""

    def test_flag_exists_and_is_bool(self):
        from bot.config import WEATHER_ENSEMBLE_V2 as cfg_flag

        assert isinstance(cfg_flag, bool)

    def test_quoter_imports_live_flag(self):
        import bot.config
        import bot.daemon.weather_quoter as wq

        # Quoter reads the module-level binding — patching `bot.config`
        # alone won't flip it. This test pins that the same name is
        # imported into the quoter namespace so monkeypatch targets the
        # right symbol.
        assert hasattr(wq, "WEATHER_ENSEMBLE_V2")
        assert wq.WEATHER_ENSEMBLE_V2 == bot.config.WEATHER_ENSEMBLE_V2


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
        n, _, _, _, _ = quoter._post_quotes(
            "TEST-T75", fair_value_cents=50, half_spread=8, inventory=20,
        )
        assert n == 2  # both bid and ask posted (dry run)

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 15)
    def test_inventory_cap_blocks_bid(self, mock_conn):
        """At inventory cap, should not post more in same direction."""
        quoter = WeatherQuoter(mock_conn)
        # inventory=10, order_size=10 -> abs(10+10)=20 > 15 -> can't buy
        n, _, _, _, _ = quoter._post_quotes(
            "TEST-T75", fair_value_cents=50, half_spread=8, inventory=10,
            order_size=10, max_inventory=15,
        )
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
        n, _, _, _, _ = quoter._post_quotes("KXHIGHNY-T75", fair_value_cents=50, half_spread=8, inventory=0)
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
        n, _, _, _, _ = quoter._post_quotes("KXHIGHNY-T75", fair_value_cents=50, half_spread=8, inventory=0)
        assert n == 2
        assert mock_api_post.call_count == 2

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", False)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    @patch("bot.daemon.weather_quoter.api_post")
    def test_live_mode_records_posted_orders(self, mock_api_post, mock_conn):
        """Successful live weather quotes must be recoverable by fills_writer."""
        mock_api_post.side_effect = [
            {"order": {"order_id": "bid-oid"}},
            {"order": {"order_id": "ask-oid"}},
        ]
        quoter = WeatherQuoter(mock_conn)
        n, bid_oid, ask_oid, _, _ = quoter._post_quotes(
            "KXHIGHNY-T75", fair_value_cents=50, half_spread=8,
            inventory=0,
        )

        assert n == 2
        assert bid_oid == "bid-oid"
        assert ask_oid == "ask-oid"
        rows = mock_conn.execute(
            "SELECT order_id, client_order_id, ticker, side, action, count, "
            "price_cents, live_mode, source_hint FROM posted_orders "
            "ORDER BY order_id"
        ).fetchall()
        assert len(rows) == 2
        by_side = {row[3]: row for row in rows}
        assert by_side["yes"][0] == "bid-oid"
        assert by_side["yes"][1].startswith("mm_wx_KXHIGHNY-T75_")
        assert by_side["yes"][2:] == (
            "KXHIGHNY-T75", "yes", "buy", 10, 42, 1, "mm_quote",
        )
        assert by_side["no"][0] == "ask-oid"
        assert by_side["no"][1].startswith("mm_wx_KXHIGHNY-T75_")
        assert by_side["no"][2:] == (
            "KXHIGHNY-T75", "no", "buy", 10, 42, 1, "mm_quote",
        )

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
    def test_cancels_only_weather_mm_resting_orders(self, mock_get, mock_delete, mock_conn):
        """Should cancel only weather-MM-owned resting orders for the ticker."""
        mock_get.return_value = {
            "orders": [
                {"order_id": "weather_1", "client_order_id": "mm_wx_KXHIGHNY-T75_1"},
                {"order_id": "manual_1", "client_order_id": ""},
                {"order_id": "dir_1", "client_order_id": "mm_dir_KXHIGHNY-T75_1"},
                {"order_id": "unknown_1"},
            ]
        }
        quoter = WeatherQuoter(mock_conn)
        n = quoter._cancel_stale_orders("KXHIGHNY-T75")
        assert n == 1
        mock_delete.assert_called_once_with("/portfolio/orders/weather_1")

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
                {"order_id": "order_1", "client_order_id": "mm_wx_KXHIGHNY-T75_1"},
                {"order_id": "order_2", "client_order_id": "mm_wx_KXHIGHNY-T75_2"},
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
        ct = _today_close_time()
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
                    "close_time": ct,
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
                    "close_time": ct,
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
                    "close_time": ct,
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
                return {"orders": [{
                    "order_id": f"old_order_{call_count[0]}",
                    "client_order_id": f"mm_wx_KXHIGHNY_{call_count[0]}",
                }]}
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
        # None is "no observation" — must propagate as None so the matcher
        # can distinguish it from a real 0¢ (which Kalshi never quotes).
        assert _safe_cents(None) is None

    def test_safe_cents_zero_becomes_none(self):
        # Kalshi's minimum quoted price is 1¢; a reported 0 can only mean
        # "missing side" and must be normalized to None so the shadow row
        # column stays NULL rather than a fake-zero crossing price.
        assert _safe_cents(0) is None
        assert _safe_cents(0.0) is None
        assert _safe_cents("0") is None

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
        assert _safe_cents("abc") is None

    def test_parse_threshold_above(self):
        t, above = _parse_threshold("KXHIGHNY-T75", "nyc high at or above 75")
        assert t == 75.0
        assert above is True

    def test_parse_threshold_below(self):
        t, above = _parse_threshold("KXHIGHNY-T72", "nyc high 72 or below")
        assert t == 72.0
        assert above is False

    def test_parse_threshold_ticker_alone_is_ambiguous(self):
        """Ticker (`-T80`) carries the number but NOT the direction. Returning
        a threshold with a guessed direction was the 2026-04-22 sign-flip
        bug — it silently inverted every Kalshi `<`-title market. The
        correct behavior is to refuse to guess."""
        t, above = _parse_threshold("KXHIGHNY-T80", "some title without temp")
        assert t is None

    def test_parse_threshold_no_match(self):
        t, above = _parse_threshold("RANDOM", "no temperature info here")
        assert t is None

    def test_parse_threshold_lt_character(self):
        """Kalshi's literal `<75°` title must parse as below-threshold."""
        t, above = _parse_threshold("KXHIGHNY-T75", "will nyc high be <75°")
        assert t == 75.0
        assert above is False

    def test_parse_threshold_gt_character(self):
        """Kalshi's literal `>82°` title must parse as above-threshold."""
        t, above = _parse_threshold("KXHIGHNY-T82", "will nyc high be >82°")
        assert t == 82.0
        assert above is True

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


class TestParseMarketBook:
    """_parse_market must pick up Kalshi's `yes_bid_dollars` / `yes_ask_dollars`
    variant. The plain `yes_bid` keys are absent on the /markets list response;
    reading only those returned None on every call, and the old
    `_safe_cents(None) → 0` silently produced 20k all-zero shadow rows
    (2026-04-21 B+D incident). Regression guard for the field-name fallback.
    """

    @staticmethod
    def _base_mkt(ticker: str = "KXHIGHNY-26APR22-T65") -> dict:
        return {
            "ticker": ticker,
            "title": "Will NYC high be 65° or above?",
            "subtitle": "65° or above",
            "yes_sub_title": "65° or above",
        }

    def test_reads_yes_bid_dollars_when_plain_absent(self):
        m = self._base_mkt()
        m["yes_bid_dollars"] = "0.4500"
        m["yes_ask_dollars"] = "0.5500"
        wm = WeatherQuoter._parse_market(m, "KXHIGHNY")
        assert wm is not None
        assert wm.yes_bid == 45
        assert wm.yes_ask == 55

    def test_plain_keys_preferred_when_present(self):
        # When both present, the unsuffixed key wins (older API shape).
        m = self._base_mkt()
        m["yes_bid"] = 42
        m["yes_ask"] = 58
        m["yes_bid_dollars"] = "0.4500"
        m["yes_ask_dollars"] = "0.5500"
        wm = WeatherQuoter._parse_market(m, "KXHIGHNY")
        assert wm is not None
        assert wm.yes_bid == 42
        assert wm.yes_ask == 58

    def test_zero_dollar_string_becomes_none(self):
        # Kalshi reports "0.0000" when a side has no resting book. Must map
        # to None (not 0) so the matcher's zero-guard can ignore it.
        m = self._base_mkt()
        m["yes_bid_dollars"] = "0.0000"
        m["yes_ask_dollars"] = "0.4800"
        wm = WeatherQuoter._parse_market(m, "KXHIGHNY")
        assert wm is not None
        assert wm.yes_bid is None
        assert wm.yes_ask == 48

    def test_both_sides_missing_propagates_none(self):
        m = self._base_mkt()
        # No yes_bid, yes_ask, yes_bid_dollars, yes_ask_dollars keys at all.
        wm = WeatherQuoter._parse_market(m, "KXHIGHNY")
        assert wm is not None
        assert wm.yes_bid is None
        assert wm.yes_ask is None

    def test_logistic_cdf_overflow_protection(self):
        """Extreme inputs should not raise OverflowError."""
        assert _logistic_cdf(-1000.0, 0.0, 0.1) == 0.0
        assert _logistic_cdf(1000.0, 0.0, 0.1) == 1.0


class TestParseMarketThresholdDirection:
    """Direction (`is_above`) for threshold markets must come from the Kalshi
    API payload's `floor_strike`/`cap_strike` fields, NOT a title regex.

    The 2026-04-22 sign-flip bug: every Kalshi market with a literal "<"
    character title fell through the regex and took a hardcoded
    `is_above=True` ticker fallback, silently inverting FV for every
    below-threshold market (poisoned 27k shadow rows; produced the
    0.9-1.0 bucket where avg_est=0.967 but yes_rate=0.103).
    """

    @staticmethod
    def _base(ticker: str, title: str) -> dict:
        return {
            "ticker": ticker,
            "title": title,
            "yes_bid_dollars": "0.4500",
            "yes_ask_dollars": "0.5500",
        }

    def test_cap_strike_only_is_below_threshold(self):
        """cap_strike set, floor_strike None → `<N°` market (is_above=False)."""
        m = self._base("KXHIGHMIA-26APR21-T75", "Will Miami high be <75°?")
        m["cap_strike"] = 75
        m["floor_strike"] = None
        wm = WeatherQuoter._parse_market(m, "KXHIGHMIA")
        assert wm is not None
        assert wm.is_bracket is False
        assert wm.threshold == 75.0
        assert wm.is_above is False, (
            "cap_strike=75 is a `high < 75` market; is_above must be False"
        )

    def test_floor_strike_only_is_above_threshold(self):
        """floor_strike set, cap_strike None → `>N°` market (is_above=True)."""
        m = self._base("KXHIGHMIA-26APR21-T82", "Will Miami high be >82°?")
        m["floor_strike"] = 82
        m["cap_strike"] = None
        wm = WeatherQuoter._parse_market(m, "KXHIGHMIA")
        assert wm is not None
        assert wm.is_bracket is False
        assert wm.threshold == 82.0
        assert wm.is_above is True

    def test_api_strikes_override_title_regex(self):
        """Even if the title regex would match and disagree, API strikes win.
        This is the whole point of the fix — stop trusting the title."""
        # Adversarial: title says "above 75" (regex would say is_above=True)
        # but API says cap_strike=75 (actually below).
        m = self._base("KXHIGHMIA-26APR21-T75", "misleading: above 75")
        m["cap_strike"] = 75
        m["floor_strike"] = None
        wm = WeatherQuoter._parse_market(m, "KXHIGHMIA")
        assert wm is not None
        assert wm.is_above is False, "API strikes must override title regex"

    def test_falls_back_to_regex_when_strikes_missing(self):
        """Defensive fallback: if Kalshi ever drops the strike fields, the
        regex handles clear titles. `<75°` must parse as below."""
        m = self._base("KXHIGHMIA-26APR21-T75", "will miami high be <75°")
        # No floor_strike, no cap_strike keys at all.
        wm = WeatherQuoter._parse_market(m, "KXHIGHMIA")
        assert wm is not None
        assert wm.threshold == 75.0
        assert wm.is_above is False

    def test_returns_none_when_direction_unrecoverable(self):
        """When neither API strikes nor regex can determine direction, return
        None. Previously the ticker-regex fallback guessed `is_above=True`
        unconditionally — that's what caused the sign flip."""
        m = self._base("KXHIGHMIA-26APR21-T75", "indeterminate title")
        # No strikes, no direction keywords in title.
        wm = WeatherQuoter._parse_market(m, "KXHIGHMIA")
        assert wm is None


class TestFairValueSignForBelowThresholdMarkets:
    """End-to-end regression for the 2026-04-22 sign flip: running_high past
    a `<N°` threshold must yield FV near 0 (YES is near-impossible), not
    near 100. This is the exact pathology from the VPS calibration data."""

    @staticmethod
    def _below_market(threshold: float) -> WeatherMarket:
        from bot.daemon.weather_quoter import WeatherMarket
        return WeatherMarket(
            ticker=f"KXHIGHMIA-26APR21-T{int(threshold)}",
            title=f"Will miami high be <{int(threshold)}°?",
            series="KXHIGHMIA",
            bracket_floor=None, bracket_cap=None,
            threshold=threshold, is_bracket=False, is_above=False,
            yes_bid=None, yes_ask=None, volume=0, close_time="",
        )

    @staticmethod
    def _above_market(threshold: float) -> WeatherMarket:
        from bot.daemon.weather_quoter import WeatherMarket
        return WeatherMarket(
            ticker=f"KXHIGHMIA-26APR21-T{int(threshold)}",
            title=f"Will miami high be >{int(threshold)}°?",
            series="KXHIGHMIA",
            bracket_floor=None, bracket_cap=None,
            threshold=threshold, is_bracket=False, is_above=True,
            yes_bid=None, yes_ask=None, volume=0, close_time="",
        )

    def test_below_threshold_already_past_is_near_zero(self):
        """running_high=78 on `<75°` → YES impossible. FV must be ≤ 10¢.
        Pre-fix: returned ~95c (inverted)."""
        q = WeatherQuoter.__new__(WeatherQuoter)
        fv = q._compute_fair_value(
            self._below_market(75.0),
            running_high_f=78.0,
            forecast_high_f=79.0,
            hours_left=2.0,
        )
        assert fv <= 10, f"Expected near-zero FV for past-threshold `<` market, got {fv}c"

    def test_above_threshold_already_past_is_near_one(self):
        """running_high=78 on `>75°` → YES certain. FV must be ≥ 90¢."""
        q = WeatherQuoter.__new__(WeatherQuoter)
        fv = q._compute_fair_value(
            self._above_market(75.0),
            running_high_f=78.0,
            forecast_high_f=79.0,
            hours_left=2.0,
        )
        assert fv >= 90

    def test_below_and_above_on_same_threshold_sum_near_100(self):
        """A `<75°` and `>75°` market are (approximately) complements —
        their FVs should sum close to 100¢ for any single scenario.
        The sign-flip bug made them sum to ~190 or ~10."""
        q = WeatherQuoter.__new__(WeatherQuoter)
        # Below threshold, cold day
        fv_below = q._compute_fair_value(
            self._below_market(75.0), 70.0, 72.0, 4.0,
        )
        fv_above = q._compute_fair_value(
            self._above_market(75.0), 70.0, 72.0, 4.0,
        )
        # Can't be exactly 100 due to 2/98 clamps; allow tolerance.
        assert 90 <= fv_below + fv_above <= 110, (
            f"Complementary markets should sum near 100; got "
            f"{fv_below}+{fv_above}={fv_below + fv_above}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Shadow path — never calls api_post/api_delete, writes weather_mm_shadow
# ═══════════════════════════════════════════════════════════════════════════

from bot.db import init_db
from bot.daemon.weather_quoter import ShadowResult  # noqa: E402


@pytest.fixture()
def real_conn():
    """Full schema so weather_mm_shadow exists."""
    return init_db(":memory:")


class TestComputeQuotePricesStatic:
    """The extracted pricing math should match the live path byte-for-byte."""

    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    def test_inventory_zero_symmetric_around_fv(self):
        bid, ask, hs = WeatherQuoter.compute_quote_prices(50, 8, 0)
        assert bid == 50 - hs
        assert ask == 50 + hs
        assert hs >= 8

    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_SKEW_PER_10", 2)
    def test_positive_inventory_shifts_down(self):
        bid, ask, hs = WeatherQuoter.compute_quote_prices(50, 8, 20)
        # skew = 20/10 * 2 = 4 -> both shifted down by 4
        assert bid == 50 - hs - 4
        assert ask == 50 + hs - 4

    def test_fee_floor_raises_half_spread(self):
        """Passing half_spread=1 at 50c must be raised to the fee floor."""
        _, _, hs = WeatherQuoter.compute_quote_prices(50, 1, 0)
        assert hs >= 2  # per _fee_floor_half_spread at 50c

    def test_clamps_into_legal_range(self):
        bid, ask, _ = WeatherQuoter.compute_quote_prices(97, 5, 0)
        assert 1 <= bid <= 98
        assert bid + 1 <= ask <= 99


class TestShadowRequoteCity:
    """Shadow path writes rows and returns ShadowResult; never calls the API."""

    @patch("bot.daemon.weather_quoter.api_post")
    @patch("bot.daemon.weather_quoter.api_delete")
    @patch("bot.daemon.weather_quoter.WeatherQuoter._fetch_weather_markets")
    def test_writes_row_per_market(
        self, mock_fetch, mock_delete, mock_post, real_conn,
    ):
        mock_fetch.return_value = [
            _make_bracket_market(ticker="KXHIGHNY-A", floor_val=70, cap_val=75),
            _make_threshold_market(ticker="KXHIGHNY-T70", threshold=70, is_above=True),
        ]
        quoter = WeatherQuoter(real_conn)

        results = quoter.shadow_requote_city(
            series="KXHIGHNY", station="KJFK",
            running_high_f=69.0, forecast_high_f=74.0, hours_left=8.0,
            old_temp_f=67.0, new_temp_f=69.0,
        )
        assert len(results) == 2
        for r in results:
            assert isinstance(r, ShadowResult)
            assert r.shadow_row_id is not None

        mock_post.assert_not_called()
        mock_delete.assert_not_called()

        rows = real_conn.execute(
            "SELECT ticker, series, station, old_temp_f, new_temp_f, live_mode, "
            "fair_value_cents, proposed_bid_cents, proposed_ask_cents "
            "FROM weather_mm_shadow ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r[1] == "KXHIGHNY"
            assert r[2] == "KJFK"
            assert r[3] == 67.0
            assert r[4] == 69.0
            assert r[5] == 0  # live_mode = False
            assert 2 <= r[6] <= 98
            assert 1 <= r[7] < r[8] <= 99

    @patch("bot.daemon.weather_quoter.WeatherQuoter._fetch_weather_markets")
    def test_smart_gate_rejection_still_writes_row(self, mock_fetch, real_conn):
        """Even when the gate says don't quote, we persist the decision for
        the step-9 gate — 'counterfactual we didn't quote' is a data point."""
        mock_fetch.return_value = [
            _make_bracket_market(ticker="KXHIGHNY-A", floor_val=70, cap_val=75),
        ]
        quoter = WeatherQuoter(real_conn)

        def gate(*args, **kwargs):
            return False, "time-of-day", 1.0

        results = quoter.shadow_requote_city(
            series="KXHIGHNY", station="KJFK",
            running_high_f=69.0, forecast_high_f=74.0, hours_left=8.0,
            smart_gates=gate,
        )
        assert len(results) == 1
        assert results[0].gate_should_quote is False
        assert "time-of-day" in (results[0].gate_reason or "")

        row = real_conn.execute(
            "SELECT gate_should_quote, gate_reason FROM weather_mm_shadow"
        ).fetchone()
        assert row[0] == 0
        assert "time-of-day" in row[1]

    @patch("bot.daemon.weather_quoter.WeatherQuoter._fetch_weather_markets")
    def test_extreme_fair_value_is_flagged(self, mock_fetch, real_conn):
        """FV at the rails (<=2 or >=98) is logged but marked skipped."""
        # A bracket well past the running high -> FV ~2c
        mock_fetch.return_value = [
            _make_bracket_market(ticker="KXHIGHNY-HI", floor_val=120, cap_val=122),
        ]
        quoter = WeatherQuoter(real_conn)

        results = quoter.shadow_requote_city(
            series="KXHIGHNY", station="KJFK",
            running_high_f=70.0, forecast_high_f=75.0, hours_left=10.0,
        )
        assert len(results) == 1
        r = results[0]
        assert r.fair_value_cents <= 2 or r.fair_value_cents >= 98
        assert r.gate_should_quote is False
        assert "extreme_fv" in (r.gate_reason or "")

    @patch("bot.daemon.weather_quoter.WeatherQuoter._fetch_weather_markets")
    def test_no_markets_returns_empty(self, mock_fetch, real_conn):
        mock_fetch.return_value = []
        quoter = WeatherQuoter(real_conn)
        out = quoter.shadow_requote_city(
            series="KXHIGHNY", station="KJFK",
            running_high_f=70.0, forecast_high_f=75.0, hours_left=8.0,
        )
        assert out == []
        row = real_conn.execute(
            "SELECT COUNT(*) FROM weather_mm_shadow"
        ).fetchone()
        assert row[0] == 0

    @patch("bot.daemon.weather_quoter.WeatherQuoter._fetch_weather_markets")
    def test_market_mid_computed(self, mock_fetch, real_conn):
        """market_mid is the average of yes_bid and yes_ask when both set."""
        mock_fetch.return_value = [
            _make_bracket_market(
                ticker="KXHIGHNY-A", floor_val=70, cap_val=75,
                yes_bid=30, yes_ask=40,
            ),
        ]
        quoter = WeatherQuoter(real_conn)
        quoter.shadow_requote_city(
            series="KXHIGHNY", station="KJFK",
            running_high_f=72.0, forecast_high_f=74.0, hours_left=5.0,
        )
        row = real_conn.execute(
            "SELECT market_yes_bid, market_yes_ask, market_mid "
            "FROM weather_mm_shadow"
        ).fetchone()
        assert row[0] == 30
        assert row[1] == 40
        assert row[2] == 35


class TestQuoterLiveFlag:
    def test_default_is_shadow(self, mock_conn):
        q = WeatherQuoter(mock_conn)
        assert q.live is False

    def test_live_flag_sticks(self, mock_conn):
        q = WeatherQuoter(mock_conn, live=True)
        assert q.live is True


# ═══════════════════════════════════════════════════════════════════════════
# _log_requote — opportunity_log writer
# ═══════════════════════════════════════════════════════════════════════════

class TestLogRequote:
    """Integration tests for ``_log_requote`` against the canonical
    ``opportunity_log`` schema produced by ``init_db``.

    The 2026-04-22 audit finding: the writer targeted a stale
    ``(timestamp, ticker, action, data)`` shape that had been dropped
    during the MM-deletion pivot. Every call raised
    ``no such column: timestamp`` and was swallowed by a blanket
    ``except: pass`` — the event path's audit trail had been silently
    empty for the entire daemon era. These tests pin the writer to the
    current schema so the regression can't repeat.
    """

    def test_writes_row_with_canonical_fields(self, mock_conn):
        quoter = WeatherQuoter(mock_conn)
        result = RequoteResult(
            ticker="KXHIGHNY-26APR16-B74",
            fair_value_cents=42,
            orders_posted=2,
            orders_cancelled=1,
            skipped=False,
            skip_reason=None,
            latency_ms=123.4,
        )

        quoter._log_requote(result)

        rows = mock_conn.execute(
            "SELECT ticker, strategy, action, side, ensemble_prob, "
            "sources_json, skip_reason FROM opportunity_log"
        ).fetchall()
        assert len(rows) == 1
        ticker, strategy, action, side, ensemble_prob, sources_json, skip_reason = rows[0]
        assert ticker == "KXHIGHNY-26APR16-B74"
        assert strategy == "weather_mm"
        assert action == "wx_requote"
        assert side is None
        assert ensemble_prob == pytest.approx(0.42)
        assert skip_reason is None

        import json
        payload = json.loads(sources_json)
        assert payload["fair_value_cents"] == 42
        assert payload["posted"] == 2
        assert payload["cancelled"] == 1
        assert payload["latency_ms"] == 123.4
        assert payload["skipped"] is False
        assert payload["skip_reason"] is None

    def test_writes_row_with_skip_reason(self, mock_conn):
        """Skipped requote paths (e.g. smart-gate veto) still write an
        audit row so downstream queries can count vetoes."""
        quoter = WeatherQuoter(mock_conn)
        result = RequoteResult(
            ticker="KXHIGHMIA-26APR16-T80",
            fair_value_cents=55,
            orders_posted=0,
            orders_cancelled=0,
            skipped=True,
            skip_reason="smart_gate_trajectory_diverging",
            latency_ms=5.0,
        )

        quoter._log_requote(result)

        row = mock_conn.execute(
            "SELECT strategy, action, skip_reason, ensemble_prob "
            "FROM opportunity_log WHERE ticker = ?",
            ("KXHIGHMIA-26APR16-T80",),
        ).fetchone()
        assert row is not None
        assert row[0] == "weather_mm"
        assert row[1] == "wx_requote"
        assert row[2] == "smart_gate_trajectory_diverging"
        assert row[3] == pytest.approx(0.55)

    def test_does_not_raise_on_real_schema(self, mock_conn):
        """Regression: previously every call raised OperationalError.
        The writer must execute cleanly against init_db's schema."""
        quoter = WeatherQuoter(mock_conn)
        # Multiple calls must all succeed.
        for i in range(3):
            quoter._log_requote(RequoteResult(
                ticker=f"KXHIGHNY-26APR16-B{i}",
                fair_value_cents=50,
                orders_posted=1,
                orders_cancelled=0,
                skipped=False,
                skip_reason=None,
                latency_ms=10.0,
            ))
        count = mock_conn.execute(
            "SELECT COUNT(*) FROM opportunity_log WHERE strategy='weather_mm'"
        ).fetchone()[0]
        assert count == 3

    def test_non_integrity_error_propagates(self, mock_conn, monkeypatch):
        """Narrowed except clause: only IntegrityError is swallowed.
        A schema-drift OperationalError MUST propagate so the writer's
        silent-failure mode can never come back."""
        quoter = WeatherQuoter(mock_conn)
        # Drop the table to simulate schema drift.
        mock_conn.execute("DROP TABLE opportunity_log")

        with pytest.raises(sqlite3.OperationalError):
            quoter._log_requote(RequoteResult(
                ticker="KXHIGHNY-X",
                fair_value_cents=50,
                orders_posted=0,
                orders_cancelled=0,
                skipped=False,
                skip_reason=None,
                latency_ms=1.0,
            ))


# ═══════════════════════════════════════════════════════════════════════════
# _is_today_market
# ═══════════════════════════════════════════════════════════════════════════

class TestIsTodayMarket:
    """Tests for WeatherQuoter._is_today_market().

    The method parses close_time (UTC ISO string from Kalshi API) and
    converts to station LST to determine whether the market settles today.
    Today's running_high + forecast are meaningless for tomorrow's market.
    """

    def _market(self, close_time: str, series: str = "KXHIGHNY") -> WeatherMarket:
        return WeatherMarket(
            ticker="TEST-TICKER",
            title="Test",
            series=series,
            bracket_floor=None,
            bracket_cap=None,
            threshold=75.0,
            is_bracket=False,
            is_above=True,
            yes_bid=40,
            yes_ask=45,
            volume=100,
            close_time=close_time,
        )

    def test_today_market_passes(self):
        """A market whose close_time is today in LST should return True."""
        from datetime import datetime, timezone, timedelta
        # KXHIGHNY uses lst_offset=-5 (EST)
        lst_tz = timezone(timedelta(hours=-5))
        today_lst = datetime.now(lst_tz).date()
        # Build a close_time that is today at 11:59 PM LST = +5h UTC
        close_lst = datetime(today_lst.year, today_lst.month, today_lst.day,
                             23, 59, 0, tzinfo=lst_tz)
        close_utc = close_lst.astimezone(timezone.utc)
        close_time_str = close_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        market = self._market(close_time_str)
        assert WeatherQuoter._is_today_market(market, "KNYC") is True

    def test_next_day_market_filtered(self):
        """A market whose close_time is tomorrow in LST should return False."""
        from datetime import datetime, timezone, timedelta
        lst_tz = timezone(timedelta(hours=-5))
        today_lst = datetime.now(lst_tz).date()
        # Tomorrow at 6:59 AM LST (typical Kalshi APR+1 close in UTC)
        from datetime import date
        tomorrow_lst = date(today_lst.year, today_lst.month, today_lst.day)
        from datetime import timedelta as td
        tomorrow_dt = datetime(today_lst.year, today_lst.month, today_lst.day,
                               tzinfo=lst_tz) + td(days=1)
        close_time_str = tomorrow_dt.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        market = self._market(close_time_str)
        assert WeatherQuoter._is_today_market(market, "KNYC") is False

    def test_missing_close_time_passes(self):
        """Markets with empty close_time string should not be blocked."""
        market = self._market(close_time="")
        assert WeatherQuoter._is_today_market(market, "KNYC") is True

    def test_none_close_time_passes(self):
        """close_time=None should not be blocked (no date info available)."""
        market = WeatherMarket(
            ticker="TEST",
            title="Test",
            series="KXHIGHNY",
            bracket_floor=None,
            bracket_cap=None,
            threshold=75.0,
            is_bracket=False,
            is_above=True,
            yes_bid=40,
            yes_ask=45,
            volume=100,
            close_time=None,
        )
        assert WeatherQuoter._is_today_market(market, "KNYC") is True

    def test_malformed_close_time_passes(self):
        """Unparseable close_time should not block the market (fail open)."""
        market = self._market(close_time="not-a-date")
        assert WeatherQuoter._is_today_market(market, "KNYC") is True

    def test_kalshi_apr24_format_is_filtered(self):
        """Regression: KXHIGHDEN-26APR24 close_time=2026-04-25T06:59:00Z
        should be filtered (it's APR24 Denver MST = UTC-7 = Apr 24, not Apr 23).
        This was the exact format from the Kalshi API that caused wrong FVs."""
        # Hard-code the observed format from the backtest analysis.
        # April 25 UTC 06:59 = April 24 MST (UTC-7) — so it's *tomorrow* relative
        # to April 23 MST.
        from datetime import datetime, timezone, timedelta
        lst_tz = timezone(timedelta(hours=-7))  # Denver MST
        today_lst = datetime.now(lst_tz).date()
        # Create a close_time that is one day after today in Denver LST
        from datetime import timedelta as td
        tomorrow_close_utc = datetime(
            today_lst.year, today_lst.month, today_lst.day,
            tzinfo=lst_tz
        ) + td(days=1, hours=6, minutes=59)
        close_time_str = tomorrow_close_utc.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        market = self._market(close_time_str, series="KXHIGHDEN")
        # KDEN = Denver, lst_offset=-7
        assert WeatherQuoter._is_today_market(market, "KDEN") is False

    def test_unknown_station_uses_fallback_offset(self):
        """Unknown station ID falls back to lst_offset=-5. Should not raise."""
        from datetime import datetime, timezone, timedelta
        lst_tz = timezone(timedelta(hours=-5))
        today_lst = datetime.now(lst_tz).date()
        # Market closing today at noon LST
        close_lst = datetime(today_lst.year, today_lst.month, today_lst.day,
                             12, 0, 0, tzinfo=lst_tz)
        close_time_str = close_lst.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        market = self._market(close_time_str)
        # "KUNKNOWN" is not in STATIONS — should fall back to -5 and not raise
        result = WeatherQuoter._is_today_market(market, "KUNKNOWN")
        assert isinstance(result, bool)


# ═══════════════════════════════════════════════════════════════════════════
# market_price_bounds gate
# ═══════════════════════════════════════════════════════════════════════════

class TestMarketPriceBounds:
    """Tests for the market_price_bounds gate in _requote_single / _shadow_requote_single.

    Mirrors score_market's price_bounds filter: skip (or flag gate=0) when the
    market itself considers the outcome near-certain (yes_ask ≤ 8 or yes_bid ≥ 92),
    regardless of what our model says.  The canonical failure mode this prevents:
    early-morning T-suffix 'below' markets at 1-2¢ where our blended_mu is dragged
    down by the overnight low, producing FV=70¢ and instant adverse fills.
    """

    def _market_near_certain_ask(
        self, yes_ask: int = 2, yes_bid: int = 1,
    ) -> WeatherMarket:
        """Market pricing YES as near-worthless (near-certain NO)."""
        return WeatherMarket(
            ticker="KXHIGHCHI-26APR23-T78",
            title="CHI high below 78",
            series="KXHIGHCHI",
            bracket_floor=None,
            bracket_cap=None,
            threshold=78.0,
            is_bracket=False,
            is_above=False,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            volume=50,
            close_time=_today_close_time(),
        )

    def _market_near_certain_bid(
        self, yes_bid: int = 95, yes_ask: int = 97,
    ) -> WeatherMarket:
        """Market pricing YES as near-certain YES."""
        return WeatherMarket(
            ticker="KXHIGHCHI-26APR23-T60",
            title="CHI high above 60",
            series="KXHIGHCHI",
            bracket_floor=None,
            bracket_cap=None,
            threshold=60.0,
            is_bracket=False,
            is_above=True,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            volume=50,
            close_time=_today_close_time(),
        )

    def _normal_market(self) -> WeatherMarket:
        return WeatherMarket(
            ticker="KXHIGHCHI-26APR23-T75",
            title="CHI high above 75",
            series="KXHIGHCHI",
            bracket_floor=None,
            bracket_cap=None,
            threshold=75.0,
            is_bracket=False,
            is_above=True,
            yes_bid=35,
            yes_ask=40,
            volume=80,
            close_time=_today_close_time(),
        )

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    def test_live_path_skips_near_certain_ask(self, mock_conn):
        """Live _requote_single skips with 'market_price_bounds' when yes_ask ≤ 8."""
        quoter = WeatherQuoter(mock_conn)
        result = quoter._requote_single(
            market=self._market_near_certain_ask(yes_ask=2),
            station="KMDW",
            running_high_f=62.0,
            forecast_high_f=82.0,
            hours_left=17.0,
            trajectory_f_per_hr=0.0,
            smart_gates=None,
        )
        assert result.skipped is True
        assert result.skip_reason == "market_price_bounds"
        assert result.orders_posted == 0

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    def test_live_path_skips_near_certain_bid(self, mock_conn):
        """Live _requote_single skips with 'market_price_bounds' when yes_bid ≥ 92."""
        quoter = WeatherQuoter(mock_conn)
        result = quoter._requote_single(
            market=self._market_near_certain_bid(yes_bid=95),
            station="KMDW",
            running_high_f=62.0,
            forecast_high_f=82.0,
            hours_left=17.0,
            trajectory_f_per_hr=0.0,
            smart_gates=None,
        )
        assert result.skipped is True
        assert result.skip_reason == "market_price_bounds"

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    def test_live_path_boundary_8_cents_skips(self, mock_conn):
        """Boundary: yes_ask == 8 is still skipped (≤ 8 inclusive)."""
        quoter = WeatherQuoter(mock_conn)
        result = quoter._requote_single(
            market=self._market_near_certain_ask(yes_ask=8, yes_bid=6),
            station="KMDW",
            running_high_f=62.0,
            forecast_high_f=82.0,
            hours_left=17.0,
            trajectory_f_per_hr=0.0,
            smart_gates=None,
        )
        assert result.skipped is True
        assert result.skip_reason == "market_price_bounds"

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    def test_live_path_9_cents_is_not_filtered(self, mock_conn):
        """Boundary: yes_ask == 9 is NOT filtered (> 8)."""
        quoter = WeatherQuoter(mock_conn)
        with patch("bot.daemon.weather_quoter.api_get", return_value={"orders": []}):
            result = quoter._requote_single(
                market=self._market_near_certain_ask(yes_ask=9, yes_bid=7),
                station="KMDW",
                running_high_f=62.0,
                forecast_high_f=82.0,
                hours_left=17.0,
                trajectory_f_per_hr=0.0,
                smart_gates=None,
            )
        # Should NOT be skipped due to price_bounds (may be skipped for extreme_fv etc)
        assert result.skip_reason != "market_price_bounds"

    def test_shadow_path_marks_gate_zero_not_early_return(self, mock_conn):
        """Shadow _shadow_requote_single marks gate=0, but still writes and returns."""
        quoter = WeatherQuoter(mock_conn)
        result = quoter._shadow_requote_single(
            market=self._market_near_certain_ask(yes_ask=2),
            station="KMDW",
            running_high_f=62.0,
            forecast_high_f=82.0,
            hours_left=17.0,
            trajectory_f_per_hr=0.0,
            smart_gates=None,
            old_temp_f=60.0,
            new_temp_f=62.0,
        )
        # Shadow path: gate=0, reason contains the tag, row is still written
        assert result.gate_should_quote is False
        assert "market_price_bounds" in (result.gate_reason or "")
        # Shadow row was written (row_id present)
        assert result.shadow_row_id is not None
        # Confirm in DB
        row = mock_conn.execute(
            "SELECT gate_should_quote, gate_reason FROM weather_mm_shadow WHERE id=?",
            (result.shadow_row_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == 0
        assert "market_price_bounds" in row[1]

    @patch("bot.daemon.weather_quoter.MM_DRY_RUN", True)
    @patch("bot.daemon.weather_quoter.MM_ORDER_SIZE", 10)
    @patch("bot.daemon.weather_quoter.MM_MAX_INVENTORY", 50)
    @patch("bot.daemon.weather_quoter.MM_HALF_SPREAD", 4)
    def test_normal_market_not_filtered(self, mock_conn):
        """A market with yes_bid=35, yes_ask=40 passes the gate."""
        quoter = WeatherQuoter(mock_conn)
        with patch("bot.daemon.weather_quoter.api_get", return_value={"orders": []}):
            result = quoter._requote_single(
                market=self._normal_market(),
                station="KMDW",
                running_high_f=72.0,
                forecast_high_f=78.0,
                hours_left=8.0,
                trajectory_f_per_hr=0.5,
                smart_gates=None,
            )
        assert result.skip_reason != "market_price_bounds"

    def test_none_bid_ask_not_filtered(self, mock_conn):
        """Market with yes_bid=None, yes_ask=None should not trip the gate."""
        market = WeatherMarket(
            ticker="KXHIGHCHI-26APR23-T75",
            title="CHI high above 75",
            series="KXHIGHCHI",
            bracket_floor=None,
            bracket_cap=None,
            threshold=75.0,
            is_bracket=False,
            is_above=True,
            yes_bid=None,
            yes_ask=None,
            volume=80,
            close_time=_today_close_time(),
        )
        quoter = WeatherQuoter(mock_conn)
        result = quoter._shadow_requote_single(
            market=market,
            station="KMDW",
            running_high_f=72.0,
            forecast_high_f=78.0,
            hours_left=8.0,
            trajectory_f_per_hr=0.5,
            smart_gates=None,
            old_temp_f=70.0,
            new_temp_f=72.0,
        )
        assert "market_price_bounds" not in (result.gate_reason or "")
