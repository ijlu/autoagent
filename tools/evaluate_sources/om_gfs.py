"""GFS via Open-Meteo Historical Forecast API — baseline / sanity check.

This source is what our existing ``weather`` and ``nbm`` sources are
already pulling (Open-Meteo default for US lat/lons = GFS-seamless). Use
this in the eval to confirm:

  1. The framework can reproduce results matching the production HRRR
     and METAR per-station MAE we measured earlier (sanity check).
  2. Independence: GFS-OM should be highly correlated with our
     ``weather`` source (we proved them identical 2026-04-29).

If the eval shows GFS-OM independence > 0.9 vs current ``weather`` rows,
that confirms the calibration of the framework — and that we shouldn't
re-add GFS-OM as a separate source.
"""

from __future__ import annotations

from typing import Optional

import requests

from tools.evaluate_data_source import register


_MAX_CALLS = 500
_call_count = 0
_CACHE: dict = {}


@register("gfs_om")
def fetch(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York") -> Optional[tuple[float, Optional[float]]]:
    global _call_count
    if _call_count >= _MAX_CALLS:
        return None
    _call_count += 1

    cache_key = (station, lst_date, "gfs")
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    url = (
        "https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={lst_date}&end_date={lst_date}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone={tz}"
        "&models=gfs_seamless"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            _CACHE[cache_key] = None
            return None
        highs = r.json().get("daily", {}).get("temperature_2m_max", [])
        if not highs or highs[0] is None:
            _CACHE[cache_key] = None
            return None
        out = (float(highs[0]), 1.8)
        _CACHE[cache_key] = out
        return out
    except Exception as e:
        print(f"[gfs_om] {station} {lst_date}: {type(e).__name__}: {e}")
        _CACHE[cache_key] = None
        return None
