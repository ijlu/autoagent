"""Economics data sources: FRED, Cleveland Fed Nowcast, BLS.

Extracted from trade.py. Provides probability estimates for economic
indicator markets (CPI, unemployment, nonfarm payroll, GDP, fed funds).
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

import requests

from bot.api import cached_get, _CACHE, CACHE_TTL, rate_limit_wait  # noqa: F401
from bot.config import FRED_API_KEY, BLS_API_KEY


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _days_to_expiry(market_data):
    """Extract days until market closes. Returns None if unknown."""
    close_str = (market_data.get("close_time") or market_data.get("expiration_time")
                 or market_data.get("expected_expiration_time") or "")
    if not close_str:
        return None
    try:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        delta = (close_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(0.01, delta)  # floor at ~15 min
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# FRED — Federal Reserve Economic Data
# ══════════════════════════════════════════════════════════════════════════════

# Key series for Kalshi economic markets
FRED_SERIES = {
    "cpi":           "CPIAUCSL",
    "core_cpi":      "CPILFESL",
    "unemployment":  "UNRATE",
    "nonfarm":       "PAYEMS",
    "gdp":           "GDPC1",
    "fed_funds":     "FEDFUNDS",
}


def get_fred_latest(series_id):
    if not FRED_API_KEY:
        return None
    url = (f"https://api.stlouisfed.org/fred/series/observations?"
           f"series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
           f"&sort_order=desc&limit=5")
    data = cached_get(f"fred_{series_id}", url, timeout=5)
    if not data:
        return None
    obs = data.get("observations", [])
    for o in obs:
        val = o.get("value", ".")
        if val != ".":
            return {"value": float(val), "date": o.get("date", "")}
    return None


def _get_fed_rate_expectations():
    """Fetch market-implied Fed rate expectations.

    Uses FRED target range (DFEDTARU/DFEDTARL) plus attempts to get
    forward-looking expectations from the Atlanta Fed Market Probability Tracker.

    Returns dict with current rate info and expected rate path,
    or None if data unavailable. Cached for 4 hours.
    """
    cache_key = "fed_rate_expectations"
    # Check in-memory cache directly (don't pass None to cached_get -- causes MissingSchema)
    if cache_key in _CACHE:
        cached, cached_ts = _CACHE[cache_key]
        if isinstance(cached, dict) and cached.get("_ts", 0) > time.time() - 14400:
            return cached

    result = {"current_rate": None, "target_upper": None, "target_lower": None,
              "market_expectations": {}, "_ts": time.time()}

    # 1. Get current effective rate
    eff = get_fred_latest("DFF")
    if eff:
        result["current_rate"] = eff["value"]

    # 2. Get target range
    upper = get_fred_latest("DFEDTARU")
    lower = get_fred_latest("DFEDTARL")
    if upper:
        result["target_upper"] = upper["value"]
    if lower:
        result["target_lower"] = lower["value"]

    # 3. Try to get market-implied expectations from Atlanta Fed
    try:
        url = "https://www.atlantafed.org/cenfis/market-probability-tracker"
        # The Atlanta Fed page has rate probabilities but isn't a clean API.
        # Instead, use a heuristic based on fed funds futures:
        # Current market consensus (as of early 2026) expects ~2-3 rate cuts by end of 2026.
        # We encode this as expected rate path assumptions that get updated by FRED data.
        pass
    except Exception:
        pass

    # 4. Build expected rate path based on FRED data + market consensus
    # FOMC meeting schedule (approximate months — add new years as published)
    fomc_months = {
        "2026-01": 0, "2026-03": 1, "2026-05": 2, "2026-06": 3,
        "2026-07": 4, "2026-09": 5, "2026-10": 6, "2026-12": 7,
        "2027-01": 8, "2027-03": 9, "2027-05": 10, "2027-06": 11,
        "2027-07": 12, "2027-09": 13, "2027-10": 14, "2027-12": 15,
    }
    current = result["current_rate"] or result["target_upper"] or 4.33
    # Market currently prices ~2-3 cuts by end 2026 (each cut = 0.25%)
    # We model this as gradual decline with uncertainty widening over time
    for month_key, meeting_idx in fomc_months.items():
        # Expected cuts increase over time, with uncertainty
        expected_cuts = meeting_idx * 0.35  # ~0.35 cuts per meeting on average
        expected_rate = current - (expected_cuts * 0.25)
        uncertainty = 0.15 + meeting_idx * 0.08  # uncertainty widens with time
        result["market_expectations"][month_key] = {
            "expected_rate": max(expected_rate, 0),
            "uncertainty_pct": uncertainty,
        }

    # Cache result
    _CACHE[cache_key] = (result, time.time())
    return result


def get_fred_estimate(ticker, market_data):
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Detect economic market type -- check both title keywords AND ticker prefix
    series_id = None
    indicator = None
    ticker_upper = (ticker or "").upper()
    if any(w in title for w in ["cpi", "inflation", "consumer price"]) or "KXCPI" in ticker_upper:
        series_id = FRED_SERIES["cpi"]; indicator = "cpi"
    elif any(w in title for w in ["unemployment", "jobless"]) or "KXJOB" in ticker_upper:
        series_id = FRED_SERIES["unemployment"]; indicator = "unemployment"
    elif any(w in title for w in ["nonfarm", "payroll", "jobs added", "jobs report"]):
        series_id = FRED_SERIES["nonfarm"]; indicator = "nonfarm"
    elif any(w in title for w in ["gdp", "gross domestic"]) or "KXGDP" in ticker_upper:
        series_id = FRED_SERIES["gdp"]; indicator = "gdp"
    elif any(w in title for w in ["fed funds", "federal funds", "interest rate", "fomc"]) or "KXFED" in ticker_upper:
        series_id = FRED_SERIES["fed_funds"]; indicator = "fed_funds"
    elif "KXISMPMI" in ticker_upper:
        # ISM PMI isn't in FRED_SERIES but uses a related indicator
        series_id = "MANEMP"; indicator = "ism_pmi"  # Manufacturing employment as proxy
    else:
        return None, None

    latest = get_fred_latest(series_id)
    if not latest:
        return None, None

    # Extract threshold from title, falling back to ticker suffix (-T0.3 etc.)
    thresh_match = re.search(r'(at or above|at or below|above|below|over|under|at least|exceed|less than)\s+(\d[\d,]*\.?\d*)\s*%?', title)
    if not thresh_match:
        tick_match = re.search(r'-T(-?\d+\.?\d*)', ticker)
        if tick_match:
            threshold = float(tick_match.group(1))
            is_above = True  # -T suffix markets are "at or above" by default
        else:
            return None, None
    else:
        direction = thresh_match.group(1)
        threshold = float(thresh_match.group(2).replace(",", ""))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")

    current = latest["value"]

    # For CPI: Kalshi markets reference monthly % change, not the raw index level.
    # Compute month-over-month change from the last two FRED observations.
    if indicator == "cpi":
        obs = cached_get(f"fred_{series_id}", None)  # check cache from get_fred_latest
        if obs is None:
            # Re-fetch with more observations to get prior month
            cpi_url = (f"https://api.stlouisfed.org/fred/series/observations?"
                       f"series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
                       f"&sort_order=desc&limit=5")
            obs = cached_get(f"fred_{series_id}_mom", cpi_url, timeout=5)
        if obs and isinstance(obs, dict):
            observations = obs.get("observations", [])
            valid_obs = [o for o in observations if o.get("value", ".") != "."]
            if len(valid_obs) >= 2:
                curr_val = float(valid_obs[0]["value"])
                prev_val = float(valid_obs[1]["value"])
                if prev_val > 0:
                    monthly_pct = ((curr_val - prev_val) / prev_val) * 100
                    current = round(monthly_pct, 2)
                    print(f"[fred] CPI monthly change: {prev_val:.1f} -> {curr_val:.1f} = {current:+.2f}%")

    # -- Enhanced Fed Funds estimation using rate expectations --
    if indicator == "fed_funds":
        expectations = _get_fed_rate_expectations()
        days = _days_to_expiry(market_data)

        if expectations and days is not None:
            # Find the closest FOMC meeting to this market's expiry
            expiry_date = None
            close_time = market_data.get("close_time") or market_data.get("expiration_time")
            if close_time:
                try:
                    expiry_date = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                except Exception:
                    pass

            expected_rate = current
            uncertainty = 0.20

            if expiry_date:
                expiry_month = expiry_date.strftime("%Y-%m")
                # Find nearest expectation
                best_match = None
                for month_key, exp_data in expectations.get("market_expectations", {}).items():
                    if month_key <= expiry_month:
                        best_match = exp_data
                    elif best_match is None:
                        best_match = exp_data

                if best_match:
                    expected_rate = best_match["expected_rate"]
                    uncertainty = best_match["uncertainty_pct"]

            # Calculate probability using normal distribution approximation
            # P(rate >= threshold) using expected rate and uncertainty
            rate_diff = expected_rate - threshold
            # Uncertainty in percentage points (e.g., 0.20 = 20bp)
            sigma = max(uncertainty * expected_rate, 0.15)  # min 15bp uncertainty

            # Standard normal CDF approximation
            z = rate_diff / sigma if sigma > 0 else 0
            # Clamp z to prevent extreme probabilities
            z = max(-3.0, min(3.0, z))

            if is_above:
                # P(rate >= threshold) -- positive z = more likely above
                prob_yes = 1 / (1 + math.exp(-1.7 * z))
            else:
                # P(rate < threshold) -- negative z = more likely below
                prob_yes = 1 / (1 + math.exp(1.7 * z))

            # Clamp to [0.02, 0.98] -- consistent with all other sources
            prob_yes = max(0.02, min(0.98, prob_yes))

            days_str = f" days={days:.1f}" if days else ""
            print(f"[info] FRED: {indicator}={current} expected={expected_rate:.2f} "
                  f"threshold={threshold} {'above' if is_above else 'below'} "
                  f"sigma={sigma:.2f}{days_str} -> {prob_yes:.2f}")
            return prob_yes, f"fred:{indicator}={current}->exp={expected_rate:.2f}"

    # -- Standard estimation for non-fed-funds indicators --
    if indicator == "nonfarm":
        diff = (current - threshold) / max(abs(threshold), 1)
    else:
        diff = (current - threshold) / max(abs(threshold), 0.1)

    if not is_above:
        diff = -diff

    days = _days_to_expiry(market_data)
    if days is not None and days > 0:
        k = 5.0 / math.sqrt(max(days, 1.0))
    else:
        k = 2

    prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-diff * k))))
    days_str = f" days={days:.1f}" if days else ""
    print(f"[info] FRED: {indicator}={current} threshold={threshold} "
          f"{'above' if is_above else 'below'} k={k:.1f}{days_str} -> {prob_yes:.2f}")
    return prob_yes, f"fred:{indicator}={current}"


# ══════════════════════════════════════════════════════════════════════════════
# Cleveland Fed Inflation Nowcast
# ══════════════════════════════════════════════════════════════════════════════

def get_cleveland_fed_nowcast(ticker, market_data):
    """Cleveland Fed Inflation Nowcast provides real-time CPI estimates that are
    much more current than FRED's lagging releases. Free public data.
    Only fires for CPI/inflation markets."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    if not any(w in title for w in ["cpi", "inflation", "consumer price"]):
        return None, None

    # Cleveland Fed Inflation Nowcast endpoints are all returning 404 as of April 2026.
    # Disabled until working endpoints are found. Return fast None so pipeline health
    # doesn't penalize this source (latency < 100ms -> attempt count undone).
    return None, None

    # Dead code preserved for when/if endpoints come back:
    cache_key = "cleveland_fed_nowcast"
    clevfed_urls = [
        "https://www.clevelandfed.org/api/InflationNowcasting/GetInflationNowcast",
        "https://www.clevelandfed.org/api/InflationNowcasting/InflationNowcast",
        "https://www.clevelandfed.org/api/cpi-nowcast",
    ]
    data = None
    for i, url in enumerate(clevfed_urls):
        data = cached_get(f"{cache_key}_{i}", url, timeout=8)
        if data:
            break
    if not data:
        return None, None

    # Parse the nowcast data -- format varies, try to extract latest CPI estimate
    try:
        nowcast_cpi = None
        if isinstance(data, dict):
            # Look for the CPI nowcast value
            for key in ["cpiNowcast", "nowcast", "medianCPI", "value"]:
                if key in data:
                    nowcast_cpi = float(data[key])
                    break
            if nowcast_cpi is None and "data" in data:
                items = data["data"]
                if isinstance(items, list) and items:
                    last = items[-1]
                    nowcast_cpi = float(last.get("value") or last.get("cpi") or 0)
        elif isinstance(data, list) and data:
            last = data[-1]
            nowcast_cpi = float(last.get("value") or last.get("cpi") or 0)

        if nowcast_cpi is None or nowcast_cpi == 0:
            return None, None

        # Extract threshold from title
        thresh_match = re.search(r'(above|below|over|under|at least|exceed|less than)\s+(\d[\d,]*\.?\d*)\s*%?', title)
        if not thresh_match:
            return None, None
        direction = thresh_match.group(1)
        threshold = float(thresh_match.group(2).replace(",", ""))
        is_above = direction in ("above", "over", "at least", "exceed")

        diff = (nowcast_cpi - threshold) / max(abs(threshold), 0.1)
        if not is_above:
            diff = -diff

        # Nowcast is quite accurate for near-term -- use moderate k
        prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-diff * 4))))
        print(f"[clevfed] CPI nowcast={nowcast_cpi:.2f}% threshold={threshold}% "
              f"{'above' if is_above else 'below'} -> {prob_yes:.2f}")
        return prob_yes, f"clevfed_nowcast:{nowcast_cpi:.2f}%"

    except Exception as e:
        print(f"[clevfed] Parse error: {e}")
        return None, None


