"""Tests for the shared Open-Meteo forecast cache.

Covers:
- Empty cache returns None.
- `set()` populates the map and updates `last_refresh`.
- `refresh()` parses Open-Meteo JSON for every primary station.
- `refresh_if_stale()` skips when fresh and runs when stale.
- Transient per-station failure preserves prior values (merge, not replace).
- HTTP non-200 and request exceptions are swallowed as warnings.
- `snapshot()` returns a defensive copy.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from bot.daemon.forecast_cache import ForecastCache, FORECAST_REFRESH_INTERVAL_S
from bot.daemon.stations import STATIONS


def _ok_response(temp_f: float) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"daily": {"temperature_2m_max": [temp_f]}}
    return resp


class TestBasics:
    def test_empty_cache_returns_none(self):
        cache = ForecastCache()
        assert cache.get("KJFK") is None
        assert cache.last_refresh() == 0.0

    def test_set_populates(self):
        cache = ForecastCache()
        cache.set("KJFK", 78.5)
        assert cache.get("KJFK") == 78.5
        assert cache.last_refresh() > 0

    def test_snapshot_is_copy(self):
        cache = ForecastCache()
        cache.set("KJFK", 78.5)
        snap = cache.snapshot()
        snap["KJFK"] = 0.0
        # Mutating the snapshot does not affect the cache
        assert cache.get("KJFK") == 78.5


class TestRefresh:
    @patch("bot.daemon.forecast_cache.requests.get")
    def test_refresh_populates_all_stations(self, mock_get):
        """Every primary station gets a single HTTP call and a cached value."""
        mock_get.return_value = _ok_response(75.0)
        cache = ForecastCache()

        n = cache.refresh()

        assert n == len(STATIONS)
        assert mock_get.call_count == len(STATIONS)
        for sid in STATIONS:
            assert cache.get(sid) == 75.0

    @patch("bot.daemon.forecast_cache.requests.get")
    def test_transient_failure_preserves_prior(self, mock_get):
        """A single failing station must not wipe prior values for others."""
        first_resp = _ok_response(80.0)
        # Second refresh: KJFK fails, rest succeed
        def side_effect(url, **kwargs):
            # Identify station by lat — KJFK's lat=40.64.
            if "latitude=40.64" in url:
                raise requests.ConnectionError("boom")
            return _ok_response(82.0)

        cache = ForecastCache()
        mock_get.return_value = first_resp
        cache.refresh()
        assert cache.get("KJFK") == 80.0

        mock_get.side_effect = side_effect
        mock_get.return_value = None  # side_effect wins
        cache.refresh()

        # KJFK preserved from first refresh (merge, not replace)
        assert cache.get("KJFK") == 80.0
        # Other stations got the fresh value
        for sid in STATIONS:
            if sid != "KJFK":
                assert cache.get(sid) == 82.0

    @patch("bot.daemon.forecast_cache.requests.get")
    def test_non_200_is_swallowed(self, mock_get):
        bad = MagicMock()
        bad.status_code = 503
        mock_get.return_value = bad

        cache = ForecastCache()
        n = cache.refresh()
        assert n == 0
        assert cache.snapshot() == {}
        # last_refresh stamp still moves so refresh_if_stale doesn't spin
        assert cache.last_refresh() > 0

    @patch("bot.daemon.forecast_cache.requests.get")
    def test_empty_temps_skipped(self, mock_get):
        bad = MagicMock()
        bad.status_code = 200
        bad.json.return_value = {"daily": {"temperature_2m_max": []}}
        mock_get.return_value = bad
        cache = ForecastCache()
        n = cache.refresh()
        assert n == 0
        assert cache.snapshot() == {}


class TestRefreshIfStale:
    @patch("bot.daemon.forecast_cache.requests.get")
    def test_skips_when_fresh(self, mock_get):
        mock_get.return_value = _ok_response(70.0)
        cache = ForecastCache()
        cache.refresh()
        mock_get.reset_mock()

        ran = cache.refresh_if_stale(interval_s=FORECAST_REFRESH_INTERVAL_S)
        assert ran is False
        mock_get.assert_not_called()

    @patch("bot.daemon.forecast_cache.requests.get")
    def test_runs_when_stale(self, mock_get):
        mock_get.return_value = _ok_response(70.0)
        cache = ForecastCache()
        cache.refresh()
        # Force staleness by rewinding last_refresh
        cache._last_refresh = time.time() - (FORECAST_REFRESH_INTERVAL_S + 10)

        mock_get.reset_mock()
        ran = cache.refresh_if_stale(interval_s=FORECAST_REFRESH_INTERVAL_S)
        assert ran is True
        assert mock_get.call_count == len(STATIONS)

    @patch("bot.daemon.forecast_cache.requests.get")
    def test_custom_interval(self, mock_get):
        mock_get.return_value = _ok_response(70.0)
        cache = ForecastCache()
        cache.refresh()
        # Setting interval_s=0 forces refresh on the next call
        ran = cache.refresh_if_stale(interval_s=0)
        assert ran is True
