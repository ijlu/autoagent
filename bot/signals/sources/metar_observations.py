"""Real-time METAR weather observations for Kalshi high-temperature markets.

Unlike Open-Meteo and Tomorrow.io (which provide FORECASTS), this source uses
live Aviation Weather METAR observations from the actual NWS settlement stations.
As the day progresses, uncertainty shrinks -- if the running daily high already
exceeds the threshold, probability goes near 1.0 regardless of what forecasts say.

API: https://aviationweather.gov/api/data/metar (free, no auth, 1-5 min updates)
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.daemon.stations import (
    STATION_BY_SERIES,
    lst_offset_for_station,
    station_for_ticker,
)
from bot.db import get_connection, kv_get, kv_set
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# Station list driven by the canonical registry (T1.1) — the daemon poller,
# signal source, MADIS basket, and smart_gates all resolve to the same
# primary ICAOs. Sorted for deterministic URL for cache-hit stability.
_METAR_URL = (
    "https://aviationweather.gov/api/data/metar"
    "?ids=" + ",".join(sorted(s.icao for s in STATION_BY_SERIES.values()))
    + "&format=json"
)
_METAR_CACHE_KEY = "metar_obs"
_METAR_CACHE_TTL = 300  # 5 minutes

# CRITICAL: Settlement uses LOCAL STANDARD TIME (LST), NOT daylight saving.
# During DST months NYC is UTC-4 locally, but settlement boundary is always UTC-5.
# Offsets come from the canonical registry — do NOT maintain a parallel table.

# KV cache TTL for daily high tracking (25 hours -- covers full settlement day + buffer)
_DAILY_HIGH_TTL = 90_000


# ── A4: learned per-(station, LST hour) diurnal fit ──
#
# Written by tools/backfill_weather_effective_n.py --persist-diurnal.
# Key prefix constant is pinned by the drift-guard test.
_DIURNAL_KEY_PREFIX: str = "weather_metar_diurnal_"
# Hard clamps on learned σ at read time — mirrors the persist-side guard
# and also protects against corrupted kv payloads.
_DIURNAL_SIGMA_FLOOR_F: float = 0.3
_DIURNAL_SIGMA_CEIL_F: float = 12.0

# Cold-start σ floor applied when kv lookup for forecast_high returns None
# (kv not yet populated post-restart). 10°F captures "we have an
# observation but no forecast — eventual peak could be anywhere within a
# typical day's range." The combiner downweights this Gaussian heavily
# (precision ∝ 1/σ²), so other sources dominate until kv fills in.
_COLD_START_SIGMA_F: float = 10.0


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard logistic CDF for temperature probability estimation."""
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _get_lst_now(station: str) -> datetime:
    """Return current datetime in the station's LOCAL STANDARD TIME (fixed offset)."""
    offset_hours = lst_offset_for_station(station)
    lst_tz = timezone(timedelta(hours=offset_hours))
    return datetime.now(lst_tz)


def _get_lst_date(station: str) -> str:
    """Return today's date string (YYYY-MM-DD) in the station's LST."""
    return _get_lst_now(station).strftime("%Y-%m-%d")


def _hours_remaining_in_settlement_day(station: str) -> float:
    """Hours remaining until 11:59 PM LST (settlement day end).

    Settlement period: 12:00 AM - 11:59 PM LST.
    """
    now_lst = _get_lst_now(station)
    end_of_day = now_lst.replace(hour=23, minute=59, second=59, microsecond=0)
    remaining = (end_of_day - now_lst).total_seconds() / 3600.0
    return max(0.0, remaining)


def _parse_threshold_from_market(ticker: str, title: str) -> tuple[float | None, bool]:
    """Extract temperature threshold and direction from market title or ticker.

    Returns (threshold, is_above) or (None, True) on failure.

    Examples:
        "Will the high temperature in NYC be 75 deg F or above?" -> (75, True)
        "72° or below"                                           -> (72, False)
        Ticker KXHIGHNY-26APR14-T75 -> (75, True)
        Ticker KXHIGHNY-26APR14-B74 -> (74, True)  # brackets handle direction separately
    """
    # Pattern 1: direction keyword BEFORE number — "at or above 75", "below 68"
    temp_match = re.search(
        r'(at or above|at or below|above|below|over|under|at least|exceed)\s+(\d+\.?\d*)',
        title,
    )
    if temp_match:
        direction = temp_match.group(1)
        threshold = float(temp_match.group(2))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")
        return threshold, is_above

    # Pattern 2: number BEFORE direction keyword — "72° or below", "75°F or above"
    # This is common in Kalshi contract labels (e.g., "72° or below").
    reverse_match = re.search(
        r'(\d+\.?\d*)\s*°?\s*[fF]?\s+(or above|or below|and above|and below)',
        title,
    )
    if reverse_match:
        threshold = float(reverse_match.group(1))
        direction = reverse_match.group(2)
        is_above = direction in ("or above", "and above")
        return threshold, is_above

    # Try ticker: -T75, -B74
    tick_match = re.search(r'-[TB](-?\d+\.?\d*)', ticker)
    if tick_match:
        return float(tick_match.group(1)), True  # default to "above" for tickers

    # Try bare number near degree symbol: "75°F"
    deg_match = re.search(r'(\d+\.?\d*)\s*°\s*[fF]', title)
    if deg_match:
        # Check surrounding context for direction
        full_text = title.lower()
        threshold = float(deg_match.group(1))
        if 'or below' in full_text or 'under' in full_text or 'at most' in full_text:
            return threshold, False
        return threshold, True  # default to "above"

    return None, True


