"""Wires METAR temperature changes through smart gates into the WeatherQuoter.

`METARPoller(on_result=handler)` passes the list of detected
`TemperatureChange` events to this handler. The handler owns the per-series
cooldown dict, fetches the station forecast, runs smart gates, and invokes
either the shadow or live path of :class:`WeatherQuoter`.

Per-series shadow-vs-live gate (Phase 1 step 10, option A.7):
:func:`bot.learning.mm_promotion.is_mm_live` / ``get_mm_order_size_multiplier``
decide per series whether to post real orders, and at what fraction of
``MM_ORDER_SIZE``. SHADOW → 0.0 (log only); LIVE → a Thompson-sampled
multiplier in [0, ``MM_SIZING_CAP_MULTIPLIER``] drawn from the posterior
over realized shadow P&L. The legacy env-wide ``WEATHER_MM_LIVE`` is the
operator-override master switch: when false, every series is forced into
shadow mode regardless of its kv state.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Protocol

from bot.config import WEATHER_MM_LIVE
from bot.daemon.dispatcher import AsyncEventDispatcher
from bot.daemon.forecast_cache import ForecastCache
from bot.daemon.metar_poller import TemperatureChange
from bot.daemon.requote_triggers import (
    REASON_METAR_CHANGE,
    VALID_REASONS,
)
from bot.daemon.smart_gates import evaluate_all_gates
from bot.daemon.stations import STATION_BY_SERIES
from bot.daemon.weather_quoter import WeatherQuoter
from bot.learning.mm_promotion import (
    get_mm_order_size_multiplier,
    is_mm_live,
)

logger = logging.getLogger(__name__)


# Minimum seconds between requotes for the same series. The poller's own
# change threshold (1°F) plus this cooldown keeps us from hammering Kalshi
# during a rapid temperature oscillation.
DEFAULT_COOLDOWN_S = 10.0

# Forecast fallback when Open-Meteo hasn't landed a reading for this station.
FORECAST_FALLBACK_DELTA_F = 5.0


class _SmartGateFn(Protocol):
    def __call__(
        self,
        station: str,
        bracket_floor: Optional[float],
        bracket_cap: Optional[float],
        running_high: float,
        forecast_high: float,
        hours_left: float,
        trajectory_f_per_hr: float,
    ) -> tuple[bool, str, float]: ...


def default_smart_gate(
    station: str,
    bracket_floor: Optional[float],
    bracket_cap: Optional[float],
    running_high: float,
    forecast_high: float,
    hours_left: float,
    trajectory_f_per_hr: float,
) -> tuple[bool, str, float]:
    """Smart-gate wrapper that handles threshold markets (no bracket bounds).

    `evaluate_all_gates` needs numeric floor/cap for its bracket-proximity
    check. For threshold markets we widen to 0/200°F so the proximity gate
    is effectively a no-op while the other gates still fire.
    """
    b_floor = bracket_floor if bracket_floor is not None else 0.0
    b_cap = bracket_cap if bracket_cap is not None else 200.0
    return evaluate_all_gates(
        station=station,
        bracket_floor=b_floor,
        bracket_cap=b_cap,
        running_high=running_high,
        forecast_high=forecast_high,
        hours_left=hours_left,
        trajectory_f_per_hr=trajectory_f_per_hr,
    )


class WeatherChangeHandler:
    """Callable invoked by ``METARPoller`` on each batch of temperature changes.

    Holds a reference to the shared DB-backed :class:`WeatherQuoter` and
    :class:`ForecastCache`. Per-series cooldown lives on the instance so it
    survives across calls without relying on global state.

    The handler is shadow-first *and* per-series: the global
    ``WEATHER_MM_LIVE`` env flag is a master kill-switch. When it is on,
    each series reads its own mm_live kv state and posts at the state's
    order-size multiplier (canary=0.5, full=1.0). When it is off, every
    series is forced through ``shadow_requote_city`` regardless of kv state.
    """

    def __init__(
        self,
        quoter: WeatherQuoter,
        forecast_cache: ForecastCache,
        *,
        smart_gate: Optional[_SmartGateFn] = None,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        live: Optional[bool] = None,
        conn=None,
        dispatcher: Optional[AsyncEventDispatcher] = None,
        poller=None,
        on_fire: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.quoter = quoter
        self.forecast_cache = forecast_cache
        self.smart_gate: _SmartGateFn = smart_gate or default_smart_gate
        self.cooldown_s = float(cooldown_s)
        # ``self.live`` is now the *master* env-level switch. Per-series
        # decisions happen inside ``_handle_one``.
        self.live = WEATHER_MM_LIVE if live is None else bool(live)
        # Connection used for kv-state lookups. Falls back to quoter.conn
        # (shared daemon connection) when not explicitly wired.
        self.conn = conn if conn is not None else getattr(quoter, "conn", None)
        # Per-series dispatcher — pushes ``_handle_one`` off the poller
        # thread so slow requotes don't delay METAR polling for other
        # cities. None = synchronous dispatch (test-friendly default).
        self.dispatcher = dispatcher
        # Poller handle for enqueue_synthetic — we need current running-high
        # and trajectory snapshots to synthesize a TemperatureChange when
        # the time-decay or forecast-change drivers fire.
        self.poller = poller
        # Called with (series, ts) on every successful requote regardless of
        # reason. Used by TimeDecayDriver to reset its cadence clock so a
        # fresh METAR change doesn't produce a redundant time-decay fire
        # moments later.
        self.on_fire = on_fire
        self._last_requote: dict[str, float] = {}
        self.stats: dict[str, int] = {
            "changes_seen": 0,
            "changes_throttled": 0,
            "requotes_dispatched": 0,
            "markets_shadowed": 0,
            "markets_quoted": 0,
            "markets_skipped": 0,
            "live_forecast_missing_skips": 0,
            "synthetic_enqueued": 0,
            "synthetic_rejected_no_state": 0,
            "synthetic_rejected_cooldown": 0,
            "errors": 0,
        }

    def _series_live_state(self, series: str) -> tuple[bool, float]:
        """Resolve (live?, order_size_multiplier) for this series right now.

        Master env flag wins — if WEATHER_MM_LIVE is false, everyone is
        shadow. Otherwise look up the kv state.
        """
        if not self.live or self.conn is None:
            return False, 0.0
        try:
            live = is_mm_live(self.conn, series)
            mult = get_mm_order_size_multiplier(self.conn, series) if live else 0.0
        except Exception as exc:  # pragma: no cover — kv read should not fail
            logger.warning("[wx-handler] mm_live lookup failed for %s: %s",
                           series, exc)
            return False, 0.0
        return live, mult

    # ------------------------------------------------------------------
    # Poller callback
    # ------------------------------------------------------------------

    def __call__(self, changes: list[TemperatureChange] | None) -> None:
        """Entry point used as ``METARPoller(on_result=handler)``.

        When a dispatcher is wired, each change is handed off to a
        per-series worker thread (size-1 coalescing) so the poller
        returns in microseconds. Without a dispatcher we fall back to
        synchronous dispatch — fine for tests, but in production the
        daemon should always supply one.
        """
        if not changes:
            return
        if self.dispatcher is None:
            for change in changes:
                self._handle_one(change)
            return
        for change in changes:
            # Bind ``change`` into a no-arg closure so the worker has
            # everything it needs; ``_handle_one`` is thread-safe
            # because the per-key worker serializes same-series work.
            self.dispatcher.dispatch(
                change.series, lambda c=change: self._handle_one(c)
            )

    def _handle_one(
        self,
        change: TemperatureChange,
        reason: str = REASON_METAR_CHANGE,
    ) -> None:
        self.stats["changes_seen"] += 1
        if reason not in VALID_REASONS:
            logger.warning("[wx-handler] invalid reason %r, coercing to %r",
                           reason, REASON_METAR_CHANGE)
            reason = REASON_METAR_CHANGE

        now = time.time()
        last = self._last_requote.get(change.series, 0.0)
        if now - last < self.cooldown_s:
            self.stats["changes_throttled"] += 1
            logger.debug(
                "[wx-handler] cooldown %s reason=%s (%.1fs since last)",
                change.series, reason, now - last,
            )
            return

        series_live, mult = self._series_live_state(change.series)

        forecast_high = self.forecast_cache.get(change.station)
        if forecast_high is None:
            if series_live:
                self.stats["live_forecast_missing_skips"] += 1
                logger.warning(
                    "[wx-handler] live fail-closed for %s/%s: no forecast",
                    change.series, change.station,
                )
                return
            forecast_high = change.running_high_f + FORECAST_FALLBACK_DELTA_F
            logger.warning(
                "[wx-handler] no forecast for %s; fallback %.0f°F",
                change.station, forecast_high,
            )

        logger.info(
            "[wx-handler] %s %s  %s°F→%.0f°F  high=%.0f  fc=%.0f  "
            "traj=%+.1f°F/hr  hrs_left=%.1f  mode=%s  mult=%.2f  reason=%s",
            change.series, change.station,
            "?" if change.old_temp_f is None else f"{change.old_temp_f:.0f}",
            change.new_temp_f, change.running_high_f, forecast_high,
            change.trajectory_f_per_hr, change.hours_left,
            "LIVE" if series_live else "SHADOW", mult, reason,
        )

        try:
            if series_live:
                results = self.quoter.requote_city(
                    series=change.series,
                    station=change.station,
                    running_high_f=change.running_high_f,
                    forecast_high_f=forecast_high,
                    hours_left=change.hours_left,
                    trajectory_f_per_hr=change.trajectory_f_per_hr,
                    smart_gates=self.smart_gate,
                    order_size_multiplier=mult,
                    old_temp_f=change.old_temp_f,
                    new_temp_f=change.new_temp_f,
                    trigger_reason=reason,
                )
                quoted = sum(1 for r in results if not r.skipped)
                skipped = sum(1 for r in results if r.skipped)
                self.stats["markets_quoted"] += quoted
                self.stats["markets_skipped"] += skipped
            else:
                results = self.quoter.shadow_requote_city(
                    series=change.series,
                    station=change.station,
                    running_high_f=change.running_high_f,
                    forecast_high_f=forecast_high,
                    hours_left=change.hours_left,
                    trajectory_f_per_hr=change.trajectory_f_per_hr,
                    smart_gates=self.smart_gate,
                    old_temp_f=change.old_temp_f,
                    new_temp_f=change.new_temp_f,
                    trigger_reason=reason,
                )
                self.stats["markets_shadowed"] += len(results)
        except Exception as exc:
            self.stats["errors"] += 1
            logger.error(
                "[wx-handler] requote failure for %s: %s",
                change.series, exc, exc_info=True,
            )
            return

        self.stats["requotes_dispatched"] += 1
        fired_at = time.time()
        self._last_requote[change.series] = fired_at
        # Notify the time-decay driver so its cadence clock resets — keeps a
        # fresh METAR fire from producing a redundant time-decay fire moments
        # later (or vice versa for forecast_change events).
        if self.on_fire is not None:
            try:
                self.on_fire(change.series, fired_at)
            except Exception as exc:  # pragma: no cover — on_fire should not fail
                logger.warning("[wx-handler] on_fire callback failed: %s", exc)

    # ------------------------------------------------------------------
    # Synthetic-event entry point — TimeDecayDriver / ForecastChangeDriver
    # ------------------------------------------------------------------

    def enqueue_synthetic(self, series: str, station: str, reason: str) -> bool:
        """Dispatch a non-METAR requote for ``series``.

        Called by :class:`TimeDecayDriver` and :class:`ForecastChangeDriver`
        with one of the reason labels in :data:`VALID_REASONS`. Resolves the
        current per-station state from the METAR poller + forecast cache,
        synthesizes a :class:`TemperatureChange` payload, and dispatches it
        through the same path used by real METAR events (so the cooldown,
        dispatcher, and on_fire callback all behave identically).

        Returns ``True`` when the event was successfully enqueued,
        ``False`` when state was unavailable or cooldown blocked it —
        drivers use the boolean to decide whether to advance their own
        cadence clock.
        """
        if reason not in VALID_REASONS:
            logger.warning(
                "[wx-handler] enqueue_synthetic rejected bad reason=%r", reason,
            )
            return False
        if self.poller is None:
            logger.debug(
                "[wx-handler] enqueue_synthetic rejected %s/%s: no poller wired",
                series, station,
            )
            self.stats["synthetic_rejected_no_state"] += 1
            return False

        state = self.poller.get_state(station)
        if state is None or state.last_temp_f is None:
            self.stats["synthetic_rejected_no_state"] += 1
            logger.debug(
                "[wx-handler] enqueue_synthetic rejected %s/%s: no state",
                series, station,
            )
            return False

        # Cooldown check up front — drivers want fast feedback on whether
        # the event would actually fire, before we bother synthesizing
        # the payload or hitting the dispatcher.
        now = time.time()
        last = self._last_requote.get(series, 0.0)
        if now - last < self.cooldown_s:
            self.stats["synthetic_rejected_cooldown"] += 1
            logger.debug(
                "[wx-handler] enqueue_synthetic cooldown %s reason=%s (%.1fs)",
                series, reason, now - last,
            )
            return False

        # Recover the station's city + hours_left the same way the real
        # poller computes them. Keep the registry as the single source.
        station_cfg = STATION_BY_SERIES.get(series)
        city = station_cfg.city if station_cfg is not None else ""

        # hours_left: use the poller's LST helper if available (same formula
        # the poller itself uses to fill TemperatureChange.hours_left) so
        # synthetic and natural events are directly comparable. Fall back
        # to the pure requote_triggers helper if the poller doesn't expose
        # it (test stubs won't).
        hours_left = 0.0
        if hasattr(self.poller, "_get_hours_remaining"):
            try:
                hours_left = float(self.poller._get_hours_remaining(station))
            except Exception:
                hours_left = 0.0
        if hours_left <= 0.0 and station_cfg is not None:
            from bot.daemon.requote_triggers import _hours_left_for_station
            hours_left = _hours_left_for_station(station_cfg, now)

        # Trajectory: derive from the most recent readings window if the
        # poller exposes it. Fall back to 0.0 — synthetic events don't
        # know about warming rate themselves.
        trajectory = 0.0
        try:
            trajectory = float(
                getattr(state, "trajectory_f_per_hr", 0.0) or 0.0
            )
        except Exception:
            trajectory = 0.0

        # Use the most-recent StationReading as the `reading` attribute;
        # fall back to a synthesized placeholder so downstream code
        # (which only reads .temp_f / .obs_time defensively) still works.
        reading = None
        if state.readings:
            reading = state.readings[-1]
        if reading is None:
            # Deferred import — avoids circular with metar_poller at module load
            from bot.daemon.metar_poller import StationReading
            reading = StationReading(
                station=station,
                temp_f=state.last_temp_f,
                temp_c=(state.last_temp_f - 32.0) * 5.0 / 9.0,
                obs_time="",
                poll_time=now,
            )

        synthetic = TemperatureChange(
            station=station,
            city=city,
            series=series,
            old_temp_f=state.last_temp_f,  # no delta — old == new for synthetic
            new_temp_f=state.last_temp_f,
            running_high_f=state.running_high_f
                if state.running_high_f > -900 else state.last_temp_f,
            hours_left=hours_left,
            trajectory_f_per_hr=trajectory,
            reading=reading,
        )

        self.stats["synthetic_enqueued"] += 1
        if self.dispatcher is None:
            self._handle_one(synthetic, reason=reason)
        else:
            self.dispatcher.dispatch(
                series, lambda c=synthetic, r=reason: self._handle_one(c, reason=r),
            )
        return True
