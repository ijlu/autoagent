"""Yahoo Finance ZQ futures data source for market-implied fed funds rate probabilities.

30-Day Fed Funds Futures (ZQ contracts) settle at the average effective fed funds
rate for their delivery month. By reading the implied rate from each contract month,
we can decompose meeting-by-meeting rate change probabilities — the same math that
powers the CME FedWatch Tool, but using freely available Yahoo Finance data instead
of CME's IP-blocked JSON endpoint.

Contract convention:
  Symbol: ZQ + month_code + year_suffix + ".CBT"
  Month codes: F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec
  Example: ZQK25.CBT = May 2025 Fed Funds Futures

Implied rate: 100 - settlement_price

Meeting-by-meeting decomposition:
  For a month with a meeting on day D (out of N total days):
  post_rate = (monthly_implied * N - D * pre_rate) / (N - D)

  Where pre_rate = rate before the meeting (known from prior month's contract),
  and monthly_implied = 100 - futures_price.
"""

from __future__ import annotations

import calendar
import math
import re
import time
from datetime import datetime, timezone, timedelta

import requests

from bot.api import _CACHE
from bot.signals.sources._fomc_calendar import (
    FOMC_MEETING_DATES as _FOMC_MEETINGS,
    RATE_RANGES as _RATE_RANGES,
    parse_fomc_dates as _fomc_dates_parsed,
)


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

_ZQ_CACHE_KEY = "zq_futures_data"
_ZQ_CACHE_TTL = 7200  # 2 hours — futures prices don't move minute-to-minute

# Month code mapping for ZQ contract symbols
_MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}
_CODE_TO_MONTH = {v: k for k, v in _MONTH_CODES.items()}

_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


# ══════════════════════════════════════════════════════════════════════════════
# Yahoo Finance ZQ Data Fetching
# ══════════════════════════════════════════════════════════════════════════════

def _zq_symbol(year: int, month: int) -> str:
    """Build Yahoo Finance ZQ futures symbol.

    Example: year=2025, month=5 → ZQK25.CBT
    """
    code = _MONTH_CODES[month]
    yr = str(year)[-2:]  # last 2 digits
    return f"ZQ{code}{yr}.CBT"


