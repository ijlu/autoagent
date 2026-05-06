"""T1.2 — non-METAR requote triggers.

The METAR poller fires the :class:`WeatherChangeHandler` only on
material temperature changes (≥1 °F delta). That misses two classes
of genuinely new information:

1. **Time decay.** Fair-value uncertainty (``sigma``) shrinks steeply
   in the last few hours of the settlement day. Even when the running
   high hasn't moved, the probability distribution has narrowed — a
   bracket that was "coin-flip" at 8 h-left becomes near-certain at
   30 min-left. Stale quotes parked at the old fair value get picked
   off by counterparties that re-evaluate on a clock.

2. **Forecast refresh.** Open-Meteo updates every 15 min. A 2 °F swing
   in the forecast high is independent new information, on par with a
   direct METAR move. We should requote the affected series promptly
   rather than wait for the next temperature tick — which might be
   an hour away on a steady afternoon.

Both drivers feed the same :meth:`WeatherChangeHandler.enqueue_synthetic`
entry point, reuse the same dispatcher + per-series cooldown, and
attribute their rows via ``weather_mm_shadow.trigger_reason``. The
step-9 shadow-to-live gate breaks P&L out by trigger reason so we can
measure whether the added requotes earn edge or just burn fees.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional, Protocol

from bot.daemon.stations import STATION_BY_SERIES, WeatherStation

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# Trigger-reason labels — written into weather_mm_shadow.trigger_reason
# ═════════════════════════════════════════════════════════════════════════════

REASON_METAR_CHANGE = "metar_change"
REASON_TIME_DECAY = "time_decay"
REASON_FORECAST_CHANGE = "forecast_change"

VALID_REASONS: frozenset[str] = frozenset({
    REASON_METAR_CHANGE, REASON_TIME_DECAY, REASON_FORECAST_CHANGE,
})


# ═════════════════════════════════════════════════════════════════════════════
# Time-decay cadence
# ═════════════════════════════════════════════════════════════════════════════

# Cadence rule: shorter as the day compresses. Picked so the last 2 hours —
# when sigma drops from ~2 °F to ~0.3 °F — get at least 4 refreshes while
# the mid-day "waiting" window stays quiet.
#
#   hours_left >= 8   → 600 s  (10 min)   — sigma ~2 °F, slow drift
#   4 ≤ hrs_left < 8  → 300 s  (5 min)
#   2 ≤ hrs_left < 4  → 180 s  (3 min)
#   1 ≤ hrs_left < 2  →  90 s
#   0 < hrs_left < 1  →  45 s              — sigma < 0.5 °F, volatile tail
#   hrs_left <= 0     → inf   (day closed; nothing to requote)
#
# Day-closed returns inf so the driver silently skips instead of spamming.
_CADENCE_STEPS: tuple[tuple[float, float], ...] = (
    (0.0, float("inf")),    # hours_left <= 0
    (1.0, 45.0),            # 0 < hours_left < 1
    (2.0, 90.0),            # 1 <= hours_left < 2
    (4.0, 180.0),           # 2 <= hours_left < 4
    (8.0, 300.0),           # 4 <= hours_left < 8
    (float("inf"), 600.0),  # hours_left >= 8
)


def time_decay_interval_s(hours_left: float) -> float:
    """Return the seconds between time-decay requotes at this time-to-settle.

    Returns ``float("inf")`` for ``hours_left <= 0`` so the day-closed case
    is a natural no-op in the driver loop.
    """
    if hours_left <= 0:
        return float("inf")
    for upper, interval in _CADENCE_STEPS[1:]:  # skip the <= 0 sentinel
        if hours_left < upper:
            return interval
    # Last tuple is the catch-all.
    return _CADENCE_STEPS[-1][1]


# ═════════════════════════════════════════════════════════════════════════════
# Handler protocol — what drivers need from the WeatherChangeHandler
# ═════════════════════════════════════════════════════════════════════════════

class _SyntheticHandler(Protocol):
    """Minimal contract so tests can substitute a stub handler."""

    def enqueue_synthetic(self, series: str, station: str, reason: str) -> bool:
        ...


# ═════════════════════════════════════════════════════════════════════════════
# Time-decay driver
# ═════════════════════════════════════════════════════════════════════════════

class TimeDecayDriver:
    """Periodic-task callable that fires a synthetic requote per series.

    Register with the daemon scheduler on a short (≤ 30 s) interval:
    ``scheduler.register("wx_time_decay", driver, interval_s=30)``.
    On each invocation we iterate the canonical station registry, ask
    the METAR poller for the current state, and if the elapsed time
    since that series's last *any-trigger* requote exceeds the
    time-decay cadence for the current ``hours_left``, we hand off a
    synthetic event.

    Zero API calls and zero DB writes happen here — the driver only
    orchestrates event dispatch. Heavy lifting still lives behind the
    dispatcher + ``WeatherChangeHandler``.
    """

    def __init__(
        self,
        handler: _SyntheticHandler,
        poller,
        *,
        stations: Optional[Iterable[WeatherStation]] = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            handler: Receives ``enqueue_synthetic(series, station, reason)``.
            poller: The METARPoller; we read station state via ``get_state``.
            stations: Which stations to manage. Defaults to the full registry.
            now_fn: Injection seam for tests that drive virtual time.
        """
        self.handler = handler
        self.poller = poller
        self._stations: tuple[WeatherStation, ...] = tuple(
            stations if stations is not None else STATION_BY_SERIES.values()
        )
        self._now_fn = now_fn
        self._last_fire_ts: dict[str, float] = {}  # keyed by series
        self._lock = threading.Lock()
        self.stats: dict[str, int] = {
            "ticks": 0,
            "fired": 0,
            "skipped_no_state": 0,
            "skipped_day_closed": 0,
            "skipped_cadence": 0,
            "skipped_enqueue_rejected": 0,
        }

    def note_external_fire(self, series: str, ts: Optional[float] = None) -> None:
        """Record that *some other trigger* (METAR / forecast) just requoted
        ``series``. Keeps the time-decay clock from firing redundantly right
        after a natural event. Called by the handler on every successful
        requote regardless of reason.
        """
        with self._lock:
            self._last_fire_ts[series] = ts if ts is not None else self._now_fn()

    def __call__(self) -> None:
        """Scheduler entry point."""
        self.run_once()

    def run_once(self) -> int:
        """Evaluate every series, fire where due. Returns count fired.

        Held under a lock so the scheduler never races with itself if a
        prior tick took longer than the scheduler interval.
        """
        self.stats["ticks"] += 1
        fired = 0
        now = self._now_fn()

        for station in self._stations:
            state = self.poller.get_state(station.icao) if self.poller else None
            if state is None or state.last_temp_f is None:
                # No reading yet today (early-morning boot, before 1st poll).
                self.stats["skipped_no_state"] += 1
                continue

            hours_left = _hours_left_for_station(station, now)
            if hours_left <= 0:
                self.stats["skipped_day_closed"] += 1
                continue

            interval = time_decay_interval_s(hours_left)
            with self._lock:
                last_ts = self._last_fire_ts.get(station.series, 0.0)
            if now - last_ts < interval:
                self.stats["skipped_cadence"] += 1
                continue

            accepted = self.handler.enqueue_synthetic(
                series=station.series,
                station=station.icao,
                reason=REASON_TIME_DECAY,
            )
            if accepted:
                with self._lock:
                    self._last_fire_ts[station.series] = now
                self.stats["fired"] += 1
                fired += 1
                logger.info(
                    "[wx-time-decay] fired %s hrs_left=%.2f interval=%.0fs",
                    station.series, hours_left, interval,
                )
            else:
                # Handler refused (cooldown / no-state / shutdown) — don't
                # advance our timestamp so we retry on the next tick.
                self.stats["skipped_enqueue_rejected"] += 1
                logger.debug(
                    "[wx-time-decay] handler rejected %s", station.series,
                )
        return fired


