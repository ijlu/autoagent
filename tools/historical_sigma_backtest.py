#!/usr/bin/env python3
"""
Historical sigma calibration backtest.

Reconstructs synthetic shadow rows from two data sources:
  1. Kalshi API: all settled KXHIGH markets over the last LOOKBACK_DAYS.
     Gives us ticker, threshold, is_above (from floor/cap strike), result.
  2. Open-Meteo historical archive: hourly temperatures for each station.
     Gives us running_high_f at every simulated time point.

For each (market, simulated_hours_left), computes what fair_value_cents
the WeatherQuoter would have posted under each candidate sigma schedule,
then buckets by (suffix, fv_bucket) vs actual yes/no outcome to report
calibration bias.

Run on the VPS (needs Kalshi API key):
    python3 tools/historical_sigma_backtest.py

Outputs:
  - Calibration tables for each sigma schedule
  - Summary: which schedule passes all n>=50 buckets at |bias| <= 0.05
  - Recommended sigma schedule written to stdout
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
from bot.api import api_get

# ── Configuration ─────────────────────────────────────────────────────────────

LOOKBACK_DAYS = 90          # how many days of settled markets to fetch
KALSHI_PAGES_PER_SERIES = 15  # 200 markets/page × 15 = up to 3000 per series
CACHE_DIR = Path(".cache_historical_backtest")

# Time points to simulate per market-day (hours before settlement midnight)
SIMULATE_HOURS_LEFT = [22.0, 18.0, 14.0, 10.0, 7.0, 5.0, 3.0, 1.5, 0.5]

# Station metadata (from bot/daemon/stations.py)
STATIONS = {
    "KXHIGHNY":  {"icao": "KNYC", "lat": 40.78, "lon": -73.97, "lst_offset": -5},
    "KXHIGHCHI": {"icao": "KMDW", "lat": 41.79, "lon": -87.75, "lst_offset": -6},
    "KXHIGHLAX": {"icao": "KLAX", "lat": 33.94, "lon": -118.41, "lst_offset": -8},
    "KXHIGHAUS": {"icao": "KAUS", "lat": 30.19, "lon": -97.67, "lst_offset": -6},
    "KXHIGHMIA": {"icao": "KMIA", "lat": 25.79, "lon": -80.29, "lst_offset": -5},
    "KXHIGHDEN": {"icao": "KDEN", "lat": 39.86, "lon": -104.67, "lst_offset": -7},
}


# ── Sigma schedules ───────────────────────────────────────────────────────────

def _sigma_current(h: float) -> float:
    if h <= 0:      return 0.1
    elif h < 1:     return 0.3
    elif h < 2:     return 0.5 + (h - 1.0) * 0.3
    elif h < 6:     return 0.8 + (h - 2.0) * 0.175
    elif h < 12:    return 1.5 + (h - 6.0) * 0.083
    else:           return 2.0


def _make_scaled(factor: float):
    def f(h: float) -> float:
        return min(15.0, _sigma_current(h) * factor)
    return f


def _sigma_v2(h: float) -> float:
    if h <= 0:      return 0.5
    elif h < 1:     return 1.5
    elif h < 2:     return 3.0
    elif h < 4:     return 4.5
    elif h < 6:     return 6.0
    elif h < 12:    return 7.5
    else:           return 9.0


def _sigma_v3(h: float) -> float:
    if h <= 0:      return 0.5
    elif h < 1:     return 1.0
    elif h < 2:     return 2.0
    elif h < 4:     return 3.5
    elif h < 6:     return 5.0
    elif h < 12:    return 6.5
    else:           return 8.0


def _sigma_v4(h: float) -> float:
    """Moderate with boosted sub-6h range based on afternoon variance."""
    if h <= 0:      return 0.5
    elif h < 1:     return 1.0
    elif h < 2:     return 2.0
    elif h < 3:     return 3.0
    elif h < 5:     return 4.0
    elif h < 8:     return 5.5
    elif h < 12:    return 7.0
    else:           return 8.5


SCHEDULES = [
    ("current (1x)",  _sigma_current),
    ("3x scale",      _make_scaled(3)),
    ("5x scale",      _make_scaled(5)),
    ("8x scale",      _make_scaled(8)),
    ("v2 empirical",  _sigma_v2),
    ("v3 moderate",   _sigma_v3),
    ("v4 boosted",    _sigma_v4),
]


# ── Math helpers ──────────────────────────────────────────────────────────────

def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _blended_mu(rh: float, fh: float, hours_left: float) -> float:
    if hours_left <= 0:
        return rh
    frac_elapsed = max(0.0, min(1.0, 1.0 - hours_left / 24.0))
    fw = max(0.1, 1.0 - frac_elapsed)
    return fw * max(fh, rh) + (1.0 - fw) * rh


def _compute_fv(rh: float, fh: float, hours_left: float,
                threshold: float, is_above: bool, sigma_fn) -> int:
    if rh >= threshold:
        margin = rh - threshold
        if margin >= 3.0:   prob_above = 0.98
        elif margin >= 1.0: prob_above = 0.96
        else:               prob_above = 0.95
    else:
        mu = _blended_mu(rh, fh, hours_left)
        sigma = sigma_fn(hours_left)
        prob_above = max(0.02, min(0.98, 1.0 - _logistic_cdf(threshold, mu, sigma)))

    prob = prob_above if is_above else max(0.02, min(0.98, 1.0 - prob_above))
    return max(2, min(98, int(round(prob * 100))))


# ── Kalshi market fetcher ─────────────────────────────────────────────────────

def _parse_close_date_lst(close_time: str, lst_offset: int) -> Optional[date]:
    """Convert Kalshi close_time ISO string to the LST settlement date."""
    if not close_time:
        return None
    try:
        dt = datetime.fromisoformat(close_time.rstrip("Z")).replace(
            tzinfo=timezone.utc
        )
        lst_dt = dt + timedelta(hours=lst_offset)
        # The settlement date is the day the market was open for —
        # close_time is typically midnight LST of the *next* day.
        # Subtract 1 second to get the actual settlement day.
        settlement_dt = lst_dt - timedelta(seconds=1)
        return settlement_dt.date()
    except Exception:
        return None


def fetch_kalshi_settled(series: str, lst_offset: int,
                         lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Fetch settled markets for one series from the Kalshi API."""
    cache_file = CACHE_DIR / f"kalshi_{series}.json"
    if cache_file.exists():
        age_h = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_h < 6:
            with open(cache_file) as f:
                data = json.load(f)
            print(f"  [{series}] loaded {len(data)} markets from cache")
            return data

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    markets = []
    cursor = None
    for page in range(KALSHI_PAGES_PER_SERIES):
        params = f"series_ticker={series}&status=settled&limit=200"
        if cursor:
            params += f"&cursor={cursor}"
        try:
            resp = api_get(f"/markets?{params}")
        except Exception as exc:
            print(f"  [{series}] API error on page {page}: {exc}")
            break
        page_markets = resp.get("markets", [])
        if not page_markets:
            break
        for m in page_markets:
            ct = m.get("close_time", "")
            if ct and ct < cutoff:
                # Reached markets older than lookback — stop paginating
                cursor = None
                break
            markets.append(m)
        else:
            cursor = resp.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)
            continue
        break

    print(f"  [{series}] fetched {len(markets)} settled markets from API")
    CACHE_DIR.mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(markets, f)
    return markets