# ══════════════════════════════════════════════════════════════════════════════
# METAR API fetch
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_metar_data() -> list[dict] | None:
    """Fetch METAR observations for all tracked stations.

    Returns JSON array on success, None on failure.
    Uses in-memory _CACHE with 5-minute TTL.
    """
    now = time.time()
    if _METAR_CACHE_KEY in _CACHE:
        cached_data, cached_ts = _CACHE[_METAR_CACHE_KEY]
        if now - cached_ts < _METAR_CACHE_TTL:
            return cached_data

    try:
        rate_limit_wait(_METAR_URL)
        r = requests.get(_METAR_URL, timeout=8, headers={
            "User-Agent": "KalshiTradingBot/1.0 (contact: bot@example.com)",
            "Accept": "application/json",
        })
        if r.status_code != 200:
            print(f"[metar] API HTTP {r.status_code}")
            return None
        data = r.json()
        if not isinstance(data, list):
            print(f"[metar] Unexpected response type: {type(data).__name__}")
            return None
        _CACHE[_METAR_CACHE_KEY] = (data, now)
        return data
    except Exception as e:
        print(f"[metar] API error: {type(e).__name__}: {e}")
        return None


def _extract_station_obs(metar_data: list[dict], station: str) -> dict | None:
    """Find the observation record for a specific station ID."""
    for obs in metar_data:
        if obs.get("icaoId") == station or obs.get("stationId") == station:
            return obs
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Freshness check (used by the trade-decision METAR-required gate)
# ══════════════════════════════════════════════════════════════════════════════

# Default freshness window. METAR transmits hourly with SPECIs for
# significant changes; IEM serves with 5-15 min latency; we poll every
# 30s. So the typical "fresh" reading is < 20 min old. 30 min gives a
# small buffer for IEM hiccups. Anything older means the station may be
# down or the daemon's poller is stalled — refuse to trade.
DEFAULT_METAR_FRESHNESS_S: float = 30 * 60


def is_metar_fresh_for_ticker(
    ticker: str, max_age_seconds: float = DEFAULT_METAR_FRESHNESS_S
) -> bool:
    """True iff this ticker's primary station has a recent METAR observation.

    Used by the directional shadow + weather quoter to refuse to trade
    when METAR is the only way we know the actual temperature trajectory
    and that data is stale. Without fresh METAR our combined μ falls back
    to forecast-only sources (HRRR, NWS Point, Open-Meteo) which the
    2026-04-29 validation showed are systematically cold-biased by 1-3°F.

    Returns True when ``metar_daily_high_<station>_<date>``'s
    ``last_obs_time`` is within ``max_age_seconds`` of now. Returns False
    on any failure (no station mapping, no kv entry, parse error) — fail
    closed, since "couldn't verify METAR is fresh" should block trading
    just as much as "definitely stale."
    """
    station_record = station_for_ticker(ticker)
    if station_record is None:
        return False
    # station_for_ticker returns a WeatherStation dataclass; the kv_cache
    # daily-high keys use the bare ICAO string (e.g., "KNYC"), so extract.
    station = station_record.icao if hasattr(station_record, "icao") else str(station_record)
    date_lst = _get_lst_date(station)
    kv_key = f"metar_daily_high_{station}_{date_lst}"
    try:
        conn = get_connection()
        record = kv_get(conn, kv_key)
    except (RuntimeError, Exception):
        return False
    if not isinstance(record, dict):
        return False
    last_obs_time = record.get("last_obs_time")
    if not last_obs_time:
        return False
    try:
        # last_obs_time is the METAR observation timestamp (Z-suffixed ISO)
        if last_obs_time.endswith("Z"):
            obs_dt = datetime.fromisoformat(last_obs_time[:-1] + "+00:00")
        else:
            obs_dt = datetime.fromisoformat(last_obs_time)
        age_seconds = (datetime.now(timezone.utc) - obs_dt).total_seconds()
        return age_seconds <= max_age_seconds
    except (ValueError, TypeError):
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Daily high tracker
# ══════════════════════════════════════════════════════════════════════════════

