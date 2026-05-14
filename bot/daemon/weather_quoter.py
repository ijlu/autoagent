"""Fast weather requote engine for the trading daemon.

When METAR temperature changes, this module:
1. Finds all open weather markets for the affected city
2. Evaluates smart gates (time, proximity, trajectory)
3. Cancels stale orders for those markets
4. Computes updated fair values using the new observation
5. Posts new two-sided quotes at fair_value +/- half_spread

Target: complete full requote cycle in <5 seconds.

Phase 1 shadow mode: `shadow_requote_city` / `shadow_requote_single` share
the FV + smart-gate + bid/ask math with the live path but never call
`api_post`/`api_delete`. Proposed quotes are written to the
`weather_mm_shadow` table so the step-9 gate can measure counterfactual P&L.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from bot.api import api_get, api_post, api_delete
from bot.core.money import kalshi_maker_fee
from bot.config import (
    MM_DRY_RUN,
    MM_HALF_SPREAD,
    MM_ORDER_SIZE,
    MM_MAX_INVENTORY,
    MM_SKEW_PER_10,
    MM_ORDER_TAG,
    WEATHER_ENSEMBLE_V2,
)
from bot.daemon.locks import DB_WRITE_LOCK
from bot.daemon.stations import STATIONS
from bot.daemon.fills_writer import record_posted_order
from bot.db import db_write_ctx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WeatherMarket:
    """A weather market we might quote."""
    ticker: str
    title: str
    series: str
    bracket_floor: Optional[float]  # None for threshold markets
    bracket_cap: Optional[float]
    threshold: Optional[float]  # None for bracket markets
    is_bracket: bool
    is_above: bool  # for threshold markets
    yes_bid: Optional[int]  # cents; None when Kalshi returns no book on yes side
    yes_ask: Optional[int]  # cents; None when Kalshi returns no book on yes side
    volume: int
    close_time: str
    # A6: raw Kalshi /markets payload, retained so `_compute_fair_value` can feed
    # `weather_ensemble_v2.predict_v2` without re-fetching. Default empty so legacy
    # test fixtures that don't pass it still land on the v1 path.
    raw: dict = field(default_factory=dict)


@dataclass
class RequoteResult:
    """Result of requoting a single market."""
    ticker: str
    fair_value_cents: int
    orders_posted: int
    orders_cancelled: int
    skipped: bool
    skip_reason: Optional[str]
    latency_ms: float


@dataclass
class ShadowResult:
    """Result of a shadow-mode requote — logged, not placed."""
    ticker: str
    fair_value_cents: int
    proposed_bid_cents: int
    proposed_ask_cents: int
    half_spread_cents: int
    gate_should_quote: bool
    gate_reason: Optional[str]
    latency_ms: float
    shadow_row_id: Optional[int]


# Type alias for the smart-gate callback.
# (station, bracket_floor, bracket_cap, running_high, forecast_high, hours_left, trajectory)
#   -> (should_quote: bool, reason: str, spread_multiplier: float)
SmartGateFn = Callable[
    [str, Optional[float], Optional[float], float, float, float, float],
    Tuple[bool, str, float],
]


# ---------------------------------------------------------------------------
# Probability helpers (mirrors bot/signals/sources/metar_observations.py)
# ---------------------------------------------------------------------------

def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard logistic CDF for temperature probability estimation."""
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _sigma_for_hours(hours_left: float) -> float:
    """Compute uncertainty sigma based on hours remaining in settlement day.

    v2: 3-4x larger than original. Prior schedule (σ=0.3-2°F) was ~5x too
    narrow — afternoon temperature variance is 4-8°F so pricing near the
    threshold was overconfident. Physical reasoning: σ should approximate
    the std-dev of (actual_high - current_running_high) over the remaining
    hours. Validated via shadow calibration (re-calibrate after 2 weeks
    of shadow data using tools/backtest_sigma.py).
    """
    if hours_left <= 0:
        return 0.5
    elif hours_left < 1:
        return 1.0
    elif hours_left < 2:
        return 2.0
    elif hours_left < 4:
        return 3.5
    elif hours_left < 6:
        return 5.0
    elif hours_left < 12:
        return 6.5
    else:
        return 8.0


def _blended_mu(
    running_high_f: float,
    forecast_high_f: float,
    hours_left: float,
) -> float:
    """Compute expected eventual daily high, blending forecast with observations.

    Early in the day the forecast dominates; late in the day the running high
    dominates because most warming has already occurred.
    """
    total_day_hours = 24.0
    day_fraction_elapsed = max(0.0, min(1.0, 1.0 - hours_left / total_day_hours))
    if hours_left > 0:
        forecast_weight = max(0.1, 1.0 - day_fraction_elapsed)
        obs_weight = 1.0 - forecast_weight
        return (
            forecast_weight * max(forecast_high_f, running_high_f)
            + obs_weight * running_high_f
        )
    else:
        # Day is over -- the running high IS the final high.
        return running_high_f


# ---------------------------------------------------------------------------
# Market-fetch cache (in-memory, very short TTL -- markets don't change fast)
# ---------------------------------------------------------------------------

_market_cache: dict[str, Tuple[list[WeatherMarket], float]] = {}
_MARKET_CACHE_TTL = 60.0  # seconds


# ---------------------------------------------------------------------------
# WeatherQuoter
# ---------------------------------------------------------------------------