def parse_market(m: dict, series: str, lst_offset: int) -> Optional[dict]:
    """Extract (ticker, date, threshold, is_above, won_yes) from a Kalshi market."""
    ticker = m.get("ticker", "")
    result = m.get("result", "")
    if result not in ("yes", "no"):
        return None

    # Determine threshold and direction from API strikes (same logic as WeatherQuoter)
    api_floor = m.get("floor_strike")
    api_cap   = m.get("cap_strike")
    threshold: Optional[float] = None
    is_above  = True

    if api_cap is not None and api_floor is None:
        try:
            threshold = float(api_cap)
            is_above = False
        except (ValueError, TypeError):
            pass
    elif api_floor is not None and api_cap is None:
        try:
            threshold = float(api_floor)
            is_above = True
        except (ValueError, TypeError):
            pass

    if threshold is None:
        # Fallback: parse from ticker suffix -T{value}
        m2 = re.search(r'-[Tt](-?\d+\.?\d*)', ticker)
        if not m2:
            return None
        threshold = float(m2.group(1))
        is_above = True  # assume above for fallback (KXHIGH standard)

    settle_date = _parse_close_date_lst(m.get("close_time", ""), lst_offset)
    if settle_date is None:
        return None

    return {
        "ticker":    ticker,
        "series":    series,
        "date":      settle_date,
        "threshold": threshold,
        "is_above":  is_above,
        "won_yes":   (result == "yes"),
    }