def _update_running_daily_high(station: str, temp_f: float, obs_time: str) -> dict:
    """Update the running daily high for a station using persistent kv_cache.

    Returns the updated daily-high record:
        {"high_f": float, "last_obs_time": str, "obs_count": int}
    """
    date_lst = _get_lst_date(station)
    kv_key = f"metar_daily_high_{station}_{date_lst}"

    try:
        conn = get_connection()
    except RuntimeError:
        # DB not initialized -- return ephemeral record
        return {"high_f": temp_f, "last_obs_time": obs_time, "obs_count": 1}

    existing = kv_get(conn, kv_key)

    if existing is None:
        record = {"high_f": temp_f, "last_obs_time": obs_time, "obs_count": 1}
    else:
        record = existing
        record["obs_count"] = record.get("obs_count", 0) + 1
        record["last_obs_time"] = obs_time
        if temp_f > record.get("high_f", -999):
            record["high_f"] = temp_f

    kv_set(conn, kv_key, record, _DAILY_HIGH_TTL)
    return record


# ══════════════════════════════════════════════════════════════════════════════
# Forecast high integration
# ══════════════════════════════════════════════════════════════════════════════

def _get_forecast_high(station: str, running_high: float) -> Optional[float]:
    """Retrieve the cached forecast high for today, or ``None`` when the
    kv cache hasn't been populated yet.

    The kv key is written by the forecast sources (weather, HRRR, NBM,
    NWS Point) when they first fire for ``day_idx == 0``. After daemon
    restart there's a window of 1-2 cycles where this key is missing.

    Cold-start audit (2026-04-28): the previous fallback returned
    ``running_high + 5°F`` — i.e. a guess based on current temperature.
    On hot days that guess is way too low (running_high at 9 AM is
    ~10-15°F below the eventual peak). The combiner's
    ``_metar_expected_eventual_high`` blends this fallback at high
    forecast_weight early in the day, collapsing METAR's μ toward
    running_high. Net effect: METAR contributes a 5-10°F-too-low
    Gaussian during the cold-start window. By mid-afternoon the kv
    populates and METAR works correctly, but the morning damage is done
    on hot-day projections.

    Returning None lets the caller widen σ rather than guess. The caller
    (``get_metar_gaussian``) treats a None forecast as "no extra info
    beyond running_high" and applies the wider hours-left σ schedule
    until kv populates.
    """
    date_lst = _get_lst_date(station)
    forecast_key = f"metar_forecast_high_{station}_{date_lst}"

    try:
        conn = get_connection()
        cached_forecast = kv_get(conn, forecast_key)
        if cached_forecast is not None and isinstance(cached_forecast, (int, float)):
            return float(cached_forecast)
    except RuntimeError:
        pass

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Probability model
# ══════════════════════════════════════════════════════════════════════════════

