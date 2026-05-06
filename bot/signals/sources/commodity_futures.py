"""Commodity-futures source for Kalshi KXCPI / inflation-adjacent markets.

Front-month crude oil (CL), natural gas (NG), and wheat (ZW) futures are
high-frequency leading indicators of CPI. When CL rallies 10% in a month,
gasoline component of CPI typically rises ~2-3% MoM. When NG / agricultural
commodities spike, food & energy CPI follows with a 1-2 month lag.

We fetch front-month futures prices from Yahoo Finance (same source as the
existing ZQ futures integration). Compute 30-day pct change. Build a
probabilistic CPI signal from the weighted commodity basket move.

Data model:
  - CL (crude) weighted 60% (gasoline is largest non-shelter CPI component)
  - NG (nat gas) weighted 25% (natgas + electricity in energy CPI)
  - ZW (wheat) weighted 15% (food-at-home)

Free, no auth.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from bot.api import _CACHE, rate_limit_wait


_COMMODITY_CACHE_TTL = 7200  # 2h

# Front-month continuous contract symbols on Yahoo Finance
_COMMODITY_SYMBOLS = {
    "CL": "CL=F",  # Crude oil
    "NG": "NG=F",  # Natural gas
    "ZW": "ZW=F",  # Wheat
}

# CPI-component weights (rough OER-free inflation basket)
_COMMODITY_WEIGHTS = {"CL": 0.60, "NG": 0.25, "ZW": 0.15}

# Historical: a 10% commodity-basket move over 30d translates to ~0.3pp MoM
# CPI surprise (vs consensus). Inverse: 1 pct commodity move ≈ 0.03pp CPI.
_COMMODITY_TO_CPI_MULT = 0.03


def _gaussian_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def _fetch_yahoo_range(symbol: str, days: int = 35) -> Optional[list[float]]:
    """Fetch recent daily closes for a symbol. Returns list in chronological order."""
    cache_key = f"commodity::{symbol}::{days}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _COMMODITY_CACHE_TTL:
            return data

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        rate_limit_wait(url)
        r = requests.get(
            url,
            params={"range": f"{days}d", "interval": "1d"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 KalshiBot"},
        )
        if r.status_code != 200:
            print(f"[commodity] HTTP {r.status_code} for {symbol}")
            return None
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        closes = (result[0].get("indicators", {}).get("quote", [{}])[0]
                  .get("close", []))
        closes = [c for c in closes if c is not None]
        if len(closes) < 2:
            return None
        _CACHE[cache_key] = (closes, now)
        return closes
    except Exception as e:
        print(f"[commodity] {symbol} error: {type(e).__name__}: {e}")
        return None


def _basket_30d_change() -> Optional[float]:
    """Return the weighted-basket 30d pct change (e.g. 0.08 = +8%)."""
    total_weight = 0.0
    weighted_change = 0.0
    for sym_key, sym in _COMMODITY_SYMBOLS.items():
        closes = _fetch_yahoo_range(sym, days=35)
        if not closes or len(closes) < 20:
            continue
        start = closes[0]
        end = closes[-1]
        if start <= 0:
            continue
        pct = (end - start) / start
        w = _COMMODITY_WEIGHTS[sym_key]
        weighted_change += pct * w
        total_weight += w
    if total_weight <= 0:
        return None
    return weighted_change / total_weight  # weight-normalized


def _parse_cpi_threshold(ticker: str, title: str) -> tuple[Optional[float], bool]:
    """Parse CPI threshold (YoY %) and direction."""
    upper = (ticker or "").upper()
    m = re.search(r"-T(-?\d+\.?\d*)", upper)
    if m:
        return float(m.group(1)), True

    title_l = (title or "").lower()
    m = re.search(r"(above|over|exceed|at least|more than)\s+(-?\d+\.?\d*)\s*%?", title_l)
    if m:
        return float(m.group(2)), True
    m = re.search(r"(below|under|less than)\s+(-?\d+\.?\d*)\s*%?", title_l)
    if m:
        return float(m.group(2)), False

    return None, True


def get_commodity_cpi_estimate(ticker: str, market_data: dict) -> tuple:
    """CPI estimate from commodity-basket 30d move.

    Returns (prob, source_tag) or (None, None).
    """
    if market_data is None:
        return None, None
    ticker_upper = (ticker or "").upper()
    title = (market_data.get("title") or market_data.get("subtitle") or "")

    is_cpi_market = ticker_upper.startswith("KXCPI") or any(
        kw in title.lower() for kw in (
            "cpi", "consumer price index", "inflation", "core cpi",
        )
    )
    if not is_cpi_market:
        return None, None

    threshold, is_above = _parse_cpi_threshold(ticker, title)
    if threshold is None:
        return None, None

    basket_pct = _basket_30d_change()
    if basket_pct is None:
        return None, None

    # Translate commodity move into an MoM CPI surprise (pp vs consensus).
    cpi_surprise_pp = basket_pct * 100.0 * _COMMODITY_TO_CPI_MULT
    # Assume consensus ~2.5% YoY (typical Kalshi setup); CPI estimate = consensus + surprise
    consensus_yoy = 2.5
    cpi_estimate = consensus_yoy + cpi_surprise_pp
    # Sigma: CPI surprise vol across many months is ~0.15pp
    sigma = 0.25

    prob_above = 1.0 - _gaussian_cdf(threshold, cpi_estimate, sigma)
    prob = prob_above if is_above else (1.0 - prob_above)
    prob = max(0.02, min(0.98, prob))

    direction = "above" if is_above else "below"
    print(
        f"[commodity] basket_30d={basket_pct:+.1%} surprise={cpi_surprise_pp:+.2f}pp "
        f"cpi_est={cpi_estimate:+.2f}% threshold={threshold:+.2f}% ({direction}) -> {prob:.3f}"
    )
    return prob, f"commodity:basket={basket_pct:+.1%}_{direction}_{threshold:+.2f}"
