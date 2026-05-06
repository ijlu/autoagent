"""GEM (Canadian Meteorological Centre) forecast source via Open-Meteo.

Validated 2026-04-30 (`tools/investigate_new_forecast_sources.py`,
n=174 city-days, Apr 1–29 2026):

  - Pooled MAE: **1.80°F** (vs HRRR 1.33, ICON 2.18, UKMO 2.37)
  - Pooled bias: **+0.08°F** (essentially zero; remarkable)
  - Pairwise residual ρ vs HRRR: 0.61, vs ICON: 0.60, vs UKMO: 0.62
    (moderately independent of all current sources)
  - Ensemble impact: adding GEM to (HRRR+ICON+UKMO) baseline cut
    pooled MAE 1.79°F → 1.575°F (**−12%**, the best single addition
    of the 6 candidates we validated)

Different from other sources in the combine:
  - Canadian Meteorological Centre's GEM model — independent
    physics core + data assimilation chain from the European (ECMWF /
    ICON / UKMO) and American (HRRR / NWS_point) systems
  - North-America-tuned but with stronger Arctic + boundary handling
    than HRRR

State machine starts in PROBATIONARY; auto-promotes to ACTIVE after
50+ settled rows with non-regression on combined Brier. σ + bias
prior values match the eval data, refined by `_apply_learned_sigma`
+ `_apply_mos_bias` once accumulated.
"""

from __future__ import annotations

import math
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


# GEM runs four times per day (00/06/12/18 UTC) with ~3h publish latency.
# 5h 30min cache fits inside one model cycle — same shape as ICON/UKMO.
_GEM_CACHE_TTL = 19800
_GEM_MODEL = "gem_seamless"


def _fetch_gem_forecast(city_key: str) -> Optional[dict]:
    """Fetch GEM daily forecasts for the next 7 days for one city."""
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None

    cache_key = f"gem::{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _GEM_CACHE_TTL:
            return data

    # timezone= is critical: daily_max is over the LST day, not UTC.
    from bot.signals.sources._openmeteo import forecast_url
    url = forecast_url(
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7"
        f"&models={_GEM_MODEL}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[gem] HTTP {r.status_code}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[gem] error: {type(e).__name__}: {e}")
        return None


def _gem_sigma_for_day(day_idx: int) -> float:
    """GEM per-day prior σ. Eval (n=174) MAE pooled across 6 stations
    was 1.80°F → σ_prior 2.0°F base. Add 0.5°F per day out.

    Tighter than ICON's 2.5°F base because GEM's MAE is meaningfully
    lower in the validation. The pre-seeded learned σ in kv_cache
    (per-city) overrides this prior once accumulated.
    """
    return 2.0 + day_idx * 0.5


def get_gem_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
    """Return GEM's daily-high distribution for the settlement day."""
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

    forecast = _fetch_gem_forecast(city_key)
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
    sigma_f = _gem_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="gem",
        source_tag=f"gem:{city_key}_{date_label}",
    )
