"""Wires METAR temperature changes through smart gates into the WeatherQuoter.

`METARPoller(on_result=handler)` passes the list of detected
`TemperatureChange` events to this handler. The handler owns the per-series
cooldown dict, fetches the station forecast, runs smart gates, and invokes
either the shadow or live path of :class:`WeatherQuoter`.

The shadow-vs-live gate is the `WEATHER_MM_LIVE` env var (mirrored into
`bot.config.WEATHER_MM_LIVE`). Shadow mode writes to `weather_mm_shadow` but
never touches the Kalshi API — matching the Phase 1 plan of proving out
event-driven quoting on counterfactual data before flipping to live.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Protocol

from bot.config import WEATHER_MM_LIVE
from bot.daemon.forecast_cache import ForecastCache
from bot.daemon.metar_poller import TemperatureChange
from bot.daemon.smart_gates import evaluate_all_gates
from bot.daemon.weather_quoter import WeatherQuoter

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

    The handler is intentionally shadow-first: `WEATHER_MM_LIVE=false`
    (default) routes every change through ``shadow_requote_city``. Flipping
    the env var + restarting the daemon is the single switch that promotes
    event-driven weather MM to live trading.
    """

    def __init__(
        self,
        quoter: WeatherQuoter,
        forecast_cache: ForecastCache,
        *,
        smart_gate: Optional[_SmartGateFn] = None,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        live: Optional[bool] = None,
    ) -> None:
        self.quoter = quoter
        self.forecast_cache = forecast_cache
        self.smart_gate: _SmartGateFn = smart_gate or default_smart_gate
        self.cooldown_s = float(cooldown_s)
        self.live = WEATHER_MM_LIVE if live is None else bool(live)
        self._last_requote: dict[str, float] = {}
        self.stats: dict[str, int] = {
            "changes_seen": 0,
            "changes_throttled": 0,
            "requotes_dispatched": 0,
            "markets_shadowed": 0,
            "markets_quoted": 0,
            "markets_skipped": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------------
    # Poller callback
    # ------------------------------------------------------------------

    def __call__(self, changes: list[TemperatureChange] | None) -> None:
        """Entry point used as ``METARPoller(on_result=handler)``."""
        if not changes:
            return
        for change in changes:
            self._handle_one(change)

    def _handle_one(self, change: TemperatureChange) -> None:
        self.stats["changes_seen"] += 1

        now = time.time()
        last = self._last_requote.get(change.series, 0.0)
        if now - last < self.cooldown_s:
            self.stats["changes_throttled"] += 1
            logger.debug(
                "[wx-handler] cooldown %s (%.1fs since last)",
                change.series, now - last,
            )
            return

        forecast_high = self.forecast_cache.get(change.station)
        if forecast_high is None:
            forecast_high = change.running_high_f + FORECAST_FALLBACK_DELTA_F
            logger.warning(
                "[wx-handler] no forecast for %s; fallback %.0f°F",
                change.station, forecast_high,
            )

        logger.info(
            "[wx-handler] %s %s  %s°F→%.0f°F  high=%.0f  fc=%.0f  "
            "traj=%+.1f°F/hr  hrs_left=%.1f  mode=%s",
            change.series, change.station,
            "?" if change.old_temp_f is None else f"{change.old_temp_f:.0f}",
            change.new_temp_f, change.running_high_f, forecast_high,
            change.trajectory_f_per_hr, change.hours_left,
            "LIVE" if self.live else "SHADOW",
        )

        try:
            if self.live:
                results = self.quoter.requote_city(
                    series=change.series,
                    station=change.station,
                    running_high_f=change.running_high_f,
                    forecast_high_f=forecast_high,
                    hours_left=change.hours_left,
                    trajectory_f_per_hr=change.trajectory_f_per_hr,
                    smart_gates=self.smart_gate,
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
        self._last_requote[change.series] = time.time()