class WeatherQuoter:
    """Fast weather requote engine.

    Usage::

        quoter = WeatherQuoter(conn)
        # When METAR changes:
        results = quoter.requote_city(
            series="KXHIGHNY",
            station="KJFK",
            running_high_f=73.0,
            forecast_high_f=76.0,
            hours_left=8.5,
            trajectory_f_per_hr=1.2,
        )
    """

    def __init__(self, conn, live: bool = False):
        """
        Args:
            conn: SQLite connection for inventory lookups and opportunity logging.
            live: When False (default), the shadow path is used — proposed
                quotes are logged to `weather_mm_shadow` and no API calls are
                made. When True, `requote_city` posts real orders subject to
                `MM_DRY_RUN`. The CLAUDE.md-specified `WEATHER_MM_LIVE=false`
                shadow-to-live gate reads this attribute via the caller.
        """
        self.conn = conn
        self.live = bool(live)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def requote_city(
        self,
        series: str,
        station: str,
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
        trajectory_f_per_hr: float = 0.0,
        smart_gates: Optional[SmartGateFn] = None,
        order_size_multiplier: float = 1.0,
        old_temp_f: Optional[float] = None,
        new_temp_f: Optional[float] = None,
        trigger_reason: str = "metar_change",
    ) -> list[RequoteResult]:
        """Requote all open markets for a weather series.

        ``order_size_multiplier`` scales ``MM_ORDER_SIZE`` and the
        ``MM_MAX_INVENTORY`` cap for the graduated promotion states
        (canary=0.5, full=1.0). The live path also writes a paired
        ``weather_mm_shadow`` row with ``live_mode=1`` so the T.6 shadow-vs-
        live calibration monitor has inputs.
        """
        results: list[RequoteResult] = []

        try:
            markets = self._fetch_weather_markets(series)
        except Exception as exc:
            logger.error("[wx-quoter] Failed to fetch markets for %s: %s", series, exc)
            return results

        if not markets:
            logger.info("[wx-quoter] No open markets for series %s", series)
            return results

        for market in markets:
            if not self._is_today_market(market, station):
                logger.debug(
                    "[wx-quoter] skipping next-day market %s", market.ticker
                )
                continue
            try:
                result = self._requote_single(
                    market=market,
                    station=station,
                    running_high_f=running_high_f,
                    forecast_high_f=forecast_high_f,
                    hours_left=hours_left,
                    trajectory_f_per_hr=trajectory_f_per_hr,
                    smart_gates=smart_gates,
                    order_size_multiplier=order_size_multiplier,
                    old_temp_f=old_temp_f,
                    new_temp_f=new_temp_f,
                    trigger_reason=trigger_reason,
                )
                results.append(result)
            except Exception as exc:
                logger.error(
                    "[wx-quoter] Error requoting %s: %s", market.ticker, exc,
                    exc_info=True,
                )
                results.append(RequoteResult(
                    ticker=market.ticker,
                    fair_value_cents=0,
                    orders_posted=0,
                    orders_cancelled=0,
                    skipped=True,
                    skip_reason=f"error: {exc}",
                    latency_ms=0.0,
                ))

        return results

    # ------------------------------------------------------------------
    # Shadow path — compute FV + quote, write to weather_mm_shadow, no API
    # ------------------------------------------------------------------

    def shadow_requote_city(
        self,
        series: str,
        station: str,
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
        trajectory_f_per_hr: float = 0.0,
        smart_gates: Optional[SmartGateFn] = None,
        old_temp_f: Optional[float] = None,
        new_temp_f: Optional[float] = None,
        trigger_reason: str = "metar_change",
    ) -> list["ShadowResult"]:
        """Shadow sibling of ``requote_city``.

        Computes fair value + proposed bid/ask + smart-gate decision for every
        open market in the series, writes one ``weather_mm_shadow`` row per
        market, and returns a list of ShadowResult. **Never calls api_post or
        api_delete.** Used by the Phase 1 shadow-to-live gate: the same FV
        and gate logic as live mode, but the only side-effect is the DB row.
        """
        out: list[ShadowResult] = []
        try:
            markets = self._fetch_weather_markets(series)
        except Exception as exc:
            logger.error("[wx-shadow] fetch markets failed for %s: %s", series, exc)
            return out

        if not markets:
            logger.info("[wx-shadow] no open markets for %s", series)
            return out

        for market in markets:
            if not self._is_today_market(market, station):
                logger.debug(
                    "[wx-shadow] skipping next-day market %s", market.ticker
                )
                continue
            try:
                res = self._shadow_requote_single(
                    market=market,
                    station=station,
                    running_high_f=running_high_f,
                    forecast_high_f=forecast_high_f,
                    hours_left=hours_left,
                    trajectory_f_per_hr=trajectory_f_per_hr,
                    smart_gates=smart_gates,
                    old_temp_f=old_temp_f,
                    new_temp_f=new_temp_f,
                    trigger_reason=trigger_reason,
                )
                out.append(res)
            except Exception as exc:
                logger.error(
                    "[wx-shadow] error shadow-quoting %s: %s", market.ticker, exc,
                    exc_info=True,
                )
        return out

    def _shadow_requote_single(
        self,
        market: WeatherMarket,
        station: str,
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
        trajectory_f_per_hr: float,
        smart_gates: Optional[SmartGateFn],
        old_temp_f: Optional[float],
        new_temp_f: Optional[float],
        trigger_reason: str = "metar_change",
    ) -> "ShadowResult":
        t0 = time.monotonic()
        ticker = market.ticker

        gate_should_quote = True
        gate_reason: Optional[str] = None
        spread_mult = 1.0
        if smart_gates is not None:
            gate_should_quote, gate_reason, spread_mult = smart_gates(
                station,
                market.bracket_floor,
                market.bracket_cap,
                running_high_f,
                forecast_high_f,
                hours_left,
                trajectory_f_per_hr,
            )

        # Market price bounds — same filter as score_market's price_bounds check.
        # Log the row (so we can see the disagreement), but mark gate=0.
        if (market.yes_ask is not None and market.yes_ask <= 8) or (
            market.yes_bid is not None and market.yes_bid >= 92
        ):
            gate_should_quote = False
            gate_reason = (gate_reason or "") + ";market_price_bounds"

        fair_value_cents = self._compute_fair_value(
            market, running_high_f, forecast_high_f, hours_left,
        )

        base_hs = max(MM_HALF_SPREAD, 8)
        effective_hs_req = max(1, int(round(base_hs * spread_mult)))
        inventory = self._get_inventory(ticker)
        bid, ask, effective_hs = self.compute_quote_prices(
            fair_value_cents, effective_hs_req, inventory,
        )

        extreme_fv = fair_value_cents <= 2 or fair_value_cents >= 98
        if extreme_fv:
            # Log the row, but flag as skipped — step-9 gate treats these as
            # "would-not-have-traded" regardless of fill model.
            gate_should_quote = False
            gate_reason = (gate_reason or "") + f";extreme_fv={fair_value_cents}c"

        latency_ms = _ms_since(t0)
        row_id = self._write_shadow_row(
            ticker=ticker,
            series=market.series,
            station=station,
            old_temp_f=old_temp_f,
            new_temp_f=new_temp_f,
            running_high_f=running_high_f,
            forecast_high_f=forecast_high_f,
            hours_left=hours_left,
            trajectory_f_per_hr=trajectory_f_per_hr,
            fair_value_cents=fair_value_cents,
            bid_cents=bid,
            ask_cents=ask,
            half_spread_cents=effective_hs,
            market_yes_bid=market.yes_bid,
            market_yes_ask=market.yes_ask,
            inventory=inventory,
            gate_should_quote=gate_should_quote,
            gate_reason=gate_reason,
            gate_spread_mult=spread_mult,
            latency_ms=latency_ms,
            live_mode=False,
            trigger_reason=trigger_reason,
        )

        return ShadowResult(
            ticker=ticker,
            fair_value_cents=fair_value_cents,
            proposed_bid_cents=bid,
            proposed_ask_cents=ask,
            half_spread_cents=effective_hs,
            gate_should_quote=gate_should_quote,
            gate_reason=gate_reason,
            latency_ms=latency_ms,
            shadow_row_id=row_id,
        )

    def _write_shadow_row(
        self,
        *,
        ticker: str,
        series: str,
        station: str,
        old_temp_f: Optional[float],
        new_temp_f: Optional[float],
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
        trajectory_f_per_hr: float,
        fair_value_cents: int,
        bid_cents: int,
        ask_cents: int,
        half_spread_cents: int,
        market_yes_bid: Optional[int],
        market_yes_ask: Optional[int],
        inventory: int,
        gate_should_quote: bool,
        gate_reason: Optional[str],
        gate_spread_mult: float,
        latency_ms: float,
        live_mode: bool,
        live_order_id_bid: Optional[str] = None,
        live_order_id_ask: Optional[str] = None,
        order_size: Optional[int] = None,
        trigger_reason: str = "metar_change",
    ) -> Optional[int]:
        """Insert one row into weather_mm_shadow. Returns rowid on success."""
        now = time.time()
        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        market_mid = (
            (market_yes_bid + market_yes_ask) // 2
            if market_yes_bid and market_yes_ask else None
        )
        try:
            with db_write_ctx(self.conn):
                cur = self.conn.execute(
                    """INSERT INTO weather_mm_shadow
                    (ts_unix, ts_iso, ticker, series, station,
                     old_temp_f, new_temp_f, running_high_f, forecast_high_f,
                     hours_left, trajectory_f_per_hr,
                     fair_value_cents, proposed_bid_cents, proposed_ask_cents,
                     half_spread_cents, market_yes_bid, market_yes_ask, market_mid,
                     inventory, gate_should_quote, gate_reason, gate_spread_mult,
                     latency_ms, live_mode,
                     live_order_id_bid, live_order_id_ask, order_size,
                     trigger_reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        int(now), now_iso, ticker, series, station,
                        old_temp_f, new_temp_f, running_high_f, forecast_high_f,
                        hours_left, trajectory_f_per_hr,
                        fair_value_cents, bid_cents, ask_cents,
                        half_spread_cents, market_yes_bid, market_yes_ask, market_mid,
                        inventory, 1 if gate_should_quote else 0, gate_reason,
                        gate_spread_mult, latency_ms, 1 if live_mode else 0,
                        live_order_id_bid, live_order_id_ask, order_size,
                        trigger_reason,
                    ),
                )
                rowid = cur.lastrowid
            return rowid
        except Exception as exc:
            logger.error("[wx-shadow] failed to insert shadow row for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Internal: single-market requote
    # ------------------------------------------------------------------

    def _requote_single(
        self,
        market: WeatherMarket,
        station: str,
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
        trajectory_f_per_hr: float,
        smart_gates: Optional[SmartGateFn],
        order_size_multiplier: float = 1.0,
        old_temp_f: Optional[float] = None,
        new_temp_f: Optional[float] = None,
        trigger_reason: str = "metar_change",
    ) -> RequoteResult:
        """Cancel stale orders, compute new fair value, post fresh quotes."""
        t0 = time.monotonic()
        ticker = market.ticker

        # -- Smart gate check --
        spread_mult = 1.0
        gate_reason: Optional[str] = None
        if smart_gates is not None:
            should_quote, gate_reason, spread_mult = smart_gates(
                station,
                market.bracket_floor,
                market.bracket_cap,
                running_high_f,
                forecast_high_f,
                hours_left,
                trajectory_f_per_hr,
            )
            if not should_quote:
                return RequoteResult(
                    ticker=ticker,
                    fair_value_cents=0,
                    orders_posted=0,
                    orders_cancelled=0,
                    skipped=True,
                    skip_reason=gate_reason,
                    latency_ms=_ms_since(t0),
                )

        # -- Market price bounds (mirrors score_market price_bounds filter) --
        # Skip markets where the market itself has near-certain pricing.
        # Our FV disagreeing with a 1-2¢ market means adverse selection, not edge.
        if (market.yes_ask is not None and market.yes_ask <= 8) or (
            market.yes_bid is not None and market.yes_bid >= 92
        ):
            return RequoteResult(
                ticker=ticker,
                fair_value_cents=0,
                orders_posted=0,
                orders_cancelled=0,
                skipped=True,
                skip_reason="market_price_bounds",
                latency_ms=_ms_since(t0),
            )

        # -- Compute fair value --
        fair_value_cents = self._compute_live_fair_value(
            market, running_high_f, forecast_high_f, hours_left,
        )
        if fair_value_cents is None:
            return RequoteResult(
                ticker=ticker,
                fair_value_cents=0,
                orders_posted=0,
                orders_cancelled=0,
                skipped=True,
                skip_reason="v2_fair_value_unavailable",
                latency_ms=_ms_since(t0),
            )

        # Skip extreme fair values (no reliable edge at the rails)
        if fair_value_cents <= 2 or fair_value_cents >= 98:
            return RequoteResult(
                ticker=ticker,
                fair_value_cents=fair_value_cents,
                orders_posted=0,
                orders_cancelled=0,
                skipped=True,
                skip_reason=f"extreme fair value {fair_value_cents}c",
                latency_ms=_ms_since(t0),
            )

        # -- Cancel stale orders --
        n_cancelled = self._cancel_stale_orders(ticker)

        # -- Get current inventory --
        inventory = self._get_inventory(ticker)

        # -- Compute effective half-spread --
        base_hs = MM_HALF_SPREAD
        # Apply category override for weather (wider base)
        base_hs = max(base_hs, 8)
        effective_hs = max(1, int(round(base_hs * spread_mult)))

        # -- Effective order size (graduated promotion multiplier) --
        eff_order_size = max(1, int(round(MM_ORDER_SIZE * order_size_multiplier)))

        # -- Post new quotes --
        n_posted, bid_oid, ask_oid, bid_price, ask_price = self._post_quotes(
            ticker=ticker,
            fair_value_cents=fair_value_cents,
            half_spread=effective_hs,
            inventory=inventory,
            order_size=eff_order_size,
            max_inventory=max(eff_order_size, int(round(
                MM_MAX_INVENTORY * order_size_multiplier))),
        )

        latency = _ms_since(t0)

        # T.6 paired logging — write a live-mode weather_mm_shadow row with
        # the posted prices + order IDs so the shadow fill model can be
        # compared against realized live fills at settlement.
        self._write_shadow_row(
            ticker=ticker,
            series=market.series,
            station=station,
            old_temp_f=old_temp_f,
            new_temp_f=new_temp_f,
            running_high_f=running_high_f,
            forecast_high_f=forecast_high_f,
            hours_left=hours_left,
            trajectory_f_per_hr=trajectory_f_per_hr,
            fair_value_cents=fair_value_cents,
            bid_cents=bid_price,
            ask_cents=ask_price,
            half_spread_cents=effective_hs,
            market_yes_bid=market.yes_bid,
            market_yes_ask=market.yes_ask,
            inventory=inventory,
            gate_should_quote=True,
            gate_reason=gate_reason,
            gate_spread_mult=spread_mult,
            latency_ms=latency,
            live_mode=True,
            live_order_id_bid=bid_oid,
            live_order_id_ask=ask_oid,
            order_size=eff_order_size,
        )

        result = RequoteResult(
            ticker=ticker,
            fair_value_cents=fair_value_cents,
            orders_posted=n_posted,
            orders_cancelled=n_cancelled,
            skipped=False,
            skip_reason=None,
            latency_ms=latency,
        )

        # -- Log to opportunity_log --
        self._log_requote(result)

        logger.info(
            "[wx-quoter] %s  fv=%dc  cancel=%d  post=%d  inv=%d  hs=%d  "
            "size=%d  mult=%.2f  %.0fms",
            ticker, fair_value_cents, n_cancelled, n_posted, inventory,
            effective_hs, eff_order_size, order_size_multiplier, latency,
        )

        return result

    # ------------------------------------------------------------------
    # Market fetching
    # ------------------------------------------------------------------

    @staticmethod
    def _is_today_market(market: WeatherMarket, station: str) -> bool:
        """Return True if this market settles on today's LST date.

        Weather quoter receives today's running_high and today's forecast.
        Pricing next-day markets with that data is wrong: tomorrow's expected
        high may be 10°F warmer, and running_high is irrelevant for tomorrow.
        Skip any market whose close_time falls on a different LST day.
        """
        if not market.close_time:
            return True  # no date info — don't block
        try:
            close_dt = datetime.fromisoformat(
                market.close_time.replace("Z", "+00:00")
            )
            cfg = STATIONS.get(station)
            lst_offset = cfg.lst_offset if cfg else -5
            lst_tz = timezone(timedelta(hours=lst_offset))
            close_lst_date = close_dt.astimezone(lst_tz).date()
            today_lst_date = datetime.now(lst_tz).date()
            return close_lst_date == today_lst_date
        except Exception:
            return True  # on parse failure, don't skip

    def _fetch_weather_markets(self, series: str) -> list[WeatherMarket]:
        """Fetch open weather markets for a series from the Kalshi API.

        Results are cached for 60 seconds -- market metadata does not
        change fast enough to warrant hitting the API on every METAR tick.
        """
        now = time.time()
        if series in _market_cache:
            cached_markets, cached_ts = _market_cache[series]
            if now - cached_ts < _MARKET_CACHE_TTL:
                return cached_markets

        resp = api_get(f"/markets?status=open&series_ticker={series}&limit=200")
        raw_markets = resp.get("markets", [])

        parsed: list[WeatherMarket] = []
        for m in raw_markets:
            parsed_market = self._parse_market(m, series)
            if parsed_market is not None:
                parsed.append(parsed_market)

        _market_cache[series] = (parsed, now)
        return parsed

    @staticmethod
    def _parse_market(m: dict, series: str) -> Optional[WeatherMarket]:
        """Parse a single Kalshi API market dict into a WeatherMarket."""
        ticker = m.get("ticker", "")
        title = (m.get("title") or m.get("subtitle") or "").lower()
        is_bracket = "-B" in ticker.upper() or "-b" in ticker

        bracket_floor: Optional[float] = None
        bracket_cap: Optional[float] = None
        threshold: Optional[float] = None
        is_above = True

        if is_bracket:
            # Try API-provided strikes first
            api_floor = m.get("floor_strike")
            api_cap = m.get("cap_strike")
            if api_floor is not None and api_cap is not None:
                try:
                    bracket_floor = float(api_floor)
                    bracket_cap = float(api_cap)
                except (ValueError, TypeError):
                    pass

            # Fallback: parse from title -- "74 to 75", "74-75", "74F and 75F"
            if bracket_floor is None or bracket_cap is None:
                range_match = re.search(
                    r'(\d+\.?\d*)\s*\u00b0?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)',
                    title,
                )
                if range_match:
                    bracket_floor = float(range_match.group(1))
                    bracket_cap = float(range_match.group(2))

            # Last resort: ticker contains -B{value} -> floor=value, cap=value+2
            if bracket_floor is None or bracket_cap is None:
                tick_match = re.search(r'-[Bb](-?\d+\.?\d*)', ticker)
                if tick_match:
                    bracket_floor = float(tick_match.group(1))
                    bracket_cap = bracket_floor + 2.0

            if bracket_floor is None or bracket_cap is None:
                return None  # cannot determine bracket bounds
        else:
            # Threshold market. Direction (`is_above`) is authoritatively
            # encoded by which strike Kalshi sets on the payload:
            #   cap_strike present, floor_strike None  → "high < cap"  (is_above=False)
            #   floor_strike present, cap_strike None  → "high > floor" (is_above=True)
            # Previously this fell through a title regex that did not match
            # Kalshi's literal "<"/">" titles and defaulted to is_above=True
            # on parse failure, silently inverting every below-threshold
            # market's fair value (2026-04-22 sign-flip fix — poisoned 27k
            # shadow rows and produced the 0.9-1.0 bucket pathology where
            # avg_est=0.967 but yes_rate=0.103).
            api_floor = m.get("floor_strike")
            api_cap = m.get("cap_strike")
            parsed_from_api = False
            if api_cap is not None and api_floor is None:
                try:
                    threshold = float(api_cap)
                    is_above = False
                    parsed_from_api = True
                except (ValueError, TypeError):
                    pass
            elif api_floor is not None and api_cap is None:
                try:
                    threshold = float(api_floor)
                    is_above = True
                    parsed_from_api = True
                except (ValueError, TypeError):
                    pass
            if not parsed_from_api:
                # Defensive fallback: title regex. Log so we can detect
                # API coverage gaps — in steady state this should never fire.
                threshold, is_above = _parse_threshold(ticker, title)
                if threshold is not None:
                    logger.warning(
                        "[wx-quoter] %s: strikes missing from payload "
                        "(floor=%r cap=%r); fell back to title regex "
                        "(threshold=%s is_above=%s). "
                        "Title=%r",
                        ticker, api_floor, api_cap,
                        threshold, is_above, title,
                    )
            if threshold is None:
                return None

        # Parse order book snapshot. Kalshi's /markets list response returns
        # book prices under the `_dollars` suffix (stringified decimal dollars,
        # e.g. "0.4500"); the unsuffixed `yes_bid`/`yes_ask` keys are absent on
        # this endpoint. `trade.py` has long carried the same fallback — the
        # quoter used to rely on `_safe_cents(None) → 0`, which silently
        # produced the 20k all-zero shadow rows (see 2026-04-21 B+D incident).
        yes_bid = _safe_cents(m.get("yes_bid") or m.get("yes_bid_dollars"))
        yes_ask = _safe_cents(m.get("yes_ask") or m.get("yes_ask_dollars"))

        return WeatherMarket(
            ticker=ticker,
            title=m.get("title") or m.get("subtitle") or "",
            series=series,
            bracket_floor=bracket_floor,
            bracket_cap=bracket_cap,
            threshold=threshold,
            is_bracket=is_bracket,
            is_above=is_above,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            volume=int(m.get("volume", 0) or 0),
            close_time=m.get("close_time") or m.get("expiration_time") or "",
            raw=m,
        )

    # ------------------------------------------------------------------
    # Fair value computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_fair_value_v2(market: "WeatherMarket") -> Optional[int]:
        """Call ``weather_ensemble_v2.predict_v2`` and convert prob→cents.

        Returns ``None`` on any error (module import, source fetch failure,
        unparseable market, extreme prob) so the caller can decide whether
        to fall back to v1 or fail closed.
        Clamped to [2, 98] — same bounds the v1 path enforces.
        """
        try:
            from bot.signals.weather_ensemble_v2 import predict_v2

            prob, tag = predict_v2(market.ticker, market.raw)
            if prob is None:
                return None
            cents = int(round(max(0.02, min(0.98, float(prob))) * 100))
            cents = max(2, min(98, cents))
            logger.info(
                "[wx-quoter] v2 FV %s: p=%.3f → %d¢  tag=%s",
                market.ticker, prob, cents, tag,
            )
            return cents
        except Exception as exc:
            logger.warning(
                "[wx-quoter] v2 FV failed for %s: %s",
                market.ticker, exc,
            )
            return None

    def _compute_live_fair_value(
        self,
        market: WeatherMarket,
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
    ) -> Optional[int]:
        """Compute live fair value; fail closed if an attempted v2 read fails."""
        if WEATHER_ENSEMBLE_V2 and market.raw:
            v2_cents = self._compute_fair_value_v2(market)
            if v2_cents is not None:
                return v2_cents
            logger.warning(
                "[wx-quoter] live fail-closed for %s: v2 fair value unavailable",
                market.ticker,
            )
            try:
                self._v2_fail_closed_count = getattr(
                    self, "_v2_fail_closed_count", 0
                ) + 1
            except Exception:
                pass
            return None
        return self._compute_fair_value(
            market, running_high_f, forecast_high_f, hours_left,
        )

    def _compute_fair_value(
        self,
        market: WeatherMarket,
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
    ) -> int:
        """Compute fair value in cents (2-98) for a weather market.

        When ``WEATHER_ENSEMBLE_V2`` is enabled and the raw Kalshi payload is
        attached to the ``WeatherMarket``, delegate to
        ``weather_ensemble_v2.predict_v2`` — the precision-weighted multi-source
        Gaussian combine (A1–A5). Any error or ``None`` return falls back to
        the v1 METAR-only logistic CDF below so a broken v2 read can never
        leave us without a fair value.

        F (2026-04-27): the v1 fallback is a known liability — it skips
        per-city skill σ, MOS bias, group correlation discount, staleness
        inflation, and truncated projection. Falling back silently means a
        latent v2 bug can quietly degrade us to v1 quoting without alerting
        anyone. Log a WARNING (not info) when the fallback fires + bump the
        v1-fallback counter so the daemon health log surfaces the rate.
        """
        if WEATHER_ENSEMBLE_V2 and market.raw:
            v2_cents = self._compute_fair_value_v2(market)
            if v2_cents is not None:
                return v2_cents
            # v2 is enabled but didn't return — falling back to v1.
            logger.warning(
                "[wx-quoter] v1 FALLBACK fired for %s — predict_v2 returned None or "
                "raised; v1 path bypasses skill σ / MOS bias / group ρ / staleness / "
                "truncation. If this is frequent, investigate predict_v2 failures.",
                market.ticker,
            )
            try:
                self._v1_fallback_count = getattr(self, "_v1_fallback_count", 0) + 1
            except Exception:
                pass

        mu = _blended_mu(running_high_f, forecast_high_f, hours_left)
        sigma = _sigma_for_hours(hours_left)

        if market.is_bracket:
            assert market.bracket_floor is not None
            assert market.bracket_cap is not None
            floor_val = market.bracket_floor
            cap_val = market.bracket_cap

            # Special case: running high already past bracket ceiling.
            # Daily high only goes up, so P(in bracket) is near zero.
            if running_high_f >= cap_val:
                prob = 0.02
            elif running_high_f >= floor_val:
                # Currently inside bracket -- depends on whether eventual high
                # stays inside or exceeds the cap.
                prob_below_cap = _logistic_cdf(cap_val, mu, sigma)
                prob = max(0.02, min(0.98, prob_below_cap))
            else:
                # Below bracket -- need to reach it but not exceed it.
                cdf_upper = _logistic_cdf(cap_val, mu, sigma)
                cdf_lower = _logistic_cdf(floor_val, mu, sigma)
                prob = max(0.02, min(0.98, cdf_upper - cdf_lower))
        else:
            # Threshold market
            assert market.threshold is not None
            threshold = market.threshold

            if running_high_f >= threshold:
                # Already exceeded -- near certainty for "above", near zero for "below"
                margin = running_high_f - threshold
                if margin >= 3.0:
                    prob_above = 0.98
                elif margin >= 1.0:
                    prob_above = 0.96
                else:
                    prob_above = 0.95
            else:
                # Not yet exceeded -- model remaining warming potential.
                prob_above = 1.0 - _logistic_cdf(threshold, mu, sigma)
                prob_above = max(0.02, min(0.98, prob_above))

            prob = prob_above if market.is_above else max(0.02, min(0.98, 1.0 - prob_above))

        return max(2, min(98, int(round(prob * 100))))

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    def _weather_client_order_id(self, order: dict) -> str:
        """Return the weather-MM client id for a resting order, if known."""
        client_id = order.get("client_order_id") or ""
        if client_id:
            return str(client_id)

        order_id = order.get("order_id") or ""
        if not order_id:
            return ""
        try:
            row = self.conn.execute(
                "SELECT client_order_id FROM posted_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning(
                "[wx-quoter] posted_orders lookup failed for order_id=%s: %s",
                order_id, exc,
            )
            return ""
        return str(row[0]) if row and row[0] else ""

    def _cancel_stale_orders(self, ticker: str) -> int:
        """Cancel weather-MM-owned resting orders for a ticker."""
        cancelled = 0
        try:
            resp = api_get(f"/portfolio/orders?ticker={ticker}&status=resting")
            orders = resp.get("orders", [])
        except Exception as exc:
            logger.warning("[wx-quoter] Failed to fetch resting orders for %s: %s", ticker, exc)
            return 0

        for order in orders:
            order_id = order.get("order_id", "")
            if not order_id:
                continue
            client_id = self._weather_client_order_id(order)
            if not client_id.startswith("mm_wx_"):
                continue
            try:
                api_delete(f"/portfolio/orders/{order_id}")
                cancelled += 1
            except Exception as exc:
                logger.warning("[wx-quoter] Failed to cancel order %s: %s", order_id, exc)

        return cancelled

    # ------------------------------------------------------------------
    # Quote posting
    # ------------------------------------------------------------------

    @staticmethod
    def compute_quote_prices(
        fair_value_cents: int,
        half_spread: int,
        inventory: int,
    ) -> tuple[int, int, int]:
        """Pure pricing math — fee-floored half-spread, inventory skew, clamps.

        Returns ``(bid, ask, effective_half_spread)`` as YES-equivalent cents.
        Shared between the live `_post_quotes` path and the shadow path so
        the two can never drift.
        """
        min_half = _fee_floor_half_spread(fair_value_cents)
        effective_hs = max(half_spread, min_half)
        skew = int(round(inventory * MM_SKEW_PER_10 / 10.0))
        bid = fair_value_cents - effective_hs - skew
        ask = fair_value_cents + effective_hs - skew
        bid = max(1, min(98, bid))
        ask = max(bid + 1, min(99, ask))
        return bid, ask, effective_hs

    def _post_quotes(
        self,
        ticker: str,
        fair_value_cents: int,
        half_spread: int,
        inventory: int,
        order_size: int = MM_ORDER_SIZE,
        max_inventory: int = MM_MAX_INVENTORY,
    ) -> Tuple[int, Optional[str], Optional[str], int, int]:
        """Post two-sided limit orders for a weather market.

        Returns ``(n_posted, bid_order_id, ask_order_id, bid_price, ask_price)``.
        Order IDs may be ``None`` when the leg was skipped or failed. DRY_RUN
        emits a synthetic ``dry_<ticker>_<ts>`` ID so the paired shadow row
        still has a handle.
        """
        bid, ask, effective_hs = self.compute_quote_prices(
            fair_value_cents, half_spread, inventory,
        )

        orders_posted = 0
        bid_oid: Optional[str] = None
        ask_oid: Optional[str] = None
        ts = int(time.time())
        expiration_ts = ts + 90  # 90s expiry (faster cycle = shorter life)

        # -- Inventory cap enforcement --
        can_buy = abs(inventory + order_size) <= max_inventory
        can_sell = abs(inventory - order_size) <= max_inventory

        # -- Post BID (buy YES) --
        if can_buy:
            safe_ticker = ticker.replace(".", "_")
            client_id = f"mm_wx_{safe_ticker}_{ts}_{uuid.uuid4().hex[:8]}"
            bid_body = {
                "ticker": ticker,
                "side": "yes",
                "type": "limit",
                "count": order_size,
                "yes_price": bid,
                "action": "buy",
                "expiration_ts": expiration_ts,
                "client_order_id": client_id,
                "post_only": True,
            }
            if MM_DRY_RUN:
                logger.info("[wx-quoter] DRY BID %s YES x%d @ %dc", ticker, order_size, bid)
                orders_posted += 1
                bid_oid = f"dry_{client_id}"
            else:
                try:
                    resp = api_post("/portfolio/orders", bid_body)
                    oid = resp.get("order", {}).get("order_id", "")
                    if oid:
                        orders_posted += 1
                        bid_oid = oid
                        record_posted_order(
                            self.conn,
                            order_id=oid,
                            client_order_id=client_id,
                            ticker=ticker,
                            side="yes",
                            action="buy",
                            count=order_size,
                            price_cents=bid,
                            source_hint="mm_quote",
                            live_mode=True,
                        )
                        logger.info("[wx-quoter] BID %s YES x%d @ %dc  oid=%s", ticker, order_size, bid, oid)
                    else:
                        logger.warning("[wx-quoter] BID %s: API returned empty order_id", ticker)
                except Exception as exc:
                    logger.error("[wx-quoter] BID %s FAILED: %s", ticker, exc)

        # -- Post ASK (buy NO at 100 - ask) --
        if can_sell:
            no_price = 100 - ask
            safe_ticker = ticker.replace(".", "_")
            client_id = f"mm_wx_{safe_ticker}_{ts}_{uuid.uuid4().hex[:8]}"
            ask_body = {
                "ticker": ticker,
                "side": "no",
                "type": "limit",
                "count": order_size,
                "no_price": no_price,
                "action": "buy",
                "expiration_ts": expiration_ts,
                "client_order_id": client_id,
                "post_only": True,
            }
            if MM_DRY_RUN:
                logger.info("[wx-quoter] DRY ASK %s NO x%d @ %dc (YES ask=%dc)", ticker, order_size, no_price, ask)
                orders_posted += 1
                ask_oid = f"dry_{client_id}"
            else:
                try:
                    resp = api_post("/portfolio/orders", ask_body)
                    oid = resp.get("order", {}).get("order_id", "")
                    if oid:
                        orders_posted += 1
                        ask_oid = oid
                        record_posted_order(
                            self.conn,
                            order_id=oid,
                            client_order_id=client_id,
                            ticker=ticker,
                            side="no",
                            action="buy",
                            count=order_size,
                            price_cents=no_price,
                            source_hint="mm_quote",
                            live_mode=True,
                        )
                        logger.info("[wx-quoter] ASK %s NO x%d @ %dc  oid=%s", ticker, order_size, no_price, oid)
                    else:
                        logger.warning("[wx-quoter] ASK %s: API returned empty order_id", ticker)
                except Exception as exc:
                    logger.error("[wx-quoter] ASK %s FAILED: %s", ticker, exc)

        return orders_posted, bid_oid, ask_oid, bid, ask

    # ------------------------------------------------------------------
    # Inventory lookup
    # ------------------------------------------------------------------

    def _get_inventory(self, ticker: str) -> int:
        """Get current net position for a ticker from mm_inventory."""
        try:
            row = self.conn.execute(
                "SELECT net_position FROM mm_inventory WHERE ticker=?", (ticker,)
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Opportunity logging
    # ------------------------------------------------------------------

    def _log_requote(self, result: RequoteResult) -> None:
        """Log requote to the ``opportunity_log`` table.

        Writes against the canonical schema declared in ``bot/db.py``
        (matches the ``trade.log_opportunity`` pattern). Before the
        2026-04-22 audit fix this targeted a legacy
        ``(timestamp, ticker, action, data)`` shape that was dropped
        during the MM-deletion pivot — every insert raised
        ``no such column: timestamp`` and a blanket ``except: pass``
        swallowed the error, silently nuking the event path's audit
        trail.

        Runs on the poller/worker thread — MUST go through
        ``db_write_ctx`` to serialize with the cycle thread's writes.

        ``side=None`` because a requote event touches both sides of the
        book; the per-side bid/ask prices + counts are in
        ``sources_json`` so downstream queries can still reconstruct.
        """
        payload = json.dumps({
            "fair_value_cents": result.fair_value_cents,
            "posted": result.orders_posted,
            "cancelled": result.orders_cancelled,
            "latency_ms": round(result.latency_ms, 1),
            "skipped": result.skipped,
            "skip_reason": result.skip_reason,
        })
        try:
            with db_write_ctx(self.conn):
                self.conn.execute(
                    """INSERT INTO opportunity_log
                       (ticker, strategy, action, side, ensemble_prob,
                        sources_json, skip_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result.ticker,
                        "weather_mm",
                        "wx_requote",
                        None,
                        result.fair_value_cents / 100.0,
                        payload,
                        result.skip_reason,
                    ),
                )
        except sqlite3.IntegrityError:
            # UNIQUE / FK constraint — tolerated (e.g. concurrent duplicate
            # on a retry). Any other sqlite3.Error or unexpected exception
            # must propagate so schema drift fails loudly instead of
            # silently eating the audit trail again.
            pass


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _ms_since(t0: float) -> float:
    """Milliseconds elapsed since *t0* (from time.monotonic)."""
    return (time.monotonic() - t0) * 1000.0


