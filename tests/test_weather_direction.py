"""Tests for weather source direction handling (above/below).

Verifies that Open-Meteo (get_weather_estimate) and Tomorrow.io
(get_tomorrow_weather_estimate) correctly handle HIGH temperature
"or below" markets by using forecast_high, not forecast_low.

The key invariant: for KXHIGH markets (which are about the daily HIGH
temperature), both "above" and "below" paths must use forecast_high
as the distribution center.  Using forecast_low for "below" markets
was a bug that massively overestimated P(YES) because the low temp
is far below the threshold for most high-temp markets.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from bot.signals.sources.weather import (
    _logistic_cdf,
    _parse_threshold,
    get_weather_estimate,
    get_tomorrow_weather_estimate,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_market(title: str, **kwargs) -> dict:
    """Build a minimal market_data dict for testing."""
    base = {"title": title}
    base.update(kwargs)
    return base


def _fake_forecast(high: float, low: float, date: str = "2026-04-16"):
    """Return a normalized forecast dict matching Open-Meteo / Tomorrow.io shape."""
    return {
        "daily": {
            "temperature_2m_max": [high],
            "temperature_2m_min": [low],
            "time": [date],
        }
    }


# ── _parse_threshold ─────────────────────────────────────────────────────────

class TestParseThreshold:
    def test_above_keyword(self):
        threshold, is_above = _parse_threshold(
            "KXHIGHNY-26APR16-T85", "will nyc high be at or above 85")
        assert threshold == 85
        assert is_above is True

    def test_below_keyword(self):
        threshold, is_above = _parse_threshold(
            "KXHIGHNY-26APR16-T72", "will nyc high be at or below 72")
        assert threshold == 72
        assert is_above is False

    def test_ticker_fallback_defaults_above(self):
        threshold, is_above = _parse_threshold(
            "KXHIGHNY-26APR16-T80", "nyc temperature weather high")
        assert threshold == 80
        assert is_above is True


# ── Direction consistency: Open-Meteo ────────────────────────────────────────

class TestOpenMeteoDirection:
    """Verify that the 'below' path uses forecast_high, not forecast_low."""

    @patch("bot.signals.sources.weather.get_weather_forecast")
    @patch("bot.signals.sources.weather._determine_day_index", return_value=0)
    def test_below_uses_forecast_high(self, mock_day, mock_forecast):
        """For 'high temp 80F or below' with forecast_high=85, P(YES) should be LOW."""
        mock_forecast.return_value = _fake_forecast(high=85.0, low=65.0)

        market = _make_market("will the high temperature in nyc be at or below 80")
        prob, src = get_weather_estimate("KXHIGHNY-26APR16-T80", market)

        assert prob is not None
        # P(high <= 80 | forecast_high=85, sigma=2) should be small
        # Using logistic CDF: 1/(1+exp(-(80-85)/2)) = 1/(1+exp(2.5)) ~ 0.076
        assert prob < 0.15, f"P(high<=80 | forecast_high=85) should be <15%, got {prob:.2%}"

    @patch("bot.signals.sources.weather.get_weather_forecast")
    @patch("bot.signals.sources.weather._determine_day_index", return_value=0)
    def test_below_high_threshold(self, mock_day, mock_forecast):
        """For 'high temp 95F or below' with forecast_high=85, P(YES) should be HIGH."""
        mock_forecast.return_value = _fake_forecast(high=85.0, low=65.0)

        market = _make_market("will the high temperature in nyc be at or below 95")
        prob, src = get_weather_estimate("KXHIGHNY-26APR16-T95", market)

        assert prob is not None
        # P(high <= 95 | forecast_high=85, sigma=2) should be very high
        assert prob > 0.90, f"P(high<=95 | forecast_high=85) should be >90%, got {prob:.2%}"

    @patch("bot.signals.sources.weather.get_weather_forecast")
    @patch("bot.signals.sources.weather._determine_day_index", return_value=0)
    def test_above_market(self, mock_day, mock_forecast):
        """For 'high temp above 80' with forecast_high=85, P(YES) should be HIGH."""
        mock_forecast.return_value = _fake_forecast(high=85.0, low=65.0)

        market = _make_market("will the high temperature in nyc be at or above 80")
        prob, src = get_weather_estimate("KXHIGHNY-26APR16-T80", market)

        assert prob is not None
        assert prob > 0.85, f"P(high>=80 | forecast_high=85) should be >85%, got {prob:.2%}"

    @patch("bot.signals.sources.weather.get_weather_forecast")
    @patch("bot.signals.sources.weather._determine_day_index", return_value=0)
    def test_above_and_below_are_complementary(self, mock_day, mock_forecast):
        """P(above T) + P(below T) should approximately equal 1.0."""
        mock_forecast.return_value = _fake_forecast(high=85.0, low=65.0)

        market_above = _make_market("will the high temperature in nyc be at or above 83")
        market_below = _make_market("will the high temperature in nyc be at or below 83")

        prob_above, _ = get_weather_estimate("KXHIGHNY-26APR16-T83", market_above)
        prob_below, _ = get_weather_estimate("KXHIGHNY-26APR16-T83", market_below)

        assert prob_above is not None
        assert prob_below is not None
        # They should sum to ~1.0 (not exactly due to clamping at 0.02/0.98)
        total = prob_above + prob_below
        assert 0.95 <= total <= 1.05, (
            f"P(above) + P(below) should be ~1.0, got {total:.3f} "
            f"(above={prob_above:.3f}, below={prob_below:.3f})"
        )


# ── Direction consistency: Tomorrow.io ───────────────────────────────────────

class TestTomorrowDirection:
    """Verify Tomorrow.io source has the same correct behavior."""

    @patch("bot.signals.sources.weather.get_tomorrow_forecast")
    @patch("bot.signals.sources.weather._determine_day_index", return_value=0)
    def test_below_uses_forecast_high(self, mock_day, mock_forecast):
        """For 'high temp 80F or below' with forecast_high=85, P(YES) should be LOW."""
        mock_forecast.return_value = _fake_forecast(high=85.0, low=65.0)

        market = _make_market("will the high temperature in nyc be at or below 80")
        prob, src = get_tomorrow_weather_estimate("KXHIGHNY-26APR16-T80", market)

        assert prob is not None
        assert prob < 0.15, f"P(high<=80 | forecast_high=85) should be <15%, got {prob:.2%}"

    @patch("bot.signals.sources.weather.get_tomorrow_forecast")
    @patch("bot.signals.sources.weather._determine_day_index", return_value=0)
    def test_above_and_below_are_complementary(self, mock_day, mock_forecast):
        """P(above T) + P(below T) should approximately equal 1.0."""
        mock_forecast.return_value = _fake_forecast(high=85.0, low=65.0)

        market_above = _make_market("will the high temperature in nyc be at or above 83")
        market_below = _make_market("will the high temperature in nyc be at or below 83")

        prob_above, _ = get_tomorrow_weather_estimate("KXHIGHNY-26APR16-T83", market_above)
        prob_below, _ = get_tomorrow_weather_estimate("KXHIGHNY-26APR16-T83", market_below)

        assert prob_above is not None
        assert prob_below is not None
        total = prob_above + prob_below
        assert 0.95 <= total <= 1.05, (
            f"P(above) + P(below) should be ~1.0, got {total:.3f} "
            f"(above={prob_above:.3f}, below={prob_below:.3f})"
        )


# ── Regression: the original bug would fail these ───────────────────────────

class TestRegressionForecastLowBug:
    """These tests would FAIL with the old code that used forecast_low for 'below' markets.

    With forecast_high=85, forecast_low=65, threshold=80, sigma=2:
      - OLD (buggy): diff = 80-65 = 15  -> P = 1/(1+exp(-15/2)) = 0.999 (WRONG)
      - NEW (fixed): diff = 80-85 = -5  -> P = 1/(1+exp(5/2))   = 0.076 (CORRECT)
    """

    def test_logistic_cdf_math(self):
        """Verify the mathematical correctness of our fix."""
        sigma = 2.0

        # P(high <= 80 | mu=85) should be small
        p_correct = _logistic_cdf(80, 85, sigma)
        assert p_correct < 0.10, f"Expected <10%, got {p_correct:.4f}"

        # P(high <= 80 | mu=65) would be huge (the old bug)
        p_buggy = _logistic_cdf(80, 65, sigma)
        assert p_buggy > 0.99, f"Buggy path would give {p_buggy:.4f} -- confirms bug existed"

    @patch("bot.signals.sources.weather.get_weather_forecast")
    @patch("bot.signals.sources.weather._determine_day_index", return_value=0)
    def test_open_meteo_not_using_forecast_low(self, mock_day, mock_forecast):
        """With forecast_high=90, forecast_low=60, 'below 70' should have LOW probability.

        Old buggy code would use forecast_low=60, giving P(X<=70|mu=60) ~ 0.99.
        Fixed code uses forecast_high=90, giving P(X<=70|mu=90) ~ 0.00004 -> clamped to 0.02.
        """
        mock_forecast.return_value = _fake_forecast(high=90.0, low=60.0)

        market = _make_market("will the high temperature in nyc be at or below 70")
        prob, _ = get_weather_estimate("KXHIGHNY-26APR16-T70", market)

        assert prob is not None
        # The forecast high is 90, so P(high <= 70) should be extremely low
        assert prob < 0.10, (
            f"P(high<=70 | forecast_high=90) must be <10%, got {prob:.2%}. "
            f"If this is >50%, the code is still using forecast_low!"
        )
