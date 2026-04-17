"""Tests for the METAR → WeatherQuoter adapter (bot/daemon/weather_handler.py).

Covers:
- Shadow mode routes through shadow_requote_city (never requote_city).
- Live mode routes through requote_city (never shadow_requote_city).
- Forecast fallback: missing forecast uses running_high + 5°F.
- Per-series cooldown suppresses rapid re-fires.
- Empty / None change lists are no-ops.
- Stats increment correctly on dispatched, throttled, and errored calls.
- Handler passes the expected smart_gate callable through to the quoter.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.daemon.forecast_cache import ForecastCache
from bot.daemon.metar_poller import StationReading, TemperatureChange
from bot.daemon.weather_handler import (
    DEFAULT_COOLDOWN_S,
    FORECAST_FALLBACK_DELTA_F,
    WeatherChangeHandler,
    default_smart_gate,
)


def _make_change(
    *, series="KXHIGHNY", station="KJFK", new_temp=72.0, old_temp=70.0,
    running_high=72.0, hours_left=8.0, trajectory=1.0,
) -> TemperatureChange:
    reading = StationReading(
        station=station, temp_f=new_temp, temp_c=(new_temp - 32) * 5 / 9,
        obs_time="2026-04-16T18:00:00Z", poll_time=time.time(),
    )
    return TemperatureChange(
        station=station,
        city="nyc",
        series=series,
        old_temp_f=old_temp,
        new_temp_f=new_temp,
        running_high_f=running_high,
        hours_left=hours_left,
        trajectory_f_per_hr=trajectory,
        reading=reading,
    )


@pytest.fixture()
def quoter():
    q = MagicMock()
    q.shadow_requote_city.return_value = [MagicMock(), MagicMock()]
    q.requote_city.return_value = [MagicMock(skipped=False), MagicMock(skipped=True)]
    return q


@pytest.fixture()
def fcache():
    c = ForecastCache()
    c.set("KJFK", 78.0)
    return c


class TestShadowMode:
    def test_shadow_is_default(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=False)
        h([_make_change()])
        quoter.shadow_requote_city.assert_called_once()
        quoter.requote_city.assert_not_called()

    def test_shadow_passes_forecast_high(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=False)
        h([_make_change()])
        kwargs = quoter.shadow_requote_city.call_args.kwargs
        assert kwargs["forecast_high_f"] == 78.0
        assert kwargs["series"] == "KXHIGHNY"
        assert kwargs["station"] == "KJFK"
        assert kwargs["smart_gates"] is default_smart_gate
        assert kwargs["old_temp_f"] == 70.0
        assert kwargs["new_temp_f"] == 72.0

    def test_shadow_stats(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=False)
        h([_make_change()])
        assert h.stats["changes_seen"] == 1
        assert h.stats["requotes_dispatched"] == 1
        assert h.stats["markets_shadowed"] == 2
        assert h.stats["errors"] == 0


class TestLiveMode:
    def test_live_routes_to_requote_city(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=True)
        h([_make_change()])
        quoter.requote_city.assert_called_once()
        quoter.shadow_requote_city.assert_not_called()

    def test_live_does_not_pass_old_new_temps(self, quoter, fcache):
        """`requote_city` signature has no old_temp_f/new_temp_f — the shadow
        path is the only one that persists them (for markout analysis)."""
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=True)
        h([_make_change()])
        kwargs = quoter.requote_city.call_args.kwargs
        assert "old_temp_f" not in kwargs
        assert "new_temp_f" not in kwargs

    def test_live_counts_quoted_and_skipped(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=True)
        h([_make_change()])
        assert h.stats["markets_quoted"] == 1
        assert h.stats["markets_skipped"] == 1


class TestForecastFallback:
    def test_missing_forecast_uses_running_high_plus_delta(self, quoter):
        empty = ForecastCache()  # no forecast for any station
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=empty)
        change = _make_change(running_high=71.0)
        h([change])

        kwargs = quoter.shadow_requote_city.call_args.kwargs
        assert kwargs["forecast_high_f"] == pytest.approx(
            71.0 + FORECAST_FALLBACK_DELTA_F
        )


class TestCooldown:
    def test_throttles_rapid_second_change(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, cooldown_s=10.0)
        h([_make_change()])
        h([_make_change()])  # second call within cooldown window
        assert quoter.shadow_requote_city.call_count == 1
        assert h.stats["changes_throttled"] == 1
        assert h.stats["requotes_dispatched"] == 1

    def test_different_series_not_throttled(self, quoter, fcache):
        fcache.set("KMIA", 88.0)
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, cooldown_s=10.0)
        h([_make_change(series="KXHIGHNY", station="KJFK")])
        h([_make_change(series="KXHIGHMIA", station="KMIA")])
        assert quoter.shadow_requote_city.call_count == 2
        assert h.stats["changes_throttled"] == 0

    def test_cooldown_elapsed_allows_requote(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, cooldown_s=1.0)
        h([_make_change()])
        # Rewind the last-requote stamp to simulate elapsed cooldown
        h._last_requote["KXHIGHNY"] = time.time() - 5.0
        h([_make_change()])
        assert quoter.shadow_requote_city.call_count == 2


class TestEdgeCases:
    def test_none_changes_is_noop(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        h(None)
        quoter.shadow_requote_city.assert_not_called()
        quoter.requote_city.assert_not_called()

    def test_empty_changes_is_noop(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        h([])
        quoter.shadow_requote_city.assert_not_called()

    def test_quoter_exception_counted(self, fcache):
        quoter = MagicMock()
        quoter.shadow_requote_city.side_effect = RuntimeError("boom")
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        h([_make_change()])
        assert h.stats["errors"] == 1
        assert h.stats["requotes_dispatched"] == 0
        # After an error we should NOT stamp last_requote, so the next valid
        # event can retry immediately.
        assert "KXHIGHNY" not in h._last_requote


class TestDefaultCooldown:
    def test_default_cooldown_is_10s(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        assert h.cooldown_s == DEFAULT_COOLDOWN_S == 10.0


class TestDefaultSmartGate:
    def test_threshold_markets_get_sane_defaults(self):
        """Threshold markets pass None for floor/cap — the gate wrapper must
        expand those to 0/200°F so the proximity check is a no-op."""
        # Pick safe time-of-day + trajectory values that won't trip other gates.
        should_quote, reason, mult = default_smart_gate(
            station="KJFK",
            bracket_floor=None,
            bracket_cap=None,
            running_high=70.0,
            forecast_high=75.0,
            hours_left=10.0,  # ~2pm LST, well within 7am-7pm window
            trajectory_f_per_hr=0.5,
        )
        # Result must be callable-shaped; we don't assert True because other
        # gates may gate it down — the key property is no KeyError / TypeError.
        assert isinstance(should_quote, bool)
        assert isinstance(reason, str)
        assert isinstance(mult, float)
