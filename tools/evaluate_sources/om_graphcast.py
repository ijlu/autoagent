"""GraphCast via Open-Meteo.

DeepMind's ML weather model. Often beats IFS at multi-day horizons; not
sure how it does at 24h short range. Open-Meteo serves it under
``models=graphcast``.

Expected: ML-based, so its inductive bias differs from physics-based
HRRR/ECMWF/GFS. Independence vs HRRR should be lower than GFS-vs-HRRR.
"""

from __future__ import annotations

from typing import Optional

import requests

from tools.evaluate_data_source import register


_MAX_CALLS = 500
_call_count = 0
_CACHE: dict = {}


@register("graphcast_om")
def fetch(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York") -> Optional[tuple[float, Optional[float]]]:
    global _call_count
    if _call_count >= _MAX_CALLS:
        return None
    _call_count += 1

    cache_key = (station, lst_date, "graphcast")
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # Open-Meteo's GraphCast endpoint name varies by API version. Latest
    # docs (2026-04) use ``graphcast``; older ``graphcast025`` returns 404.
    # Try canonical name first, fall back to versioned.
    url = (
        "https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={lst_date}&end_date={lst_date}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone={tz}"
        "&models=graphcast"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"[graphcast_om] HTTP {r.status_code} for {station} {lst_date}")
            _CACHE[cache_key] = None
            return None
        highs = r.json().get("daily", {}).get("temperature_2m_max", [])
        if not highs or highs[0] is None:
            _CACHE[cache_key] = None
            return None
        out = (float(highs[0]), 2.0)
        _CACHE[cache_key] = out
        return out
    except Exception as e:
        print(f"[graphcast_om] {station} {lst_date}: {type(e).__name__}: {e}")
        _CACHE[cache_key] = None
        return None
