"""Tests for the weather daemon orchestrator."""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from bot.daemon.orchestrator import WeatherDaemon, ForecastCache
from bot.daemon.metar_poller import TemperatureChange, StationReading, METARPoller
from bot.daemon.stations import STATIONS


class TestForecastCache(unittest.TestCase):
    """Tests for ForecastCache."""

    def test_get_returns_none_when_empty(self):
        cache = ForecastCache()
        self.assertIsNone(cache.get("KJFK"))

    def test_get_returns_value_after_set(self):
        cache = ForecastCache()
        cache._forecasts["KJFK"] = 75.0
        self.assertEqual(cache.get("KJFK"), 75.0)

    def test_refresh_if_stale_skips_when_fresh(self):
        cache = ForecastCache()
        cache._last_refresh = time.time()  # just refreshed
        with patch.object(cache, "refresh") as mock_refresh:
            cache.refresh_if_stale()
            mock_refresh.assert_not_called()

    def test_refresh_if_stale_refreshes_when_old(self):
        cache = ForecastCache()
        cache._last_refresh = time.time() - 99999  # very old
        with patch.object(cache, "refresh") as mock_refresh:
            cache.refresh_if_stale()
            mock_refresh.assert_called_once()

    def test_refresh_handles_api_error(self):
        """Refresh should not crash on API errors."""
        import requests as req_mod
        with patch.object(req_mod, "get", side_effect=ConnectionError("offline")):
            cache = ForecastCache()
            cache.refresh()  # should not raise
            self.assertEqual(len(cache._forecasts), 0)


class TestWeatherDaemon(unittest.TestCase):
    """Tests for WeatherDaemon initialization and configuration."""

    def test_init_defaults(self):
        daemon = WeatherDaemon(dry_run=True)
        self.assertEqual(daemon.poll_interval, 30)
        self.assertTrue(daemon.dry_run)
        self.assertFalse(daemon._running)
        self.assertIsInstance(daemon.poller, METARPoller)
        self.assertIsInstance(daemon.forecasts, ForecastCache)

    def test_init_custom_interval(self):
        daemon = WeatherDaemon(poll_interval=15, dry_run=True)
        self.assertEqual(daemon.poll_interval, 15)

    def test_stats_initialized(self):
        daemon = WeatherDaemon(dry_run=True)
        self.assertEqual(daemon.stats["polls"], 0)
        self.assertEqual(daemon.stats["changes_detected"], 0)
        self.assertEqual(daemon.stats["requotes_triggered"], 0)
        self.assertEqual(daemon.stats["errors"], 0)


class TestSmartGateWrapper(unittest.TestCase):
    """Tests for the daemon's smart gate wrapper."""

    def test_gate_wrapper_with_bracket(self):
        daemon = WeatherDaemon(dry_run=True)
        should_quote, reason, mult = daemon._smart_gate_fn(
            station="KJFK",
            bracket_floor=72.0,
            bracket_cap=74.0,
            running_high=70.0,
            forecast_high=75.0,
            hours_left=10.0,
            trajectory=0.5,
        )
        # Should be quoteable — normal conditions during active hours
        self.assertIsInstance(should_quote, bool)
        self.assertIsInstance(mult, float)

    def test_gate_wrapper_with_none_bracket(self):
        """Threshold markets pass None for bracket bounds."""
        daemon = WeatherDaemon(dry_run=True)
        # Should not crash with None bracket values
        should_quote, reason, mult = daemon._smart_gate_fn(
            station="KJFK",
            bracket_floor=None,
            bracket_cap=None,
            running_high=70.0,
            forecast_high=75.0,
            hours_left=10.0,
            trajectory=0.5,
        )
        self.assertIsInstance(should_quote, bool)