def _compute_probability(
    running_high: float,
    threshold: float,
    hours_left: float,
    forecast_high: float,
) -> float:
    """Compute P(daily high >= threshold) given observations so far.

    Key insight: as the day progresses and more observations arrive, our
    uncertainty about the eventual daily high shrinks.

    Model:
      - If running_high already >= threshold: near certainty (0.95-0.98),
        with small allowance for NWS reporting adjustments.
      - Otherwise: estimate expected eventual high and remaining uncertainty.
        mu  = max(running_high, weighted blend of forecast and running high)
        sigma decreases as hours_left decreases (more certainty late in day)
    """
    # Already observed above threshold -- near certainty
    if running_high >= threshold:
        # Slight uncertainty for potential NWS reporting discrepancy (rounding, station drift)
        margin = running_high - threshold
        if margin >= 3.0:
            return 0.98
        elif margin >= 1.0:
            return 0.96
        else:
            return 0.95

    # Not yet at threshold -- model remaining warming potential
    gap = threshold - running_high

    # Expected eventual high: blend forecast with observations based on time of day
    # Early in the day (many hours left), trust the forecast more.
    # Late in the day (few hours left), trust the running high more.
    total_day_hours = 24.0
    day_fraction_elapsed = max(0.0, min(1.0, 1.0 - hours_left / total_day_hours))

    # Weighted mu: early -> forecast dominates, late -> running high dominates
    if hours_left > 0:
        forecast_weight = max(0.1, 1.0 - day_fraction_elapsed)
        obs_weight = 1.0 - forecast_weight
        expected_eventual_high = (
            forecast_weight * max(forecast_high, running_high)
            + obs_weight * running_high
        )
    else:
        # Day is over -- the running high IS the final high
        expected_eventual_high = running_high

    # Sigma: uncertainty decreases as hours remaining decrease
    # 12+ hours: ~2.0 deg F (full forecast uncertainty)
    #  6 hours:  ~1.5 deg F
    #  2 hours:  ~0.8 deg F
    #  <1 hour:  ~0.3 deg F
    #  0 hours:  ~0.1 deg F (essentially zero -- day is done)
    if hours_left <= 0:
        sigma = 0.1
    elif hours_left < 1:
        sigma = 0.3
    elif hours_left < 2:
        sigma = 0.5 + (hours_left - 1.0) * 0.3  # 0.5 to 0.8
    elif hours_left < 6:
        sigma = 0.8 + (hours_left - 2.0) * 0.175  # 0.8 to 1.5
    elif hours_left < 12:
        sigma = 1.5 + (hours_left - 6.0) * 0.083  # 1.5 to 2.0
    else:
        sigma = 2.0

    # Probability = P(eventual_high >= threshold) via logistic CDF
    # _logistic_cdf(threshold, mu, sigma) gives P(X <= threshold),
    # so P(X >= threshold) = 1 - _logistic_cdf(threshold, mu, sigma)
    prob = 1.0 - _logistic_cdf(threshold, expected_eventual_high, sigma)

    # Clamp to valid range
    return max(0.02, min(0.98, prob))


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def _metar_expected_eventual_high(
    running_high: float, forecast_high: float, hours_left: float
) -> float:
    """Blended expected eventual daily high.

    Early in the day (many hours left) → trust the forecast more.
    Late in the day (few hours left) → trust the running observation more.
    eventual_high is always ≥ running_high (daily HIGH only rises).
    """
    if hours_left <= 0:
        return running_high
    total_day_hours = 24.0
    day_fraction_elapsed = max(0.0, min(1.0, 1.0 - hours_left / total_day_hours))
    forecast_weight = max(0.1, 1.0 - day_fraction_elapsed)
    obs_weight = 1.0 - forecast_weight
    return (
        forecast_weight * max(forecast_high, running_high)
        + obs_weight * running_high
    )


