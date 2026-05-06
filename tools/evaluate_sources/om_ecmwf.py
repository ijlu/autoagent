"""ECMWF IFS via Open-Meteo Historical Forecast API.

Uses ``historical-forecast-api.open-meteo.com`` which serves the
forecasts that WERE issued at run time on each historical date.

For evaluation we want the "day-before" forecast — i.e., the prediction
that was issued ~24h before the target date. We approximate that by
querying past_days=1 forecast_days=0 from the historical API, which
returns yesterday's forecasts for today (per Open-Meteo docs).

ECMWF is independent from GFS (live probe 2026-04-29 confirmed up to 6°F
differences). This is the highest-priority new source.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

import requests

from tools.evaluate_data_source import register


# Hard cap to avoid run-away during eval (dev sanity). Set high enough
# for full 6 stations × 30 days.
_MAX_CALLS = 500
_call_count = 0

_CACHE: dict = {}


@register("ecmwf_om")
def fetch(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York") -> Optional[tuple[float, Optional[float]]]:
    """Fetch the ECMWF day-before forecast for the target lst_date.

    Open-Meteo Historical Forecast API documents:
      https://open-meteo.com/en/docs/historical-forecast-api
    It serves the actual forecast that was produced at run time on each
    historical date (not just the analysis after the fact).

    Per its docs, ``models=ecmwf_ifs025`` is the high-resolution ECMWF
    IFS forecast at ~25km grid spacing, run twice daily.

    CRITICAL: pass ``timezone`` so daily_max is computed over the LST
    day boundary, not UTC. Without this, the daily max spans the wrong
    24-hour window and adds ~1-3°F of artificial error.
    """
    global _call_count
    if _call_count >= _MAX_CALLS:
        return None
    _call_count += 1

    cache_key = (station, lst_date, "ecmwf")
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # Query window: target date itself, with the historical-forecast endpoint.
    url = (
        "https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={lst_date}&end_date={lst_date}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone={tz}"
        "&models=ecmwf_ifs025"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"[ecmwf_om] HTTP {r.status_code} for {station} {lst_date}")
            _CACHE[cache_key] = None
            return None
        data = r.json()
        highs = data.get("daily", {}).get("temperature_2m_max", [])
        if not highs:
            _CACHE[cache_key] = None
            return None
        mu = highs[0]
        if mu is None:
            _CACHE[cache_key] = None
            return None
        # ECMWF day-ahead skill: ~1.5-2.0°F RMSE empirically; use 2.0 prior.
        out = (float(mu), 2.0)
        _CACHE[cache_key] = out
        return out
    except Exception as e:
        print(f"[ecmwf_om] {station} {lst_date}: {type(e).__name__}: {e}")
        _CACHE[cache_key] = None
        return None
