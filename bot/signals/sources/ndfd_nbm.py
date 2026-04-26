"""NOAA NBM (National Blend of Models) source for Kalshi weather markets.

NBM is NOAA's weighted ensemble of ~30 deterministic + probabilistic forecast
models. It's widely considered the most skillful single operational forecast
product for 1-10 day temperature forecasts.

We fetch NBM via Open-Meteo's JSON proxy (`models=gfs_seamless` falls back to
NBM at short ranges; `gfs_hrrr` is handled separately). This avoids parsing
GRIB2 binaries. Open-Meteo serves NBM data under the `models=bom_access_global`
or `gfs_seamless` path depending on lead time; we use `gfs_seamless` which
NBM-backs short-range fields. The server key is `model=gfs_seamless`.

Free, no auth, 10k calls/day per IP — well under our budget.
"""

from __future__ import annotations

import math
import re
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
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


_NBM_CACHE_TTL = 1800  # 30 min
_NBM_MODEL = "gfs_seamless"  # Open-Meteo's NBM-backed seamless forecast


def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _fetch_nbm_forecast(city_key: str) -> Optional[dict]:
    """Fetch NBM daily high/low for the next 7 days."""
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None

    cache_key = f"nbm::{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _NBM_CACHE_TTL:
            return data

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7"
        f"&models={_NBM_MODEL}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[nbm] HTTP {r.status_code}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[nbm] error: {type(e).__name__}: {e}")
        return None


def _nbm_sigma_for_day(day_idx: int) -> float:
    """NBM per-day forecast sigma in °F. 1.8°F at day 0, +0.5°F per day.

    Empirically NBM is slightly noisier than HRRR for short range but
    extends cleanly through 7 days. A3 will replace these with fitted σ's.
    """
    return 1.8 + day_idx * 0.5


def get_nbm_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
    """Return NBM's temperature distribution for the settlement day.

    Threshold/bracket-independent; the ensemble projects per-market.
    """
    if market_data is None:
        return None
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = (ticker or "").upper()
    is_weather = "KXHIGH" in ticker_upper or any(
        kw in title for kw in ("temperature", "temp", "°f", "degrees", "high")
    )
    if not is_weather:
        return None

    city_key = _detect_city(ticker_upper, title)
    if not city_key:
        return None

    threshold, _ = _parse_threshold(ticker, market_data)
    if threshold is None or threshold < -40 or threshold > 140:
        return None

    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None:
        return None

    forecast = _fetch_nbm_forecast(city_key)
    if not forecast:
        return None

    daily = forecast.get("daily", {})
    highs = daily.get("temperature_2m_max", [])
    dates = daily.get("time", [])
    if day_idx >= len(highs):
        return None
    forecast_high = highs[day_idx]
    if forecast_high is None:
        return None

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    date_label = dates[day_idx] if day_idx < len(dates) else "?"
    sigma_f = _nbm_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="nbm",
        source_tag=f"nbm:{city_key}_{date_label}",
    )


def get_nbm_estimate(ticker: str, market_data: dict) -> tuple:
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

    threshold, is_above = _parse_threshold(ticker, market_data)
    if threshold is None or threshold < -40 or threshold > 140:
        return None, None

    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None:
        return None, None

    forecast = _fetch_nbm_forecast(city_key)
    if not forecast:
        return None, None
    daily = forecast.get("daily", {})
    highs = daily.get("temperature_2m_max", [])
    dates = daily.get("time", [])
    if day_idx >= len(highs):
        return None, None
    forecast_high = highs[day_idx]
    if forecast_high is None:
        return None, None

    forecast_sigma = _nbm_sigma_for_day(day_idx)  # NBM slightly more skillful than raw Open-Meteo

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
        cdf_upper = _logistic_cdf(bracket_cap, forecast_high, forecast_sigma)
        cdf_lower = _logistic_cdf(bracket_floor, forecast_high, forecast_sigma)
        prob = max(0.02, min(0.98, cdf_upper - cdf_lower))
    elif is_above:
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-(forecast_high - threshold) / forecast_sigma))))
    else:
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-(threshold - forecast_high) / forecast_sigma))))

    date_label = dates[day_idx] if day_idx < len(dates) else "?"
    print(
        f"[nbm] {city_key} day={day_idx} high={forecast_high:.0f}°F "
        f"threshold={threshold}°F sigma={forecast_sigma:.1f}°F -> {prob:.3f}"
    )
    return prob, f"nbm:{city_key}_{date_label}"