class TestHandleChanges(unittest.TestCase):
    """Tests for the daemon's change handling logic."""

    def _make_change(self, station="KJFK", series="KXHIGHNY",
                     old_temp=70.0, new_temp=72.0) -> TemperatureChange:
        reading = StationReading(
            station=station, temp_f=new_temp, temp_c=(new_temp - 32) * 5 / 9,
            obs_time="2026-04-16T12:00:00Z", poll_time=time.time(),
        )
        return TemperatureChange(
            station=station, city="nyc", series=series,
            old_temp_f=old_temp, new_temp_f=new_temp,
            running_high_f=new_temp, hours_left=8.0,
            trajectory_f_per_hr=1.5, reading=reading,
        )

    def test_handle_changes_calls_requote(self):
        daemon = WeatherDaemon(dry_run=True)
        mock_quoter = MagicMock()
        mock_quoter.requote_city.return_value = []
        daemon.quoter = mock_quoter
        daemon.forecasts._forecasts["KJFK"] = 76.0

        changes = [self._make_change()]
        daemon._handle_changes(changes)

        mock_quoter.requote_city.assert_called_once()
        call_kwargs = mock_quoter.requote_city.call_args
        self.assertEqual(call_kwargs.kwargs.get("series") or call_kwargs[1].get("series", call_kwargs[0][0] if call_kwargs[0] else None), "KXHIGHNY")

    def test_handle_changes_respects_cooldown(self):
        daemon = WeatherDaemon(dry_run=True)
        mock_quoter = MagicMock()
        mock_quoter.requote_city.return_value = []
        daemon.quoter = mock_quoter
        daemon.forecasts._forecasts["KJFK"] = 76.0

        # First call should go through
        changes = [self._make_change()]
        daemon._handle_changes(changes)
        self.assertEqual(mock_quoter.requote_city.call_count, 1)

        # Second call immediately should be cooldown-blocked
        daemon._handle_changes(changes)
        self.assertEqual(mock_quoter.requote_city.call_count, 1)  # still 1

    def test_handle_changes_uses_fallback_forecast(self):
        daemon = WeatherDaemon(dry_run=True)
        mock_quoter = MagicMock()
        mock_quoter.requote_city.return_value = []
        daemon.quoter = mock_quoter
        # No forecast cached — should use fallback

        changes = [self._make_change(new_temp=72.0)]
        daemon._handle_changes(changes)

        # Should still call requote (with fallback forecast = running_high + 5)
        mock_quoter.requote_city.assert_called_once()

    def test_handle_changes_increments_stats(self):
        daemon = WeatherDaemon(dry_run=True)
        mock_quoter = MagicMock()
        mock_quoter.requote_city.return_value = []
        daemon.quoter = mock_quoter
        daemon.forecasts._forecasts["KJFK"] = 76.0

        changes = [self._make_change()]
        daemon._handle_changes(changes)

        self.assertEqual(daemon.stats["requotes_triggered"], 1)

    def test_handle_changes_survives_requote_error(self):
        daemon = WeatherDaemon(dry_run=True)
        mock_quoter = MagicMock()
        mock_quoter.requote_city.side_effect = RuntimeError("API down")
        daemon.quoter = mock_quoter
        daemon.forecasts._forecasts["KJFK"] = 76.0

        changes = [self._make_change()]
        daemon._handle_changes(changes)  # should not raise

        self.assertEqual(daemon.stats["errors"], 1)
        self.assertEqual(daemon._consecutive_errors, 1)


class TestSignalHandling(unittest.TestCase):
    """Tests for graceful shutdown."""

    def test_signal_handler_sets_running_false(self):
        daemon = WeatherDaemon(dry_run=True)
        daemon._running = True
        daemon._handle_signal(15, None)  # SIGTERM = 15
        self.assertFalse(daemon._running)


class TestStationsConfig(unittest.TestCase):
    """Verify station configuration is complete."""

    def test_all_stations_have_required_fields(self):
        for station_id, cfg in STATIONS.items():
            self.assertIn("city", cfg, f"{station_id} missing 'city'")
            self.assertIn("series", cfg, f"{station_id} missing 'series'")
            self.assertIn("lst_offset", cfg, f"{station_id} missing 'lst_offset'")
            self.assertIn("lat", cfg, f"{station_id} missing 'lat'")
            self.assertIn("lon", cfg, f"{station_id} missing 'lon'")

    def test_station_count(self):
        self.assertGreaterEqual(len(STATIONS), 6)  # at least original 6

    def test_series_mapping_is_bijective(self):
        """Each station maps to a unique series."""
        series_set = {cfg["series"] for cfg in STATIONS.values()}
        self.assertEqual(len(series_set), len(STATIONS))


if __name__ == "__main__":
    unittest.main()
