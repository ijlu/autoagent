"""Deribit options chain vol surface for crypto implied volatility.

Fetches actual options chain data from Deribit's public API (no auth needed)
to build a term-structure-aware implied volatility surface. Provides:

  1. fetch_deribit_vol_surface()  -- full vol surface with term structure + smile
  2. get_deribit_term_vol()       -- interpolated IV for a specific time horizon
  3. get_deribit_implied_prob()   -- Black-Scholes implied probability P(S > K)

Replaces the crude 24h-range vol proxy in crypto.py with real options data.
"""

from __future__ import annotations

import math
import time
import re
from datetime import datetime, timezone

import requests

from bot.api import _CACHE, rate_limit_wait


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

_DERIBIT_BASE = "https://www.deribit.com/api/v2/public"

_SYMBOL_MAP = {"bitcoin": "BTC", "ethereum": "ETH"}

# Cache vol surface for 30 minutes -- options IVs move slowly relative to
# our 2-minute bot cycle, and we don't want to hammer the Deribit API.
_VOL_SURFACE_TTL = 1800  # seconds

# When selecting ATM options, consider options within this moneyness band
# around spot (i.e. strike within +/-10% of spot).
_ATM_MONEYNESS_BAND = 0.10

# Minimum open interest + volume for an option to be considered reliable.
_MIN_OI_VOLUME = 1


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _parse_instrument_name(name: str) -> dict | None:
    """Parse a Deribit instrument name like 'BTC-28APR26-100000-C'.

    Returns dict with keys: currency, expiry_str, strike, option_type
    or None if unparseable.
    """
    parts = name.split("-")
    if len(parts) != 4:
        return None
    currency, expiry_str, strike_str, opt_type = parts
    if opt_type not in ("C", "P"):
        return None
    try:
        strike = float(strike_str)
    except ValueError:
        return None
    return {
        "currency": currency,
        "expiry_str": expiry_str,
        "strike": strike,
        "option_type": opt_type,
    }


