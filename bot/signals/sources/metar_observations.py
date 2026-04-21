"""Real-time METAR weather observations for Kalshi high-temperature markets.

Unlike Open-Meteo and Tomorrow.io (which provide FORECASTS), this source uses
live Aviation Weather METAR observations from the actual NWS settlement stations.
As the day progresses, uncertainty shrinks -- if the running daily high already
exceeds the threshold, probability goes near 1.0 regardless of what forecasts say.

API: https://aviationweather.gov/api/data/metar (free, no auth, 1-5 min updates)
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.daemon.stations import (
    STATION_BY_SERIES,
    lst_offset_for_station,
    station_for_ticker,
)
from bot.db import get_connection, kv_get, kv_set


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# Station list driven by the canonical registry (T1.1) — the daemon poller,
# signal source, MADIS basket, and smart_gates all resolve to the same
# primary ICAOs. Sorted for deterministic URL for cache-hit stability.
_METAR_URL = (
    "https://aviationweather.gov/api/data/metar"
    "?ids=" + ",".join(sorted(s.icao for s in STATION_BY_SERIES.values()))
    + "&format=json"
)
_METAR_CACHE_KEY = "metar_obs"
_METAR_CACHE_TTL = 300  # 5 minutes

# CRITICAL: Settlement uses LOCAL STANDARD TIME (LST), NOT daylight saving.
# During DST months NYC is UTC-4 locally, but settlement boundary is always UTC-5.
# Offsets come from the canonical registry — do NOT maintain a parallel table.

# KV cache TTL for daily high tracking (25 hours -- covers full settlement day + buffer)
_DAILY_HIGH_TTL = 90_000


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard logistic CDF for temperature probability estimation."""
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _get_lst_now(station: str) -> datetime:
    """Return current datetime in the station's LOCAL STANDARD TIME (fixed offset)."""
    offset_hours = lst_offset_for_station(station)
    lst_tz = timezone(timedelta(hours=offset_hours))
    return datetime.now(lst_tz)


def _get_lst_date(station: str) -> str:
    """Return today's date string (YYYY-MM-DD) in the station's LST."""
    return _get_lst_now(station).strftime("%Y-%m-%d")


def _hours_remaining_in_settlement_day(station: str) -> float:
    """Hours remaining until 11:59 PM LST (settlement day end).

    Settlement period: 12:00 AM - 11:59 PM LST.
    """
    now_lst = _get_lst_now(station)
    end_of_day = now_lst.replace(hour=23, minute=59, second=59, microsecond=0)
    remaining = (end_of_day - now_lst).total_seconds() / 3600.0
    return max(0.0, remaining)


def _parse_threshold_from_market(ticker: str, title: str) -> tuple[float | None, bool]:
    """Extract temperature threshold and direction from market title or ticker.

    Returns (threshold, is_above) or (None, True) on failure.

    Examples:
        "Will the high temperature in NYC be 75 deg F or above?" -> (75, True)
        "72° or below"                                           -> (72, False)
        Ticker KXHIGHNY-26APR14-T75 -> (75, True)
        Ticker KXHIGHNY-26APR14-B74 -> (74, True)  # brackets handle direction separately
    """
    # Pattern 1: direction keyword BEFORE number — "at or above 75", "below 68"
    temp_match = re.search(
        r'(at or above|at or below|above|below|over|under|at least|exceed)\s+(\d+\.?\d*)',
        title,
    )
    if temp_match:
        direction = temp_match.group(1)
        threshold = float(temp_match.group(2))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")
        return threshold, is_above

    # Pattern 2: number BEFORE direction keyword — "72° or below", "75°F or above"
    # This is common in Kalshi contract labels (e.g., "72° or below").
    reverse_match = re.search(
        r'(\d+\.?\d*)\s*°?\s*[fF]?\s+(or above|or below|and above|and below)',
        title,
    )
    if reverse_match:
        threshold = float(reverse_match.group(1))
        direction = reverse_match.group(2)
        is_above = direction in ("or above", "and above")
        return threshold, is_above

    # Try ticker: -T75, -B74
    tick_match = re.search(r'-[TB](-?\d+\.?\d*)', ticker)
    if tick_match:
        return float(tick_match.group(1)), True  # default to "above" for tickers

    # Try bare number near degree symbol: "75°F"
    deg_match = re.search(r'(\d+\.?\d*)\s*°\s*[fF]', title)
    if deg_match:
        # Check surrounding context for direction
        full_text = title.lower()
        threshold = float(deg_match.group(1))
        if 'or below' in full_text or 'under' in full_text or 'at most' in full_text:
            return threshold, False
        return threshold, True  # default to "above"

    return None, True


