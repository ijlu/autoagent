"""ICON (German DWD) forecast source via Open-Meteo.

Live probe 2026-04-29 confirmed independence vs HRRR at 0.39 (one of
the best in our eval; only IEM 1-min was lower). Eval MAE 2.15°F across
6 stations × 30 days. Particularly accurate at KMIA (1.02°F MAE).

Different from HRRR/GFS:
  - German DWD's icosahedral grid (different mesh; behaves differently
    near poles + complex terrain)
  - European observation network (more European stations contribute)
  - Independent data assimilation chain

These properties together produce errors uncorrelated with US-tuned
GFS/HRRR — exactly the diversification the precision-weighted combine
benefits from.

Pre-seeded σ + bias from eval data via
``tools/seed_source_priors_from_eval.py``. State machine starts in
PROBATIONARY; auto-promotes to ACTIVE after 50+ settled rows with non-
regression on combined Brier.
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


_ICON_CACHE_TTL = 1800  # 30 min
_ICON_MODEL = "icon_seamless"


def _fetch_icon_forecast(city_key: str) -> Optional[dict]:
    """Fetch ICON daily forecasts for the next 7 days for one city."""
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None

    cache_key = f"icon::{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _ICON_CACHE_TTL:
            return data

    # CRITICAL: pass timezone= so daily_max is computed over the LST day,
    # not UTC. Without this, ICON's "daily max" can span the wrong window
    # and add ~1-3°F of artificial error (verified 2026-04-29 in eval).
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7"
        f"&models={_ICON_MODEL}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[icon] HTTP {r.status_code}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[icon] error: {type(e).__name__}: {e}")
        return None


def _icon_sigma_for_day(day_idx: int) -> float:
    """ICON per-day prior σ. Eval (n=174) MAE pooled across 6 stations
    was 2.15°F → σ_prior 2.5°F base. Add 0.5°F per day out.

    The pre-seeded learned σ in kv_cache (per-city, by station) overrides
    this prior in production via ``_apply_learned_sigma``.
    """
    return 2.5 + day_idx * 0.5


def get_icon_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
    """Return ICON's daily-high distribution for the settlement day."""
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

    forecast = _fetch_icon_forecast(city_key)
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
    sigma_f = _icon_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="icon",
        source_tag=f"icon:{city_key}_{date_label}",
    )
