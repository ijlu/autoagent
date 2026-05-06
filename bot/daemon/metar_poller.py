"""METAR station poller with change detection and trajectory tracking.

Polls aviationweather.gov every ~30 seconds. Detects temperature changes,
tracks running daily high, and computes temperature trajectory for smart gating.

This is the core speed advantage of the daemon over the 2-minute oneshot cycle.
In-memory state persists across polls (no kv_cache round-trip), and 30-second
intervals mean we detect METAR updates within seconds of publication.

Phase 1: inherits from `bot.daemon.poller_base.Poller` so it gets a
start()/stop()/threaded-run loop for free when registered with the
daemon scheduler. The sync `poll()` API is preserved for back-compat
with the old orchestrator.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from bot.daemon.poller_base import Poller
from bot.daemon.stations import STATIONS, ALL_STATION_IDS
from bot.db import get_connection, kv_get, kv_set

logger = logging.getLogger(__name__)

# TTL for kv_cache daily-high entries — 25 hours, matches metar_observations.py
_DAILY_HIGH_TTL = 90_000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StationReading:
    """A single METAR observation for a station."""

    station: str
    temp_f: float
    temp_c: float
    obs_time: str
    poll_time: float  # time.time() when we fetched it
    wind_dir: int | None = None
    wind_speed_kt: int | None = None
    raw_metar: str = ""


@dataclass
class StationState:
    """Tracked state for a station across polls."""

    station: str
    last_temp_f: float | None = None
    running_high_f: float = -999.0
    running_high_date: str = ""  # YYYY-MM-DD in LST
    readings: list[StationReading] = field(default_factory=list)  # recent history
    MAX_READINGS: int = 90  # 45 minutes of history at 30s intervals
    # Trajectory requires enough spread + sample count to avoid noise amplification.
    # Prior bug: 2°F change over 9 minutes produced "+13F/hr" trajectory.
    MIN_TRAJ_SAMPLES: int = 5
    MIN_TRAJ_SPAN_SEC: float = 1800.0  # 30 minutes


@dataclass
class TemperatureChange:
    """A detected temperature change for a station."""

    station: str
    city: str
    series: str
    old_temp_f: float | None
    new_temp_f: float
    running_high_f: float
    hours_left: float
    trajectory_f_per_hr: float  # rate of change (positive = warming)
    reading: StationReading


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

class METARPoller(Poller):
    """Polls METAR stations and detects temperature changes.

    Two usage modes:

    1. Synchronous (oneshot / legacy orchestrator)::

        poller = METARPoller()
        changes = poller.poll()  # returns list[TemperatureChange]

    2. Background thread (daemon)::

        poller = METARPoller(on_result=handle_changes)
        poller.start()
        ...
        poller.stop()

    Both modes share the same internal state (locked by ``self._lock``),
    so it's safe to call ``get_state()`` / ``get_all_states()`` from
    another thread while the background loop runs.
    """

    METAR_URL = "https://aviationweather.gov/api/data/metar?ids={ids}&format=json"
    CHANGE_THRESHOLD_F = 1.0  # minimum delta to trigger requote
    POLL_TIMEOUT = 8  # seconds

    # Poller ABC contract
    name = "metar"
    interval_s = 30.0

    def __init__(self, on_result: Optional[Callable[[Any], None]] = None) -> None:
        super().__init__(on_result=on_result)
        self._states: dict[str, StationState] = {}
        self._lock = threading.Lock()
        self._last_poll_time: float = 0

        # Initialize state for all primary stations
        for station_id in STATIONS:
            self._states[station_id] = StationState(station=station_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll(self) -> list[TemperatureChange]:
        """Poll all stations and return list of temperature changes.

        Thread-safe.  Returns empty list if no changes detected or on error.
        """
        raw_data = self._fetch_metar()
        if raw_data is None:
            return []

        # Parse each observation into StationReading keyed by ICAO id
        readings: dict[str, StationReading] = {}
        for obs in raw_data:
            reading = self._parse_observation(obs)
            if reading is not None:
                readings[reading.station] = reading

        # Back-fill primary stations from backup stations when primary is missing
        self._backfill_from_backups(readings)

        with self._lock:
            changes = self._detect_changes(readings)
            self._last_poll_time = time.time()

        self._persist_running_highs()

        return changes

    def _poll_once(self) -> list[TemperatureChange]:
        """Poller ABC hook — delegates to poll(). Returns only non-empty
        change lists to the on_result callback (empty list means no
        changes, which is a no-op event)."""
        changes = self.poll()
        return changes if changes else None  # type: ignore[return-value]

    def get_state(self, station: str) -> StationState | None:
        """Public accessor -- returns a copy of a single station's state."""
        with self._lock:
            state = self._states.get(station)
            if state is None:
                return None
            # Return a shallow copy so callers don't mutate internal state
            return StationState(
                station=state.station,
                last_temp_f=state.last_temp_f,
                running_high_f=state.running_high_f,
                running_high_date=state.running_high_date,
                readings=list(state.readings),
            )

    def get_all_states(self) -> dict[str, StationState]:
        """Snapshot of all primary station states."""
        with self._lock:
            result: dict[str, StationState] = {}
            for sid, state in self._states.items():
                result[sid] = StationState(
                    station=state.station,
                    last_temp_f=state.last_temp_f,
                    running_high_f=state.running_high_f,
                    running_high_date=state.running_high_date,
                    readings=list(state.readings),
                )
            return result

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def error_count(self) -> int:
        return self._error_count

    # ------------------------------------------------------------------
    # HTTP fetch
    # ------------------------------------------------------------------

    def _fetch_metar(self) -> list[dict] | None:
        """Fetch METAR JSON for all tracked stations in one HTTP request.

        Returns parsed JSON list on success, ``None`` on failure.
        """
        ids_str = ",".join(ALL_STATION_IDS)
        url = self.METAR_URL.format(ids=ids_str)

        try:
            resp = requests.get(
                url,
                timeout=self.POLL_TIMEOUT,
                headers={
                    "User-Agent": "KalshiWeatherDaemon/1.0",
                    "Accept": "application/json",
                },
            )
            if resp.status_code != 200:
                logger.warning("[metar-poller] HTTP %d from aviationweather.gov", resp.status_code)
                self._error_count += 1
                return None

            data = resp.json()
            if not isinstance(data, list):
                logger.warning("[metar-poller] Unexpected response type: %s", type(data).__name__)
                self._error_count += 1
                return None

            return data

        except requests.RequestException as exc:
            logger.warning("[metar-poller] Request failed: %s: %s", type(exc).__name__, exc)
            self._error_count += 1
            return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_observation(self, raw: dict) -> StationReading | None:
        """Parse one METAR JSON object into a ``StationReading``.

        Returns ``None`` if the observation is missing critical fields.
        """
        station = raw.get("icaoId") or raw.get("stationId")
        if not station:
            return None

        temp_c_raw = raw.get("temp")
        if temp_c_raw is None:
            return None

        try:
            temp_c = float(temp_c_raw)
        except (TypeError, ValueError):
            return None

        temp_f = temp_c * 9.0 / 5.0 + 32.0

        # Wind (optional)
        wind_dir: int | None = None
        wind_speed: int | None = None
        try:
            wdir = raw.get("wdir")
            if wdir is not None and str(wdir).isdigit():
                wind_dir = int(wdir)
            wspd = raw.get("wspd")
            if wspd is not None:
                # round() per CLAUDE.md §5 fixed-point convention
                wind_speed = round(float(wspd))
        except (TypeError, ValueError):
            pass

        obs_time = raw.get("reportTime") or raw.get("obsTime") or ""
        raw_metar_str = raw.get("rawOb") or ""

        return StationReading(
            station=station,
            temp_f=temp_f,
            temp_c=temp_c,
            obs_time=obs_time,
            poll_time=time.time(),
            wind_dir=wind_dir,
            wind_speed_kt=wind_speed,
            raw_metar=raw_metar_str,
        )

    # ------------------------------------------------------------------
    # Backup station back-fill
    # ------------------------------------------------------------------

    def _backfill_from_backups(self, readings: dict[str, StationReading]) -> None:
        """If a primary station has no reading, copy from the best backup."""
        for primary_id, cfg in STATIONS.items():
            if primary_id in readings:
                continue
            for backup_id in cfg.backups:
                backup_reading = readings.get(backup_id)
                if backup_reading is not None:
                    # Create a synthetic reading tagged as the primary station
                    readings[primary_id] = StationReading(
                        station=primary_id,
                        temp_f=backup_reading.temp_f,
                        temp_c=backup_reading.temp_c,
                        obs_time=backup_reading.obs_time,
                        poll_time=backup_reading.poll_time,
                        wind_dir=backup_reading.wind_dir,
                        wind_speed_kt=backup_reading.wind_speed_kt,
                        raw_metar=f"(backup:{backup_id}) {backup_reading.raw_metar}",
                    )
                    logger.info(
                        "[metar-poller] %s missing, using backup %s (%.1f F)",
                        primary_id, backup_id, backup_reading.temp_f,
                    )
                    break  # use first available backup

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def _detect_changes(self, readings: dict[str, StationReading]) -> list[TemperatureChange]:
        """Compare new readings to previous state and return changes.

        Must be called while holding ``self._lock``.
        """
        changes: list[TemperatureChange] = []

        for station_id in STATIONS:
            reading = readings.get(station_id)
            if reading is None:
                continue

            state = self._states[station_id]
            old_temp = state.last_temp_f

            # Update internal state (running high, trajectory buffer, etc.)
            self._update_state(station_id, reading)

            # Determine if this constitutes a meaningful change
            is_change = False
            if old_temp is None:
                # First reading ever for this station -- always report
                is_change = True
            elif abs(reading.temp_f - old_temp) >= self.CHANGE_THRESHOLD_F:
                is_change = True

            if is_change:
                cfg = STATIONS[station_id]
                changes.append(TemperatureChange(
                    station=station_id,
                    city=cfg.city,
                    series=cfg.series,
                    old_temp_f=old_temp,
                    new_temp_f=reading.temp_f,
                    running_high_f=self._states[station_id].running_high_f,
                    hours_left=self._get_hours_remaining(station_id),
                    trajectory_f_per_hr=self._compute_trajectory(station_id),
                    reading=reading,
                ))

        return changes

    # ------------------------------------------------------------------
    # kv_cache persistence
    # ------------------------------------------------------------------

    def _persist_running_highs(self) -> None:
        """Write each station's current running_high_f to kv_cache.

        Called after every successful METAR poll so that seed_running_high()
        can recover in-memory state after a daemon restart — even when
        metar_observations signal source is never called (e.g. all weather
        markets are trading at extreme prices and fail the price_bounds
        filter before the ensemble is invoked).
        """
        try:
            conn = get_connection()
        except RuntimeError:
            return  # DB not yet initialised

        snapshot: list[tuple[str, float, str]] = []
        with self._lock:
            for station_id, state in self._states.items():
                if state.running_high_f > -999.0:
                    snapshot.append(
                        (station_id, state.running_high_f, state.running_high_date)
                    )

        for station_id, high_f, date_lst in snapshot:
            kv_key = f"metar_daily_high_{station_id}_{date_lst}"
            try:
                existing = kv_get(conn, kv_key)
                if existing is None:
                    record: dict = {"high_f": high_f, "obs_count": 0}
                else:
                    if high_f <= existing.get("high_f", -999.0):
                        continue  # kv already has equal or higher value — nothing to do
                    record = dict(existing)
                    record["high_f"] = high_f
                kv_set(conn, kv_key, record, _DAILY_HIGH_TTL)
            except Exception as exc:
                logger.warning(
                    "[metar-poller] persist daily high failed for %s: %s",
                    station_id, exc,
                )

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def seed_running_high(self, conn) -> dict[str, float]:
        """Seed running_high_f from kv_cache on daemon startup.

        _persist_running_highs() writes ``metar_daily_high_{station}_{date_lst}``
        after every successful METAR poll (every ~30s).  On a mid-day restart
        that entry already captures the afternoon peak; without seeding we would
        start from the current (evening) METAR reading and miss it entirely,
        causing wrong-direction quotes near settlement.

        Returns {station: seeded_high_f} for each station that was seeded.
        Safe to call before start() — holds self._lock internally.
        """
        seeded: dict[str, float] = {}
        with self._lock:
            for station_id, state in self._states.items():
                today_lst = self._get_lst_date(station_id)
                kv_key = f"metar_daily_high_{station_id}_{today_lst}"
                try:
                    record = kv_get(conn, kv_key)
                except Exception as exc:
                    logger.warning(
                        "[metar-poller] seed kv_get failed for %s: %s",
                        station_id, exc,
                    )
                    continue

                if not isinstance(record, dict):
                    continue
                high_f = record.get("high_f")
                if not isinstance(high_f, (int, float)):
                    continue
                high_f = float(high_f)

                if high_f < -40 or high_f > 130:
                    logger.warning(
                        "[metar-poller] seed skipped %s: implausible high_f=%.1f",
                        station_id, high_f,
                    )
                    continue

                if high_f > state.running_high_f:
                    state.running_high_f = high_f
                    state.running_high_date = today_lst
                    seeded[station_id] = high_f
                    logger.info(
                        "[metar-poller] seeded %s running_high_f=%.1f°F "
                        "from kv_cache (obs_count=%d)",
                        station_id, high_f, record.get("obs_count", 0),
                    )

        return seeded

    def _update_state(self, station: str, reading: StationReading) -> None:
        """Update running high, last temp, and trajectory buffer.

        Must be called while holding ``self._lock``.
        """
        state = self._states[station]

        # -- Midnight rollover: reset running high when LST date changes --
        today_lst = self._get_lst_date(station)
        if state.running_high_date != today_lst:
            state.running_high_f = -999.0
            state.running_high_date = today_lst
            state.readings.clear()

        # -- Update running daily high --
        if reading.temp_f > state.running_high_f:
            state.running_high_f = reading.temp_f

        # -- Update last temp --
        state.last_temp_f = reading.temp_f

        # -- Append to trajectory buffer (bounded) --
        state.readings.append(reading)
        if len(state.readings) > state.MAX_READINGS:
            state.readings = state.readings[-state.MAX_READINGS:]

    # ------------------------------------------------------------------
    # Trajectory (rate-of-change)
    # ------------------------------------------------------------------

    def _compute_trajectory(self, station: str) -> float:
        """Compute temperature rate-of-change in deg-F per hour.

        Uses ordinary least-squares linear regression over the recent
        readings buffer.  Returns 0.0 if insufficient data — requires
        ``MIN_TRAJ_SAMPLES`` readings spanning at least ``MIN_TRAJ_SPAN_SEC``
        to avoid noise amplification at short horizons.
        """
        state = self._states.get(station)
        if state is None:
            return 0.0

        readings = state.readings
        n = len(readings)

        # Need enough samples to distinguish signal from noise.
        if n < state.MIN_TRAJ_SAMPLES:
            return 0.0

        # Use poll_time (epoch seconds) as the x-axis
        t0 = readings[0].poll_time
        xs = [r.poll_time - t0 for r in readings]
        ys = [r.temp_f for r in readings]

        # Need enough time span — a 2°F change over 9 min amplifies to +13°F/hr.
        if xs[-1] - xs[0] < state.MIN_TRAJ_SPAN_SEC:
            return 0.0

        # OLS: slope = (n * sum(x*y) - sum(x)*sum(y)) / (n * sum(x^2) - sum(x)^2)
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_x2 = sum(x * x for x in xs)

        denom = n * sum_x2 - sum_x * sum_x
        if abs(denom) < 1e-12:
            return 0.0

        slope_per_sec = (n * sum_xy - sum_x * sum_y) / denom
        slope_per_hour = slope_per_sec * 3600.0

        return round(slope_per_hour, 2)

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    def _get_hours_remaining(self, station: str) -> float:
        """Hours remaining until 11:59 PM LST for this station."""
        now_lst = self._get_lst_now(station)
        end_of_day = now_lst.replace(hour=23, minute=59, second=59, microsecond=0)
        remaining = (end_of_day - now_lst).total_seconds() / 3600.0
        return max(0.0, round(remaining, 2))

    def _get_lst_date(self, station: str) -> str:
        """Today's date string (YYYY-MM-DD) in the station's LST."""
        return self._get_lst_now(station).strftime("%Y-%m-%d")

    @staticmethod
    def _get_lst_now(station: str) -> datetime:
        """Current datetime in the station's LOCAL STANDARD TIME (fixed offset)."""
        cfg = STATIONS.get(station)
        offset_hours = cfg.lst_offset if cfg else -5
        lst_tz = timezone(timedelta(hours=offset_hours))
        return datetime.now(lst_tz)
