"""Weather data sources for prediction market estimation.

Contains three weather sources:
  1. get_weather_estimate()       — Open-Meteo (free, no auth)
  2. get_tomorrow_weather_estimate() — Tomorrow.io (premium, 500 calls/day)
  3. get_noaa_alerts_for_market() — NOAA severe weather alerts (free, no auth)
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from bot.api import _CACHE, cached_get, rate_limit_wait
from bot.config import TOMORROW_API_KEY
from bot.daemon.stations import STATION_BY_CITY, STATION_BY_SERIES
from bot.db import get_connection, kv_get, kv_set
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


# Open-Meteo default-blend forecasts update at the same cadence as the
# underlying global model (GFS at temperate-zone US lat/lons), which runs
# every 6 hours. The default `cached_get` TTL is 60s — far shorter than
# the data refresh — so we'd been re-fetching identical bytes every
# ensemble cycle, burning ~8,600 calls/day per the daily-quota math.
# Local cache with 5h 30min TTL aligns the fetch cadence to the data
# cadence; same shape as hrrr / icon / ukmo / nbm. See deploy notes
# 2026-04-30.
_WEATHER_OM_CACHE_TTL = 19800


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# Broad city catalog for signal sources. A superset of the daemon's
# tradeable-series registry (``bot.daemon.stations``) — includes cities
# used by directional signals on markets we score but don't market-make
# (phoenix, seattle, boston, etc.).
#
# 2026-05-04 — TRADEABLE-CITY LAT/LON UPDATED to match the settlement
# station's coordinates from ``bot.daemon.stations``. The original
# design used "downtown" lat/lon, with the rationale that forecast APIs
# grid better to city center. But Kalshi settles on the AIRPORT METAR
# (KNYC Central Park, KLAX, KMIA, etc.) and forecast grids that pull
# from "downtown" produce systematically biased forecasts vs the
# settlement station. Concrete example:
#   * NYC at 40.71,-74.01 (Lower Manhattan) resolves NWS grid to
#     "Hoboken, NJ" — different microclimate from KNYC.
#   * LAX at 34.05,-118.24 (Downtown LA) resolves to "Vernon, CA",
#     completely missing the marine layer that drives KLAX's daily
#     high. Result: 7+ forecast sources show +5 to +10°F warm bias
#     against KLAX truth.
#   * Denver at 39.74,-104.99 resolves to "Glendale, CO", not KDEN.
# Settlement-aligned coordinates are the source of truth.
#
# Non-tradeable directional cities (phoenix, seattle, boston, etc.) keep
# their downtown coords — those markets aren't on Kalshi yet so settlement
# alignment doesn't apply.
WEATHER_CITIES = {
    "nyc":          {"lat": 40.78, "lon": -73.97, "tz": "America/New_York"},  # KNYC
    "new york":     {"lat": 40.78, "lon": -73.97, "tz": "America/New_York"},  # KNYC
    "chicago":      {"lat": 41.79, "lon": -87.75, "tz": "America/Chicago"},   # KMDW
    "miami":        {"lat": 25.79, "lon": -80.29, "tz": "America/New_York"},  # KMIA
    "austin":       {"lat": 30.19, "lon": -97.67, "tz": "America/Chicago"},   # KAUS
    "los angeles":  {"lat": 33.94, "lon": -118.41, "tz": "America/Los_Angeles"},  # KLAX
    "la":           {"lat": 33.94, "lon": -118.41, "tz": "America/Los_Angeles"},  # KLAX
    "phoenix":      {"lat": 33.45, "lon": -112.07, "tz": "America/Phoenix"},
    "houston":      {"lat": 29.76, "lon": -95.37, "tz": "America/Chicago"},
    "dallas":       {"lat": 32.78, "lon": -96.80, "tz": "America/Chicago"},
    "denver":       {"lat": 39.86, "lon": -104.67, "tz": "America/Denver"},   # KDEN
    "atlanta":      {"lat": 33.75, "lon": -84.39, "tz": "America/New_York"},
    "seattle":      {"lat": 47.61, "lon": -122.33, "tz": "America/Los_Angeles"},
    "boston":        {"lat": 42.36, "lon": -71.06, "tz": "America/New_York"},
    "san francisco":{"lat": 37.77, "lon": -122.42, "tz": "America/Los_Angeles"},
    "sf":           {"lat": 37.77, "lon": -122.42, "tz": "America/Los_Angeles"},
    "dc":           {"lat": 38.91, "lon": -77.04, "tz": "America/New_York"},
    "washington":   {"lat": 38.91, "lon": -77.04, "tz": "America/New_York"},
    "minneapolis":  {"lat": 44.98, "lon": -93.27, "tz": "America/Chicago"},
    "detroit":      {"lat": 42.33, "lon": -83.05, "tz": "America/New_York"},
    "las vegas":    {"lat": 36.17, "lon": -115.14, "tz": "America/Los_Angeles"},
}

# Ticker prefix → city key map. Tradeable series derive from the
# canonical registry (T1.1). Dead-market prefixes (KXHIGHHOU/PHX/SF)
# were removed 2026-04-16 — signal sources now gracefully return None
# for those tickers instead of fabricating city guesses.
_TICKER_CITY_MAP: dict[str, str] = {
    series: station.city for series, station in STATION_BY_SERIES.items()
}

# Tomorrow.io in-memory cache: {city_key: (data, timestamp)}
_TOMORROW_CACHE: dict = {}
_TOMORROW_TTL = 1800  # 30 minutes -- 9 cities x 48 fetches/day = 432 calls (under 500)

# LST offsets for settlement-day computation (Kalshi weather markets settle
# in Local Standard Time year-round). Tradeable cities pull from the
# canonical registry; non-tradeable cities keep hardcoded offsets so the
# directional signal path on those markets stays unchanged.
_NON_TRADEABLE_LST_OFFSET: dict[str, int] = {
    "phoenix": -7, "houston": -6, "dallas": -6, "atlanta": -5,
    "seattle": -8, "boston": -5, "san francisco": -8, "sf": -8,
    "dc": -5, "washington": -5, "minneapolis": -6, "detroit": -5,
    "las vegas": -8,
}
_CITY_LST_OFFSET: dict[str, int] = {
    **{city: s.lst_offset for city, s in STATION_BY_CITY.items()},
    **_NON_TRADEABLE_LST_OFFSET,
}

_WEATHER_KEYWORDS = [
    "temperature", "temp", "\u00b0f", "\u00b0c", "degrees", "high", "low",
    "weather", "heat", "cold", "freeze", "highest temperature",
]

# Reverse mapping: city key → primary METAR station ID.
# Used to persist forecast highs into kv_cache so metar_observations.py
# can compare observations against forecasts (Defense 4: weather conditional quoting).
# Derived from the canonical registry — do not hand-maintain.
_CITY_STATION_MAP: dict[str, str] = {
    city: s.icao for city, s in STATION_BY_CITY.items()
}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _logistic_cdf(x, mu, sigma):
    """Standard logistic CDF used for temperature probability estimation."""
    return 1 / (1 + math.exp(-(x - mu) / sigma))


def _detect_city(ticker_upper: str, title: str):
    """Return city_key by checking ticker prefix first, then title keywords."""
    for prefix, city in _TICKER_CITY_MAP.items():
        if prefix in ticker_upper:
            return city
    for key in WEATHER_CITIES:
        if key in title:
            return key
    return None


def _parse_threshold(ticker: str, market_data: dict):
    """Extract temperature threshold and direction from a Kalshi market response.

    Returns ``(threshold, is_above)`` or ``(None, None)`` on failure.

    Direction priority (most → least authoritative):

      1. **API strikes** on the market payload. For T-markets exactly one of
         ``floor_strike`` / ``cap_strike`` is populated — Kalshi's settlement
         engine reads from these fields and they are the ground truth:
            cap_strike only   → "high < cap"   → is_above=False
            floor_strike only → "high > floor" → is_above=True
      2. ``yes_sub_title`` text — Kalshi's per-side question. Carries phrases
         like "78° or below" / "59° or higher" (number-then-keyword shape).
      3. Legacy ``title`` / ``subtitle`` regex (keyword-then-number, "above 80").
      4. Bare ``<N`` / ``>N`` characters in any title field.
      5. For B-markets: ``-B<N>`` ticker suffix or "X-Y°F" range in title
         (direction is meaningless for brackets; placeholder True).
      6. Otherwise — refuse to guess. The ``-T<N>`` suffix alone does NOT
         encode direction. Returning a default ('above') silently inverts
         every below-direction market — exactly the bug that produced the
         late-day Brier 0.46-0.93 vs market 0.000 pathology and matches the
         2026-04-22 weather_quoter sign-flip incident (poisoned 27k shadow
         rows). Sources that get (None, None) drop out of the ensemble for
         that market — that is the correct behavior.
    """
    ticker_upper = (ticker or "").upper()
    is_t = "-T" in ticker_upper
    is_b = "-B" in ticker_upper

    title_blob = " ".join(filter(None, [
        market_data.get("yes_sub_title") or "",
        market_data.get("title") or "",
        market_data.get("subtitle") or "",
    ])).lower()

    # 1. API strikes — authoritative for threshold markets.
    if is_t:
        api_floor = market_data.get("floor_strike")
        api_cap = market_data.get("cap_strike")
        try:
            if api_cap is not None and api_floor is None:
                return float(api_cap), False
            if api_floor is not None and api_cap is None:
                return float(api_floor), True
        except (ValueError, TypeError):
            pass

    # 2. Keyword-then-number ("at or above 75").
    m = re.search(
        r'(at or above|at or below|above|below|over|under|at least|exceed)\s+(\d+\.?\d*)',
        title_blob,
    )
    if m:
        direction = m.group(1)
        return (
            float(m.group(2)),
            direction in ("above", "over", "at least", "exceed", "at or above"),
        )

    # 3. Number-then-keyword ("78° or below", "75F or higher") — the shape
    # Kalshi's yes_sub_title actually uses.
    m = re.search(
        r'(\d+\.?\d*)\s*\u00b0?\s*[fF]?\s+(or above|or below|or higher|or lower|and above|and below)',
        title_blob,
    )
    if m:
        direction = m.group(2)
        return (
            float(m.group(1)),
            direction in ("or above", "or higher", "and above"),
        )

    # 4. Bare "<N" / ">N" characters.
    lt = re.search(r'<\s*(\d+\.?\d*)', title_blob)
    if lt:
        return float(lt.group(1)), False
    gt = re.search(r'>\s*(\d+\.?\d*)', title_blob)
    if gt:
        return float(gt.group(1)), True

    # 5. Bracket markets — direction is irrelevant; just extract a threshold
    # so callers' bracket logic can read floor/cap separately.
    if is_b:
        tm = re.search(r'-B(-?\d+\.?\d*)', ticker_upper)
        if tm:
            return float(tm.group(1)), True
        rm = re.search(
            r'(\d+\.?\d*)\s*\u00b0?[fF]?\s*[-\u2013]\s*(\d+\.?\d*)\s*\u00b0?[fF]?',
            title_blob,
        )
        if rm:
            return (float(rm.group(1)) + float(rm.group(2))) / 2, True

    # No direction information recoverable. Refuse to guess.
    return None, None


def _determine_day_index(title: str, market_data: dict | None = None,
                         city_key: str | None = None) -> int | None:
    """Determine which forecast day to use based on market expiry and title.

    Uses LOCAL STANDARD TIME for date boundaries (matches Kalshi settlement
    rules).  Tries market_data expiry first, then falls back to title heuristics.

    Returns integer day index (0 = today, 1 = tomorrow, ...) or None if the
    settlement date is beyond the 7-day forecast horizon.
    """
    # Compute "today" in the city's local standard time
    offset = _CITY_LST_OFFSET.get(city_key or "", -5)
    lst_tz = timezone(timedelta(hours=offset))
    today_local = datetime.now(lst_tz).date()

    # 1. Try market_data expiry date (most reliable). If ANY expiry field
    # was provided and successfully parsed, we trust it absolutely — even
    # if it's out of the 7-day forecast horizon. The only valid reason to
    # fall through to title heuristics is "no expiry was supplied at all"
    # (test fixtures, malformed inputs). For real Kalshi markets that have
    # already closed (delta < 0), the correct answer is None — we must
    # never produce "today's" forecast for yesterday's settled market.
    saw_valid_expiry = False
    if market_data:
        for field in ("close_time", "expiration_time", "expected_expiration_time"):
            val = market_data.get(field)
            if val and isinstance(val, str):
                try:
                    expiry_dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                expiry_local = expiry_dt.astimezone(lst_tz).date()
                delta = (expiry_local - today_local).days
                saw_valid_expiry = True
                if 0 <= delta < 7:
                    return delta
                # Past or beyond-7-days: refuse to guess. The original
                # bug here was falling through to title heuristics on
                # past-close markets, which silently returned 0
                # (today's forecast) for yesterday's settled market —
                # generating 5-17°F-cold stale predictions in
                # weather_forecast_snapshots for every closed weather
                # ticker. Pin: tests/signals/test_weather_day_index_past.py
                if delta < 0:
                    return None
                print(f"[weather] Settlement {expiry_local} is {delta}d from "
                      f"today ({today_local}) — outside 7-day forecast")
                return None

    # 2. Fall back to title-based heuristics ONLY when no expiry field
    # was successfully parsed. If we saw an expiry that was rejected on
    # range, we already returned None above; we must never reach here
    # with a known-bad expiry.
    if saw_valid_expiry:
        return None

    title_lower = title if title == title.lower() else title.lower()

    if "day after tomorrow" in title_lower:
        return 2
    if "tomorrow" in title_lower:
        return 1

    # Day name matching
    day_names = ["monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday"]
    for i, day_name in enumerate(day_names):
        if day_name in title_lower:
            current_dow = today_local.weekday()  # 0=Monday
            delta = (i - current_dow) % 7
            # delta==0 means title mentions today's day name → use today
            return delta

    # Specific date patterns: "April 8", "apr 14"
    date_match = re.search(
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})',
        title_lower,
    )
    if date_match:
        try:
            month_abbr = date_match.group(1)[:3]
            day_num = int(date_match.group(2))
            month_map = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
            }
            target_month = month_map.get(month_abbr)
            if target_month:
                for yr in (today_local.year, today_local.year + 1):
                    try:
                        target = datetime(yr, target_month, day_num).date()
                        delta = (target - today_local).days
                        if 0 <= delta < 7:
                            return delta
                        if delta >= 0:
                            print(f"[weather] Title date "
                                  f"'{date_match.group(0)}' is {delta}d "
                                  f"out — beyond 7-day forecast")
                            return None
                    except ValueError:
                        continue
        except Exception:
            pass

    return 0  # default: today (no date info in title)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Open-Meteo (free, no auth)
# ══════════════════════════════════════════════════════════════════════════════

def get_weather_forecast(city_key):
    """Fetch 7-day forecast from Open-Meteo for the given city key.

    Cached locally for ``_WEATHER_OM_CACHE_TTL`` seconds (~5.5h) — the
    Open-Meteo default blend (GFS at US lats) only updates every 6h, so
    a 60s TTL via cached_get was wasted bandwidth + quota. Same long-TTL
    pattern as ``hrrr.py`` / ``icon.py`` / ``ukmo.py``.
    """
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None
    cache_key = f"weather_{city_key}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _WEATHER_OM_CACHE_TTL:
            return data
    from bot.signals.sources._openmeteo import forecast_url
    url = forecast_url(
        f"latitude={city['lat']}&longitude={city['lon']}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            print(f"[weather] HTTP {r.status_code} for {city_key}")
            return None
        data = r.json()
        _CACHE[cache_key] = (data, now)
        return data
    except Exception as e:
        print(f"[weather] error for {city_key}: {type(e).__name__}: {e}")
        return None


def _open_meteo_sigma_for_day(day_idx: int) -> float:
    """Open-Meteo (vanilla, no model override) sigma in °F.

    Roughly: 2°F at day 0, +0.6°F per day out. Slightly looser than NBM
    because Open-Meteo's default blend averages multiple models without
    NOAA's NBM bias-correction step. A3 replaces this with fitted σ.
    """
    return 2.0 + day_idx * 0.6


def get_weather_gaussian(ticker, market_data):
    """Return Open-Meteo's temperature distribution for the settlement day.

    Threshold/bracket-independent; ensemble projects per-market.
    """
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = ticker.upper() if ticker else ""

    is_weather = any(kw in title for kw in _WEATHER_KEYWORDS) or "KXHIGH" in ticker_upper
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

    forecast = get_weather_forecast(city_key)
    if not forecast:
        return None

    daily = forecast.get("daily", {})
    temps_max = daily.get("temperature_2m_max", [])
    dates = daily.get("time", [])
    if day_idx >= len(temps_max):
        return None
    forecast_high = temps_max[day_idx]
    if forecast_high is None:
        return None

    # Persist today's forecast high for METAR comparison (same as v1)
    if day_idx == 0:
        _station = _CITY_STATION_MAP.get(city_key)
        if _station:
            try:
                _conn = get_connection()
                _offset = _CITY_LST_OFFSET.get(city_key, -5)
                _lst_date = datetime.now(timezone(timedelta(hours=_offset))).strftime("%Y-%m-%d")
                kv_set(_conn, f"metar_forecast_high_{_station}_{_lst_date}", forecast_high, 86400)
            except Exception:
                pass

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    date_label = dates[day_idx] if day_idx < len(dates) else "?"
    sigma_f = _open_meteo_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    from bot.signals.sources._freshness import open_meteo_latest_issued_at
    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="weather",
        source_tag=f"weather:{city_key}_{date_label}",
        issued_at=open_meteo_latest_issued_at(),
    )


def get_weather_estimate(ticker, market_data):
    """Open-Meteo weather source: estimate probability for weather markets.

    Returns (probability, source_description) or (None, None).
    """
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = ticker.upper() if ticker else ""

    # Detect weather market -- require weather-related keywords + city name
    is_weather = any(kw in title for kw in _WEATHER_KEYWORDS) or "KXHIGH" in ticker_upper
    if not is_weather:
        return None, None

    city_key = _detect_city(ticker_upper, title)
    if not city_key:
        return None, None

    # Extract temperature threshold + direction (API-first, falls back to title text).
    threshold, is_above = _parse_threshold(ticker, market_data)
    if threshold is None:
        return None, None

    # Sanity check: reject obviously non-temperature values
    if threshold < -40 or threshold > 140:
        return None, None

    # Determine which forecast day BEFORE fetching (avoids wasted API calls
    # for markets beyond the 7-day horizon)
    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None:
        return None, None

    forecast = get_weather_forecast(city_key)
    if not forecast:
        return None, None

    daily = forecast.get("daily", {})
    temps_max = daily.get("temperature_2m_max", [])
    temps_min = daily.get("temperature_2m_min", [])
    dates = daily.get("time", [])

    if day_idx >= len(temps_max):
        return None, None

    forecast_high = temps_max[day_idx]
    forecast_low = temps_min[day_idx]

    # Defense 4: Persist today's forecast high to kv_cache for METAR comparison.
    # metar_observations.py reads this via _get_forecast_high() to blend forecast
    # with live observations. Also used by quotes.py weather conditional quoting.
    if day_idx == 0:
        _station = _CITY_STATION_MAP.get(city_key)
        if _station:
            try:
                _conn = get_connection()
                _offset = _CITY_LST_OFFSET.get(city_key, -5)
                _lst_date = datetime.now(timezone(timedelta(hours=_offset))).strftime("%Y-%m-%d")
                kv_set(_conn, f"metar_forecast_high_{_station}_{_lst_date}", forecast_high, 86400)
            except Exception:
                pass  # DB may not be initialized in test contexts

    # Forecast error model: accuracy degrades with forecast horizon
    # Day 0 (today): ~2F error, Day 1: ~3F, Day 3: ~4.5F, Day 7: ~6F
    forecast_sigma = _open_meteo_sigma_for_day(day_idx)  # linear increase in uncertainty

    # Check if this is a bracket market (-B suffix) -- needs CDF(upper) - CDF(lower)
    is_bracket = "-B" in ticker_upper if ticker_upper else False
    if is_bracket:
        # Extract bracket bounds. Priority:
        # 1. API floor_strike / cap_strike (most reliable)
        # 2. Title regex ("83 to 85", "83°F and 85°F", "83-85")
        # 3. Default: ticker -B value as floor, +2°F as cap (Kalshi standard)
        bracket_floor = threshold  # -B suffix value is typically the floor
        bracket_cap = threshold + 2.0  # Kalshi weather brackets are 2°F wide

        # Best: use API-provided strikes
        _api_floor = market_data.get("floor_strike") if market_data else None
        _api_cap = market_data.get("cap_strike") if market_data else None
        if _api_floor is not None and _api_cap is not None:
            try:
                bracket_floor = float(_api_floor)
                bracket_cap = float(_api_cap)
            except (ValueError, TypeError):
                pass
        else:
            # Fallback: parse from title — "83 to 85", "83-85", "83°F and 85°F"
            range_match_b = re.search(
                r'(\d+\.?\d*)\s*\u00b0?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)', title)
            if range_match_b:
                bracket_floor = float(range_match_b.group(1))
                bracket_cap = float(range_match_b.group(2))

        # CDF(cap) - CDF(floor): probability temp falls within bracket
        cdf_upper = _logistic_cdf(bracket_cap, forecast_high, forecast_sigma)
        cdf_lower = _logistic_cdf(bracket_floor, forecast_high, forecast_sigma)
        prob_yes = cdf_upper - cdf_lower
        prob_yes = max(0.02, min(0.98, prob_yes))
        print(f"[info] Weather: {city_key} day={day_idx} forecast_high={forecast_high:.0f}\u00b0F "
              f"bracket=[{bracket_floor:.1f},{bracket_cap:.1f}]\u00b0F "
              f"sigma={forecast_sigma:.1f}\u00b0F \u2192 {prob_yes:.2f}")
    elif is_above:
        # "Will temp be above X?" -- compare forecast high to threshold
        diff = forecast_high - threshold
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))
        prob_yes = max(0.02, min(0.98, prob_yes))
        print(f"[info] Weather: {city_key} day={day_idx} forecast_high={forecast_high:.0f}\u00b0F "
              f"threshold={threshold:.0f}\u00b0F (above) "
              f"sigma={forecast_sigma:.1f}\u00b0F \u2192 {prob_yes:.2f}")
    else:
        # "Will high temp be below X?" -- P(high <= threshold)
        # Use forecast_high (not forecast_low) -- the market is about the HIGH temp.
        # P(high <= T) = logistic_cdf(T, forecast_high, sigma)
        diff = threshold - forecast_high
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))
        prob_yes = max(0.02, min(0.98, prob_yes))
        print(f"[info] Weather: {city_key} day={day_idx} forecast_high={forecast_high:.0f}\u00b0F "
              f"threshold={threshold:.0f}\u00b0F (below) "
              f"sigma={forecast_sigma:.1f}\u00b0F \u2192 {prob_yes:.2f}")

    return prob_yes, f"weather:{city_key}_{dates[day_idx]}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Tomorrow.io (premium weather forecasts, 500 calls/day)
# ══════════════════════════════════════════════════════════════════════════════

def get_tomorrow_forecast(city_key):
    """Fetch forecast from Tomorrow.io (formerly Climacell). Returns dict with
    daily highs/lows in Fahrenheit, or None on failure."""
    if not TOMORROW_API_KEY:
        return None
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None
    # Use dedicated long-TTL cache to stay within 500 calls/day
    # Check in-memory first, then persistent SQLite cache
    now = time.time()
    if city_key in _TOMORROW_CACHE:
        cached_data, cached_ts = _TOMORROW_CACHE[city_key]
        if now - cached_ts < _TOMORROW_TTL:
            return cached_data
    # Check persistent cache (survives across oneshot runs)
    try:
        conn = get_connection()
        db_cached = kv_get(conn, f"tomorrow_{city_key}")
        if db_cached is not None:
            _TOMORROW_CACHE[city_key] = (db_cached, now)
            return db_cached
    except RuntimeError:
        conn = None  # DB not initialized yet
    url = (
        f"https://api.tomorrow.io/v4/weather/forecast?"
        f"location={city['lat']},{city['lon']}"
        f"&timesteps=1d"
        f"&units=imperial"
        f"&apikey={TOMORROW_API_KEY}"
    )
    data = cached_get(f"tomorrow_{city_key}", url, timeout=8)
    if not data:
        return None
    try:
        daily = data.get("timelines", {}).get("daily", [])
        if not daily:
            return None
        # Normalize to same structure as Open-Meteo for reuse
        result = {"daily": {
            "temperature_2m_max": [],
            "temperature_2m_min": [],
            "time": [],
        }}
        for day in daily[:7]:
            values = day.get("values", {})
            high = values.get("temperatureMax")
            low = values.get("temperatureMin")
            date_str = day.get("time", "")[:10]
            if high is not None and low is not None:
                result["daily"]["temperature_2m_max"].append(high)
                result["daily"]["temperature_2m_min"].append(low)
                result["daily"]["time"].append(date_str)
        parsed = result if result["daily"]["temperature_2m_max"] else None
        _TOMORROW_CACHE[city_key] = (parsed, time.time())
        # Persist to SQLite for cross-run caching
        if conn is not None and parsed:
            kv_set(conn, f"tomorrow_{city_key}", parsed, _TOMORROW_TTL)
        return parsed
    except Exception as e:
        print(f"[tomorrow] Parse error for {city_key}: {e}")
        return None


def _tomorrow_sigma_for_day(day_idx: int) -> float:
    """Tomorrow.io per-day sigma. Same schedule as Open-Meteo for now;
    they're at similar skill levels on daily highs. A3 will fit separate σ's."""
    return 2.0 + day_idx * 0.6


def get_tomorrow_gaussian(ticker, market_data):
    """Return Tomorrow.io's temperature distribution for the settlement day."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = ticker.upper() if ticker else ""

    is_weather = any(kw in title for kw in _WEATHER_KEYWORDS) or "KXHIGH" in ticker_upper
    if not is_weather:
        return None

    city_key = _detect_city(ticker_upper, title)
    if not city_key:
        return None

    # Reuse the canonical parser so direction-handling stays in one place.
    # The Gaussian path doesn't need is_above (the combiner projects per-market
    # via probability_for_market downstream), but we still need a threshold.
    threshold, _ = _parse_threshold(ticker, market_data)
    if threshold is None or threshold < -40 or threshold > 140:
        return None

    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None:
        return None

    forecast = get_tomorrow_forecast(city_key)
    if not forecast:
        return None

    daily = forecast.get("daily", {})
    temps_max = daily.get("temperature_2m_max", [])
    dates = daily.get("time", [])
    if day_idx >= len(temps_max):
        return None
    forecast_high = temps_max[day_idx]
    if forecast_high is None:
        return None

    # Persist today's forecast high for METAR comparison (same as v1)
    if day_idx == 0:
        _station = _CITY_STATION_MAP.get(city_key)
        if _station:
            try:
                _conn = get_connection()
                _offset = _CITY_LST_OFFSET.get(city_key, -5)
                _lst_date = datetime.now(timezone(timedelta(hours=_offset))).strftime("%Y-%m-%d")
                kv_set(_conn, f"metar_forecast_high_{_station}_{_lst_date}", forecast_high, 86400)
            except Exception:
                pass

    tz_offset = _CITY_LST_OFFSET.get(city_key, -5)
    date_label = dates[day_idx] if day_idx < len(dates) else "?"
    sigma_f = _tomorrow_sigma_for_day(day_idx)
    horizon_hours = hours_until_settlement_end(tz_offset, day_idx)

    return GaussianForecast(
        mean_f=float(forecast_high),
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="tomorrow",
        source_tag=f"tomorrow:{city_key}_{date_label}",
    )