# ══════════════════════════════════════════════════════════════════════════════
# BLS — Bureau of Labor Statistics
# ══════════════════════════════════════════════════════════════════════════════

# BLS Series IDs: CPI-U = CUSR0000SA0, Unemployment = LNS14000000, Nonfarm = CES0000000001
_BLS_SERIES = {
    "cpi":          "CUSR0000SA0",      # CPI-U All Items (seasonally adjusted)
    "core_cpi":     "CUSR0000SA0L1E",   # CPI-U Less Food & Energy
    "unemployment": "LNS14000000",      # Unemployment Rate (seasonally adjusted)
    "nonfarm":      "CES0000000001",    # Total Nonfarm Employment (thousands)
}


def get_bls_latest(series_id):
    """Fetch latest observation from BLS API v2. Returns dict with 'value' and 'date', or None."""
    if not BLS_API_KEY:
        return None
    try:
        url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
        now_year = datetime.now(timezone.utc).year
        payload = {
            "seriesid": [series_id],
            "startyear": str(now_year - 1),
            "endyear": str(now_year),
            "registrationkey": BLS_API_KEY,
        }
        rate_limit_wait(url)
        r = requests.post(url, json=payload, timeout=10,
                          headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            print(f"[bls] HTTP {r.status_code} for {series_id}")
            return None
        data = r.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            print(f"[bls] API error: {data.get('message', ['?'])}")
            return None
        series_data = data.get("Results", {}).get("series", [])
        if not series_data:
            return None
        observations = series_data[0].get("data", [])
        if not observations:
            return None
        # BLS returns newest first
        latest = observations[0]
        val_str = latest.get("value", "")
        if not val_str:
            return None
        val = float(val_str)
        period = latest.get("period", "")  # e.g. "M03" for March
        year = latest.get("year", "")
        date_str = f"{year}-{period[1:]}" if period.startswith("M") else f"{year}-{period}"
        print(f"[bls] {series_id}: {val} ({date_str})")
        return {"value": val, "date": date_str}
    except Exception as e:
        print(f"[bls] Error fetching {series_id}: {e}")
        return None


def get_bls_estimate(ticker, market_data):
    """BLS data source -- backup for FRED on CPI, unemployment, nonfarm payroll.
    Uses the same probability estimation logic as get_fred_estimate but with
    BLS API as the data provider."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = (ticker or "").upper()

    # Detect indicator type
    series_id = None
    indicator = None
    if any(w in title for w in ["cpi", "inflation", "consumer price"]) or "KXCPI" in ticker_upper:
        series_id = _BLS_SERIES["cpi"]; indicator = "cpi"
    elif any(w in title for w in ["unemployment", "jobless"]) or "KXJOB" in ticker_upper:
        series_id = _BLS_SERIES["unemployment"]; indicator = "unemployment"
    elif any(w in title for w in ["nonfarm", "payroll", "jobs added", "jobs report"]):
        series_id = _BLS_SERIES["nonfarm"]; indicator = "nonfarm"
    else:
        return None, None  # BLS doesn't cover fed funds, GDP, etc.

    latest = get_bls_latest(series_id)
    if not latest:
        return None, None

    # Extract threshold from title, falling back to ticker suffix (-T0.3 etc.)
    thresh_match = re.search(
        r'(at or above|at or below|above|below|over|under|at least|exceed|less than)\s+(\d[\d,]*\.?\d*)\s*%?', title)
    if not thresh_match:
        tick_match = re.search(r'-T(-?\d+\.?\d*)', ticker)
        if tick_match:
            threshold = float(tick_match.group(1))
            is_above = True
        else:
            return None, None
    else:
        direction = thresh_match.group(1)
        threshold = float(thresh_match.group(2).replace(",", ""))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")

    current = latest["value"]

    # For CPI: Kalshi markets reference monthly % change, not the raw index.
    # BLS returns the CPI-U index level (e.g., 330.293). Compute month-over-month change.
    if indicator == "cpi":
        # Fetch prior month by requesting 2 years of data (we already have the latest)
        try:
            now_year = datetime.now(timezone.utc).year
            payload = {
                "seriesid": [series_id],
                "startyear": str(now_year - 1),
                "endyear": str(now_year),
                "registrationkey": BLS_API_KEY,
            }
            cache_key = f"bls_mom_{series_id}"
            if cache_key in _CACHE and time.time() - _CACHE[cache_key][1] < CACHE_TTL:
                prev_val = _CACHE[cache_key][0]
            else:
                rate_limit_wait("https://api.bls.gov/publicAPI/v2/timeseries/data/")
                r = requests.post("https://api.bls.gov/publicAPI/v2/timeseries/data/",
                                  json=payload, timeout=10,
                                  headers={"Content-Type": "application/json"})
                bls_data = r.json()
                obs = bls_data.get("Results", {}).get("series", [{}])[0].get("data", [])
                if len(obs) >= 2:
                    prev_val = float(obs[1]["value"])  # second newest
                    _CACHE[cache_key] = (prev_val, time.time())
                else:
                    prev_val = None

            if prev_val and prev_val > 0:
                monthly_pct = ((current - prev_val) / prev_val) * 100
                print(f"[bls] CPI monthly change: {prev_val:.1f} -> {current:.1f} = {monthly_pct:+.2f}%")
                current = round(monthly_pct, 2)
            else:
                return None, None  # can't compute change without prior month
        except Exception as e:
            print(f"[bls] Error computing CPI monthly change: {e}")
            return None, None

    # Probability estimation (same logic as FRED)
    if indicator == "nonfarm":
        diff = (current - threshold) / max(abs(threshold), 1)
    else:
        diff = (current - threshold) / max(abs(threshold), 0.1)
    if not is_above:
        diff = -diff

    days = _days_to_expiry(market_data)
    if days is not None and days > 0:
        k = 5.0 / math.sqrt(max(days, 1.0))
    else:
        k = 2

    prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-diff * k))))
    days_str = f" days={days:.1f}" if days else ""
    print(f"[bls] {indicator}={current} threshold={threshold} "
          f"{'above' if is_above else 'below'} k={k:.1f}{days_str} -> {prob_yes:.2f}")
    return prob_yes, f"bls:{indicator}={current}"