def _fetch_zq_price(symbol: str) -> float | None:
    """Fetch the last price for a ZQ futures contract from Yahoo Finance.

    Returns the price (e.g., 95.695) or None on failure.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "1d"}
    try:
        resp = requests.get(url, params=params, timeout=10, headers=_YAHOO_HEADERS)
        if resp.status_code != 200:
            return None
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        meta = result[0].get("meta", {})
        # Use regularMarketPrice (most recent) or previousClose
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        if price is not None:
            return float(price)
        # Fallback: last close from the indicators
        closes = (result[0].get("indicators", {}).get("quote", [{}])[0]
                  .get("close", []))
        for c in reversed(closes):
            if c is not None:
                return float(c)
        return None
    except Exception as e:
        print(f"[zq] Error fetching {symbol}: {e}")
        return None


def fetch_zq_implied_rates() -> dict[str, float]:
    """Fetch implied fed funds rates from ZQ futures for the next 12 months.

    Returns:
        dict mapping "YYYY-MM" to implied rate (e.g., {"2025-05": 4.305, ...})
    """
    # Check cache
    now_ts = time.time()
    if _ZQ_CACHE_KEY in _CACHE:
        cached_val, cached_ts = _CACHE[_ZQ_CACHE_KEY]
        if now_ts - cached_ts < _ZQ_CACHE_TTL:
            return cached_val

    now = datetime.now(timezone.utc)
    rates = {}

    # Fetch current month + next 12 months
    for offset in range(0, 13):
        year = now.year + (now.month + offset - 1) // 12
        month = (now.month + offset - 1) % 12 + 1
        symbol = _zq_symbol(year, month)
        price = _fetch_zq_price(symbol)
        if price is not None:
            implied_rate = 100.0 - price
            key = f"{year}-{month:02d}"
            rates[key] = round(implied_rate, 4)

    if rates:
        _CACHE[_ZQ_CACHE_KEY] = (rates, now_ts)
        print(f"[zq] Fetched {len(rates)} ZQ implied rates: "
              + ", ".join(f"{k}={v:.3f}%" for k, v in sorted(rates.items())[:6]))

    return rates


# ══════════════════════════════════════════════════════════════════════════════
# Meeting-by-Meeting Rate Decomposition
# ══════════════════════════════════════════════════════════════════════════════

def _meetings_in_month(year: int, month: int) -> list[datetime]:
    """Return FOMC meetings that fall in a given month."""
    fomc = _fomc_dates_parsed()
    return [m for m in fomc if m.year == year and m.month == month]


def decompose_meeting_rates(
    implied_rates: dict[str, float],
    current_rate: float,
) -> list[dict]:
    """Decompose monthly ZQ implied rates into per-meeting rate expectations.

    Uses the standard FedWatch decomposition:
      For a month with meeting on day D (of N days total):
      post_meeting_rate = (monthly_avg * N - pre_rate * D) / (N - D)

    For months with no meeting: rate stays at pre_rate.
    For months with one meeting: standard formula.
    For months with two meetings: more complex (rare, skip second).

    Args:
        implied_rates: {"YYYY-MM": rate, ...} from ZQ futures
        current_rate: Current effective fed funds rate (from FRED DFF or similar)

    Returns:
        List of meeting dicts with probability distributions, compatible
        with fedwatch.py's fetch_fedwatch_probabilities() format:
        [{"date": "YYYY-MM-DD", "probabilities": {"4.25-4.50": 0.65, ...}}, ...]
    """
    fomc_dates = _fomc_dates_parsed()
    now = datetime.now(timezone.utc)
    future_meetings = [m for m in fomc_dates if m > now]

    if not future_meetings:
        return []

    # Track the "pre-meeting" rate as we step through time
    pre_rate = current_rate
    meetings_out = []

    for meeting in future_meetings[:8]:  # limit to 8 meetings ahead
        month_key = f"{meeting.year}-{meeting.month:02d}"

        if month_key not in implied_rates:
            # No ZQ data for this month — use synthetic estimate
            # (rate stays at pre_rate with some uncertainty)
            probs = _rate_to_gaussian_probs(pre_rate, sigma=0.15)
            meetings_out.append({
                "date": meeting.strftime("%Y-%m-%d"),
                "datetime": meeting,
                "probabilities": probs,
            })
            continue

        monthly_implied = implied_rates[month_key]
        meeting_day = meeting.day
        days_in_month = calendar.monthrange(meeting.year, meeting.month)[1]

        # Decompose: post_rate = (monthly_implied * N - pre_rate * D) / (N - D)
        N = days_in_month
        D = meeting_day
        if N - D <= 0:
            # Meeting is on last day of month — monthly rate IS the pre-rate + 1 day of post
            # Just use the implied rate directly
            post_rate = monthly_implied
        else:
            post_rate = (monthly_implied * N - pre_rate * D) / (N - D)

        # Clamp to reasonable range (0% to 10%)
        post_rate = max(0.0, min(10.0, post_rate))

        # Convert post_rate into a probability distribution over 25bp ranges
        probs = _rate_to_target_probs(post_rate)

        meetings_out.append({
            "date": meeting.strftime("%Y-%m-%d"),
            "datetime": meeting,
            "probabilities": probs,
        })

        # Update pre_rate for the next meeting
        # After this meeting, the rate is expected to be at post_rate
        pre_rate = post_rate

    return meetings_out


def _rate_to_target_probs(rate: float) -> dict[str, float]:
    """Convert an expected rate into a probability distribution over 25bp target ranges.

    If the rate falls exactly at a range midpoint, that range gets ~100%.
    If it falls between two ranges, probability is split proportionally.

    Example: rate=4.375 → {"4.25-4.50": 1.0}  (midpoint of the range)
    Example: rate=4.30  → {"4.00-4.25": 0.40, "4.25-4.50": 0.60}
    """
    probs: dict[str, float] = {}

    # Find the two closest ranges
    best_range = None
    best_dist = float("inf")
    second_range = None
    second_dist = float("inf")

    for lo, hi in _RATE_RANGES:
        mid = (lo + hi) / 2.0
        dist = abs(rate - mid)
        if dist < best_dist:
            second_range = best_range
            second_dist = best_dist
            best_range = (lo, hi)
            best_dist = dist
        elif dist < second_dist:
            second_range = (lo, hi)
            second_dist = dist

    if best_range is None:
        return {}

    if second_range is None or best_dist < 0.001:
        # Rate is exactly at a range midpoint
        key = f"{best_range[0]:.2f}-{best_range[1]:.2f}"
        probs[key] = 1.0
    else:
        # Split proportionally (inverse distance weighting)
        total_dist = best_dist + second_dist
        if total_dist > 0:
            w1 = 1.0 - (best_dist / total_dist)
            w2 = 1.0 - (second_dist / total_dist)
            total_w = w1 + w2
            key1 = f"{best_range[0]:.2f}-{best_range[1]:.2f}"
            key2 = f"{second_range[0]:.2f}-{second_range[1]:.2f}"
            probs[key1] = w1 / total_w
            probs[key2] = w2 / total_w
        else:
            key = f"{best_range[0]:.2f}-{best_range[1]:.2f}"
            probs[key] = 1.0

    return probs


def _rate_to_gaussian_probs(rate: float, sigma: float = 0.25) -> dict[str, float]:
    """Convert a rate into a Gaussian probability distribution over 25bp ranges.

    Used when we don't have futures data for a month (fallback).
    """
    probs: dict[str, float] = {}
    total = 0.0

    for lo, hi in _RATE_RANGES:
        mid = (lo + hi) / 2.0
        z = (mid - rate) / max(sigma, 0.01)
        p = math.exp(-0.5 * z * z)
        key = f"{lo:.2f}-{hi:.2f}"
        probs[key] = p
        total += p

    # Normalize
    if total > 0:
        probs = {k: v / total for k, v in probs.items() if v / total >= 0.005}
        total2 = sum(probs.values())
        if total2 > 0:
            probs = {k: v / total2 for k, v in probs.items()}

    return probs


# ══════════════════════════════════════════════════════════════════════════════
# Integration with fedwatch.py
# ══════════════════════════════════════════════════════════════════════════════

def fetch_zq_fedwatch_probabilities(current_rate: float | None = None) -> dict | None:
    """Fetch FedWatch-style probabilities using ZQ futures data.

    Returns the same format as fedwatch.fetch_fedwatch_probabilities():
        {
            "current_rate": float,
            "target_upper": float,
            "target_lower": float,
            "source": "zq_futures",
            "meetings": [{"date": "YYYY-MM-DD", "probabilities": {...}}, ...]
        }

    Args:
        current_rate: Override current rate (for testing). If None, uses DFF from FRED.
    """
    try:
        implied_rates = fetch_zq_implied_rates()
        if not implied_rates:
            print("[zq] No ZQ data available")
            return None

        # Get current effective rate
        if current_rate is None:
            current_rate = _get_current_effr()
            if current_rate is None:
                # Last resort: infer from current month's ZQ contract
                now = datetime.now(timezone.utc)
                cur_key = f"{now.year}-{now.month:02d}"
                if cur_key in implied_rates:
                    current_rate = implied_rates[cur_key]
                else:
                    print("[zq] Cannot determine current rate")
                    return None

        # Find the target range containing the current rate
        target_lower = None
        target_upper = None
        for lo, hi in _RATE_RANGES:
            if lo <= current_rate + 0.001 and current_rate < hi + 0.001:
                target_lower = lo
                target_upper = hi
                break

        if target_lower is None:
            # Fallback: assume 4.25-4.50 (common range)
            target_lower = 4.25
            target_upper = 4.50

        # Decompose into per-meeting probabilities
        meetings = decompose_meeting_rates(implied_rates, current_rate)

        if not meetings:
            print("[zq] No meeting probabilities computed")
            return None

        result = {
            "current_rate": current_rate,
            "target_upper": target_upper,
            "target_lower": target_lower,
            "source": "zq_futures",
            "meetings": meetings,
        }

        print(f"[zq] ZQ source ready: rate={current_rate:.3f}%, "
              f"range={target_lower:.2f}-{target_upper:.2f}%, "
              f"{len(meetings)} meetings")

        return result

    except Exception as e:
        print(f"[zq] Error building probabilities: {e}")
        return None


def _get_current_effr() -> float | None:
    """Get current effective federal funds rate from FRED."""
    from bot.config import FRED_API_KEY
    if not FRED_API_KEY:
        return None
    try:
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id=DFF&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=5"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        observations = data.get("observations", [])
        for obs in observations:
            val = obs.get("value", ".")
            if val != ".":
                return float(val)
        return None
    except Exception:
        return None