def _fee_floor_half_spread(fair_value_cents: int) -> int:
    """Minimum half-spread that covers per-side maker fees.

    We need the full spread to exceed total round-trip maker fees.
    min_half = ceil(total_maker_fee / order_size) rounded up, plus 1c margin.
    """
    fee = kalshi_maker_fee(MM_ORDER_SIZE, fair_value_cents)
    # fee is total for MM_ORDER_SIZE contracts. Per-contract = fee / order_size.
    # We need half_spread >= per-contract fee (each side earns half the spread).
    per_contract = fee / max(1, MM_ORDER_SIZE)
    return max(1, math.ceil(per_contract) + 1)


def _safe_cents(val) -> Optional[int]:
    """Convert an API price field to integer cents, or None if absent.

    Kalshi's /markets list response omits yes_bid / yes_ask when a side has
    no resting liquidity. Returning 0 for that case silently conflates
    "no book" with "price is 0¢" — which poisons downstream fill-match
    logic (a zero price compares favorably against every real bid). The
    fix: propagate None all the way to the DB (columns are nullable) so
    the matcher can distinguish "unknown" from "observed zero".

    Kalshi's minimum quoted price is 1¢, so a legitimate observation can
    never be 0; any input that parses to 0 is treated as missing too.
    """
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    # Kalshi sometimes returns dollar amounts (0.50) vs cent amounts (50).
    # Normalize to integer cents first.
    cents = int(round(v * 100)) if 0 < v <= 1.0 else int(round(v))
    return cents if cents > 0 else None


