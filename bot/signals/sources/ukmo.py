"""UKMO (UK Met Office) forecast source via Open-Meteo.

Live probe 2026-04-29 confirmed independence vs HRRR at 0.40. Eval MAE
2.27°F across 6 stations × 30 days. Particularly strong on Atlantic-
influenced weather (relevant for NYC, MIA, even LAX via Pacific).

Different from HRRR/GFS:
  - UK Met Office's Unified Model (UM) — different physics suite
  - Strong North Atlantic / mid-latitude observation network
  - Independent data assimilation

Pre-seeded σ + bias from eval via
``tools/seed_source_priors_from_eval.py``. Starts in PROBATIONARY.
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


# UKMO runs four times per day (00/06/12/18 UTC) with ~3-5h publish
# latency. 5h 30min cache fits inside one cycle. Was 1800 (30 min);
# the new TTL aligns to the data's actual update cadence. See deploy
# notes 2026-04-30.
_UKMO_CACHE_TTL = 19800
_UKMO_MODEL = "ukmo_seamless"


def _fetch_ukmo_forecast(city_key: str) -> Optional[dict]:
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None

    cache_key = f"ukmo::{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _UKMO_CACHE_TTL:
            return data

    # See icon.py for the timezone= rationale — same applies here.
    from bot.signals.sources._openmeteo import forecast_url
    url = forecast_url(
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7"
        f"&models={_UKMO_MODEL}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[ukmo] HTTP {r.status_code}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[ukmo] error: {type(e).__name__}: {e}")
        return None


def _ukmo_sigma_for_day(day_idx: int) -> float:
    """UKMO per-day prior σ. Eval MAE 2.27°F → σ_prior ~2.85°F. Per-city
    learned σ in kv_cache overrides via ``_apply_learned_sigma``.
    """
    return 2.85 + day_idx * 0.5


def get_ukmo_gaussian(ticker: str, market_data: dict) -> Optional[GaussianForecast]:
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

    forecast = _fetch_ukmo_forecast(city_key)
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
    sigma_f = _ukmo_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="ukmo",
        source_tag=f"ukmo:{city_key}_{date_label}",
    )