# ══════════════════════════════════════════════════════════════════════════════
# METAR API fetch
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_metar_data() -> list[dict] | None:
    """Fetch METAR observations for all tracked stations.

    Returns JSON array on success, None on failure.
    Uses in-memory _CACHE with 5-minute TTL.
    """
    now = time.time()
    if _METAR_CACHE_KEY in _CACHE:
        cached_data, cached_ts = _CACHE[_METAR_CACHE_KEY]
        if now - cached_ts < _METAR_CACHE_TTL:
            return cached_data

    try:
        rate_limit_wait(_METAR_URL)
        r = requests.get(_METAR_URL, timeout=8, headers={
            "User-Agent": "KalshiTradingBot/1.0 (contact: bot@example.com)",
            "Accept": "application/json",
        })
        if r.status_code != 200:
            print(f"[metar] API HTTP {r.status_code}")
            return None
        data = r.json()
        if not isinstance(data, list):
            print(f"[metar] Unexpected response type: {type(data).__name__}")
            return None
        _CACHE[_METAR_CACHE_KEY] = (data, now)
        return data
    except Exception as e:
        print(f"[metar] API error: {type(e).__name__}: {e}")
        return None


def _extract_station_obs(metar_data: list[dict], station: str) -> dict | None:
    """Find the observation record for a specific station ID."""
    for obs in metar_data:
        if obs.get("icaoId") == station or obs.get("stationId") == station:
            return obs
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Daily high tracker
# ══════════════════════════════════════════════════════════════════════════════

def _update_running_daily_high(station: str, temp_f: float, obs_time: str) -> dict:
    """Update the running daily high for a station using persistent kv_cache.

    Returns the updated daily-high record:
        {"high_f": float, "last_obs_time": str, "obs_count": int}
    """
    date_lst = _get_lst_date(station)
    kv_key = f"metar_daily_high_{station}_{date_lst}"

    try:
        conn = get_connection()
    except RuntimeError:
        # DB not initialized -- return ephemeral record
        return {"high_f": temp_f, "last_obs_time": obs_time, "obs_count": 1}

    existing = kv_get(conn, kv_key)

    if existing is None:
        record = {"high_f": temp_f, "last_obs_time": obs_time, "obs_count": 1}
    else:
        record = existing
        record["obs_count"] = record.get("obs_count", 0) + 1
        record["last_obs_time"] = obs_time
        if temp_f > record.get("high_f", -999):
            record["high_f"] = temp_f

    kv_set(conn, kv_key, record, _DAILY_HIGH_TTL)
    return record


# ══════════════════════════════════════════════════════════════════════════════
# Forecast high integration
# ══════════════════════════════════════════════════════════════════════════════

def _get_forecast_high(station: str, running_high: float) -> float:
    """Retrieve the forecast high for today from kv_cache.

    Falls back to running_high + 5 deg F if no forecast is cached.
    """
    date_lst = _get_lst_date(station)
    forecast_key = f"metar_forecast_high_{station}_{date_lst}"

    try:
        conn = get_connection()
        cached_forecast = kv_get(conn, forecast_key)
        if cached_forecast is not None and isinstance(cached_forecast, (int, float)):
            return float(cached_forecast)
    except RuntimeError:
        pass

    # Conservative fallback: assume some warming potential remains
    return running_high + 5.0


# ══════════════════════════════════════════════════════════════════════════════
# Probability model
# ══════════════════════════════════════════════════════════════════════════════

