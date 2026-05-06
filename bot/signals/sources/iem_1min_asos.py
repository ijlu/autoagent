"""IEM 1-minute ASOS observations — finer-resolution METAR.

Standard METAR transmits hourly + SPECIs for big changes. The
underlying ASOS station takes a temperature observation EVERY MINUTE.
Iowa Environmental Mesonet (IEM) serves the underlying minute stream
that METAR samples from.

Live probe + eval 2026-04-29:
  * MAE 1.15°F across 6 stations × 30 days — better than every forecast
  * Independence vs HRRR ≈ 0 (it's an observation, not a forecast)
  * 84% of predictions within 1°F of the actual daily high
  * KMDW MAE 0.37°F (best); KNYC 2.32°F (known data-gap issue)

This is the SAME PHYSICAL DATA SOURCE as METAR (same ASOS station,
same thermometer) — just at minute resolution instead of hourly. The
``observation_source`` channel in ``_collect_gaussians`` prefers IEM
1-min when available, falls back to METAR on data gaps.

CRITICAL convention decisions baked in:

  1. ``IEM ?format=onlytdf`` returns COMMA-delimited despite the
     'tdf' suffix suggesting tab. Live probe 2026-04-29 confirmed:
     ``station,station_name,valid(UTC),tmpf``. Parser uses comma.

  2. We pull a 36-hour window in UTC, then filter rows by LST date.
     LST not UTC because Kalshi settles on LST day boundaries — the
     daily max we want is the max within the LST 00:00→23:59 window.

  3. σ_prior 1.5°F at day 0 (matches eval MAE × 1.3). Day-1 fits via
     pre-seeded ``weather_skill_iem_1min_<city>_<bucket>`` kv keys.

Only fires for ``day_idx == 0`` — IEM 1-min is observation, not
forecast, so it has no value as a next-day prediction.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.daemon.stations import station_for_ticker
from bot.signals.sources.weather import (
    _CITY_LST_OFFSET,
    _detect_city,
    _determine_day_index,
    _parse_threshold,
)
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


_IEM_CACHE_TTL = 60  # 1 min — minute data is updated rapidly; short TTL
_IEM_MIN_OBS_PER_DAY = 60  # need at least 1 hour of data for a meaningful max


def _icao_to_iem_id(icao: str) -> str:
    """Convert ICAO 4-letter to IEM 3-letter station ID. ASOS IEM IDs
    drop the leading 'K' — KNYC → NYC, KMDW → MDW, etc."""
    return icao[1:] if icao.startswith("K") else icao


def _fetch_iem_1min_max(
    icao: str, lst_date_str: str, tz_offset_hours: int,
) -> Optional[float]:
    """Fetch IEM 1-min observations and return MAX(tmpf) over the LST
    day matching ``lst_date_str``. Returns None on any failure or if
    fewer than _IEM_MIN_OBS_PER_DAY samples were returned.
    """
    iem_id = _icao_to_iem_id(icao)
    cache_key = f"iem_1min::{iem_id}::{lst_date_str}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _IEM_CACHE_TTL:
            return data

    # Pull a 36-hour window in UTC straddling the target LST date so we
    # capture the full LST 00:00→23:59 range regardless of LST offset.
    # The LST date in UTC starts at LST 00:00 = UTC (00 - tz_offset)
    # = UTC tz_offset (positive for Western tz_offsets which are negative).
    target = datetime.strptime(lst_date_str, "%Y-%m-%d")
    # LST day starts at this UTC time:
    start_utc = target - timedelta(hours=tz_offset_hours)  # earlier in UTC
    # Pad 4h on each side for safety against DST / clock-skew edge cases
    start_utc = start_utc - timedelta(hours=4)
    end_utc = start_utc + timedelta(hours=36)

    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
        f"?station={iem_id}&vars=tmpf"
        f"&year1={start_utc.year}&month1={start_utc.month:02d}"
        f"&day1={start_utc.day:02d}&hour1={start_utc.hour:02d}"
        f"&year2={end_utc.year}&month2={end_utc.month:02d}"
        f"&day2={end_utc.day:02d}&hour2={end_utc.hour:02d}"
        "&format=onlytdf&tz=UTC"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"[iem_1min] HTTP {r.status_code} for {icao} {lst_date_str}")
            _CACHE[cache_key] = (None, now)
            return None
        body = r.text.strip()
        if not body or "no data" in body.lower():
            _CACHE[cache_key] = (None, now)
            return None
    except Exception as e:
        print(f"[iem_1min] {icao} {lst_date_str}: {type(e).__name__}: {e}")
        _CACHE[cache_key] = (None, now)
        return None

    # IEM `format=onlytdf` returns COMMA-delimited (despite 'tdf'). Header:
    # station,station_name,valid(UTC),tmpf
    target_lst_date = target.date()
    max_temp: Optional[float] = None
    n_obs = 0
    reader = csv.DictReader(io.StringIO(body), delimiter=",")
    for row in reader:
        try:
            t = row.get("valid(UTC)") or row.get("valid")
            tmpf = row.get("tmpf")
            if not t or not tmpf or tmpf in ("M", ""):
                continue
            utc = datetime.strptime(t, "%Y-%m-%d %H:%M")
            lst = utc + timedelta(hours=tz_offset_hours)
            if lst.date() != target_lst_date:
                continue
            v = float(tmpf)
            if v < -50 or v > 130:
                continue
            n_obs += 1
            if max_temp is None or v > max_temp:
                max_temp = v
        except (ValueError, KeyError, TypeError):
            continue

    if max_temp is None or n_obs < _IEM_MIN_OBS_PER_DAY:
        _CACHE[cache_key] = (None, now)
        return None

    _CACHE[cache_key] = (max_temp, now)
    return max_temp


def get_iem_1min_gaussian(
    ticker: str, market_data: dict
) -> Optional[GaussianForecast]:
    """Return IEM 1-min ASOS daily-high distribution for the settlement day.

    Only fires for day_idx == 0 — observation source, no forward-look value.
    Returns None on data gap; caller (``_collect_observation_gaussian``)
    falls back to METAR.
    """
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
    if day_idx != 0:
        # Past-date guard already handles None; future days have no
        # observation data yet.
        return None

    # Resolve station from ticker (canonical registry)
    station_record = station_for_ticker(ticker)
    if station_record is None:
        return None
    icao = station_record.icao

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    lst_date = datetime.now(timezone(timedelta(hours=tz_offset))).strftime("%Y-%m-%d")

    max_temp = _fetch_iem_1min_max(icao, lst_date, tz_offset)
    if max_temp is None:
        return None

    # σ_prior: matches eval MAE × 1.3. Pre-seeded learned σ takes
    # over once kv_cache is populated.
    sigma_f = 1.5
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(max_temp),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="iem_1min",
        source_tag=f"iem_1min:{icao}_{lst_date}",
    )
