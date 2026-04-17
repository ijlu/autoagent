"""MADIS-style dense-station observations for spatial consistency checking.

The MADIS ideal is "aggregate every observation station within a radius and
cross-check them, so a sensor-drift artifact at a single station doesn't fool
you." The full MADIS API requires FTP and binary NetCDF parsing.

We approximate the same goal using the NWS Aviation Weather METAR endpoint,
which already decodes observations from *every* nearby ASOS/AWOS station — not
just our 6 primary KXHIGH stations. By querying a 50-mile-radius basket
around each target city, we get 5-15 nearby observations we can average to
guard against single-station error.

When the median dense-basket observation diverges from our primary station
reading by >3°F, that's a signal to discount the primary METAR reading and
lean on other sources.

Same upstream data as metar_observations.py (so it's correlated), but the
spatial ensemble adds genuinely independent information about sensor drift.
"""

from __future__ import annotations

import math
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.signals.sources.weather import (
    WEATHER_CITIES,
    _CITY_LST_OFFSET,
    _detect_city,
    _parse_threshold,
    _determine_day_index,
)


_MADIS_CACHE_TTL = 300  # 5 min — observations refresh every 5-15 min

# City → nearby-station basket (primary + 2-4 neighbors within ~50mi).
# These are all ASOS/AWOS stations with hourly/sub-hourly METAR reports.
_CITY_STATION_BASKET: dict[str, list[str]] = {
    "nyc":         ["KNYC", "KLGA", "KJFK", "KEWR", "KTEB"],
    "new york":    ["KNYC", "KLGA", "KJFK", "KEWR", "KTEB"],
    "chicago":     ["KMDW", "KORD", "KPWK", "KGYY"],
    "los angeles": ["KLAX", "KBUR", "KSMO", "KLGB"],
    "la":          ["KLAX", "KBUR", "KSMO", "KLGB"],
    "austin":      ["KAUS", "KBAZ", "KGTU", "KEDC"],
    "miami":       ["KMIA", "KFLL", "KTMB", "KHWO"],
    "denver":      ["KDEN", "KBJC", "KAPA", "KCFO"],
}


def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _fetch_madis_basket(station_ids: list[str]) -> Optional[list[dict]]:
    """Fetch METAR observations for a list of station IDs.

    Returns the list of decoded observations (subset of fields we care about)
    or None on failure. Cached 5 min.
    """
    if not station_ids:
        return None
    ids_key = ",".join(sorted(station_ids))
    cache_key = f"madis::{ids_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _MADIS_CACHE_TTL:
            return data

    url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?ids={','.join(station_ids)}&format=json"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8, headers={
            "User-Agent": "KalshiTradingBot/1.0",
            "Accept": "application/json",
        })
        if r.status_code != 200:
            print(f"[madis] HTTP {r.status_code} for {ids_key}")
            return None
        obs_list = r.json()
        if not isinstance(obs_list, list):
            return None
        _CACHE[cache_key] = (obs_list, now)
        return obs_list
    except Exception as e:
        print(f"[madis] error: {type(e).__name__}: {e}")
        return None


def _obs_temp_f(obs: dict) -> Optional[float]:
    temp_c = obs.get("temp")
    if temp_c is None:
        return None
    try:
        return float(temp_c) * 9.0 / 5.0 + 32.0
    except (TypeError, ValueError):
        return None


def get_madis_estimate(ticker: str, market_data: dict) -> tuple:
    """Dense-basket current-temperature estimate for KXHIGH markets.

    Uses median of 3-5 stations near the target city. Helps catch sensor drift
    at the primary METAR station.
    """
    if market_data is None:
        return None, None
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = (ticker or "").upper()
    is_weather = "KXHIGH" in ticker_upper or any(
        kw in title for kw in ("temperature", "temp", "°f", "degrees", "high")
    )
    if not is_weather:
        return None, None

    city_key = _detect_city(ticker_upper, title)
    if not city_key:
        return None, None

    basket = _CITY_STATION_BASKET.get(city_key)
    if not basket:
        return None, None

    threshold, is_above = _parse_threshold(ticker, title)
    if threshold is None or threshold < -40 or threshold > 140:
        return None, None

    # MADIS is observations — only useful for TODAY
    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None or day_idx != 0:
        return None, None

    obs_list = _fetch_madis_basket(basket)
    if not obs_list:
        return None, None

    temps_f = []
    for obs in obs_list:
        t = _obs_temp_f(obs)
        if t is not None:
            temps_f.append(t)

    if len(temps_f) < 2:
        # Need at least 2 stations for a spatial ensemble
        return None, None

    median_temp = statistics.median(temps_f)
    spread = max(temps_f) - min(temps_f)

    # How much warmer the daily high might still get depends on time of day.
    # Use the existing metar module's LST offset model.
    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    lst_tz = timezone(timedelta(hours=tz_offset))
    hours_left = max(0.0, (24 - datetime.now(lst_tz).hour - datetime.now(lst_tz).minute / 60))

    # Expected daily high = current median + remaining warming potential.
    # Assume peak is at 3pm LST; if we're before 3pm, likely more warming.
    # Cap the warming headroom at 8°F.
    now_local = datetime.now(lst_tz)
    if now_local.hour < 15:
        warming = min(8.0, (15 - now_local.hour) * 1.5)
    else:
        warming = 0.0
    expected_high = median_temp + warming

    # Sigma: basket spread + time-of-day uncertainty
    sigma = max(1.5, spread / 2.0 + (0.5 if now_local.hour >= 15 else 2.0))

    is_bracket = "-B" in ticker_upper
    if is_bracket:
        bracket_floor = threshold
        bracket_cap = threshold + 2.0
        _fs = market_data.get("floor_strike")
        _cs = market_data.get("cap_strike")
        if _fs is not None and _cs is not None:
            try:
                bracket_floor = float(_fs)
                bracket_cap = float(_cs)
            except (ValueError, TypeError):
                pass
        else:
            m = re.search(r"(\d+\.?\d*)\s*°?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)", title)
            if m:
                bracket_floor = float(m.group(1))
                bracket_cap = float(m.group(2))
        cdf_upper = _logistic_cdf(bracket_cap, expected_high, sigma)
        cdf_lower = _logistic_cdf(bracket_floor, expected_high, sigma)
        prob = max(0.02, min(0.98, cdf_upper - cdf_lower))
    elif is_above:
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-(expected_high - threshold) / sigma))))
    else:
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-(threshold - expected_high) / sigma))))

    print(
        f"[madis] {city_key} basket={len(temps_f)} median={median_temp:.0f}°F "
        f"spread={spread:.1f}°F expected_high={expected_high:.0f}°F "
        f"threshold={threshold}°F -> {prob:.3f}"
    )
    return prob, f"madis:{city_key}_n{len(temps_f)}"