# ── IEM ASOS historical METAR fetcher ────────────────────────────────────────

def fetch_asos_temps(series: str, icao: str,
                     start: date, end: date) -> dict[int, float]:
    """
    Fetch actual METAR temperature observations from Iowa Environmental Mesonet
    ASOS archive. Returns {unix_timestamp → temp_f} at each observation time.

    Uses the same underlying station data as our live METARPoller — so the
    running_high simulation exactly matches what the daemon sees in real-time.
    """
    # Extend end by 1 day to capture the full final settlement day
    fetch_end = end + timedelta(days=1)
    cache_file = CACHE_DIR / f"asos_{icao}_{start}_{fetch_end}.json"
    if cache_file.exists():
        with open(cache_file) as f:
            data = json.load(f)
        obs = {int(k): float(v) for k, v in data.items()}
        print(f"  [{series}] loaded IEM ASOS {icao}: {len(obs)} obs from cache")
        return obs

    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={icao}&data=tmpf"
        f"&year1={start.year}&month1={start.month:02d}&day1={start.day:02d}"
        f"&year2={fetch_end.year}&month2={fetch_end.month:02d}&day2={fetch_end.day:02d}"
        "&tz=UTC&format=comma&latlon=no&direct=no"
    )
    for attempt in range(5):
        resp = requests.get(url, timeout=120)
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"  [{series}] rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        raise RuntimeError(f"IEM ASOS rate limited after 5 attempts for {icao}")

    obs: dict[int, float] = {}
    for line in resp.text.splitlines():
        if not line or line.startswith("station") or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            tmpf_str = parts[2].strip()
            if tmpf_str in ("M", "", "None", "null"):
                continue
            ts_str = parts[1].strip()  # "2026-01-24 05:30"
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            obs[int(dt.timestamp())] = float(tmpf_str)
        except (ValueError, IndexError):
            continue

    CACHE_DIR.mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump({str(k): v for k, v in obs.items()}, f)
    print(f"  [{series}] fetched IEM ASOS {icao}: {len(obs)} obs ({start} to {fetch_end})")
    time.sleep(3)
    return obs


# ── Open-Meteo Historical Forecast fetcher ────────────────────────────────────

