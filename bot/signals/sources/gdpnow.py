"""Atlanta Fed GDPNow nowcast source for Kalshi KXGDP markets.

GDPNow is the Atlanta Fed's real-time nowcast of GDP for the current quarter,
updated ~weekly as new data flows in. It runs 1-2 months ahead of the BEA's
official advance estimate, which is the data point Kalshi's KXGDP markets
settle on.

Data: FRED series GDPNOW (Atlanta Fed GDPNow). The series is updated as the
Atlanta Fed publishes new nowcasts.

Probability model: P(advance_GDP >= threshold) given nowcast. Historical
nowcast error (RMSE vs BEA advance) for GDPNow is ~0.5pp in the final month
of a quarter, ~1.0pp at mid-quarter, ~1.5pp at start of quarter.

Free, requires FRED_API_KEY.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.config import FRED_API_KEY


_GDPNOW_CACHE_TTL = 21600  # 6h
_GDPNOW_SERIES = "GDPNOW"
# Historical RMSE of GDPNow vs BEA advance estimate, by weeks-before-release:
# - 0-4 weeks before: 0.5pp
# - 4-8 weeks before: 1.0pp
# - 8-12 weeks before: 1.5pp
# Use 1.0pp as a safe default (we don't know the Kalshi market's target release).
_GDPNOW_RMSE_PP = 1.0


def _gaussian_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def _fetch_gdpnow() -> Optional[float]:
    """Return the latest GDPNow nowcast (annualized % QoQ growth)."""
    if not FRED_API_KEY:
        return None
    cache_key = "gdpnow::latest"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _GDPNOW_CACHE_TTL:
            return data

    url = (
        f"https://api.stlouisfed.org/fred/series/observations?"
        f"series_id={_GDPNOW_SERIES}&api_key={FRED_API_KEY}&file_type=json"
        f"&sort_order=desc&limit=5"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[gdpnow] HTTP {r.status_code}")
            return None
        obs = r.json().get("observations", [])
        for o in obs:
            if o.get("value", ".") != ".":
                val = float(o["value"])
                _CACHE[cache_key] = (val, now)
                return val
        return None
    except Exception as e:
        print(f"[gdpnow] error: {type(e).__name__}: {e}")
        return None


def _parse_kxgdp_threshold(ticker: str, title: str) -> tuple[Optional[float], bool]:
    """Parse the GDP growth threshold (annualized %) + direction.

    Examples:
      "KXGDP-26Q2-T2.5" → 2.5, above
      "Will Q2 GDP grow more than 2.5%?" → 2.5, above
      "below 1%" → 1.0, below
    """
    upper = (ticker or "").upper()
    m = re.search(r"-T(-?\d+\.?\d*)", upper)
    if m:
        return float(m.group(1)), True

    title_l = (title or "").lower()
    m = re.search(r"(above|over|exceed|at least|more than|grow more than)\s+(-?\d+\.?\d*)\s*%?", title_l)
    if m:
        return float(m.group(2)), True
    m = re.search(r"(below|under|less than|fewer than|grow less than)\s+(-?\d+\.?\d*)\s*%?", title_l)
    if m:
        return float(m.group(2)), False

    return None, True


def get_gdpnow_estimate(ticker: str, market_data: dict) -> tuple:
    """GDPNow-based probability for Kalshi KXGDP markets.

    Returns (prob, source_tag) or (None, None).
    """
    if market_data is None:
        return None, None
    ticker_upper = (ticker or "").upper()
    title = (market_data.get("title") or market_data.get("subtitle") or "")

    is_gdp_market = ticker_upper.startswith("KXGDP") or any(
        kw in title.lower() for kw in (
            "gdp", "gross domestic product", "quarterly growth",
        )
    )
    if not is_gdp_market:
        return None, None

    threshold, is_above = _parse_kxgdp_threshold(ticker, title)
    if threshold is None:
        return None, None

    nowcast = _fetch_gdpnow()
    if nowcast is None:
        return None, None

    prob_above = 1.0 - _gaussian_cdf(threshold, nowcast, _GDPNOW_RMSE_PP)
    prob = prob_above if is_above else (1.0 - prob_above)
    prob = max(0.02, min(0.98, prob))

    direction = "above" if is_above else "below"
    print(
        f"[gdpnow] nowcast={nowcast:+.2f}% threshold={threshold:+.2f}% "
        f"sigma={_GDPNOW_RMSE_PP}pp ({direction}) -> {prob:.3f}"
    )
    return prob, f"gdpnow:nowcast={nowcast:+.2f}_{direction}_{threshold:+.2f}"
