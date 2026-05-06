"""5-minute ASOS observations via NWS api.weather.gov (free, no auth).

NWS's `/stations/{station}/observations` endpoint serves ASOS station
data at 5-minute cadence for most ASOS-equipped airports — verified
2026-04-30 against KMIA, KMDW, KLAX, KAUS, KDEN, KLGA, KORD, KIAH, KSFO.
This is the only known free, no-auth, no-registration sub-hourly
observation channel that covers the airports Kalshi settles on.

Caveats discovered during verification:

  * **KNYC (Central Park) is hourly-only.** It's a manual + ASOS hybrid
    that does not publish 5-min data. For NYC sub-hourly we use KLGA
    (LaGuardia, ~7 miles away) as a proxy — different microclimate
    means a small MOS bias vs the KNYC ground truth Kalshi settles
    on. Bias must be fit + applied per the standard MOS pipeline; see
    PRIMARY_5MIN_STATION_BY_CITY.
  * **Temperature precision is integer-Celsius** in the 5-min stream
    (~1°F resolution), vs 0.1°C in the hourly METAR. Acceptable for
    daily-high tracking — at most 0.5°C / 0.9°F error in peak capture.
  * **Cadence is "best effort" 5-min**, not guaranteed. Sometimes a
    minute slips. The verification harness measures actual `obs_age`.
  * NWS asks for a User-Agent header identifying the caller.

DARK MODE — NOT yet wired into ``weather_ensemble_v2._collect_gaussians``.
The verification harness in ``tools/verify_nws_5min_freshness.py`` should
demonstrate live correctness over a multi-day window before flipping live.
Once verified, add ``"nws_5min"`` to ``GAUSSIAN_COMBINE_SOURCES`` and the
getter list in ``_collect_gaussians``.

LST-window gating: in production, callers should only invoke this source
during the local peak-heating window (LST 11-17). Outside that window,
hourly METAR + the running-high floor handle observation tracking
adequately. See ``bot/daemon/stations.py:lst_offset_for_station``.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.daemon.stations import (
    STATION_BY_SERIES,
    lst_offset_for_station,
    station_for_ticker,
)
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


_API_BASE = "https://api.weather.gov/stations"
_USER_AGENT = "kalshi-trading-bot (joshlu@a16z.com)"
_CACHE_TTL_S = 240.0  # 4 minutes — just under the 5-min publication cadence
_SOURCE_NAME = "nws_5min"
_HTTP_TIMEOUT_S = 10.0
# Freshness gate. Empirically NWS api.weather.gov publishes 5-min ASOS
# observations at a 5-minute *granularity* but with **5–25 minutes of
# publication latency** that varies through the day (verified live on
# 2026-04-30: morning probe showed ~5 min lag; afternoon probe showed
# 15-22 min lag across all 6 cities). 30 min is a generous gate that
# still rejects truly stale readings (e.g., end-of-day ASOS dropouts)
# while accepting the typical lagged-but-real cases. The gate sits
# above _CACHE_TTL_S so a stale-but-cached response still flows
# through the source for a single cycle, after which the cache
# expires and we re-fetch.
_MAX_OBS_AGE_S = 1800
_OBSERVATIONS_PER_FETCH = 144  # ~last 12h of 5-min readings (need full
                              # day window so we can extract today's max,
                              # not just the latest reading)

# LST hour gate: don't fire as a "forecast" of today's peak before this
# hour. Pre-noon, the day's running_high is meaningless as a peak signal
# (we're still in the warm-up phase) and using the latest reading as μ
# silently gives the ensemble a -6°F bias (verified live 2026-05-01:
# nws_5min had MAE 6.23°F + bias -6.23°F across 6 cities when called
# at 12-16 UTC = pre-dawn LST). At 11:00 LST we're typically within
# 4-5°F of the eventual peak; the running max becomes a real signal.
_MIN_LST_HOUR_TO_FIRE: int = 11

# σ schedule: how much we trust the latest observation as a predictor of
# the eventual daily high, by hours-to-settlement. Mirrors METAR's
# schedule but is the OBSERVATION σ — depends on time-of-day, not
# forecast horizon. Same shape works because the underlying physical
# process (temperature trajectory toward peak) is the same.
_SIGMA_SCHEDULE: tuple[tuple[float, float], ...] = (
    (0.0, 0.1),    # high already past, σ tiny
    (0.5, 0.3),
    (1.0, 0.5),
    (2.0, 0.8),
    (4.0, 1.2),
    (8.0, 1.6),
    (12.0, 2.0),
)


# Sub-hourly proxy station per Kalshi city. Cities that publish 5-min
# observations at the SAME station Kalshi settles on get an entry; cities
# without a 5-min feed at the settlement station are deliberately
# excluded — see NYC note below.
#
# 2026-05-04 — NYC removed (postmortem on cross-bracket canary loss):
# KNYC (Central Park, Kalshi settlement station) is hourly-only. The
# original design substituted KLGA for sub-hourly resolution. Live data
# showed KLGA ran 3-5°F warmer than KNYC simultaneously (Queens marine
# layer + urban heat vs Central Park). With nws_5min reporting σ=1.4°F
# (very confident), it locked in 5°F of upward bias on every NYC quote
# in the precision-weighted combine. Yesterday's KXHIGHNY-26MAY03 canary
# lost $1.45 because nws_5min said 64.6°F when KNYC actually peaked at
# 59°F — exactly because KLGA was 5°F warmer at the same moment.
#
# A learned MOS-bias correction (KLGA→KNYC delta) is the long-term fix.
# Until then, NYC is excluded — METAR (which polls KNYC directly) carries
# the observation channel for NYC and matches truth at settlement.
PRIMARY_5MIN_STATION_BY_CITY: dict[str, str] = {
    # "nyc":     intentionally absent — see header note above
    "chicago":     "KMDW",
    "los_angeles": "KLAX",
    "austin":      "KAUS",
    "miami":       "KMIA",
    "denver":      "KDEN",
}


def _sigma_for_hours_left(hours_left: float) -> float:
    """Piecewise-linear σ vs hours_left."""
    if hours_left <= 0:
        return _SIGMA_SCHEDULE[0][1]
    for i in range(len(_SIGMA_SCHEDULE) - 1):
        h0, s0 = _SIGMA_SCHEDULE[i]
        h1, s1 = _SIGMA_SCHEDULE[i + 1]
        if h0 <= hours_left < h1:
            t = (hours_left - h0) / max(1e-9, h1 - h0)
            return s0 + t * (s1 - s0)
    return _SIGMA_SCHEDULE[-1][1]


def _city_for_station(icao: str) -> Optional[str]:
    """Inverse map for the 5-min poll station → city key. Used for MOS
    bias keying so the bias is fit per (source, city) the same way the
    existing pipeline expects."""
    for city, stn in PRIMARY_5MIN_STATION_BY_CITY.items():
        if stn == icao:
            return city
    return None


def fetch_recent_observations(
    icao: str, max_age_s: float = _MAX_OBS_AGE_S,
) -> Optional[list[dict]]:
    """Fetch the last ~hour of observations for ``icao`` from NWS.

    Returns a list of dicts ``[{"temp_f": ..., "obs_time_utc": ...,
    "is_metar": bool, "raw": ...}]`` ordered newest-first, filtered to
    observations within ``max_age_s`` of now.

    Returns None on fetch failure. Returns an empty list when the
    endpoint succeeds but no recent obs match the freshness gate.

    Caches the parsed list for ``_CACHE_TTL_S`` seconds keyed by ICAO.
    """
    cache_key = f"nws_5min::{icao}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached is not None:
        data, ts = cached
        if now - ts < _CACHE_TTL_S:
            return data

    url = f"{_API_BASE}/{icao}/observations?limit={_OBSERVATIONS_PER_FETCH}"
    try:
        rate_limit_wait(url)
        r = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/geo+json"},
            timeout=_HTTP_TIMEOUT_S,
        )
        if r.status_code != 200:
            print(f"[nws_5min] HTTP {r.status_code} for {icao}: {r.text[:200]}")
            _CACHE[cache_key] = (None, now)
            return None
        body = r.json()
    except Exception as e:
        print(f"[nws_5min] {icao}: {type(e).__name__}: {e}")
        _CACHE[cache_key] = (None, now)
        return None

    features = body.get("features") or []
    out: list[dict] = []
    now_utc = datetime.now(timezone.utc)
    for f in features:
        props = f.get("properties") or {}
        ts_str = props.get("timestamp")
        temp = props.get("temperature") or {}
        temp_c = temp.get("value")
        if ts_str is None or temp_c is None:
            continue
        try:
            obs_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            temp_c_val = float(temp_c)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(temp_c_val) or temp_c_val < -60 or temp_c_val > 60:
            continue
        age_s = (now_utc - obs_time).total_seconds()
        if age_s > max_age_s:
            continue
        temp_f = temp_c_val * 9.0 / 5.0 + 32.0
        is_metar = bool((props.get("rawMessage") or "").strip())
        out.append({
            "temp_f": temp_f,
            "obs_time_utc": obs_time,
            "is_metar": is_metar,
            "raw": props,
        })

    _CACHE[cache_key] = (out, now)
    return out


def get_recent_max_temp_f(icao: str) -> Optional[tuple[float, datetime]]:
    """Return ``(max_temp_f, obs_time_utc)`` over the last hour of obs.

    Used by the running-high tracker: the highest observed temp in the
    last 60 min is more useful than just the latest reading because
    5-min readings can dip 1°C below the actual peak due to integer
    rounding.

    Returns None when no fresh observations are available.
    """
    obs = fetch_recent_observations(icao)
    if not obs:
        return None
    best = max(obs, key=lambda o: o["temp_f"])
    return (best["temp_f"], best["obs_time_utc"])


def get_today_running_high_f(
    icao: str, lst_offset: int,
) -> Optional[tuple[float, datetime]]:
    """Return ``(today_max_temp_f, obs_time_utc_of_max)`` from the last
    12 hours of NWS observations, restricted to today's LST date.

    Why this exists: ``get_recent_max_temp_f`` only looks at the last
    hour, which gives a stale snapshot (e.g. at 8 AM LST the last
    hour's max is the dawn temp, not the day's peak). For the
    ``get_nws_5min_gaussian`` "what's today's high?" use case we need
    today's running max across ALL of today's readings.

    Returns None when no observations from today are present in the
    fetched window (very early morning, day rollover edge cases).
    """
    obs = fetch_recent_observations(icao)
    if not obs:
        return None
    # LST offset is fixed per station; computing "today's LST date"
    # from current UTC + offset matches the same convention METAR
    # uses for daily_high tracking.
    from datetime import timedelta
    lst_now = datetime.now(timezone.utc) + timedelta(hours=lst_offset)
    today_lst_date = lst_now.strftime("%Y-%m-%d")
    today_obs = [
        o for o in obs
        if (o["obs_time_utc"] + timedelta(hours=lst_offset)).strftime("%Y-%m-%d")
            == today_lst_date
    ]
    if not today_obs:
        return None
    best = max(today_obs, key=lambda o: o["temp_f"])
    return (best["temp_f"], best["obs_time_utc"])


def get_nws_5min_gaussian(
    ticker: str, market_data: dict,
) -> Optional[GaussianForecast]:
    """Sub-hourly observation Gaussian for today's daily-high.

    Returns ``None`` BEFORE LST hour _MIN_LST_HOUR_TO_FIRE (default 11):
    pre-noon the running max is meaningless as a peak signal — using
    the dawn temperature as a "forecast" for today's peak silently
    drags ensemble μ -6°F (verified 2026-05-01).

    From LST 11+ onward returns ``mean_f = today's running max``
    (computed from the last 12h of NWS 5-min readings, filtered to
    today's LST date), with σ scaling by hours-to-settlement.

    Same contract as ``metar_observations.get_metar_gaussian``.
    """
    ws = station_for_ticker((ticker or "").upper())
    if ws is None:
        return None

    # Resolve city → 5-min poll station. Cities without a 5-min feed at
    # the settlement station (currently: NYC) are intentionally absent
    # from the map and skipped here. See PRIMARY_5MIN_STATION_BY_CITY's
    # docstring for the rationale.
    city = ws.city.lower().replace(" ", "_")
    poll_station = PRIMARY_5MIN_STATION_BY_CITY.get(city)
    if poll_station is None:
        return None

    lst_offset = lst_offset_for_station(ws.icao)

    # ── LST-hour gate ──────────────────────────────────────────────
    # Skip pre-LST-11: the day hasn't warmed up enough for the running
    # max to be a real peak signal. Returning None here means the
    # ensemble silently drops nws_5min from the obs group on early-
    # morning calls and METAR carries the observation channel alone.
    from datetime import timedelta as _td
    lst_now = datetime.now(timezone.utc) + _td(hours=lst_offset)
    if lst_now.hour < _MIN_LST_HOUR_TO_FIRE:
        return None

    # ── Fetch today's running max (not just last hour) ─────────────
    pair = get_today_running_high_f(poll_station, lst_offset)
    if pair is None:
        return None
    temp_f, obs_time = pair

    obs_age_s = (datetime.now(timezone.utc) - obs_time).total_seconds()
    if obs_age_s > _MAX_OBS_AGE_S:
        return None

    horizon_hours = hours_until_settlement_end(lst_offset, day_idx=0)
    sigma_f = _sigma_for_hours_left(horizon_hours)

    return GaussianForecast(
        mean_f=temp_f,
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name=_SOURCE_NAME,
        source_tag=f"{_SOURCE_NAME}:{poll_station}",
        issued_at=obs_time.timestamp(),
    )
