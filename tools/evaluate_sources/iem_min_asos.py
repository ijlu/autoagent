"""IEM 1-minute ASOS — daily max from minute-resolution observations.

Standard METAR transmits hourly + SPECI; the underlying ASOS station
takes a temperature reading every minute. The hourly transmission can
miss intra-hour peaks. IEM serves the 1-min data via a separate endpoint:

  https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py

We fetch the day's 1-minute observations and take MAX as the source's
prediction for that day's high. By design this should track the true
peak even better than the hourly METAR running max.

Caveat: this is observation-based, not forecast. So it's not "predicting"
the day-ahead high — it tells us, AFTER the fact, what the high actually
was. For evaluation purposes we treat it as "the most accurate possible
post-peak observation source" — answers "is 1-min ASOS more accurate
than CF6 TMAX (our ground truth)?" If yes, our ground truth is wrong.

IEM rate limits: undocumented but generous. Cache aggressively.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from tools.evaluate_data_source import register


_MAX_CALLS = 500
_call_count = 0
_CACHE: dict = {}


@register("iem_1min_asos")
def fetch(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York") -> Optional[tuple[float, Optional[float]]]:
    """Pull 1-min ASOS for the LST day at this station, return max temp.

    NB: IEM 1-min is in UTC; we want the local-LST day boundary. We
    request a +/- 1-day window in UTC and filter by LST date.
    """
    global _call_count
    if _call_count >= _MAX_CALLS:
        return None
    _call_count += 1

    cache_key = (station, lst_date)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # Map ICAO → IEM 4-letter (it's the same for ASOS).
    iem_id = station[1:] if station.startswith("K") else station

    # Pull a 36-hour window around the target LST date (covers both
    # boundaries regardless of LST offset).
    start = datetime.strptime(lst_date, "%Y-%m-%d") - timedelta(hours=8)
    end = start + timedelta(hours=36)
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
        f"?station={iem_id}&vars=tmpf"
        f"&year1={start.year}&month1={start.month}&day1={start.day}&hour1={start.hour}"
        f"&year2={end.year}&month2={end.month}&day2={end.day}&hour2={end.hour}"
        "&format=onlytdf&tz=UTC"
    )
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            print(f"[iem_1min] HTTP {r.status_code} for {station} {lst_date}")
            _CACHE[cache_key] = None
            return None
        body = r.text.strip()
        if not body or "no data" in body.lower():
            _CACHE[cache_key] = None
            return None
    except Exception as e:
        print(f"[iem_1min] {station} {lst_date}: {type(e).__name__}: {e}")
        _CACHE[cache_key] = None
        return None

    # IEM ``format=onlytdf`` actually returns COMMA-delimited (despite the
    # 'tdf' suggesting tab). Live probe 2026-04-29 confirmed comma headers:
    # "station,station_name,valid(UTC),tmpf"
    reader = csv.DictReader(io.StringIO(body), delimiter=",")
    target_lst = datetime.strptime(lst_date, "%Y-%m-%d").date()

    # LST offsets per station (matches production registry)
    LST_OFFSET = {"KNYC": -5, "KMIA": -5, "KMDW": -6, "KAUS": -6,
                  "KLAX": -8, "KDEN": -7}
    offset_h = LST_OFFSET.get(station, -5)

    max_temp = None
    n_obs = 0
    for row in reader:
        try:
            t = row.get("valid") or row.get("valid(UTC)")
            tmpf = row.get("tmpf")
            if not t or not tmpf or tmpf in ("M", ""):
                continue
            utc = datetime.strptime(t, "%Y-%m-%d %H:%M")
            lst = utc + timedelta(hours=offset_h)
            if lst.date() != target_lst:
                continue
            v = float(tmpf)
            if v < -50 or v > 130:
                continue
            n_obs += 1
            if max_temp is None or v > max_temp:
                max_temp = v
        except (ValueError, KeyError, TypeError):
            continue

    if max_temp is None or n_obs < 60:  # Need at least an hour of obs
        _CACHE[cache_key] = None
        return None

    # σ small — this is observation, not forecast, σ is mostly sensor noise.
    out = (round(max_temp, 1), 0.5)
    _CACHE[cache_key] = out
    return out
