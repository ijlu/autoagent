"""National weather agency forecast models via Open-Meteo Historical API.

Each major weather agency runs its own global numerical weather prediction
model. They use different observation networks, different data
assimilation, different physics parameterizations — so their forecast
errors are largely uncorrelated. That's the property we need for an
ensemble: independent signal sources.

Live probe 2026-04-29 confirmed all 5 work on Open-Meteo:

  icon_seamless        (Germany / DWD)
  meteofrance_seamless (France)
  ukmo_seamless        (UK Met Office)
  jma_seamless         (Japan Meteorological Agency)
  cma_grapes_global    (China Meteorological Administration)

Each registered separately so we can rank them individually. After
eval, the top performers should be added to ``_collect_gaussians``.
"""

from __future__ import annotations

from typing import Optional

import requests

from tools.evaluate_data_source import register


_MAX_CALLS = 1000
_call_count = 0
_CACHE: dict = {}


def _fetch_om_model(
    model: str, station: str, lst_date: str, lat: float, lon: float, tz: str
) -> Optional[tuple[float, Optional[float]]]:
    global _call_count
    if _call_count >= _MAX_CALLS:
        return None
    _call_count += 1

    cache_key = (station, lst_date, model)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    url = (
        "https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={lst_date}&end_date={lst_date}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone={tz}"
        f"&models={model}"
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
        # σ prior: agency-grade global models typically run ~2-3°F MAE
        # day-ahead. Will be replaced by learned σ from settled outcomes.
        out = (float(highs[0]), 2.5)
        _CACHE[cache_key] = out
        return out
    except Exception as e:
        print(f"[{model}] {station} {lst_date}: {type(e).__name__}: {e}")
        _CACHE[cache_key] = None
        return None


@register("icon_om")
def fetch_icon(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York"):
    return _fetch_om_model("icon_seamless", station, lst_date, lat, lon, tz)


@register("meteofrance_om")
def fetch_meteofrance(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York"):
    return _fetch_om_model("meteofrance_seamless", station, lst_date, lat, lon, tz)


@register("ukmo_om")
def fetch_ukmo(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York"):
    return _fetch_om_model("ukmo_seamless", station, lst_date, lat, lon, tz)


@register("jma_om")
def fetch_jma(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York"):
    return _fetch_om_model("jma_seamless", station, lst_date, lat, lon, tz)


@register("cma_om")
def fetch_cma(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York"):
    return _fetch_om_model("cma_grapes_global", station, lst_date, lat, lon, tz)
