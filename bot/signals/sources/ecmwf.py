"""ECMWF HRES forecast source via Open-Meteo.

Validated 2026-04-30 (`tools/investigate_new_forecast_sources.py`,
n=174 city-days, Apr 1–29 2026):

  - Pooled MAE: **2.72°F** (worse than HRRR/GEM/ICON/MetNo on direct
    accuracy)
  - Pooled bias: −0.61°F (close to HRRR's −0.60°F)
  - Pairwise residual ρ vs ICON: **0.34** (LOWEST correlation in the
    candidate set), vs HRRR: 0.53, vs UKMO: 0.51
  - Ensemble impact: adding ECMWF HRES to (HRRR+ICON+UKMO) baseline
    cut pooled MAE 1.79°F → 1.738°F (**−3%**)

The interesting story: ECMWF HRES alone is *not* a top-accuracy source
on April US daily highs (HRRR's CONUS specialization wins). But its
errors are the most independent of every current source we have —
0.34 correlation with ICON is half what UKMO–ICON shows (0.73).
That independence is exactly what the precision-weighted combine
benefits from: even a less-accurate source helps when its residuals
have low correlation with existing members.

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


# ECMWF HRES runs twice per day (00 / 12 UTC) with ~5h publish latency.
# 5h 30min cache fits one cycle but ECMWF actually runs less often —
# could probably stretch to 11h, but matching the pattern of other
# 6-hourly models for consistency.
_ECMWF_CACHE_TTL = 19800
_ECMWF_MODEL = "ecmwf_ifs025"


def _fetch_ecmwf_forecast(city_key: str) -> Optional[dict]:
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None

    cache_key = f"ecmwf::{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _ECMWF_CACHE_TTL:
            return data

    from bot.signals.sources._openmeteo import forecast_url
    url = forecast_url(
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7"
        f"&models={_ECMWF_MODEL}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[ecmwf] HTTP {r.status_code}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[ecmwf] error: {type(e).__name__}: {e}")
        return None


def _ecmwf_sigma_for_day(day_idx: int) -> float:
    """ECMWF per-day prior σ. Eval (n=174) MAE pooled across 6 stations
    was 2.72°F → σ_prior 3.0°F base. Add 0.5°F per day out.

    Wider than GEM/MetNo because ECMWF HRES's MAE is higher in our
    eval (despite being the global gold-standard at longer horizons).
    """
    return 3.0 + day_idx * 0.5


def get_ecmwf_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
    """Return ECMWF HRES's daily-high distribution for the settlement day."""
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

    forecast = _fetch_ecmwf_forecast(city_key)
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
    sigma_f = _ecmwf_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="ecmwf",
        source_tag=f"ecmwf:{city_key}_{date_label}",
    )