def get_tomorrow_weather_estimate(ticker, market_data):
    """Tomorrow.io weather source -- same logic as Open-Meteo but different data provider.
    Acts as redundant backup + cross-validation for weather markets.

    Returns (probability, source_description) or (None, None).
    """
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = ticker.upper() if ticker else ""

    is_weather = any(kw in title for kw in _WEATHER_KEYWORDS) or "KXHIGH" in ticker_upper
    if not is_weather:
        return None, None

    city_key = _detect_city(ticker_upper, title)
    if not city_key:
        return None, None

    # Extract threshold + direction via the canonical parser (API-first).
    threshold, is_above = _parse_threshold(ticker, market_data)
    if threshold is None or threshold < -40 or threshold > 140:
        return None, None

    # Determine which forecast day BEFORE fetching (avoids wasted API calls)
    day_idx = _determine_day_index(title, market_data, city_key)
    if day_idx is None:
        return None, None
    # Clamp to Tomorrow.io's reliable forecast horizon. Their API may return
    # data beyond day 7 but accuracy degrades sharply (CLAUDE.md Known Bug
    # Pattern #8). Pinned by tests/signals/test_tomorrow_horizon.py.
    if day_idx > 7:
        return None, None

    forecast = get_tomorrow_forecast(city_key)
    if not forecast:
        return None, None

    daily = forecast.get("daily", {})
    temps_max = daily.get("temperature_2m_max", [])
    temps_min = daily.get("temperature_2m_min", [])
    dates = daily.get("time", [])

    if day_idx >= len(temps_max):
        return None, None

    forecast_high = temps_max[day_idx]
    forecast_low = temps_min[day_idx]
    forecast_sigma = _tomorrow_sigma_for_day(day_idx)

    # Defense 4: Persist today's forecast high for METAR comparison (same as Open-Meteo)
    if day_idx == 0:
        _station = _CITY_STATION_MAP.get(city_key)
        if _station:
            try:
                _conn = get_connection()
                _offset = _CITY_LST_OFFSET.get(city_key, -5)
                _lst_date = datetime.now(timezone(timedelta(hours=_offset))).strftime("%Y-%m-%d")
                kv_set(_conn, f"metar_forecast_high_{_station}_{_lst_date}", forecast_high, 86400)
            except Exception:
                pass

    # Check if this is a bracket market (-B suffix) -- needs CDF(upper) - CDF(lower)
    is_bracket = "-B" in ticker_upper if ticker_upper else False
    if is_bracket:
        # Extract bracket bounds (same logic as Open-Meteo path)
        bracket_floor = threshold
        bracket_cap = threshold + 2.0  # Kalshi weather brackets are 2°F wide

        _api_floor = market_data.get("floor_strike") if market_data else None
        _api_cap = market_data.get("cap_strike") if market_data else None
        if _api_floor is not None and _api_cap is not None:
            try:
                bracket_floor = float(_api_floor)
                bracket_cap = float(_api_cap)
            except (ValueError, TypeError):
                pass
        else:
            range_match_b = re.search(
                r'(\d+\.?\d*)\s*\u00b0?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)', title)
            if range_match_b:
                bracket_floor = float(range_match_b.group(1))
                bracket_cap = float(range_match_b.group(2))

        cdf_upper = _logistic_cdf(bracket_cap, forecast_high, forecast_sigma)
        cdf_lower = _logistic_cdf(bracket_floor, forecast_high, forecast_sigma)
        prob_yes = cdf_upper - cdf_lower
        prob_yes = max(0.02, min(0.98, prob_yes))
        print(f"[tomorrow] Weather: {city_key} day={day_idx} forecast_high={forecast_high:.0f}\u00b0F "
              f"bracket=[{bracket_floor:.1f},{bracket_cap:.1f}]\u00b0F "
              f"sigma={forecast_sigma:.1f}\u00b0F \u2192 {prob_yes:.2f}")
    elif is_above:
        diff = forecast_high - threshold
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))
        prob_yes = max(0.02, min(0.98, prob_yes))
        print(f"[tomorrow] Weather: {city_key} day={day_idx} high={forecast_high:.0f}\u00b0F "
              f"threshold={threshold:.0f}\u00b0F (above) sigma={forecast_sigma:.1f}\u00b0F \u2192 {prob_yes:.2f}")
    else:
        # "Will high temp be below X?" -- P(high <= threshold)
        # Use forecast_high (not forecast_low) -- the market is about the HIGH temp.
        diff = threshold - forecast_high
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))
        prob_yes = max(0.02, min(0.98, prob_yes))
        print(f"[tomorrow] Weather: {city_key} day={day_idx} high={forecast_high:.0f}\u00b0F "
              f"threshold={threshold:.0f}\u00b0F (below) sigma={forecast_sigma:.1f}\u00b0F \u2192 {prob_yes:.2f}")

    return prob_yes, f"tomorrow:{city_key}_{dates[day_idx] if day_idx < len(dates) else '?'}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. NOAA Weather Alerts (severe weather events, free, no auth)
