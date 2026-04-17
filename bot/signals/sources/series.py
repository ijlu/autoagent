"""Series structure signal source.

Detects mispriced strikes within a Kalshi series/event by analyzing the
implied CDF from sibling markets. Uses Kalshi API (api_get) to fetch
related markets.
"""

from __future__ import annotations

import re
import time

from bot.api import api_get


# ══════════════════════════════════════════════════════════════════════════════
# Series structure -- detect mispriced strikes within Kalshi series
# ══════════════════════════════════════════════════════════════════════════════

_SERIES_CACHE = {}  # {event_ticker: (markets_list, timestamp)}


def get_series_estimate(ticker, market_data):
    """Analyze related markets in the same Kalshi series/event to detect
    mispricing. If a series of strike-price markets has an inconsistent
    implied CDF, individual strikes may be mispriced.
    Returns (adjusted_prob, source_desc) or (None, None)."""
    event_ticker = market_data.get("event_ticker") or ""
    if not event_ticker:
        return None, None

    # Cache series data for 5 min
    now = time.time()
    if event_ticker in _SERIES_CACHE and now - _SERIES_CACHE[event_ticker][1] < 300:
        siblings = _SERIES_CACHE[event_ticker][0]
    else:
        try:
            resp = api_get(f"/events/{event_ticker}/markets?limit=50&status=open")
            siblings = resp.get("markets", [])
            _SERIES_CACHE[event_ticker] = (siblings, now)
        except Exception:
            return None, None

    if len(siblings) < 3:
        return None, None  # need multiple strikes to do series analysis

    # Build the implied probability curve from sibling markets
    # Each sibling is a strike: "BTC above $90k", "BTC above $95k", etc.
    # For "above X" markets, the yes_ask prices should form a monotonically
    # decreasing CDF (higher strikes -> lower probability)
    strikes = []
    for sib in siblings:
        sib_ticker = sib.get("ticker", "")
        sib_title = (sib.get("title") or sib.get("subtitle") or "").lower()
        sib_ask = float(sib.get("yes_ask") or sib.get("yes_ask_dollars") or 0)
        if sib_ask > 1: sib_ask /= 100
        sib_bid = float(sib.get("yes_bid") or sib.get("yes_bid_dollars") or 0)
        if sib_bid > 1: sib_bid /= 100

        # Extract numeric strike from title
        strike_match = re.search(r'\$?([\d,]+(?:\.\d+)?)', sib_title)
        if strike_match and sib_ask > 0 and sib_bid > 0:
            try:
                strike_val = float(strike_match.group(1).replace(",", ""))
                if strike_val > 10:  # sanity check
                    mid = (sib_ask + sib_bid) / 2
                    strikes.append((strike_val, mid, sib_ticker))
            except: pass

    if len(strikes) < 3:
        return None, None

    # Sort by strike value
    strikes.sort(key=lambda x: x[0])

    # Check for CDF monotonicity violations
    # In a well-priced "above X" series, higher strikes should have lower probability.
    # Detect if our target market is out of line with its neighbors.
    our_strike_idx = None
    for i, (sv, mid, st) in enumerate(strikes):
        if st == ticker:
            our_strike_idx = i
            break

    if our_strike_idx is None:
        return None, None

    our_val, our_mid, _ = strikes[our_strike_idx]

    # Interpolate what the probability "should" be based on neighbors
    # Use simple linear interpolation between adjacent strikes
    if our_strike_idx > 0 and our_strike_idx < len(strikes) - 1:
        lower_strike, lower_mid, _ = strikes[our_strike_idx - 1]
        upper_strike, upper_mid, _ = strikes[our_strike_idx + 1]
        # Linear interpolation
        if upper_strike != lower_strike:
            frac = (our_val - lower_strike) / (upper_strike - lower_strike)
            interpolated = lower_mid + frac * (upper_mid - lower_mid)
            deviation = our_mid - interpolated
            if abs(deviation) > 0.03:  # >3c mispricing vs interpolated
                print(f"[series] {ticker}: market mid={our_mid:.2f} "
                      f"interpolated={interpolated:.2f} deviation={deviation:+.2f} "
                      f"({len(strikes)} strikes in {event_ticker})")
                return interpolated, f"series:{event_ticker}({len(strikes)}strikes)"

    return None, None