def _compute_probability(
    running_high: float,
    threshold: float,
    hours_left: float,
    forecast_high: float,
) -> float:
    """Compute P(daily high >= threshold) given observations so far.

    Key insight: as the day progresses and more observations arrive, our
    uncertainty about the eventual daily high shrinks.

    Model:
      - If running_high already >= threshold: near certainty (0.95-0.98),
        with small allowance for NWS reporting adjustments.
      - Otherwise: estimate expected eventual high and remaining uncertainty.
        mu  = max(running_high, weighted blend of forecast and running high)
        sigma decreases as hours_left decreases (more certainty late in day)
    """
    # Already observed above threshold -- near certainty
    if running_high >= threshold:
        # Slight uncertainty for potential NWS reporting discrepancy (rounding, station drift)
        margin = running_high - threshold
        if margin >= 3.0:
            return 0.98
        elif margin >= 1.0:
            return 0.96
        else:
            return 0.95

    # Not yet at threshold -- model remaining warming potential
    gap = threshold - running_high

    # Expected eventual high: blend forecast with observations based on time of day
    # Early in the day (many hours left), trust the forecast more.
    # Late in the day (few hours left), trust the running high more.
    total_day_hours = 24.0
    day_fraction_elapsed = max(0.0, min(1.0, 1.0 - hours_left / total_day_hours))

    # Weighted mu: early -> forecast dominates, late -> running high dominates
    if hours_left > 0:
        forecast_weight = max(0.1, 1.0 - day_fraction_elapsed)
        obs_weight = 1.0 - forecast_weight
        expected_eventual_high = (
            forecast_weight * max(forecast_high, running_high)
            + obs_weight * running_high
        )
    else:
        # Day is over -- the running high IS the final high
        expected_eventual_high = running_high

    # Sigma: uncertainty decreases as hours remaining decrease
    # 12+ hours: ~2.0 deg F (full forecast uncertainty)
    #  6 hours:  ~1.5 deg F
    #  2 hours:  ~0.8 deg F
    #  <1 hour:  ~0.3 deg F
    #  0 hours:  ~0.1 deg F (essentially zero -- day is done)
    if hours_left <= 0:
        sigma = 0.1
    elif hours_left < 1:
        sigma = 0.3
    elif hours_left < 2:
        sigma = 0.5 + (hours_left - 1.0) * 0.3  # 0.5 to 0.8
    elif hours_left < 6:
        sigma = 0.8 + (hours_left - 2.0) * 0.175  # 0.8 to 1.5
    elif hours_left < 12:
        sigma = 1.5 + (hours_left - 6.0) * 0.083  # 1.5 to 2.0
    else:
        sigma = 2.0

    # Probability = P(eventual_high >= threshold) via logistic CDF
    # _logistic_cdf(threshold, mu, sigma) gives P(X <= threshold),
    # so P(X >= threshold) = 1 - _logistic_cdf(threshold, mu, sigma)
    prob = 1.0 - _logistic_cdf(threshold, expected_eventual_high, sigma)

    # Clamp to valid range
    return max(0.02, min(0.98, prob))


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def get_metar_observation_estimate(ticker: str, market_data: dict) -> tuple:
    """Estimate probability for Kalshi high-temperature markets using live METAR observations.

    Returns (probability, source_label) or (None, None) if not applicable.
    """
    ticker_upper = ticker.upper() if ticker else ""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # ── Match ticker to station (via canonical registry) ──
    ws = station_for_ticker(ticker_upper)
    if ws is None:
        return None, None
    station = ws.icao

    # ── Parse threshold and direction ──
    threshold, is_above = _parse_threshold_from_market(ticker, title)
    if threshold is None:
        return None, None

    # Sanity check
    if threshold < -40 or threshold > 140:
        return None, None

    # ── Fetch METAR data ──
    metar_data = _fetch_metar_data()
    if metar_data is None:
        return None, None

    obs = _extract_station_obs(metar_data, station)
    if obs is None:
        print(f"[metar] Station {station} not found in METAR response")
        return None, None

    # ── Extract temperature ──
    temp_c = obs.get("temp")
    if temp_c is None:
        print(f"[metar] No temperature in METAR for {station}")
        return None, None

    try:
        temp_c = float(temp_c)
    except (TypeError, ValueError):
        print(f"[metar] Invalid temperature value for {station}: {temp_c}")
        return None, None

    temp_f = temp_c * 9.0 / 5.0 + 32.0
    obs_time = obs.get("reportTime") or obs.get("obsTime") or ""

    # ── Update running daily high ──
    daily_record = _update_running_daily_high(station, temp_f, obs_time)
    high_f = daily_record["high_f"]

    # ── Compute hours remaining in settlement day ──
    hours_left = _hours_remaining_in_settlement_day(station)

    # ── Get forecast high for remaining-potential estimation ──
    forecast_high = _get_forecast_high(station, high_f)

    # ── Check for bracket market (different probability model) ──
    is_bracket = "-B" in ticker_upper
    if is_bracket:
        # Bracket: P(floor <= daily_high < cap)
        bracket_floor = threshold
        bracket_cap = threshold + 2.0  # Kalshi weather brackets are 2°F wide

        # Best: use API-provided strikes
        _api_floor = market_data.get("floor_strike") if market_data else None
        _api_cap = market_data.get("cap_strike") if market_data else None
        if _api_floor is not None and _api_cap is not None:
            try:
                bracket_floor = float(_api_floor)
                bracket_cap = float(_api_cap)
            except (ValueError, TypeError):
                pass
        else:
            # Fallback: parse from title — "74 to 75", "74-75", "74°F and 75°F"
            range_match = re.search(
                r'(\d+\.?\d*)\s*\u00b0?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)', title,
            )
            if range_match:
                bracket_floor = float(range_match.group(1))
                bracket_cap = float(range_match.group(2))

        if high_f >= bracket_cap:
            # Running high already exceeds the bracket ceiling -- probability the
            # eventual high lands *inside* this bracket depends on whether it can drop
            # back (it cannot -- daily HIGH only goes up). So P(in bracket) is low
            # if we are already past it.
            prob = 0.02
        elif high_f >= bracket_floor:
            # Currently inside the bracket -- depends on whether we stay or move past
            prob_below_cap = _logistic_cdf(bracket_cap, max(forecast_high, high_f),
                                           _sigma_for_hours(hours_left))
            prob = max(0.02, min(0.98, prob_below_cap))
        else:
            # Below bracket -- need to reach it but not exceed it
            sigma = _sigma_for_hours(hours_left)
            mu = max(forecast_high, high_f)
            cdf_upper = _logistic_cdf(bracket_cap, mu, sigma)
            cdf_lower = _logistic_cdf(bracket_floor, mu, sigma)
            prob = max(0.02, min(0.98, cdf_upper - cdf_lower))

        print(f"[metar] {station}: obs={temp_f:.0f}\u00b0F running_high={high_f:.0f}\u00b0F "
              f"bracket=[{bracket_floor:.0f},{bracket_cap:.0f}]\u00b0F "
              f"hrs_left={hours_left:.1f} \u2192 {prob:.3f}")
        return prob, f"metar:{station}"

    # ── Threshold market ──
    # _compute_probability returns P(daily_high >= threshold).
    # For "above" markets (YES = high >= T): P(YES) = prob_above.
    # For "below" markets (YES = high <= T): P(YES) = 1 - prob_above.
    prob_above = _compute_probability(high_f, threshold, hours_left, forecast_high)

    if is_above:
        prob = prob_above
        direction_label = "above"
    else:
        prob = max(0.02, min(0.98, 1.0 - prob_above))
        direction_label = "below"

    print(f"[metar] {station}: obs={temp_f:.0f}\u00b0F running_high={high_f:.0f}\u00b0F "
          f"threshold={threshold}\u00b0F ({direction_label}) "
          f"hrs_left={hours_left:.1f} \u2192 {prob:.3f}")
    return prob, f"metar:{station}"


def _sigma_for_hours(hours_left: float) -> float:
    """Compute uncertainty sigma based on hours remaining in settlement day.

    Same model as _compute_probability but extracted for bracket reuse.
    """
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