# ═════════════════════════════════════════════════════════════════════════════
# Forecast-change driver
# ═════════════════════════════════════════════════════════════════════════════

# Minimum forecast high delta (°F) to treat as a material update.
# Chosen empirically: Open-Meteo daily forecast noise is ~0.5 °F tick-to-tick
# at the same hour; 1 °F filters jitter while catching real model revisions.
FORECAST_DELTA_THRESHOLD_F = 1.0


class ForecastChangeDriver:
    """Fires on *material* forecast-cache updates.

    Usage::

        driver = ForecastChangeDriver(handler)
        pre = forecast_cache.snapshot()
        forecast_cache.refresh()
        driver.on_refresh(pre, forecast_cache.snapshot())

    The driver resolves each primary-ICAO delta → series via the canonical
    registry and enqueues one synthetic event per affected series (tagged
    ``forecast_change``). The handler's dispatcher + cooldown handles
    coalescing if METAR and forecast events overlap.
    """

    def __init__(
        self,
        handler: _SyntheticHandler,
        *,
        threshold_f: float = FORECAST_DELTA_THRESHOLD_F,
    ) -> None:
        self.handler = handler
        self.threshold_f = float(threshold_f)
        self.stats: dict[str, int] = {
            "refreshes_seen": 0,
            "fires": 0,
            "skipped_below_threshold": 0,
            "skipped_not_tradable": 0,
            "skipped_missing_prior": 0,
        }

    def on_refresh(
        self,
        prior: dict[str, float],
        current: dict[str, float],
    ) -> int:
        """Diff two forecast snapshots and enqueue requotes where needed.

        Returns the number of series fired. Missing-on-one-side stations
        are skipped rather than treated as ∞ delta — the first-ever refresh
        is not a change event.
        """
        self.stats["refreshes_seen"] += 1
        fired = 0
        for icao, new_val in current.items():
            if icao not in prior:
                self.stats["skipped_missing_prior"] += 1
                continue
            old_val = prior[icao]
            if abs(new_val - old_val) < self.threshold_f:
                self.stats["skipped_below_threshold"] += 1
                continue

            # Resolve ICAO → series via registry. Backups (KJFK, KLGA, …)
            # don't own a series — skip them cleanly.
            series = _series_for_primary_icao(icao)
            if series is None:
                self.stats["skipped_not_tradable"] += 1
                continue

            logger.info(
                "[wx-forecast-change] %s (%s) forecast %.1f → %.1f °F",
                series, icao, old_val, new_val,
            )
            accepted = self.handler.enqueue_synthetic(
                series=series, station=icao, reason=REASON_FORECAST_CHANGE,
            )
            if accepted:
                self.stats["fires"] += 1
                fired += 1
        return fired


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _series_for_primary_icao(icao: str) -> Optional[str]:
    """Reverse-lookup: primary ICAO → series prefix, or None for backups."""
    for station in STATION_BY_SERIES.values():
        if station.icao == icao:
            return station.series
    return None


def _hours_left_for_station(station: WeatherStation, now_unix: float) -> float:
    """Hours remaining until 23:59 LST for this station.

    Duplicates the LST logic in ``METARPoller._get_lst_now`` intentionally
    — the driver must stay callable even when the poller hasn't registered
    a state yet (bootstrap case). Kept tiny and pure so the formulas stay
    in one head.
    """
    from datetime import datetime, timedelta, timezone
    lst_tz = timezone(timedelta(hours=station.lst_offset))
    now_lst = datetime.fromtimestamp(now_unix, tz=lst_tz)
    end_of_day = now_lst.replace(hour=23, minute=59, second=59, microsecond=0)
    remaining = (end_of_day - now_lst).total_seconds() / 3600.0
    return max(0.0, remaining)
