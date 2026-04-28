"""NOAA HRRR (High-Resolution Rapid Refresh) source for Kalshi weather markets.

HRRR is a 3km-resolution convection-allowing model updated hourly out to 18-48h.
It's the most accurate near-term temperature forecast available for the US.

We fetch HRRR via Open-Meteo's JSON proxy (`models=gfs_hrrr`) to avoid parsing
GRIB2 binaries. Tradeoff: Open-Meteo introduces a dependency vs native NOMADS
access, but GRIB parsing (pygrib/cfgrib) adds megabytes of binary deps and
non-trivial compilation headaches on a 1GB VPS.

HRRR only covers the first 18-48 hours, so this source returns None for day_idx >= 2.

Free, no auth.

Contract
--------
This module exports two entry points:

* ``get_hrrr_gaussian(ticker, market_data) -> GaussianForecast | None`` — the
  underlying temperature distribution. Used by ``weather_ensemble_v2`` for
  precision-weighted combining.
* ``get_hrrr_estimate(ticker, market_data) -> tuple[prob, tag]`` — back-compat
  shim for v1 callers. Projects the Gaussian onto the specific market's
  threshold/bracket via ``probability_for_market``.
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
from bot.signals.weather_forecast import (
    GaussianForecast,
    hours_until_settlement_end,
    probability_for_market,
)


_HRRR_CACHE_TTL = 1800
_HRRR_MODEL = "gfs_hrrr"


def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _fetch_hrrr_forecast(city_key: str) -> Optional[dict]:
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None
    cache_key = f"hrrr::{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _HRRR_CACHE_TTL:
            return data
    # HRRR is hourly for ~2 days
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&hourly=temperature_2m"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=2"
        f"&models={_HRRR_MODEL}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[hrrr] HTTP {r.status_code}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[hrrr] error: {type(e).__name__}: {e}")
        return None


def _daily_high_from_hourly_hrrr(
    forecast: dict, target_date: str
) -> Optional[float]:
    hourly = forecast.get("hourly", {}) if forecast else {}
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    highs: list[float] = []
    for t, v in zip(times, temps):
        if v is None:
            continue
        # Open-Meteo timezone-adjusted times are ISO local, no offset
        if t[:10] == target_date:
            highs.append(float(v))
    return max(highs) if highs else None


def _hrrr_sigma_for_day(day_idx: int) -> float:
    """HRRR per-day forecast sigma in °F.

    Fixed schedule from the v1 model: 1.2°F at day 0, +0.5°F per day out.
    A3 will replace this with a skill-curve lookup fitted from snapshots;
    the magic number stays here until the learned sigma is fresher and
    demonstrably better.
    """
    return 1.2 + day_idx * 0.5


def get_hrrr_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
    """Return HRRR's temperature distribution for the daily-high settlement.

    Independent of any specific bucket's threshold or bracket — all Gaussian
    sources on the same (city, settlement_date) share one distribution, and
    per-ticker probability is projected at combine time.

    Returns ``None`` if the market isn't weather-type, the city isn't in the
    HRRR catalog, the forecast day is beyond HRRR's 18–48h horizon
    (``day_idx > 1``), or the API fetch failed.
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

    # Threshold parsing is just used for the weather-market sanity gate here;
    # the actual probability projection happens downstream in the ensemble.
    threshold, _ = _parse_threshold(ticker, market_data)
    if threshold is None or threshold < -40 or threshold > 140:
        return None

    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None or day_idx > 1:
        # HRRR only covers today + tomorrow
        return None

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    lst_tz = timezone(timedelta(hours=tz_offset))
    target_date = (datetime.now(lst_tz) + timedelta(days=day_idx)).strftime("%Y-%m-%d")

    forecast = _fetch_hrrr_forecast(city_key)
    if not forecast:
        return None

    forecast_high = _daily_high_from_hourly_hrrr(forecast, target_date)
    if forecast_high is None:
        return None

    sigma_f = _hrrr_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    from bot.signals.sources._freshness import hrrr_latest_issued_at
    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="hrrr",
        source_tag=f"hrrr:{city_key}_{target_date}",
        issued_at=hrrr_latest_issued_at(),
    )


def get_hrrr_estimate(ticker: str, market_data: dict) -> tuple:
    """V1 probability entry point — logistic CDF, unchanged.

    We intentionally do NOT route this through ``probability_for_market``
    (which uses Normal CDF). Normal vs logistic differ by ~10pp at ±1σ,
    so swapping would silently shift v1 Brier scores. The new Gaussian
    contract (``get_hrrr_gaussian``) uses Normal as the correct
    distributional assumption; A3 will fit empirical σ's that match it.
    Until then v1 callers keep v1 behaviour.
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

    threshold, is_above = _parse_threshold(ticker, market_data)
    if threshold is None or threshold < -40 or threshold > 140:
        return None, None

    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None or day_idx > 1:
        return None, None

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    lst_tz = timezone(timedelta(hours=tz_offset))
    target_date = (datetime.now(lst_tz) + timedelta(days=day_idx)).strftime("%Y-%m-%d")

    forecast = _fetch_hrrr_forecast(city_key)
    if not forecast:
        return None, None

    forecast_high = _daily_high_from_hourly_hrrr(forecast, target_date)
    if forecast_high is None:
        return None, None

    forecast_sigma = _hrrr_sigma_for_day(day_idx)

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

    print(
        f"[hrrr] {city_key} day={day_idx} high={forecast_high:.0f}°F "
        f"threshold={threshold}°F sigma={forecast_sigma:.1f}°F -> {prob:.3f}"
    )
    return prob, f"hrrr:{city_key}_{target_date}"
