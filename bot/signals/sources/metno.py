"""MET Norway forecast source via Open-Meteo.

Validated 2026-04-30 (`tools/investigate_new_forecast_sources.py`,
n=174 city-days, Apr 1–29 2026):

  - Pooled MAE: **1.96°F** (vs HRRR 1.33, GEM 1.80, ICON 2.18)
  - Pooled bias: **−0.94°F** (slight cool bias)
  - Pairwise residual ρ vs HRRR: 0.73, vs ICON: 0.75 (less independent
    than GEM but still useful diversification)
  - Ensemble impact: adding MetNo to (HRRR+ICON+UKMO) baseline cut
    pooled MAE 1.79°F → 1.721°F (**−4%**)

MET Norway operates a Nordic-tuned model, useful as a third-party
European voice independent of UKMO and ECMWF/ICON. Smaller marginal
contribution than GEM but independent enough to be worth including.

State machine starts in PROBATIONARY; auto-promotes to ACTIVE after
50+ settled rows with non-regression on combined Brier.
"""

from __future__ import annotations

import time
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


# MET Norway updates roughly every 6 hours. Same TTL as other 6-hourly
# global models in our combine.
_METNO_CACHE_TTL = 19800
_METNO_MODEL = "metno_seamless"


def _fetch_metno_forecast(city_key: str) -> Optional[dict]:
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None

    cache_key = f"metno::{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _METNO_CACHE_TTL:
            return data

    from bot.signals.sources._openmeteo import forecast_url
    url = forecast_url(
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7"
        f"&models={_METNO_MODEL}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[metno] HTTP {r.status_code}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[metno] error: {type(e).__name__}: {e}")
        return None


def _metno_sigma_for_day(day_idx: int) -> float:
    """MetNo per-day prior σ. Eval (n=174) MAE pooled across 6 stations
    was 1.96°F → σ_prior 2.2°F base. Add 0.5°F per day out.
    """
    return 2.2 + day_idx * 0.5


def get_metno_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
    """Return MetNo's daily-high distribution for the settlement day."""
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

    forecast = _fetch_metno_forecast(city_key)
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
    sigma_f = _metno_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="metno",
        source_tag=f"metno:{city_key}_{date_label}",
    )