def _get_diurnal_fit(station: str, lst_hour: int) -> tuple[float, float, float] | None:
    """Return ``(alpha, beta, rmse)`` for (station, lst_hour) or None.

    Looks up ``weather_metar_diurnal_<station>`` in kv_cache. Missing keys,
    malformed payloads, unknown hours, or out-of-band σ all return None —
    the caller falls back to the v1 naive blend.
    """
    try:
        conn = get_connection()
    except RuntimeError:
        return None
    payload = kv_get(conn, f"{_DIURNAL_KEY_PREFIX}{station}")
    if not isinstance(payload, dict):
        return None
    hours = payload.get("hours")
    if not isinstance(hours, dict):
        return None
    cell = hours.get(str(lst_hour))
    if not isinstance(cell, dict):
        return None
    try:
        alpha = float(cell["alpha"])
        beta = float(cell["beta"])
        rmse = float(cell["rmse"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(alpha) and math.isfinite(beta) and math.isfinite(rmse)):
        return None
    if rmse < _DIURNAL_SIGMA_FLOOR_F or rmse > _DIURNAL_SIGMA_CEIL_F:
        return None
    return alpha, beta, rmse


def get_metar_gaussian(ticker: str, market_data: dict) -> GaussianForecast | None:
    """Return METAR's temperature distribution for the settlement day.

    Unlike the v1 probability entry point, this does NOT short-circuit
    to 0.95/0.96/0.98 when the running high already exceeds a threshold.
    Instead it always emits ``GaussianForecast(expected_eventual_high,
    sigma_for_hours_left)``; downstream ``probability_for_market`` will
    produce an analogous near-certainty value when threshold ≪ mean
    without the hand-coded margin table.

    Threshold/bracket-independent; per-ticker projection is done by the
    ensemble combiner.
    """
    ticker_upper = ticker.upper() if ticker else ""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    ws = station_for_ticker(ticker_upper)
    if ws is None:
        return None
    station = ws.icao

    threshold, _ = _parse_threshold_from_market(ticker, title)
    if threshold is None or threshold < -40 or threshold > 140:
        return None

    metar_data = _fetch_metar_data()
    if metar_data is None:
        return None
    obs = _extract_station_obs(metar_data, station)
    if obs is None:
        return None

    temp_c = obs.get("temp")
    if temp_c is None:
        return None
    try:
        temp_c = float(temp_c)
    except (TypeError, ValueError):
        return None
    temp_f = temp_c * 9.0 / 5.0 + 32.0
    obs_time = obs.get("reportTime") or obs.get("obsTime") or ""

    daily_record = _update_running_daily_high(station, temp_f, obs_time)
    running_high = float(daily_record["high_f"])

    hours_left = _hours_remaining_in_settlement_day(station)
    forecast_high = _get_forecast_high(station, running_high)

    # Stage 1: extract regime features from the same observation we just
    # used for tmpf. These flow into _sigma_for_hours so the regime σ
    # lookup can fire on the live obs without an extra fetch.
    regime_features = _extract_regime_features(obs)
    regime_label = _compute_regime_label(station, regime_features, temp_f)

    # A4: if a per-(station, LST hour) diurnal fit has been persisted,
    # replace the naive blend + hours_left σ schedule with the learned
    # predictor. Daily high can only rise, so clamp μ ≥ running_high.
    lst_hour_now = _get_lst_now(station).hour
    fit = _get_diurnal_fit(station, lst_hour_now)
    if fit is not None:
        alpha, beta, rmse = fit
        predicted = alpha + beta * temp_f
        expected_eventual_high = max(predicted, running_high)
        sigma_f = rmse
    else:
        if forecast_high is None:
            # Cold-start path: kv hasn't been populated yet (e.g. <2 min
            # after daemon restart, before forecast sources have fired).
            # Fall back to running_high alone — better than guessing a
            # forecast — and widen σ so the combine treats this Gaussian
            # as low-information rather than a confident sub-peak read.
            expected_eventual_high = running_high
            sigma_f = max(
                _sigma_for_hours(
                    hours_left, station=station, lst_hour=lst_hour_now,
                    regime_label=regime_label,
                ),
                _COLD_START_SIGMA_F,
            )
        else:
            expected_eventual_high = _metar_expected_eventual_high(
                running_high, forecast_high, hours_left
            )
            # Pass station + LST hour + regime label so _sigma_for_hours
            # can consult tier 1/2 regime keys (when WEATHER_REGIME_SIGMA
            # is on) before falling back to the pooled (tier 3) and
            # schedule (tier 4). Always stashes the regime σ + tier into
            # _RESIDUAL_TIER_META for snapshot capture.
            sigma_f = _sigma_for_hours(
                hours_left, station=station, lst_hour=lst_hour_now,
                regime_label=regime_label,
            )

    # Settlement-day horizon from the canonical LST offset so our horizon
    # lines up with every other source's on the same bucket.
    lst_offset = lst_offset_for_station(station)
    horizon_hours = hours_until_settlement_end(lst_offset, day_idx=0)

    # METAR has a real observation timestamp — use it directly. Falls back
    # to "now" if the timestamp is malformed; staleness inflation is a
    # no-op for live observations regardless.
    issued_at_unix: Optional[float] = None
    if obs_time:
        try:
            issued_at_unix = datetime.fromisoformat(
                obs_time.replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            issued_at_unix = None

    return GaussianForecast(
        mean_f=expected_eventual_high,
        sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name="metar",
        source_tag=f"metar:{station}",
        issued_at=issued_at_unix,
    )


def get_metar_observation_estimate(ticker: str, market_data: dict) -> tuple:
    """Estimate probability for Kalshi high-temperature markets using live METAR observations.

    Returns (probability, source_label) or (None, None) if not applicable.
    """
    ticker_upper = ticker.upper() if ticker else ""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # ── Match ticker to station (via canonical registry) ──
    ws = station_for_ticker(ticker_upper)
    if ws is None:
        return None, None
    station = ws.icao

    # ── Parse threshold and direction ──
    threshold, is_above = _parse_threshold_from_market(ticker, title)
    if threshold is None:
        return None, None

    # Sanity check
    if threshold < -40 or threshold > 140:
        return None, None

    # ── Fetch METAR data ──
    metar_data = _fetch_metar_data()
    if metar_data is None:
        return None, None

    obs = _extract_station_obs(metar_data, station)
    if obs is None:
        print(f"[metar] Station {station} not found in METAR response")
        return None, None

    # ── Extract temperature ──
    temp_c = obs.get("temp")
    if temp_c is None:
        print(f"[metar] No temperature in METAR for {station}")
        return None, None

    try:
        temp_c = float(temp_c)
    except (TypeError, ValueError):
        print(f"[metar] Invalid temperature value for {station}: {temp_c}")
        return None, None

    temp_f = temp_c * 9.0 / 5.0 + 32.0
    obs_time = obs.get("reportTime") or obs.get("obsTime") or ""

    # ── Update running daily high ──
    daily_record = _update_running_daily_high(station, temp_f, obs_time)
    high_f = daily_record["high_f"]

    # ── Compute hours remaining in settlement day ──
    hours_left = _hours_remaining_in_settlement_day(station)

    # ── Get forecast high for remaining-potential estimation ──
    forecast_high = _get_forecast_high(station, high_f)
    if forecast_high is None:
        # v1 path: legacy callers expect a numeric value. Cold-start
        # behavior here is less critical because v1 is only the fallback
        # when WEATHER_ENSEMBLE_V2 is off; preserve the historical
        # ``running_high + 5°F`` shape so downstream math doesn't divide
        # by None. v2 path (above) widens σ instead.
        forecast_high = high_f + 5.0

    # ── Check for bracket market (different probability model) ──
    is_bracket = "-B" in ticker_upper
    if is_bracket:
        # Bracket: P(floor <= daily_high < cap)
        bracket_floor = threshold
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
            # Fallback: parse from title — "74 to 75", "74-75", "74°F and 75°F"
            range_match = re.search(
                r'(\d+\.?\d*)\s*\u00b0?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)', title,
            )
            if range_match:
                bracket_floor = float(range_match.group(1))
                bracket_cap = float(range_match.group(2))

        if high_f >= bracket_cap:
            # Running high already exceeds the bracket ceiling -- probability the
            # eventual high lands *inside* this bracket depends on whether it can drop
            # back (it cannot -- daily HIGH only goes up). So P(in bracket) is low
            # if we are already past it.
            prob = 0.02
        elif high_f >= bracket_floor:
            # Currently inside the bracket -- depends on whether we stay or move past
            prob_below_cap = _logistic_cdf(bracket_cap, max(forecast_high, high_f),
                                           _sigma_for_hours(hours_left))
            prob = max(0.02, min(0.98, prob_below_cap))
        else:
            # Below bracket -- need to reach it but not exceed it
            sigma = _sigma_for_hours(hours_left)
            mu = max(forecast_high, high_f)
            cdf_upper = _logistic_cdf(bracket_cap, mu, sigma)
            cdf_lower = _logistic_cdf(bracket_floor, mu, sigma)
            prob = max(0.02, min(0.98, cdf_upper - cdf_lower))

        print(f"[metar] {station}: obs={temp_f:.0f}\u00b0F running_high={high_f:.0f}\u00b0F "
              f"bracket=[{bracket_floor:.0f},{bracket_cap:.0f}]\u00b0F "
              f"hrs_left={hours_left:.1f} \u2192 {prob:.3f}")
        return prob, f"metar:{station}"

    # ── Threshold market ──
    # _compute_probability returns P(daily_high >= threshold).
    # For "above" markets (YES = high >= T): P(YES) = prob_above.
    # For "below" markets (YES = high <= T): P(YES) = 1 - prob_above.
    prob_above = _compute_probability(high_f, threshold, hours_left, forecast_high)

    if is_above:
        prob = prob_above
        direction_label = "above"
    else:
        prob = max(0.02, min(0.98, 1.0 - prob_above))
        direction_label = "below"

    print(f"[metar] {station}: obs={temp_f:.0f}\u00b0F running_high={high_f:.0f}\u00b0F "
          f"threshold={threshold}\u00b0F ({direction_label}) "
          f"hrs_left={hours_left:.1f} \u2192 {prob:.3f}")
    return prob, f"metar:{station}"


_METAR_RESIDUAL_SIGMA_KEY_PREFIX: str = "weather_metar_residual_sigma_"


def _get_learned_residual_sigma(
    station: Optional[str], lst_hour: Optional[int],
) -> Optional[float]:
    """Look up empirically-fit per-(station, LST hour) residual σ from
    kv_cache. Returns None when the cell isn't fit (cold start or thin
    sample) so the caller falls back to the hardcoded schedule.

    The fit comes from
    ``bot.learning.weather_mos_materializer.fit_and_persist_metar_residual_sigma``
    which measures std of (eventual_daily_high − running_max_at_hour_h)
    across the hourly METAR backfill for each station.
    """
    if station is None or lst_hour is None:
        return None
    try:
        conn = get_connection()
    except RuntimeError:
        return None
    key = f"{_METAR_RESIDUAL_SIGMA_KEY_PREFIX}{station}_{int(lst_hour)}"
    try:
        payload = kv_get(conn, key)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    sigma = payload.get("sigma")
    if not isinstance(sigma, (int, float)):
        return None
    sigma_f = float(sigma)
    if not (0.1 <= sigma_f <= 15.0):
        return None
    return sigma_f


def _sigma_for_hours(
    hours_left: float,
    station: Optional[str] = None,
    lst_hour: Optional[int] = None,
    *,
    regime_label: Optional[str] = None,
) -> float:
    """Compute uncertainty σ for METAR's Gaussian.

    Reads per-(station, LST hour) learned residual σ from kv_cache when
    both are supplied — that's the empirical std of
    (eventual_daily_high − running_max_at_hour_h), the right quantity for
    a precision-weighted combine where METAR's mean is the running max.

    Falls back to a hardcoded hours-remaining schedule when the learned
    cell is missing (cold cache, thin samples, or station/hour not
    supplied by the caller). The schedule is conservative — wide enough
    that the fallback can't single-handedly produce overconfident quotes.

    When ``regime_label`` is supplied AND ``WEATHER_REGIME_SIGMA`` is on,
    the lookup first consults regime-conditional kv keys (tier 1 + 2)
    before falling back to the pooled (tier 3) and schedule (tier 4).
    The accompanying σ + tier metadata is stashed via ``_RESIDUAL_TIER_META``
    so the snapshot writer can capture it for Stage 2's Brier-comparison
    gate, regardless of whether the regime σ is the value actually used.
    """
    sigma_value, tier_used = _resolve_residual_sigma(
        station, lst_hour, regime_label, hours_left,
    )
    return sigma_value


# ── Stage 1 telemetry side-channel ──
#
# The METAR Gaussian is one of N source rows in `weather_forecast_snapshots`.
# When that snapshot row gets written, we want to capture: regime label,
# tier used, σ that was used, σ that pooled would have produced. Rather
# than threading those four extra values through every caller of
# `get_metar_gaussian` and `_sigma_for_hours`, the σ resolver writes them
# into this dict keyed by (station, lst_hour) — the snapshot writer then
# pops the entry by station immediately after the predict cycle, so even
# if two cycles race the value in flight the dict can't grow unbounded.
_RESIDUAL_TIER_META: dict[str, dict] = {}

# Health counters for the daemon's _log_health emit. Track per-tier
# resolution counts so we can see whether regime cells are firing in
# production (Stage 1: visibility-only — flag is off, so the σ used IS
# pooled, but we count what would-have-been). Counters reset on emit.
_REGIME_TIER_COUNTS: dict[str, int] = {
    "regime_hour": 0, "station_regime": 0,
    "pooled_hour": 0, "schedule": 0, "none": 0,
}
# Stage 1: also track what regime tier WOULD have been used if the flag
# were on. Tracks the available-cell rate during the visibility-only
# phase. When flag flips on, this becomes redundant with the used
# counter, but is harmless to keep.
_REGIME_TIER_AVAILABLE: dict[str, int] = {
    "regime_hour": 0, "station_regime": 0, "none": 0,
}


def get_and_reset_regime_health_stats() -> dict[str, int]:
    """Return cumulative tier-resolution counts since last call, then
    zero them. Designed for ``bot.daemon.main._log_health``.
    """
    global _REGIME_TIER_COUNTS, _REGIME_TIER_AVAILABLE
    snapshot = dict(_REGIME_TIER_COUNTS)
    snapshot["avail_regime_hour"] = _REGIME_TIER_AVAILABLE["regime_hour"]
    snapshot["avail_station_regime"] = _REGIME_TIER_AVAILABLE["station_regime"]
    snapshot["avail_none"] = _REGIME_TIER_AVAILABLE["none"]
    _REGIME_TIER_COUNTS = {k: 0 for k in _REGIME_TIER_COUNTS}
    _REGIME_TIER_AVAILABLE = {k: 0 for k in _REGIME_TIER_AVAILABLE}
    return snapshot


def _resolve_residual_sigma(
    station: Optional[str], lst_hour: Optional[int],
    regime_label: Optional[str], hours_left: float,
) -> tuple[float, str]:
    """Walk the σ-lookup hierarchy and return ``(sigma, tier_used)``.

    Tier order:
      1. ``(station, hour, regime)`` from
         ``bot.learning.regime_residual_fitter`` (only when
         ``WEATHER_REGIME_SIGMA`` is on).
      2. ``(station, regime)`` pooled across hours (same gate).
      3. ``(station, hour)`` — existing pooled fitter.
      4. Hardcoded hours-remaining schedule.

    Always populates ``_RESIDUAL_TIER_META[station]`` with the σ that the
    regime path WOULD have used and the tier — even when the flag is off
    — so Stage 2 Brier comparison has the data without a behavior shift.
    """
    from bot.config import WEATHER_REGIME_SIGMA

    pooled_sigma = _get_learned_residual_sigma(station, lst_hour)
    pooled_or_schedule = pooled_sigma
    pooled_tier = "pooled_hour"
    if pooled_or_schedule is None:
        pooled_or_schedule = _schedule_sigma(hours_left)
        pooled_tier = "schedule"

    # Compute the "what regime would have done" σ regardless of flag, so
    # capture columns are populated. tier=='none' = no regime fit
    # available (or no regime label / no station) → would have fallen
    # back to pooled anyway.
    regime_sigma: Optional[float] = None
    regime_tier = "none"
    if station is not None and lst_hour is not None and regime_label:
        try:
            from bot.db import get_connection
            from bot.learning.regime_residual_fitter import get_regime_sigma
            try:
                conn = get_connection()
            except RuntimeError:
                conn = None
            if conn is not None:
                regime_sigma, regime_tier = get_regime_sigma(
                    conn, station, int(lst_hour), regime_label,
                )
        except Exception:
            regime_sigma = None
            regime_tier = "none"

    # Stash for the snapshot writer — always, even when flag is off.
    if station is not None:
        _RESIDUAL_TIER_META[str(station)] = {
            "regime_label": regime_label,
            "regime_tier_used": (
                regime_tier if regime_sigma is not None else pooled_tier
            ),
            "regime_sigma_f": regime_sigma,
            "pooled_sigma_f": pooled_or_schedule,
        }

    # Health counters: track which tier is actually being USED, not just
    # which is available. When the flag is off, the used σ is always
    # pooled_tier (or schedule); when the flag is on, regime_tier wins
    # if it has a value. Counters reset per health-log emit window.
    chosen_tier = (
        regime_tier
        if (WEATHER_REGIME_SIGMA and regime_sigma is not None)
        else pooled_tier
    )
    if chosen_tier in _REGIME_TIER_COUNTS:
        _REGIME_TIER_COUNTS[chosen_tier] += 1
    else:
        _REGIME_TIER_COUNTS["none"] += 1

    # Also track tier availability — independently of which σ we used.
    # This is the Stage 1 promotion gate's leading indicator: how often
    # is the fitter producing usable cells that match live regime?
    if regime_tier in _REGIME_TIER_AVAILABLE:
        _REGIME_TIER_AVAILABLE[regime_tier] += 1
    else:
        _REGIME_TIER_AVAILABLE["none"] += 1

    # Decide which σ to actually use.
    if WEATHER_REGIME_SIGMA and regime_sigma is not None:
        return regime_sigma, regime_tier
    return pooled_or_schedule, pooled_tier


def _schedule_sigma(hours_left: float) -> float:
    """Hardcoded hours-remaining σ — final fallback when no fitted cell
    has data for this (station, hour). Conservative — wide enough that
    the fallback can't single-handedly produce overconfident quotes.
    """
    if hours_left <= 0:
        return 0.5
    elif hours_left < 1:
        return 1.0
    elif hours_left < 2:
        return 2.0
    elif hours_left < 4:
        return 3.5
    elif hours_left < 6:
        return 5.0
    elif hours_left < 12:
        return 6.5
    else:
        return 8.0


def get_residual_tier_meta(station: str) -> Optional[dict]:
    """Snapshot writer hook — pops the most-recent regime telemetry for
    this station. Returns None if no entry. Pops to keep the dict small.
    """
    return _RESIDUAL_TIER_META.pop(station, None)


def _extract_regime_features(obs: dict) -> dict:
    """Pull wind dir, sky cover, dewpoint from a METAR observation dict.

    Returns a dict with keys ``drct``, ``skyc1``, ``dwpf`` (any may be
    None). Used to compute the regime label for σ lookup. Mirrors the
    column semantics of the IEM ASOS archive that feeds the fitter.
    """
    out: dict = {"drct": None, "skyc1": None, "dwpf": None}
    wdir = obs.get("wdir")
    if wdir is not None:
        try:
            out["drct"] = float(wdir)
        except (TypeError, ValueError):
            pass
    dewp_c = obs.get("dewp")
    if dewp_c is not None:
        try:
            out["dwpf"] = float(dewp_c) * 9.0 / 5.0 + 32.0
        except (TypeError, ValueError):
            pass
    # Sky cover: the API returns a list of cloud layers under `clouds`,
    # each with `cover` (CLR/FEW/SCT/BKN/OVC). Take the first non-empty
    # cover code — same convention as IEM's `skyc1`.
    clouds = obs.get("clouds")
    if isinstance(clouds, list) and clouds:
        first = clouds[0]
        if isinstance(first, dict):
            cover = first.get("cover")
            if isinstance(cover, str) and cover.strip():
                out["skyc1"] = cover.strip().upper()
    return out


def _compute_regime_label(station: str, regime_features: dict,
                           tmpf: Optional[float]) -> Optional[str]:
    """Compute the regime label for a station given its current
    observation. Returns None when the configured taxonomy can't be
    resolved (NULL field). Mirrors the fitter's labeling logic.
    """
    try:
        from bot.learning.regime_residual_fitter import (
            _CITY_TAXONOMY, _regime_label,
        )
    except Exception:
        return None
    taxonomy = _CITY_TAXONOMY.get(station)
    if taxonomy is None:
        return None
    label = _regime_label(
        station, taxonomy,
        drct=regime_features.get("drct"),
        skyc1=regime_features.get("skyc1"),
        tmpf=tmpf,
        dwpf=regime_features.get("dwpf"),
    )
    if label == "unknown":
        return None
    return label
