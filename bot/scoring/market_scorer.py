"""Multi-strategy market scoring engine.

Extracted from trade.py:
  - _days_to_expiry() (~line 910)
  - _is_near_data_release() (~line 4609)
  - DATA_RELEASE_CALENDAR dict (~line 4586)
  - BLS_SERIES dict (~line 4662)
  - _fetch_bls_latest() (~line 4636)
  - _fetch_manifold_markets() (~line 4755)
  - _best_manifold_match() (~line 4777)
  - score_event_driven() (~lines 4672-4746)
  - score_cross_market() (~lines 4800-4895)
  - score_near_resolution() (~lines 4905-4988)
  - STRATEGY_REGISTRY (~line 4997)
  - score_market() (~lines 5008-5171)
"""

from __future__ import annotations

import math
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import requests

from bot.config import (
    MIN_EDGE,
    SINGLE_SOURCE_EDGE,
    SOURCE_WEIGHTS,
)
from bot.scoring.filters import categorize_market
from bot.signals.ensemble import get_independent_estimate

# These source functions are used by score_near_resolution and score_event_driven.
# They are already extracted into bot.signals.sources submodules.
from bot.signals.sources.weather import (
    get_weather_estimate,
    get_noaa_alerts_for_market,
)
from bot.signals.sources.sports import get_sports_estimate
from bot.signals.sources.economics import get_cleveland_fed_nowcast
from bot.signals.sources.crypto import get_crypto_estimate
from bot.signals.sources.prediction_markets import (
    get_polymarket_estimate,
    get_metaculus_estimate,
)

# ---------------------------------------------------------------------------
# Fee calculations — use exact Kalshi formulas instead of flat estimates
# ---------------------------------------------------------------------------
from bot.core.money import fee_per_contract_cents as _fee_per_contract_cents
ESTIMATED_EXIT_SPREAD = float(os.environ.get("ESTIMATED_EXIT_SPREAD", "0.03"))

def _round_trip_fee_dollars(price_dollars: float) -> float:
    """Round-trip fee per contract in dollars (maker entry + taker exit)."""
    pc = max(1, min(99, round(price_dollars * 100)))
    entry = _fee_per_contract_cents(pc, maker=True)
    exit_ = _fee_per_contract_cents(pc, maker=False)
    return (entry + exit_) / 100

# ---------------------------------------------------------------------------
# Rate-limit helper -- still in trade.py.  Imported lazily to avoid circular
# imports during the incremental extraction.
# TODO(refactor): Move _rate_limit_wait into bot/api.py
# ---------------------------------------------------------------------------
try:
    from bot.api import _rate_limit_wait
except ImportError:
    def _rate_limit_wait(url: str) -> None:  # noqa: D401 – stub
        """No-op stub when bot.api._rate_limit_wait is unavailable."""
        pass

# Module-level in-process cache (same pattern as trade.py _CACHE).
_CACHE: dict[str, tuple] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _days_to_expiry(market_data: dict) -> Optional[float]:
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
# Event-driven helpers
# ══════════════════════════════════════════════════════════════════════════════

DATA_RELEASE_CALENDAR = {
    # Employment
    "nonfarm":       ("bls", 8, 30, "BLS Employment Situation \u2014 first Friday of month"),
    "payroll":       ("bls", 8, 30, "BLS Employment Situation \u2014 first Friday of month"),
    "unemployment":  ("bls", 8, 30, "BLS Employment Situation \u2014 first Friday of month"),
    "jobless":       ("bls", 8, 30, "BLS Initial Jobless Claims \u2014 Thursdays"),
    "initial claims":("bls", 8, 30, "BLS Initial Jobless Claims \u2014 Thursdays"),
    # Inflation
    "cpi":           ("bls", 8, 30, "BLS Consumer Price Index \u2014 ~12th of month"),
    "ppi":           ("bls", 8, 30, "BLS Producer Price Index \u2014 ~15th of month"),
    "pce":           ("bea", 8, 30, "BEA Personal Consumption Expenditures"),
    # GDP
    "gdp":           ("bea", 8, 30, "BEA GDP \u2014 quarterly, advance/second/third"),
    # Fed
    "fed funds":     ("fomc", 14, 0, "FOMC Rate Decision \u2014 8 times/year at 2pm ET"),
    "fomc":          ("fomc", 14, 0, "FOMC Rate Decision"),
    "interest rate": ("fomc", 14, 0, "FOMC Rate Decision"),
    # Retail / Housing
    "retail sales":  ("census", 8, 30, "Census Bureau Retail Sales"),
    "housing starts":("census", 8, 30, "Census Bureau Housing Starts"),
    "home sales":    ("census", 10, 0, "NAR Existing Home Sales / Census New Home Sales"),
}

