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

import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
)
from bot.daemon.locks import DB_WRITE_LOCK

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
    yes_bid: int  # cents
    yes_ask: int  # cents
    volume: int
    close_time: str


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
    """Compute uncertainty sigma based on hours remaining in settlement day."""
    if hours_left <= 0:
        return 0.1
    elif hours_left < 1:
        return 0.3
    elif hours_left < 2:
        return 0.5 + (hours_left - 1.0) * 0.3
    elif hours_left < 6:
        return 0.8 + (hours_left - 2.0) * 0.175
    elif hours_left < 12:
        return 1.5 + (hours_left - 6.0) * 0.083
    else:
        return 2.0


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
        market_yes_bid: int,
        market_yes_ask: int,
        inventory: int,
        gate_should_quote: bool,
        gate_reason: Optional[str],
        gate_spread_mult: float,
        latency_ms: float,
        live_mode: bool,
        live_order_id_bid: Optional[str] = None,
        live_order_id_ask: Optional[str] = None,
        order_size: Optional[int] = None,
    ) -> Optional[int]:
        """Insert one row into weather_mm_shadow. Returns rowid on success."""
        now = time.time()
        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        market_mid = (
            (market_yes_bid + market_yes_ask) // 2
            if market_yes_bid and market_yes_ask else None
        )
        try:
            with DB_WRITE_LOCK:
                cur = self.conn.execute(
                    """INSERT INTO weather_mm_shadow
                    (ts_unix, ts_iso, ticker, series, station,
                     old_temp_f, new_temp_f, running_high_f, forecast_high_f,
                     hours_left, trajectory_f_per_hr,
                     fair_value_cents, proposed_bid_cents, proposed_ask_cents,
                     half_spread_cents, market_yes_bid, market_yes_ask, market_mid,
                     inventory, gate_should_quote, gate_reason, gate_spread_mult,
                     latency_ms, live_mode,
                     live_order_id_bid, live_order_id_ask, order_size)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        int(now), now_iso, ticker, series, station,
                        old_temp_f, new_temp_f, running_high_f, forecast_high_f,
                        hours_left, trajectory_f_per_hr,
                        fair_value_cents, bid_cents, ask_cents,
                        half_spread_cents, market_yes_bid, market_yes_ask, market_mid,
                        inventory, 1 if gate_should_quote else 0, gate_reason,
                        gate_spread_mult, latency_ms, 1 if live_mode else 0,
                        live_order_id_bid, live_order_id_ask, order_size,
                    ),
                )
                self.conn.commit()
                return cur.lastrowid
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

        # -- Compute fair value --
        fair_value_cents = self._compute_fair_value(
            market, running_high_f, forecast_high_f, hours_left,
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
            # Threshold market
            threshold, is_above = _parse_threshold(ticker, title)
            if threshold is None:
                return None

        # Parse order book snapshot
        yes_bid = _safe_cents(m.get("yes_bid"))
        yes_ask = _safe_cents(m.get("yes_ask"))

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
        )

    # ------------------------------------------------------------------
    # Fair value computation
    # ------------------------------------------------------------------

    def _compute_fair_value(
        self,
        market: WeatherMarket,
        running_high_f: float,
        forecast_high_f: float,
        hours_left: float,
    ) -> int:
        """Compute fair value in cents (2-98) for a weather market.

        Uses the logistic CDF model from metar_observations.py.
        """
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

    def _cancel_stale_orders(self, ticker: str) -> int:
        """Cancel all resting orders for a ticker. Returns count cancelled."""
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
        """Log requote to the opportunity_log table (if it exists)."""
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO opportunity_log
                   (timestamp, ticker, action, data)
                   VALUES (?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    result.ticker,
                    "wx_requote",
                    str({
                        "fair_value": result.fair_value_cents,
                        "posted": result.orders_posted,
                        "cancelled": result.orders_cancelled,
                        "latency_ms": round(result.latency_ms, 1),
                    }),
                ),
            )
            self.conn.commit()
        except Exception:
            # opportunity_log may not exist -- that is fine.
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


def _safe_cents(val) -> int:
    """Convert an API price field to integer cents, handling None / float / str."""
    if val is None:
        return 0
    try:
        v = float(val)
        # Kalshi sometimes returns dollar amounts (0.50) vs cent amounts (50)
        if 0 < v <= 1.0:
            return int(round(v * 100))
        return int(round(v))
    except (TypeError, ValueError):
        return 0


def _parse_threshold(ticker: str, title: str) -> Tuple[Optional[float], bool]:
    """Extract temperature threshold and direction from market title or ticker.

    Returns ``(threshold, is_above)`` or ``(None, True)`` on failure.
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

    # Try ticker: -T75
    tick_match = re.search(r'-[Tt](-?\d+\.?\d*)', ticker)
    if tick_match:
        return float(tick_match.group(1)), True

    return None, True


def clear_market_cache() -> None:
    """Clear the in-memory market cache (useful for testing)."""
    _market_cache.clear()
