"""NWS Point / Hourly Forecast source for Kalshi weather markets.

Fetches hourly temperature forecasts from the National Weather Service's
official API at api.weather.gov. Provides one more independent forecast
channel to cross-check Open-Meteo / Tomorrow.io / HRRR / NBM.

Two-step fetch (per NWS API contract):
  1. GET /points/{lat},{lon}  →  returns forecastHourly URL + grid cell ID
  2. GET {forecastHourly}     →  returns 156 hourly temperature/wind entries

We cache the forecastHourly URL for a day (it's stable per lat/lon) so a
typical cycle makes only one HTTP call per city.

Free, no auth. Rate limit: "generous" but undocumented — we respect a 5-min
TTL on hourly forecasts to stay well under any plausible cap.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.db import get_connection, kv_get, kv_set
from bot.signals.sources.weather import (
    WEATHER_CITIES,
    _CITY_LST_OFFSET,
    _detect_city,
    _parse_threshold,
    _determine_day_index,
)
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


_NWS_USER_AGENT = "KalshiTradingBot/1.0 (contact: joshlu@a16z.com)"
_NWS_HEADERS = {"User-Agent": _NWS_USER_AGENT, "Accept": "application/geo+json"}

_NWS_POINT_CACHE_TTL = 86400   # 24h — grid cell mapping is stable
_NWS_FORECAST_CACHE_TTL = 1800  # 30 min — hourly forecasts are updated ~hourly


def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _resolve_grid_url(lat: float, lon: float) -> Optional[str]:
    """Return the hourly-forecast URL for a lat/lon.

    NWS's /points endpoint returns the grid cell + forecast URLs. We cache
    the resolved URL in kv_cache (24h) so we don't hammer /points on every
    cycle."""
    kv_key = f"nws_grid_{lat:.2f}_{lon:.2f}"
    try:
        conn = get_connection()
        cached = kv_get(conn, kv_key)
        if isinstance(cached, str) and cached.startswith("http"):
            return cached
    except RuntimeError:
        conn = None

    url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8, headers=_NWS_HEADERS)
        if r.status_code != 200:
            print(f"[nws_point] /points HTTP {r.status_code} for {lat},{lon}")
            return None
        data = r.json()
        forecast_hourly = data.get("properties", {}).get("forecastHourly")
        if not forecast_hourly:
            return None
        if conn is not None:
            try:
                kv_set(conn, kv_key, forecast_hourly, _NWS_POINT_CACHE_TTL)
            except Exception:
                pass
        return forecast_hourly
    except Exception as e:
        print(f"[nws_point] /points error: {type(e).__name__}: {e}")
        return None


def _fetch_hourly_forecast(forecast_url: str) -> Optional[list[dict]]:
    """Fetch the hourly forecast. Returns list of hourly period dicts."""
    now = time.time()
    cache_key = f"nws_hourly::{forecast_url}"
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _NWS_FORECAST_CACHE_TTL:
            return data

    try:
        rate_limit_wait(forecast_url)
        r = requests.get(forecast_url, timeout=10, headers=_NWS_HEADERS)
        if r.status_code != 200:
            print(f"[nws_point] /forecastHourly HTTP {r.status_code}")
            return None
        periods = r.json().get("properties", {}).get("periods", [])
        if not periods:
            return None
        _CACHE[cache_key] = (periods, now)
        return periods
    except Exception as e:
        print(f"[nws_point] forecast error: {type(e).__name__}: {e}")
        return None


def _daily_high_from_hourly(
    periods: list[dict], target_date: str, tz_offset_hours: int
) -> Optional[float]:
    """Extract the forecast daily high (°F) for a specific date in local time.

    target_date: YYYY-MM-DD in the station's LST
    tz_offset_hours: e.g. -5 for EST
    """
    lst_tz = timezone(timedelta(hours=tz_offset_hours))
    highs: list[float] = []
    for p in periods:
        start = p.get("startTime")
        temp = p.get("temperature")
        unit = p.get("temperatureUnit", "F")
        if start is None or temp is None:
            continue
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        local_date = dt.astimezone(lst_tz).strftime("%Y-%m-%d")
        if local_date != target_date:
            continue
        t_f = float(temp) if unit.upper() == "F" else float(temp) * 9.0 / 5.0 + 32.0
        highs.append(t_f)
    return max(highs) if highs else None


def _nws_point_sigma_for_day(day_idx: int) -> float:
    """NWS point-forecast sigma. Same schedule as Open-Meteo — both ingest
    similar model data. A3 replaces with fitted σ."""
    return 2.0 + day_idx * 0.6


def get_nws_point_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
    """Return NWS hourly forecast's temperature distribution for the
    settlement day. Threshold/bracket-independent."""
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
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None

    threshold, _ = _parse_threshold(ticker, title)
    if threshold is None or threshold < -40 or threshold > 140:
        return None

    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None:
        return None

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    lst_tz = timezone(timedelta(hours=tz_offset))
    target_date = (datetime.now(lst_tz) + timedelta(days=day_idx)).strftime("%Y-%m-%d")

    forecast_url = _resolve_grid_url(city["lat"], city["lon"])
    if forecast_url is None:
        return None
    periods = _fetch_hourly_forecast(forecast_url)
    if not periods:
        return None

    forecast_high = _daily_high_from_hourly(periods, target_date, tz_offset)
    if forecast_high is None:
        return None

    sigma_f = _nws_point_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="nws_point",
        source_tag=f"nws_point:{city_key}_{target_date}",
    )


def get_nws_point_estimate(ticker: str, market_data: dict) -> tuple:
    """Probability estimate for Kalshi high-temp markets using NWS hourly forecast.

    Returns (probability, source_label) or (None, None) if not applicable.
    """
    if market_data is None:
        return None, None
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = (ticker or "").upper()

    # Only respond to weather-like tickers/titles
    is_weather = "KXHIGH" in ticker_upper or any(
        kw in title for kw in ("temperature", "temp", "°f", "degrees", "high")
    )
    if not is_weather:
        return None, None

    city_key = _detect_city(ticker_upper, title)
    if not city_key:
        return None, None
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None, None

    threshold, is_above = _parse_threshold(ticker, title)
    if threshold is None or threshold < -40 or threshold > 140:
        return None, None

    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None:
        return None, None

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    lst_tz = timezone(timedelta(hours=tz_offset))
    target_date = (datetime.now(lst_tz) + timedelta(days=day_idx)).strftime("%Y-%m-%d")

    forecast_url = _resolve_grid_url(city["lat"], city["lon"])
    if forecast_url is None:
        return None, None
    periods = _fetch_hourly_forecast(forecast_url)
    if not periods:
        return None, None

    forecast_high = _daily_high_from_hourly(periods, target_date, tz_offset)
    if forecast_high is None:
        return None, None

    # NWS hourly forecast uncertainty is roughly the same as Open-Meteo's
    # (both ingest similar model data). Widen with lead time.
    forecast_sigma = _nws_point_sigma_for_day(day_idx)

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
        diff = forecast_high - threshold
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-diff / forecast_sigma))))
    else:
        diff = threshold - forecast_high
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-diff / forecast_sigma))))

    print(
        f"[nws_point] {city_key} day={day_idx} forecast_high={forecast_high:.0f}°F "
        f"threshold={threshold}°F sigma={forecast_sigma:.1f}°F -> {prob:.3f}"
    )
    return prob, f"nws_point:{city_key}_{target_date}"