def _parse_expiry_str(expiry_str: str) -> datetime | None:
    """Parse Deribit expiry string like '28APR26' into a datetime (UTC 08:00).

    Deribit options expire at 08:00 UTC on the expiry date.
    """
    try:
        dt = datetime.strptime(expiry_str, "%d%b%y")
        return dt.replace(hour=8, minute=0, second=0, tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_index_price(currency: str) -> float | None:
    """Fetch the current index price for a currency (e.g. 'BTC')."""
    url = f"{_DERIBIT_BASE}/get_index_price?index_name={currency.lower()}_usd"
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[deribit] Index price HTTP {r.status_code} for {currency}")
            return None
        data = r.json()
        price = data.get("result", {}).get("index_price")
        if price is not None:
            return float(price)
        return None
    except Exception as e:
        print(f"[deribit] Index price fetch failed for {currency}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 1. Vol Surface
# ══════════════════════════════════════════════════════════════════════════════

def fetch_deribit_vol_surface(symbol: str) -> dict | None:
    """Fetch the full options chain and extract a term-structure-aware IV surface.

    Args:
        symbol: Lowercase coin name, e.g. "bitcoin" or "ethereum".

    Returns:
        Dict with keys:
            spot_price: float  -- current index price
            term_structure: list[dict]  -- sorted by days to expiry, each with
                expiry, days, atm_iv, n_options
            smile: dict  -- vol smile for nearest liquid expiry with
                put_25d, atm, call_25d
        Or None on any failure.
    """
    currency = _SYMBOL_MAP.get(symbol)
    if not currency:
        return None

    # Check cache first
    cache_key = f"deribit_vol_surface_{symbol}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < _VOL_SURFACE_TTL:
        return _CACHE[cache_key][0]

    # Fetch spot price
    spot = _fetch_index_price(currency)
    if not spot or spot <= 0:
        print(f"[deribit] Cannot build vol surface: no spot price for {currency}")
        return None

    # Fetch all option book summaries in one call
    url = (f"{_DERIBIT_BASE}/get_book_summary_by_currency"
           f"?currency={currency}&kind=option")
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[deribit] Book summary HTTP {r.status_code} for {currency}")
            return None
        data = r.json()
    except Exception as e:
        print(f"[deribit] Book summary fetch failed for {currency}: {e}")
        return None

    summaries = data.get("result", [])
    if not summaries:
        print(f"[deribit] No option summaries returned for {currency}")
        return None

    # Parse each option and group by expiry
    # expiry_map: expiry_str -> list of option dicts
    expiry_map: dict[str, list[dict]] = {}
    now_dt = datetime.now(timezone.utc)

    for s in summaries:
        instrument = s.get("instrument_name", "")
        parsed = _parse_instrument_name(instrument)
        if not parsed:
            continue

        mark_iv = s.get("mark_iv")
        if mark_iv is None or mark_iv <= 0:
            continue

        volume = s.get("volume", 0) or 0
        oi = s.get("open_interest", 0) or 0

        option_data = {
            "instrument": instrument,
            "strike": parsed["strike"],
            "option_type": parsed["option_type"],
            "mark_iv": mark_iv,  # Deribit returns this as percentage (e.g. 55.0)
            "volume": volume,
            "open_interest": oi,
        }

        expiry_str = parsed["expiry_str"]
        if expiry_str not in expiry_map:
            expiry_map[expiry_str] = []
        expiry_map[expiry_str].append(option_data)

    if not expiry_map:
        print(f"[deribit] No valid options parsed for {currency}")
        return None

    # Build term structure: for each expiry, find ATM IV
    term_structure = []
    smile_data = None
    nearest_days = float("inf")

    for expiry_str, options in expiry_map.items():
        expiry_dt = _parse_expiry_str(expiry_str)
        if not expiry_dt:
            continue

        days_to_exp = (expiry_dt - now_dt).total_seconds() / 86400.0
        if days_to_exp < 0:
            continue  # already expired

        expiry_date_str = expiry_dt.strftime("%Y-%m-%d")

        # Filter to options near ATM (within moneyness band)
        atm_options = [
            o for o in options
            if abs(o["strike"] - spot) / spot <= _ATM_MONEYNESS_BAND
            and (o["volume"] + o["open_interest"]) >= _MIN_OI_VOLUME
        ]

        if not atm_options:
            # Relax filter: just find the closest strikes
            sorted_by_dist = sorted(options, key=lambda o: abs(o["strike"] - spot))
            atm_options = sorted_by_dist[:6]  # top 6 closest strikes

        if not atm_options:
            continue

        # ATM IV = volume-weighted average IV of near-money options
        # Weight by inverse distance to spot + open interest
        total_weight = 0.0
        weighted_iv = 0.0
        for o in atm_options:
            dist = abs(o["strike"] - spot) / spot
            # Weight: closer to ATM = higher weight, more OI = higher weight
            w = 1.0 / (dist + 0.01) * max(o["open_interest"] + o["volume"], 1)
            weighted_iv += o["mark_iv"] * w
            total_weight += w

        if total_weight <= 0:
            continue

        atm_iv = weighted_iv / total_weight  # still in percentage form (e.g. 55.0)

        term_structure.append({
            "expiry": expiry_date_str,
            "days": round(days_to_exp, 1),
            "atm_iv": round(atm_iv / 100.0, 4),  # convert to fraction (0.55)
            "n_options": len(options),
        })

        # Build smile for the nearest expiry that has enough options
        if days_to_exp < nearest_days and len(options) >= 6:
            nearest_days = days_to_exp
            smile_data = _build_smile(options, spot)

    if not term_structure:
        print(f"[deribit] No valid expiries found for {currency}")
        return None

    # Sort term structure by days to expiry
    term_structure.sort(key=lambda x: x["days"])

    # If we didn't get a smile from the nearest expiry, try the first one
    if smile_data is None and term_structure:
        first_expiry_str = None
        for es, opts in expiry_map.items():
            expiry_dt = _parse_expiry_str(es)
            if expiry_dt:
                ed = expiry_dt.strftime("%Y-%m-%d")
                if ed == term_structure[0]["expiry"]:
                    first_expiry_str = es
                    break
        if first_expiry_str:
            smile_data = _build_smile(expiry_map[first_expiry_str], spot)

    # Default smile if we couldn't build one
    if smile_data is None:
        front_iv = term_structure[0]["atm_iv"]
        smile_data = {"put_25d": front_iv, "atm": front_iv, "call_25d": front_iv}

    result = {
        "spot_price": spot,
        "term_structure": term_structure,
        "smile": smile_data,
    }

    # Cache result
    _CACHE[cache_key] = (result, now)

    # Log summary
    n_expiries = len(term_structure)
    front_iv_pct = term_structure[0]["atm_iv"] * 100
    print(f"[deribit] {currency} vol surface: {n_expiries} expiries, "
          f"ATM IV={front_iv_pct:.1f}%, spot=${spot:,.0f}")

    return result


def _build_smile(options: list[dict], spot: float) -> dict:
    """Build a rough vol smile from a single expiry's options.

    Returns dict with put_25d, atm, call_25d implied vols (as fractions).
    25-delta approximation: roughly 10% OTM for puts, 10% OTM for calls.
    """
    atm_iv = None
    put_25d_iv = None
    call_25d_iv = None

    # Separate calls and puts
    calls = [o for o in options if o["option_type"] == "C" and o["mark_iv"] > 0]
    puts = [o for o in options if o["option_type"] == "P" and o["mark_iv"] > 0]

    # ATM: closest call or put to spot
    all_opts = calls + puts
    if all_opts:
        closest = min(all_opts, key=lambda o: abs(o["strike"] - spot))
        atm_iv = closest["mark_iv"] / 100.0

    # 25-delta put: roughly 90% of spot (10% OTM)
    target_put_strike = spot * 0.90
    if puts:
        otm_puts = [p for p in puts if p["strike"] < spot]
        if otm_puts:
            closest_put = min(otm_puts, key=lambda o: abs(o["strike"] - target_put_strike))
            put_25d_iv = closest_put["mark_iv"] / 100.0

    # 25-delta call: roughly 110% of spot (10% OTM)
    target_call_strike = spot * 1.10
    if calls:
        otm_calls = [c for c in calls if c["strike"] > spot]
        if otm_calls:
            closest_call = min(otm_calls, key=lambda o: abs(o["strike"] - target_call_strike))
            call_25d_iv = closest_call["mark_iv"] / 100.0

    # Fill in missing values with ATM if available
    if atm_iv is None:
        atm_iv = 0.50  # emergency fallback
    if put_25d_iv is None:
        put_25d_iv = atm_iv
    if call_25d_iv is None:
        call_25d_iv = atm_iv

    return {
        "put_25d": round(put_25d_iv, 4),
        "atm": round(atm_iv, 4),
        "call_25d": round(call_25d_iv, 4),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. Term Vol Interpolation
# ══════════════════════════════════════════════════════════════════════════════

def get_deribit_term_vol(symbol: str, days_to_expiry: float) -> float | None:
    """Interpolate the term structure to get IV for a specific time horizon.

    Args:
        symbol: Lowercase coin name, e.g. "bitcoin".
        days_to_expiry: Number of days until the target event/expiry.

    Returns:
        Daily volatility as a fraction (e.g. 0.029 = 2.9% daily), matching
        the unit convention of _fetch_deribit_iv() in crypto.py.
        Returns None on failure.
    """
    surface = fetch_deribit_vol_surface(symbol)
    if not surface or not surface.get("term_structure"):
        return None

    ts = surface["term_structure"]
    target = max(days_to_expiry, 0.01)  # floor at ~15 min

    # Edge cases: before shortest or beyond longest expiry
    if target <= ts[0]["days"]:
        annual_iv = ts[0]["atm_iv"]
    elif target >= ts[-1]["days"]:
        annual_iv = ts[-1]["atm_iv"]
    else:
        # Linear interpolation between bracketing expiries
        annual_iv = None
        for i in range(len(ts) - 1):
            if ts[i]["days"] <= target <= ts[i + 1]["days"]:
                d0, iv0 = ts[i]["days"], ts[i]["atm_iv"]
                d1, iv1 = ts[i + 1]["days"], ts[i + 1]["atm_iv"]
                # Interpolate in variance space (sigma^2 * T is additive)
                # then convert back to vol
                var0 = iv0 * iv0 * d0 / 365.0
                var1 = iv1 * iv1 * d1 / 365.0
                # Linear interp in total variance
                frac = (target - d0) / (d1 - d0)
                var_t = var0 + frac * (var1 - var0)
                # Convert total variance back to annualized IV
                annual_iv = math.sqrt(var_t / (target / 365.0)) if target > 0 else iv0
                break
        if annual_iv is None:
            annual_iv = ts[0]["atm_iv"]  # fallback

    # Convert annualized IV to daily vol
    daily_vol = annual_iv / math.sqrt(365.0)
    return daily_vol


# ══════════════════════════════════════════════════════════════════════════════
# 3. Implied Probability
# ══════════════════════════════════════════════════════════════════════════════

def get_deribit_implied_prob(symbol: str, strike: float,
                             days_to_expiry: float) -> float | None:
    """Compute implied probability P(S > K) at expiry using Black-Scholes d2.

    Uses the vol surface to get the appropriate IV for the given time horizon,
    then applies the standard Black-Scholes formula:

        d2 = (ln(S/K) + (r - 0.5*sigma^2)*T) / (sigma*sqrt(T))
        P(S > K) = N(d2)

    where r = 0 (crypto, no risk-free rate), S = spot, K = strike,
    sigma = annualized IV, T = days/365.

    Args:
        symbol: Lowercase coin name, e.g. "bitcoin".
        strike: The price level to evaluate (e.g. 100000 for "$100K BTC").
        days_to_expiry: Days until the market resolves.

    Returns:
        Probability (0.0 to 1.0) that the price will be above the strike
        at expiry. Returns None on failure.
    """
    surface = fetch_deribit_vol_surface(symbol)
    if not surface:
        return None

    spot = surface.get("spot_price")
    if not spot or spot <= 0 or strike <= 0:
        return None

    T = max(days_to_expiry, 0.001) / 365.0  # in years, floor to avoid div-by-zero

    # Get annualized IV for this tenor
    ts = surface.get("term_structure", [])
    if not ts:
        return None

    # Find the appropriate annualized IV (same interpolation logic as get_deribit_term_vol
    # but we need the annualized value, not daily)
    target_days = max(days_to_expiry, 0.01)

    if target_days <= ts[0]["days"]:
        sigma = ts[0]["atm_iv"]
    elif target_days >= ts[-1]["days"]:
        sigma = ts[-1]["atm_iv"]
    else:
        sigma = None
        for i in range(len(ts) - 1):
            if ts[i]["days"] <= target_days <= ts[i + 1]["days"]:
                d0, iv0 = ts[i]["days"], ts[i]["atm_iv"]
                d1, iv1 = ts[i + 1]["days"], ts[i + 1]["atm_iv"]
                var0 = iv0 * iv0 * d0 / 365.0
                var1 = iv1 * iv1 * d1 / 365.0
                frac = (target_days - d0) / (d1 - d0)
                var_t = var0 + frac * (var1 - var0)
                sigma = math.sqrt(var_t / T) if T > 0 else iv0
                break
        if sigma is None:
            sigma = ts[0]["atm_iv"]

    if sigma <= 0:
        return None

    # Also check the smile: if strike is far OTM, use skewed vol
    smile = surface.get("smile", {})
    moneyness = (spot - strike) / spot  # positive = ITM for calls
    if moneyness < -0.08 and smile.get("call_25d"):
        # OTM call territory: use call wing vol
        sigma = smile["call_25d"]
    elif moneyness > 0.08 and smile.get("put_25d"):
        # Deep ITM call = OTM put territory: use put wing vol
        sigma = smile["put_25d"]

    # Black-Scholes d2 with r=0
    r = 0.0
    d2 = (math.log(spot / strike) + (r - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    prob = _normal_cdf(d2)

    # Clamp to [0.01, 0.99] -- extreme probabilities are unreliable
    prob = max(0.01, min(0.99, prob))

    print(f"[deribit] P({symbol} > ${strike:,.0f}) in {days_to_expiry:.1f}d = {prob:.3f} "
          f"(spot=${spot:,.0f}, IV={sigma*100:.1f}%)")

    return prob
