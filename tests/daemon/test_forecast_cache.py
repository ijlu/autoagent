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


@pytest.fixture(autouse=True)
def _fast_rate_limiter(monkeypatch):
    """Auto-applied: zero out the inter-station sleep + the per-domain
    rate-limit wait so tests run instantly. The actual rate-limit
    behavior is exercised in `test_refresh_invokes_rate_limiter` below.
    """
    monkeypatch.setattr("bot.daemon.forecast_cache._INTER_STATION_SLEEP_S", 0.0)
    monkeypatch.setattr("bot.daemon.forecast_cache.rate_limit_wait",
                        lambda url: None)


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
        # Pick any station for the transient-failure test — KNYC (NY primary).
        target_station = "KNYC"
        target_lat_fragment = f"latitude={STATIONS[target_station].lat}"

        def side_effect(url, **kwargs):
            if target_lat_fragment in url:
                raise requests.ConnectionError("boom")
            return _ok_response(82.0)

        cache = ForecastCache()
        mock_get.return_value = first_resp
        cache.refresh()
        assert cache.get(target_station) == 80.0

        mock_get.side_effect = side_effect
        mock_get.return_value = None  # side_effect wins
        cache.refresh()

        # Target preserved from first refresh (merge, not replace)
        assert cache.get(target_station) == 80.0
        # Other stations got the fresh value
        for sid in STATIONS:
            if sid != target_station:
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
    def test_refresh_invokes_rate_limiter_per_station(self, mock_get, monkeypatch):
        """Regression: each station's HTTP fetch must pass through
        ``rate_limit_wait`` first. Pre-2026-04-30 the refresh bypassed
        the per-domain Open-Meteo limiter (1.0s, 3-burst) and bursted
        all 6 stations in a tight loop, producing the recurring HTTP
        429 cluster on the live VPS.
        """
        rate_limit_calls: list[str] = []
        monkeypatch.setattr(
            "bot.daemon.forecast_cache.rate_limit_wait",
            lambda url: rate_limit_calls.append(url),
        )
        mock_get.return_value = _ok_response(70.0)

        cache = ForecastCache()
        cache.refresh()

        assert len(rate_limit_calls) == len(STATIONS)
        for url in rate_limit_calls:
            assert "open-meteo.com" in url

    @patch("bot.daemon.forecast_cache.requests.get")
    def test_refresh_staggers_between_stations(self, mock_get, monkeypatch):
        """Regression: consecutive station fetches must be separated by
        at least ``_INTER_STATION_SLEEP_S`` so a tight burst doesn't
        bump the per-minute Open-Meteo cap. We measure by counting
        ``time.sleep`` invocations rather than wall-clock time so the
        test is deterministic.
        """
        sleeps: list[float] = []
        monkeypatch.setattr(
            "bot.daemon.forecast_cache._INTER_STATION_SLEEP_S", 0.123,
        )
        monkeypatch.setattr(
            "bot.daemon.forecast_cache.time.sleep",
            lambda s: sleeps.append(s),
        )
        mock_get.return_value = _ok_response(70.0)

        cache = ForecastCache()
        cache.refresh()

        # First fetch fires immediately; remaining N-1 fetches sleep first.
        # The fixture-level rate_limit_wait stub is a no-op so its sleeps
        # don't show up here — only the inter-station stagger does.
        assert sleeps == [0.123] * (len(STATIONS) - 1)

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
