"""Periodic forecast cache for the event-driven weather daemon.

Extracted from the legacy `orchestrator.WeatherDaemon` so the unified
daemon (`bot/daemon/main.py`) can share it under a scheduled refresh task.

The cache stores today's forecast high (°F) per station, refreshed every
FORECAST_REFRESH_INTERVAL seconds from Open-Meteo. It is the forecast leg
of `_blended_mu` inside `WeatherQuoter` — the running METAR high is the
other leg. In-memory, thread-safe reads; refresh runs serially on the
scheduler thread.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from bot.api import rate_limit_wait
from bot.daemon.stations import STATIONS

logger = logging.getLogger(__name__)


FORECAST_REFRESH_INTERVAL_S = 900  # 15 minutes
REQUEST_TIMEOUT_S = 8

# Minimum sleep between consecutive station fetches inside a single
# refresh pass. Belt-and-suspenders on top of the per-domain
# `rate_limit_wait` (config: open-meteo = 1.0s min interval, 3 burst).
# Without this stagger, the refresh hit Open-Meteo with 6 stations in a
# tight loop on every cycle — cold-start daemon log showed 6× HTTP 429
# clustered at every 15-min refresh boundary on 2026-04-30. Even though
# the daily quota is 10K calls (way above our footprint), Open-Meteo
# enforces a 600/min ceiling that this burst was bumping. 200ms keeps
# us well under at any plausible call count.
_INTER_STATION_SLEEP_S: float = 0.2


class ForecastCache:
    """Periodically-refreshed station-keyed forecast cache.

    Thread-safe. ``get()`` returns the most recently fetched forecast high
    (or None if no forecast has ever landed). ``refresh()`` hits Open-Meteo
    for every station in :data:`STATIONS`; ``refresh_if_stale()`` is the
    idempotent scheduler hook.
    """

    # Query template (everything after the `?`). The full URL is built
    # via `_openmeteo.forecast_url()` per fetch so it auto-routes to
    # commercial endpoint + apikey when OPENMETEO_API_KEY is set.
    QUERY_TEMPLATE = (
        "latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max"
        "&temperature_unit=fahrenheit"
        "&timezone=auto"
        "&forecast_days=1"
    )

    def __init__(self) -> None:
        self._forecasts: dict[str, float] = {}
        self._last_refresh: float = 0.0
        self._lock = threading.Lock()

    def get(self, station: str) -> Optional[float]:
        """Return cached forecast high (°F) for a station, or None."""
        with self._lock:
            return self._forecasts.get(station)

    def last_refresh(self) -> float:
        with self._lock:
            return self._last_refresh

    def snapshot(self) -> dict[str, float]:
        """Immutable copy of the current forecast map."""
        with self._lock:
            return dict(self._forecasts)

    def refresh_if_stale(self, interval_s: float = FORECAST_REFRESH_INTERVAL_S) -> bool:
        """Trigger a refresh if older than ``interval_s``. Returns True if it ran."""
        if time.time() - self.last_refresh() < interval_s:
            return False
        self.refresh()
        return True

    def refresh(self) -> int:
        """Fetch fresh forecasts for every primary station. Returns count updated.

        Rate-limited via two layers: ``rate_limit_wait`` enforces the
        per-domain Open-Meteo budget (1.0s min interval, 3 burst), and
        ``_INTER_STATION_SLEEP_S`` adds a small additional gap so the
        6-station refresh doesn't fire as a tight loop. See the constant
        docstring for the 2026-04-30 incident that motivated this.
        """
        refreshed = 0
        new_values: dict[str, float] = {}
        station_items = list(STATIONS.items())
        for idx, (station_id, cfg) in enumerate(station_items):
            lat = cfg.lat
            lon = cfg.lon
            if lat is None or lon is None:
                continue
            # Inter-station stagger (skip before the very first fetch so
            # the first refresh after startup isn't artificially delayed).
            if idx > 0 and _INTER_STATION_SLEEP_S > 0:
                time.sleep(_INTER_STATION_SLEEP_S)
            try:
                from bot.signals.sources._openmeteo import forecast_url
                url = forecast_url(self.QUERY_TEMPLATE.format(lat=lat, lon=lon))
                # Per-domain rate-limit wait — same gate every other
                # Open-Meteo caller in the codebase passes through
                # (cached_get -> rate_limit_wait). forecast_cache used to
                # bypass this and burst.
                rate_limit_wait(url)
                resp = requests.get(
                    url,
                    timeout=REQUEST_TIMEOUT_S,
                    headers={"User-Agent": "KalshiWeatherDaemon/1.0"},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "[forecast] Open-Meteo HTTP %d for %s", resp.status_code, station_id
                    )
                    continue
                data = resp.json()
                temps = data.get("daily", {}).get("temperature_2m_max", [])
                if temps:
                    new_values[station_id] = float(temps[0])
                    refreshed += 1
            except (requests.RequestException, ValueError, TypeError) as exc:
                logger.warning("[forecast] Open-Meteo error for %s: %s", station_id, exc)

        with self._lock:
            # Merge rather than replace — a transient failure on one station
            # should preserve yesterday's number rather than drop to None.
            self._forecasts.update(new_values)
            self._last_refresh = time.time()

        logger.info(
            "[forecast] refreshed %d/%d stations", refreshed, len(STATIONS)
        )
        return refreshed

    def set(self, station: str, forecast_high_f: float) -> None:
        """Manual setter for tests and synthetic data."""
        with self._lock:
            self._forecasts[station] = float(forecast_high_f)
            self._last_refresh = time.time()