def fetch_forecast_highs(series: str, lat: float, lon: float,
                         start: date, end: date) -> dict[date, float]:
    """
    Fetch what GFS actually predicted for each day's high temperature,
    as issued before that day occurred.

    Uses Open-Meteo's Historical Forecast API (not the ERA5 archive).
    The distinction: archive gives observed values; historical-forecast
    gives what the model SAID would happen — capturing genuine forecast
    uncertainty that sigma is meant to model.

    Returns {settlement_date: forecast_high_f}.
    """
    cache_file = CACHE_DIR / f"forecast_{series}_{start}_{end}.json"
    if cache_file.exists():
        with open(cache_file) as f:
            raw = json.load(f)
        result = {date.fromisoformat(k): float(v) for k, v in raw.items()}
        print(f"  [{series}] loaded forecast highs from cache ({len(result)} days)")
        return result

    url = (
        "https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start}&end_date={end}"
        "&daily=temperature_2m_max"
        "&temperature_unit=fahrenheit"
        "&timezone=GMT"
        "&models=gfs_global"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    raw_json = resp.json()

    dates = raw_json.get("daily", {}).get("time", [])
    highs = raw_json.get("daily", {}).get("temperature_2m_max", [])

    result: dict[date, float] = {}
    for d_str, h in zip(dates, highs):
        if h is not None:
            result[date.fromisoformat(d_str)] = float(h)

    CACHE_DIR.mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump({str(k): v for k, v in result.items()}, f)
    print(f"  [{series}] fetched GFS forecast highs ({start} to {end}): {len(result)} days")
    time.sleep(0.5)
    return result


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate_rows(markets: list[dict], obs_map: dict[int, float],
                  forecast_map: dict[date, float],
                  lst_offset: int) -> list[dict]:
    """
    For each market × simulated hours_left, produce one synthetic shadow row.

    obs_map: {unix_timestamp → temp_f} from IEM ASOS (raw observation times, UTC).
    forecast_map: {settlement_date → forecast_high_f} from GFS historical forecast.
      This is what the model PREDICTED before the day occurred — not the oracle.
      Genuine forecast uncertainty means sigma has real work to do.
    lst_offset: e.g. -5 for NYC EST.
    """
    rows = []
    for mkt in markets:
        settle_date: date = mkt["date"]
        threshold: float  = mkt["threshold"]
        is_above: bool    = mkt["is_above"]
        won_yes: bool     = mkt["won_yes"]

        # Use GFS day-of forecast as fh (what the model predicted that morning)
        fh = forecast_map.get(settle_date)
        if fh is None:
            continue  # no forecast available for this day — skip

        # Settlement day window in UTC
        settle_midnight_utc = int(
            datetime(settle_date.year, settle_date.month, settle_date.day,
                     tzinfo=timezone.utc).timestamp()
            - lst_offset * 3600
        )
        settle_end_utc = settle_midnight_utc + 24 * 3600

        # Collect ASOS obs falling within this settlement day
        day_obs: list[tuple[float, float]] = []  # (hours_from_midnight_lst, temp_f)
        for ts, t in obs_map.items():
            if settle_midnight_utc <= ts < settle_end_utc:
                h = (ts - settle_midnight_utc) / 3600.0
                day_obs.append((h, t))

        if len(day_obs) < 8:
            continue  # fewer than 8 obs → skip day (data gap)

        day_obs.sort(key=lambda x: x[0])

        suffix = "B" if "-B" in mkt["ticker"].upper() else "T"

        for hours_left in SIMULATE_HOURS_LEFT:
            elapsed = 24.0 - hours_left
            obs_before = [t for h, t in day_obs if h <= elapsed]
            rh = max(obs_before) if obs_before else day_obs[0][1]

            rows.append({
                "suffix":     suffix,
                "threshold":  threshold,
                "is_above":   is_above,
                "won_yes":    won_yes,
                "hours_left": hours_left,
                "rh":         rh,
                "fh_oracle":  fh,   # GFS forecast, not oracle
            })

    return rows


# ── Calibration ───────────────────────────────────────────────────────────────

def _bucket(fv: int) -> str:
    p = fv / 100.0
    lo = int(p * 10) / 10
    if lo >= 1.0: lo = 0.9
    return f"{lo:.1f}-{lo+0.1:.1f}"


def calibration_report(rows: list[dict], name: str, sigma_fn,
                        min_n: int = 100) -> dict:
    by_sb: dict = defaultdict(list)
    for r in rows:
        fv = _compute_fv(
            r["rh"], r["fh_oracle"], r["hours_left"],
            r["threshold"], r["is_above"], sigma_fn,
        )
        b = _bucket(fv)
        by_sb[(r["suffix"], b)].append((fv / 100.0, int(r["won_yes"])))

    fails = passes = 0
    results = []
    for (sfx, b), samples in sorted(by_sb.items()):
        n = len(samples)
        if n < min_n:
            continue
        avg_est  = sum(x[0] for x in samples) / n
        yes_rate = sum(x[1] for x in samples) / n
        bias = avg_est - yes_rate
        results.append((sfx, b, n, avg_est, yes_rate, bias))
        if abs(bias) <= 0.05:
            passes += 1
        else:
            fails += 1

    print(f"\n{'='*72}")
    print(f"  Sigma: {name}  |  buckets n>={min_n}: {passes+fails}  "
          f"PASS={passes}  FAIL={fails}")
    print(f"{'='*72}")
    print(f"  {'sfx':<4} {'bucket':<12} {'n':>6} {'avg_est':>8} "
          f"{'yes_rate':>9} {'bias':>8}  {'ok?':>4}")
    for (sfx, b, n, avg_est, yes_rate, bias) in results:
        ok = "PASS" if abs(bias) <= 0.05 else "FAIL"
        print(f"  {sfx:<4} {b:<12} {n:>6} {avg_est:>8.3f} "
              f"{yes_rate:>9.3f} {bias:>+8.3f}  {ok:>4}")

    return {(sfx, b): (n, avg_est, yes_rate, bias) for sfx, b, n, avg_est, yes_rate, bias in results}


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    today = date.today()
    start_date = today - timedelta(days=LOOKBACK_DAYS)

    print(f"Historical sigma backtest: {start_date} to {today}")
    print(f"Lookback: {LOOKBACK_DAYS} days  |  "
          f"Simulated time points per market: {len(SIMULATE_HOURS_LEFT)}\n")

    all_rows: list[dict] = []

    for series, info in STATIONS.items():
        print(f"\n── {series} ──")
        icao, lat, lon, lst_offset = info["icao"], info["lat"], info["lon"], info["lst_offset"]

        # 1. Fetch settled Kalshi markets
        raw_markets = fetch_kalshi_settled(series, lst_offset)
        parsed = [r for m in raw_markets
                  if (r := parse_market(m, series, lst_offset)) is not None]
        t_markets = [r for r in parsed if "-B" not in r["ticker"].upper()]
        b_markets = [r for r in parsed if "-B" in r["ticker"].upper()]
        print(f"  Parsed: {len(t_markets)} T-markets, {len(b_markets)} B-markets")

        # 2. Fetch actual METAR obs from IEM ASOS (same data source as live daemon)
        obs_map = fetch_asos_temps(series, icao, start_date, today)
        print(f"  IEM ASOS: {len(obs_map)} observations")

        # 3. Fetch GFS historical forecast highs (what the model predicted, not oracle)
        forecast_map = fetch_forecast_highs(series, lat, lon, start_date, today)
        print(f"  GFS forecasts: {len(forecast_map)} days")

        # 4. Simulate rows
        series_rows = simulate_rows(parsed, obs_map, forecast_map, lst_offset)
        print(f"  Simulated: {len(series_rows)} synthetic rows")
        all_rows.extend(series_rows)

    print(f"\n\nTotal synthetic rows: {len(all_rows):,}")

    # Break down YES rate by suffix to understand the dataset
    t_rows = [r for r in all_rows if r["suffix"] == "T"]
    b_rows = [r for r in all_rows if r["suffix"] == "B"]
    t_yes = sum(r["won_yes"] for r in t_rows) / max(1, len(t_rows))
    b_yes = sum(r["won_yes"] for r in b_rows) / max(1, len(b_rows))
    print(f"T-suffix: {len(t_rows):,} rows, base YES rate: {t_yes:.3f}")
    print(f"B-suffix: {len(b_rows):,} rows, base YES rate: {b_yes:.3f}")

    # Run calibration for each sigma schedule
    all_results: dict[str, dict] = {}
    for name, fn in SCHEDULES:
        all_results[name] = calibration_report(all_rows, name, fn)

    # Summary
    print(f"\n{'='*72}")
    print("  SUMMARY — passes (|bias|<=0.05) across buckets with n>=100")
    print(f"{'='*72}")
    best_name = None
    best_passes = -1
    for name, results in all_results.items():
        total   = len(results)
        passing = sum(1 for (n, ae, yr, bias) in results.values() if abs(bias) <= 0.05)
        worst   = max((abs(bias) for (n, ae, yr, bias) in results.values()), default=0)
        flag    = " ← BEST" if passing > best_passes else ""
        if passing > best_passes:
            best_passes = passing
            best_name = name
        print(f"  {name:<22} {passing}/{total} pass  worst_bias={worst:.3f}{flag}")

    print(f"\nRecommended sigma: {best_name}")

    # Print sigma table for key hours
    print(f"\n{'='*72}")
    print("  SIGMA VALUES at key hours_left")
    print(f"{'='*72}")
    checkpoints = [0.5, 1, 2, 3, 5, 8, 12, 18]
    print(f"  {'schedule':<22}" + "".join(f"  {h:>4}h" for h in checkpoints))
    for name, fn in SCHEDULES:
        print(f"  {name:<22}" + "".join(f"  {fn(h):>5.2f}" for h in checkpoints))


if __name__ == "__main__":
    main()