def _parse_threshold(ticker: str, title: str) -> Tuple[Optional[float], bool]:
    """Extract temperature threshold and direction from market title or ticker.

    Returns ``(threshold, is_above)`` or ``(None, False)`` on failure.

    **Call path note (2026-04-22):** `_parse_market` now reads direction
    authoritatively from `floor_strike`/`cap_strike` on the Kalshi payload
    and only falls back to this regex when the API doesn't provide them
    (should never happen in steady state). This function is also used
    directly by tests. Do NOT add a hardcoded-direction fallback to this
    function — an earlier version returned `(threshold, True)` from a
    ticker-regex fallback, which silently inverted every Kalshi "<"-title
    market's fair value (poisoned 27k calibration rows). The ticker alone
    (e.g. `-T75`) does not encode direction.
    """
    # Pattern 1: direction keyword BEFORE number
    temp_match = re.search(
        r'(at or above|at or below|above|below|over|under|at least|exceed)\s+(\d+\.?\d*)',
        title,
    )
    if temp_match:
        direction = temp_match.group(1)
        threshold = float(temp_match.group(2))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")
        return threshold, is_above

    # Pattern 2: number BEFORE direction keyword -- "72 or below", "75F or above"
    reverse_match = re.search(
        r'(\d+\.?\d*)\s*\u00b0?\s*[fF]?\s+(or above|or below|and above|and below)',
        title,
    )
    if reverse_match:
        threshold = float(reverse_match.group(1))
        direction = reverse_match.group(2)
        is_above = direction in ("or above", "and above")
        return threshold, is_above

    # Pattern 3: bare "<N" or ">N" (Kalshi's literal-character titles).
    lt_match = re.search(r'<\s*(\d+\.?\d*)', title)
    if lt_match:
        return float(lt_match.group(1)), False
    gt_match = re.search(r'>\s*(\d+\.?\d*)', title)
    if gt_match:
        return float(gt_match.group(1)), True

    # No direction information recoverable. Ticker -T75 carries the
    # threshold value but NOT the direction, so we must not guess.
    return None, False


def clear_market_cache() -> None:
    """Clear the in-memory market cache (useful for testing)."""
    _market_cache.clear()