# ══════════════════════════════════════════════════════════════════════════════

def get_noaa_alerts_for_market(ticker, market_data):
    """Check NOAA active alerts for weather-event markets.
    Catches hurricane, tornado, heat wave, freeze, and extreme weather markets
    that go beyond simple temperature forecasts.
    Returns (adjusted_probability, source_desc) or (None, None)."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Map alert-type keywords to NOAA alert event types
    alert_keywords = {
        "hurricane": ["Hurricane", "Tropical Storm"],
        "tornado": ["Tornado"],
        "heat wave": ["Excessive Heat", "Heat Advisory"],
        "heat": ["Excessive Heat", "Heat Advisory"],
        "freeze": ["Freeze Warning", "Frost Advisory", "Hard Freeze"],
        "frost": ["Freeze Warning", "Frost Advisory"],
        "blizzard": ["Blizzard", "Winter Storm"],
        "snow": ["Winter Storm", "Winter Weather Advisory"],
        "flood": ["Flood", "Flash Flood"],
        "wildfire": ["Fire Weather", "Red Flag Warning"],
    }

    matched_events = None
    for kw, events in alert_keywords.items():
        if kw in title:
            matched_events = events
            break

    if not matched_events:
        return None, None

    # Determine geographic scope -- check for state/region mentions
    # NOAA alerts API supports area codes (state abbreviations)
    state_map = {
        "florida": "FL", "texas": "TX", "california": "CA", "new york": "NY",
        "louisiana": "LA", "mississippi": "MS", "alabama": "AL", "georgia": "GA",
        "north carolina": "NC", "south carolina": "SC", "virginia": "VA",
        "oklahoma": "OK", "kansas": "KS", "nebraska": "NE", "iowa": "IA",
        "colorado": "CO", "arizona": "AZ", "nevada": "NV", "oregon": "OR",
        "washington": "WA", "illinois": "IL", "ohio": "OH", "michigan": "MI",
        "pennsylvania": "PA", "new jersey": "NJ", "massachusetts": "MA",
    }
    area = None
    for state_name, code in state_map.items():
        if state_name in title:
            area = code
            break

    # Fetch active alerts
    cache_key = f"noaa_alerts_{area or 'US'}"
    url = "https://api.weather.gov/alerts/active?status=actual&message_type=alert"
    if area:
        url += f"&area={area}"
    else:
        url += "&limit=50"

    alerts_data = cached_get(cache_key, url, timeout=8)
    if not alerts_data:
        return None, None

    features = alerts_data.get("features", [])
    if not features:
        # No active alerts -- return None so we don't pollute the ensemble with a guess
        return None, None

    # Count matching alerts
    matching = 0
    for feat in features:
        props = feat.get("properties", {})
        event = props.get("event", "")
        if any(me.lower() in event.lower() for me in matched_events):
            matching += 1

    if matching > 0:
        # Active alerts exist -> high probability (scaled by count)
        prob = min(0.90, 0.60 + matching * 0.10)
        print(f"[noaa] {matching} active '{matched_events[0]}' alerts "
              f"{'in ' + area if area else 'nationwide'} \u2192 prob={prob:.2f}")
        return prob, f"noaa:{matching}alerts:{matched_events[0][:20]}"
    else:
        # Active alerts exist but none match -- not informative, return None
        return None, None