BLS_SERIES = {
    "unemployment": "LNS14000000",       # Unemployment rate (seasonally adjusted)
    "nonfarm":      "CES0000000001",     # Total nonfarm payrolls
    "payroll":      "CES0000000001",
    "cpi":          "CUSR0000SA0",       # CPI-U all items (seasonally adjusted)
    "jobless":      "LNS13000000",       # Unemployment level (for claims proxy)
    "initial claims":"LNS13000000",
    "ppi":          "WPUFD49104",        # PPI final demand
}


def _is_near_data_release(market_data: dict):
    """Check if this market is tied to a data release happening within 4 hours.
    Returns (release_key, release_info) or (None, None)."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker = (market_data.get("ticker") or "").lower()
    text = ticker + " " + title

    for keyword, info in DATA_RELEASE_CALENDAR.items():
        if keyword in text:
            source, hour_et, minute_et, desc = info
            # Check if resolution is within 48 hours (these are near-term event markets)
            days = _days_to_expiry(market_data)
            if days is not None and days <= 2.0:
                return keyword, info
            # Also match if there's a release today
            try:
                from zoneinfo import ZoneInfo
                et = datetime.now(ZoneInfo("America/New_York"))
                release_time = et.replace(hour=hour_et, minute=minute_et, second=0)
                hours_until = (release_time - et).total_seconds() / 3600
                # Within 4 hours before or 1 hour after release
                if -1.0 <= hours_until <= 4.0:
                    return keyword, info
            except Exception:
                pass
    return None, None


def _fetch_bls_latest(series_id: str) -> Optional[float]:
    """Fetch latest value from BLS API (free, no key needed for low volume).
    BLS updates data at 8:30 AM ET on release days."""
    cache_key = f"bls_{series_id}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 300:  # 5 min cache
        return _CACHE[cache_key][0]
    try:
        year = datetime.now(timezone.utc).year
        url = (f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id}"
               f"?startyear={year}&endyear={year}&latest=true")
        _rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") == "REQUEST_SUCCEEDED":
            series = data.get("Results", {}).get("series", [])
            if series and series[0].get("data"):
                val = float(series[0]["data"][0]["value"])
                _CACHE[cache_key] = (val, now)
                print(f"[bls] {series_id} latest = {val}")
                return val
    except Exception as e:
        print(f"[bls] Failed to fetch {series_id}: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Cross-market helpers (Manifold Markets)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_manifold_markets(query: str, limit: int = 5):
    """Search Manifold Markets API for matching markets. Free, no auth needed."""
    cache_key = f"manifold_{query[:40]}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 600:  # 10 min cache
        return _CACHE[cache_key][0]
    try:
        url = f"https://api.manifold.markets/v0/search-markets?term={urllib.parse.quote(query)}&limit={limit}"
        _rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            markets = r.json()
            # Filter to binary markets only
            binary = [m for m in markets if m.get("outcomeType") == "BINARY"
                      and m.get("isResolved") is not True
                      and m.get("closeTime", 0) > time.time() * 1000]
            _CACHE[cache_key] = (binary, now)
            return binary
    except Exception as e:
        print(f"[manifold] Search failed for '{query[:30]}': {e}")
    return []


def _best_manifold_match(title: str, manifold_markets: list) -> Optional[dict]:
    """Find the best matching Manifold market by keyword overlap."""
    if not manifold_markets:
        return None
    title_words = set(title.lower().split())
    # Remove common stop words
    stop = {"the", "a", "an", "will", "be", "is", "to", "in", "on", "of", "by", "at", "for"}
    title_words -= stop

    best_match = None
    best_score = 0
    for mm in manifold_markets:
        q = (mm.get("question") or "").lower()
        q_words = set(q.split()) - stop
        if not q_words:
            continue
        overlap = len(title_words & q_words)
        jaccard = overlap / max(1, len(title_words | q_words))
        if jaccard > best_score and jaccard > 0.25:  # minimum 25% word overlap
            best_score = jaccard
            best_match = mm
    return best_match


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 2: EVENT-DRIVEN DATA RELEASE
# ══════════════════════════════════════════════════════════════════════════════

def score_event_driven(m: dict, disabled_sources=None):
    """Strategy 2: Event-driven data release trading.
    Looks for markets tied to government data releases happening soon.
    Fetches the actual released data and compares to market price.
    Returns (score, side, detail, independent_prob, market_prob, edge) or None."""

    release_key, release_info = _is_near_data_release(m)
    if not release_key:
        return None

    source, hour_et, minute_et, desc = release_info
    ticker = m.get("ticker", "")
    title = (m.get("title") or m.get("subtitle") or "").lower()

    _n = lambda v, d=99: (float(v or d) / 100 if float(v or d) > 1 else float(v or d))
    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"), 0)

    # Try to get actual data from BLS
    bls_series = BLS_SERIES.get(release_key)
    actual_value = None
    if bls_series and source == "bls":
        actual_value = _fetch_bls_latest(bls_series)

    # Also try FRED as backup (Cleveland Fed for CPI)
    if actual_value is None and release_key in ("cpi", "pce"):
        # Use existing FRED/Cleveland Fed infrastructure
        if "clevfed" not in (disabled_sources or set()):
            clevfed_prob, clevfed_src = get_cleveland_fed_nowcast(ticker, m)
            if clevfed_prob is not None:
                # Cleveland Fed gives us a probability directly
                edge = clevfed_prob - yes_ask
                if abs(edge) > MIN_EDGE:
                    side = "yes" if edge > 0 else "no"
                    mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
                    indep_prob = clevfed_prob if side == "yes" else (1 - clevfed_prob)
                    fee_adj = abs(edge) - ESTIMATED_EXIT_SPREAD - _round_trip_fee_dollars(yes_ask)
                    if fee_adj > MIN_EDGE:
                        score = fee_adj * 15 + 2.0  # bonus for event-driven
                        detail = (f"event_driven: {desc} | clevfed nowcast={clevfed_prob:.2f} "
                                  f"mkt={yes_ask:.2f} edge={edge:+.2f} fee_adj={fee_adj:.2f}")
                        return (score, side, detail, indep_prob, mkt_prob, fee_adj)

    # If we got actual BLS data, try to interpret it against the market question
    if actual_value is not None:
        # Parse threshold from title (e.g., "unemployment rate above 4.0%")
        threshold_match = re.search(r'(above|below|over|under|exceed|at least)\s*(\d+\.?\d*)', title)
        if threshold_match:
            direction = threshold_match.group(1)
            threshold = float(threshold_match.group(2))

            # Determine probability using sigmoid (smooth transition, no cliff)
            # sigma = 0.5% of threshold gives a tight but continuous curve
            sigma = max(threshold * 0.005, 0.01)  # floor at 0.01 to avoid division issues
            diff = actual_value - threshold
            if direction in ("above", "over", "exceed", "at least"):
                # Market asks: "Will X be above threshold?"
                indep_prob = 1.0 / (1.0 + math.exp(-diff / sigma))
            else:  # below, under
                indep_prob = 1.0 / (1.0 + math.exp(diff / sigma))

            edge = indep_prob - yes_ask
            fee_adj = abs(edge) - ESTIMATED_EXIT_SPREAD - _round_trip_fee_dollars(yes_ask)

            if fee_adj > MIN_EDGE * 0.8:  # slightly lower threshold for event-driven (data is strong)
                side = "yes" if edge > 0 else "no"
                mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
                final_prob = indep_prob if side == "yes" else (1 - indep_prob)
                score = fee_adj * 20 + 3.0  # strong bonus for actual data
                detail = (f"event_driven: {desc} | BLS {release_key}={actual_value} "
                          f"vs threshold={threshold} \u2192 prob={indep_prob:.2f} "
                          f"mkt={yes_ask:.2f} edge={edge:+.2f}")
                return (score, side, detail, final_prob, mkt_prob, fee_adj)

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 3: CROSS-MARKET ARBITRAGE
# ══════════════════════════════════════════════════════════════════════════════
# When multiple prediction markets (Polymarket, Manifold, Metaculus) agree on
# a probability and Kalshi diverges, the consensus is usually right.

def score_cross_market(m: dict, adaptive_weights=None, calibration_corrections=None,
                       disabled_sources=None):
    """Strategy 3: Cross-market arbitrage.
    Aggregates probability estimates from multiple prediction markets.
    If 2+ external markets agree and Kalshi diverges by >10%, that's a strong signal.
    Returns (score, side, detail, independent_prob, market_prob, edge) or None."""

    title = m.get("title") or m.get("subtitle") or ""
    ticker = m.get("ticker", "")
    if not title:
        return None

    _n = lambda v, d=99: (float(v or d) / 100 if float(v or d) > 1 else float(v or d))
    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"), 0)
    volume = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)

    _disabled = disabled_sources or set()
    external_probs = []  # list of (prob, source_name, weight)

    # 1. Polymarket (already have this infrastructure)
    if "polymarket" not in _disabled:
        try:
            poly_prob, poly_src = get_polymarket_estimate(ticker, m)
            if poly_prob is not None:
                external_probs.append((poly_prob, "polymarket", 0.80))
        except Exception:
            pass

    # 2. Manifold Markets (new)
    if "manifold" not in _disabled:
        try:
            manifold_results = _fetch_manifold_markets(title[:80])
            match = _best_manifold_match(title, manifold_results)
            if match:
                mf_prob = match.get("probability")
                mf_volume = match.get("volume", 0)
                mf_question = (match.get("question") or "")[:50]
                if mf_prob is not None and mf_volume >= 100:  # minimum volume filter
                    external_probs.append((float(mf_prob), f"manifold:{mf_question}", 0.65))
                    print(f"[manifold] Match: '{title[:40]}' \u2194 '{mf_question}' "
                          f"\u2192 prob={mf_prob:.2f} vol={mf_volume:.0f}")
        except Exception as e:
            print(f"[manifold] Error: {e}")

    # 3. Metaculus (already have this infrastructure)
    if "metaculus" not in _disabled:
        try:
            meta_prob, meta_src = get_metaculus_estimate(ticker, m)
            if meta_prob is not None:
                external_probs.append((meta_prob, f"metaculus:{meta_src}", 0.70))
        except Exception:
            pass

    # Need at least 2 external markets to form a consensus
    if len(external_probs) < 2:
        return None

    # Compute weighted consensus probability
    total_weight = sum(w for _, _, w in external_probs)
    consensus_prob = sum(p * w for p, _, w in external_probs) / total_weight

    # Check agreement: all sources must be within 15% of each other
    probs_only = [p for p, _, _ in external_probs]
    spread = max(probs_only) - min(probs_only)
    if spread > 0.15:
        # Sources disagree too much -- no consensus
        return None

    # Compare consensus to Kalshi
    edge = consensus_prob - yes_ask
    round_trip_cost = ESTIMATED_EXIT_SPREAD + _round_trip_fee_dollars(yes_ask)
    fee_adj = abs(edge) - round_trip_cost

    # Need strong divergence for cross-market arb (these are efficient markets)
    required = MIN_EDGE * 1.2  # slightly higher bar since all sources are public
    if fee_adj < required:
        return None

    side = "yes" if edge > 0 else "no"
    mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
    indep_prob = consensus_prob if side == "yes" else (1 - consensus_prob)

    sources_str = " + ".join(src for _, src, _ in external_probs)
    n_sources = len(external_probs)
    score = fee_adj * 12 + n_sources * 0.5 + 1.0  # bonus for consensus
    days = _days_to_expiry(m)
    if days and days > 0:
        time_mult = min(2.0, 1.0 / math.sqrt(max(days, 0.25)))
        score *= time_mult

    detail = (f"cross_market: {n_sources} markets agree | {sources_str} "
              f"consensus={consensus_prob:.2f} kalshi={yes_ask:.2f} "
              f"divergence={abs(edge):.2f} fee_adj={fee_adj:.2f} "
              f"source_spread={spread:.2f}")
    return (score, side, detail, indep_prob, mkt_prob, fee_adj)


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 4: NEAR-RESOLUTION CONVERGENCE
# ══════════════════════════════════════════════════════════════════════════════
# Markets resolving within 24 hours where we have strong, fresh data and the
# market price is stale. The closer to resolution, the more our data is worth
# and the less time for adverse price movement.

def score_near_resolution(m: dict, adaptive_weights=None, calibration_corrections=None,
                          disabled_sources=None):
    """Strategy 4: Near-resolution convergence trading.
    Targets markets resolving in <24h where our ensemble has strong, fresh data.
    Uses a tighter time window and higher confidence threshold.
    Returns (score, side, detail, independent_prob, market_prob, edge) or None."""

    days = _days_to_expiry(m)
    if days is None or days > 1.0:
        return None  # only care about <24h markets

    ticker = m.get("ticker", "")
    title = (m.get("title") or m.get("subtitle") or "")

    _n = lambda v, d=99: (float(v or d) / 100 if float(v or d) > 1 else float(v or d))
    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"), 0)
    volume = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)

    # Get ensemble estimate
    weights = adaptive_weights or SOURCE_WEIGHTS
    _disabled = disabled_sources or set()
    estimates = []

    # Only use high-confidence sources for near-resolution
    # These are sources that have domain-specific, current data
    high_confidence_sources = [
        ("weather", get_weather_estimate),
        ("noaa", get_noaa_alerts_for_market),
        ("odds", get_sports_estimate),
        ("clevfed", get_cleveland_fed_nowcast),
        ("crypto", get_crypto_estimate),
    ]

    for src_name, func in high_confidence_sources:
        if src_name in _disabled:
            continue
        try:
            prob, src = func(ticker, m)
            if prob is not None:
                w = weights.get(src_name, 0.5)
                estimates.append((prob, w, f"{src_name}:{src}"))
        except Exception:
            pass

    if not estimates:
        return None

    # Need higher confidence for near-resolution: either 2+ sources or 1 very strong one
    total_weight = sum(w for _, w, _ in estimates)
    if len(estimates) == 1 and total_weight < 0.75:
        return None

    ensemble_prob = sum(p * w for p, w, _ in estimates) / total_weight

    # Edge calculation
    edge = ensemble_prob - yes_ask
    round_trip_cost = ESTIMATED_EXIT_SPREAD + _round_trip_fee_dollars(yes_ask)
    fee_adj = abs(edge) - round_trip_cost

    # Lower edge threshold for near-resolution: data is fresh, resolution is imminent
    # Risk of adverse move is minimal since market closes soon
    required = MIN_EDGE * 0.7  # 30% lower threshold
    if fee_adj < required:
        return None

    side = "yes" if edge > 0 else "no"
    mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
    indep_prob = ensemble_prob if side == "yes" else (1 - ensemble_prob)

    hours_left = days * 24
    sources_str = " + ".join(src for _, _, src in estimates)
    score = fee_adj * 25 + 4.0  # big bonus for near-resolution certainty
    # Closer to resolution = higher score
    if hours_left < 6:
        score *= 1.5
    if hours_left < 2:
        score *= 2.0

    detail = (f"near_resolution: {hours_left:.1f}h left | {sources_str} "
              f"ensemble={ensemble_prob:.2f} mkt={yes_ask:.2f} "
              f"edge={abs(edge):.2f} fee_adj={fee_adj:.2f} "
              f"n_sources={len(estimates)}")
    return (score, side, detail, indep_prob, mkt_prob, fee_adj)


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-STRATEGY SCORING -- runs all strategies, picks the best signal
# ══════════════════════════════════════════════════════════════════════════════
# Each strategy returns (score, side, detail, independent_prob, market_prob, edge)
# or None if it doesn't apply. The highest-scoring strategy wins.

STRATEGY_REGISTRY = [
    # (name, function, description)
    ("info_edge",        None,                   "Ensemble mispricing -- 12-source weighted estimate vs market price"),
    ("event_driven",     score_event_driven,      "Event-driven -- government data release timing edge"),
    ("cross_market",     score_cross_market,      "Cross-market arb -- consensus from Polymarket + Manifold + Metaculus"),
    ("near_resolution",  score_near_resolution,   "Near-resolution -- <24h markets with fresh domain data"),
]


# ══════════════════════════════════════════════════════════════════════════════
# MARKET SCORING -- multi-strategy (v3.8)
# ══════════════════════════════════════════════════════════════════════════════

def score_market(m: dict, adaptive_weights=None, calibration_corrections=None,
                 category_edges=None, disabled_sources=None, disabled_strategies=None,
                 strategy_bandit=None):
    """
    Returns (score, side, strategy, detail, volume, spread_cents,
             independent_prob, market_prob, edge)
    score=0 -> no trade.
    """
    def _n(v, d=99):
        v = float(v or d); return v/100 if v > 1.0 else v

    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"),  0)
    no_ask  = _n(m.get("no_ask")  or m.get("no_ask_dollars"),  99)
    no_bid  = _n(m.get("no_bid")  or m.get("no_bid_dollars"),   0)
    volume  = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)
    spread  = yes_ask - yes_bid
    sc      = round(spread * 100, 1)
    ticker  = m.get("ticker", "")

    EMPTY = (0.0, "", "", "", 0, 0, None, None, 0)

    # Skip very illiquid or very cheap markets
    if yes_ask <= 0.08 or yes_ask >= 0.92:
        return EMPTY
    if volume < 50:
        return EMPTY
    if spread <= 0:
        return EMPTY

    # -- Get ensemble probability estimate (with adaptive weights + calibration) --
    indep_prob, info_source, n_sources = get_independent_estimate(
        ticker, m, yes_ask, volume,
        adaptive_weights=adaptive_weights,
        calibration_corrections=calibration_corrections,
        disabled_sources=disabled_sources
    )

    # -- Information-edge trading only (ensemble-backed) -------------------------
    # Market making and liquidity harvest removed -- they had no real information edge.
    # Only trade when we have independent data that diverges from market price.
    if indep_prob is None or n_sources == 0:
        return EMPTY

    # Adaptive edge threshold: more sources -> more confidence -> lower threshold
    if n_sources >= 3:
        required_edge = MIN_EDGE                    # 5% with 3+ sources
    elif n_sources == 2:
        required_edge = MIN_EDGE + 0.02             # 7% with 2 sources
    else:
        required_edge = SINGLE_SOURCE_EDGE          # 10% with only 1 source

    # Apply learned category-specific edge multiplier
    if category_edges:
        title = m.get("title", "") or m.get("subtitle", "") or ""
        cat = categorize_market(ticker, title)
        cat_mult = category_edges.get(cat, 1.0)
        if cat_mult != 1.0:
            required_edge *= cat_mult

    market_prob_yes = yes_ask
    edge_yes = indep_prob - market_prob_yes
    edge_no  = (1 - indep_prob) - (1 - yes_bid) if yes_bid > 0 else 0

    # -- Fee accounting: subtract estimated round-trip costs from edge -----------
    # Real profitability = edge - entry_spread - exit_spread - fees
    # Entry cost is already baked into the ask price. Exit slippage + fees must be subtracted.
    round_trip_cost = ESTIMATED_EXIT_SPREAD + _round_trip_fee_dollars(yes_ask)  # entry + exit fees
    fee_adjusted_edge_yes = edge_yes - round_trip_cost
    fee_adjusted_edge_no  = edge_no - round_trip_cost

    # -- Time-priority scoring: shorter-dated markets = better capital efficiency --
    # Edge per day of capital committed. Markets resolving in 1 day get full weight;
    # 30-day markets get ~20% of base score. Prevents capital lock-up in slow markets.
    days = _days_to_expiry(m)
    if days is not None and days > 0:
        time_multiplier = min(2.0, 1.0 / math.sqrt(max(days, 0.25)))  # 1d->1.0, 4d->0.5, 30d->0.18
    else:
        time_multiplier = 0.5  # unknown expiry -> conservative

    # -- Candidate collection: run ALL strategies and pick the best -------------
    # Each candidate: (score, side, strategy_name, detail, indep_prob, mkt_prob, edge)
    candidates = []
    _disabled_strats = disabled_strategies or set()

    # Strategy 1: Original info_edge (ensemble mispricing)
    if fee_adjusted_edge_yes > required_edge and spread < 0.08:
        base_score = fee_adjusted_edge_yes * 10 + volume / 10000 + n_sources * 0.1
        s1_score = base_score * time_multiplier
        detail = (f"info_edge: {info_source} indep={indep_prob:.2f} "
                  f"mkt={market_prob_yes:.2f} raw_edge={edge_yes:.2f} "
                  f"fee_adj={fee_adjusted_edge_yes:.2f} sources={n_sources} "
                  f"days={f'{days:.1f}' if days else '?'} time_mult={time_multiplier:.2f}")
        candidates.append((s1_score, "yes", "info_edge", detail, indep_prob, market_prob_yes, fee_adjusted_edge_yes))

    if fee_adjusted_edge_no > required_edge and spread < 0.08:
        base_score = fee_adjusted_edge_no * 10 + volume / 10000 + n_sources * 0.1
        s1_score = base_score * time_multiplier
        market_prob_no = 1 - yes_bid
        detail = (f"info_edge: {info_source} indep_no={1-indep_prob:.2f} "
                  f"mkt_no={market_prob_no:.2f} raw_edge={edge_no:.2f} "
                  f"fee_adj={fee_adjusted_edge_no:.2f} sources={n_sources} "
                  f"days={f'{days:.1f}' if days else '?'} time_mult={time_multiplier:.2f}")
        candidates.append((s1_score, "no", "info_edge", detail, 1-indep_prob, market_prob_no, fee_adjusted_edge_no))

    # Strategy 2: Event-driven data release
    if "event_driven" not in _disabled_strats:
        try:
            evt = score_event_driven(m, disabled_sources=disabled_sources)
            if evt:
                s, side, detail, ip, mp, edge = evt
                candidates.append((s, side, "event_driven", detail, ip, mp, edge))
        except Exception as e:
            print(f"[strategy] event_driven error: {e}")

    # Strategy 3: Cross-market arbitrage
    if "cross_market" not in _disabled_strats:
        try:
            xmkt = score_cross_market(m, adaptive_weights=adaptive_weights,
                                       calibration_corrections=calibration_corrections,
                                       disabled_sources=disabled_sources)
            if xmkt:
                s, side, detail, ip, mp, edge = xmkt
                candidates.append((s, side, "cross_market", detail, ip, mp, edge))
        except Exception as e:
            print(f"[strategy] cross_market error: {e}")

    # Strategy 4: Near-resolution convergence
    if "near_resolution" not in _disabled_strats:
        try:
            nr = score_near_resolution(m, adaptive_weights=adaptive_weights,
                                        calibration_corrections=calibration_corrections,
                                        disabled_sources=disabled_sources)
            if nr:
                s, side, detail, ip, mp, edge = nr
                candidates.append((s, side, "near_resolution", detail, ip, mp, edge))
        except Exception as e:
            print(f"[strategy] near_resolution error: {e}")

    if not candidates:
        return EMPTY

    # Weight each candidate's score by its Thompson Sampling posterior draw.
    # This means proven strategies get full credit while unproven ones are
    # discounted (but not zeroed -- always a chance to be picked).
    _bandit = strategy_bandit or {}
    def _bandit_adjusted_score(candidate):
        raw_score, _, strat_name, _, _, _, _ = candidate
        if strat_name in _bandit:
            # Thompson sample in [0,1] -- multiply by score
            # Minimum 0.1 floor so no strategy is completely silenced
            ts = max(0.1, _bandit[strat_name].get("sample", 0.5))
            return raw_score * ts
        return raw_score * 0.5  # unknown strategy -> conservative

    best = max(candidates, key=_bandit_adjusted_score)
    best_score, best_side, best_strat, best_detail, best_ip, best_mp, best_edge = best

    # If multiple strategies found a signal, note that in the detail
    if len(candidates) > 1:
        strat_names = [c[2] for c in candidates]
        best_detail += f" [also: {', '.join(s for s in strat_names if s != best_strat)}]"

    return best_score, best_side, best_strat, best_detail, volume, sc, best_ip, best_mp, best_edge


# ══════════════════════════════════════════════════════════════════════════════
# Post-scoring filter gate
# ══════════════════════════════════════════════════════════════════════════════

def passes_filters(ticker: str, strategy: str, volume: float, spread_cents: float,
                   af: dict) -> tuple[bool, str]:
    """Check whether a scored market passes the learned avoidance filters.

    *af* is the dict returned by :func:`bot.scoring.filters.compute_avoid_filters`.
    Returns ``(True, "")`` when the market is acceptable, or
    ``(False, reason)`` when it should be skipped.
    """
    vt = af.get("low_volume_threshold")
    if vt and volume < vt:
        return False, f"learned: vol {volume:.0f}<{vt}"
    if strategy in af.get("avoided_strategies", set()):
        return False, f"learned: strat '{strategy}'"
    if ticker[:6] in af.get("avoided_prefixes", set()):
        return False, f"learned: prefix '{ticker[:6]}'"
    return True, ""
