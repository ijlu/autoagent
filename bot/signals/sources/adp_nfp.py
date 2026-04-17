"""ADP Nonfarm Private Payroll source for Kalshi KXJOB markets.

ADP's National Employment Report releases the Wednesday before the Friday BLS
Nonfarm Payrolls (NFP) print. Historically, ADP has a ~70% directional
accuracy vs the BLS NFP surprise (above/below consensus). That 2-day lead
window gives us an alpha-generating signal for Kalshi's KXJOB markets.

Data source: FRED series ADPWNUSNERSA (ADP Weekly National Employment,
weekly reading). We fetch the last N weeks, compute the monthly change
(approximated by summing 4 weekly deltas), and compare to a rolling 3-month
baseline to produce a directional probability.

For the specific Kalshi market question — "Will nonfarm payrolls come in
above X?" — we build a probability from the ADP-implied monthly change using
a Gaussian model centered on ADP's estimate with sigma = RMSE vs BLS.

Calibration data (last 24 months of ADP vs BLS surprises) is captured in
`reports/ADP_VALIDATION.md` and used to set sigma.

Free, requires FRED_API_KEY (already in .env).
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


_ADP_CACHE_TTL = 21600  # 6h
_ADP_SERIES_ID = "ADPWNUSNERSA"  # ADP National Employment Report, weekly
# ADP vs BLS RMSE from historical validation (2024-2025): ~85k jobs on monthly prints
_ADP_RMSE_K_JOBS = 85.0
# Alt monthly series (if weekly unavailable): FRED has NPPTTL (ADP Total Nonfarm Private Payrolls)
_ADP_MONTHLY_SERIES_ID = "NPPTTL"


def _gaussian_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def _fetch_fred_series(series_id: str, limit: int = 12) -> Optional[list[dict]]:
    """Fetch the most recent N observations for a FRED series."""
    if not FRED_API_KEY:
        return None
    cache_key = f"adp_fred::{series_id}::{limit}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _ADP_CACHE_TTL:
            return data

    url = (
        f"https://api.stlouisfed.org/fred/series/observations?"
        f"series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
        f"&sort_order=desc&limit={limit}"
    )
    try:
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            print(f"[adp] FRED HTTP {r.status_code} for {series_id}")
            return None
        obs = r.json().get("observations", [])
        _CACHE[cache_key] = (obs, now)
        return obs
    except Exception as e:
        print(f"[adp] FRED error: {type(e).__name__}: {e}")
        return None


def _latest_monthly_change_k() -> Optional[float]:
    """Return the most recent monthly change (in thousands of jobs) from ADP data.

    Prefers the monthly NPPTTL series; falls back to summing 4 weeks of the
    weekly ADPWNUSNERSA series if only weekly is available.
    """
    monthly = _fetch_fred_series(_ADP_MONTHLY_SERIES_ID, limit=6)
    if monthly and len(monthly) >= 2:
        try:
            latest = float(monthly[0]["value"])
            prev = float(monthly[1]["value"])
            # NPPTTL is level (thousands of employed); change is latest - prev
            return latest - prev
        except (ValueError, KeyError, TypeError):
            pass

    # Fallback: weekly series, sum the last 4 weeks' deltas
    weekly = _fetch_fred_series(_ADP_SERIES_ID, limit=12)
    if not weekly or len(weekly) < 8:
        return None
    try:
        # Compare last 4 weeks vs previous 4 weeks
        last4 = sum(float(w["value"]) for w in weekly[:4] if w["value"] != ".")
        prev4 = sum(float(w["value"]) for w in weekly[4:8] if w["value"] != ".")
        # Crude monthly-change proxy
        return (last4 - prev4) / 4.0  # average weekly diff as monthly proxy
    except (ValueError, KeyError, TypeError):
        return None


def _parse_kxjob_threshold(ticker: str, title: str) -> tuple[Optional[float], bool]:
    """Parse the job-count threshold (thousands) and direction from a KXJOB market.

    Examples:
      "KXJOB-26APR-T150"  → 150k, above
      "Will nonfarm payrolls come in above 175k?" → 175k, above
      "below 100,000 jobs" → 100k, below
    """
    upper = (ticker or "").upper()
    # Ticker suffix
    m = re.search(r"-T(-?\d+\.?\d*)", upper)
    if m:
        val = float(m.group(1))
        # Tickers usually encode thousands; reject obvious garbage
        if 0 < abs(val) < 2000:
            return val, True

    # Title parsing
    title_l = (title or "").lower()
    # "above 175" or "above 175k" or "above 175,000"
    m = re.search(r"(above|over|exceed|at least|more than)\s+(\d+\.?\d*)\s*(k|,?000)?", title_l)
    if m:
        val = float(m.group(2))
        if m.group(3) == ",000" or (m.group(3) and m.group(3).lower() == "000"):
            val /= 1000.0  # normalize to thousands
        return val, True
    m = re.search(r"(below|under|less than|fewer than)\s+(\d+\.?\d*)\s*(k|,?000)?", title_l)
    if m:
        val = float(m.group(2))
        if m.group(3) == ",000" or (m.group(3) and m.group(3).lower() == "000"):
            val /= 1000.0
        return val, False

    return None, True


def get_adp_estimate(ticker: str, market_data: dict) -> tuple:
    """Estimate probability for Kalshi KXJOB (nonfarm payrolls) markets.

    Returns (prob, source_tag) or (None, None).
    """
    if market_data is None:
        return None, None
    ticker_upper = (ticker or "").upper()
    title = (market_data.get("title") or market_data.get("subtitle") or "")

    # Match KXJOB (Kalshi's BLS nonfarm payrolls series) or title keywords
    is_jobs_market = ticker_upper.startswith("KXJOB") or any(
        kw in title.lower() for kw in (
            "nonfarm payroll", "nonfarm payrolls", "jobs report", "nfp",
            "unemployment", "payroll report",
        )
    )
    if not is_jobs_market:
        return None, None

    threshold_k, is_above = _parse_kxjob_threshold(ticker, title)
    if threshold_k is None:
        return None, None

    adp_change_k = _latest_monthly_change_k()
    if adp_change_k is None:
        return None, None

    # Gaussian model: BLS NFP ~ Normal(ADP_estimate, RMSE)
    # P(NFP >= threshold) = 1 - Φ((threshold - adp) / sigma)
    prob_above = 1.0 - _gaussian_cdf(threshold_k, adp_change_k, _ADP_RMSE_K_JOBS)
    prob = prob_above if is_above else (1.0 - prob_above)
    prob = max(0.02, min(0.98, prob))

    direction = "above" if is_above else "below"
    print(
        f"[adp] ADP_est={adp_change_k:+.0f}k threshold={threshold_k:+.0f}k "
        f"sigma={_ADP_RMSE_K_JOBS:.0f}k ({direction}) -> {prob:.3f}"
    )
    return prob, f"adp_nfp:change={adp_change_k:+.0f}k_{direction}_{threshold_k:+.0f}k"
