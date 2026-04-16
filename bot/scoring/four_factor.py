"""Four-factor scoring gate for trade candidates.

Every trade candidate must score above thresholds on all four factors:
  1. Edge        (min 0.10) -- fee-adjusted edge vs market price
  2. Liquidity   (min 0.30) -- volume, spread, open interest
  3. Confidence  (min 0.40) -- source count, agreement, category track record
  4. Timing      (min 0.20) -- time to expiry, hour quality, regime match

The gate is binary: ALL four must pass. The composite (product of all four)
provides a relative ranking among passing candidates.

Defensive design: missing data always maps to *lower* scores, never higher.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bot.config import (
    CORRELATED_GROUPS,
    MIN_EDGE,
    SINGLE_SOURCE_EDGE,
    SOURCE_WEIGHTS,
)
from bot.core.money import estimate_round_trip_cost
from bot.types import FourFactorScore, Regime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp *value* into [lo, hi]."""
    return max(lo, min(hi, value))


def _safe_float(value, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _days_to_expiry(market_data: dict) -> Optional[float]:
    """Days until market closes.  Returns None if unknown."""
    close_str = (
        market_data.get("close_time")
        or market_data.get("expiration_time")
        or market_data.get("expected_expiration_time")
        or ""
    )
    if not close_str:
        return None
    try:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        delta = (close_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(0.01, delta)  # floor at ~15 min
    except Exception:
        return None


def _parse_source_estimates(source_desc: str) -> List[float]:
    """Extract individual probability estimates from a source_desc string.

    source_desc typically looks like:
      "weather=0.62(w=0.80) tomorrow=0.58(w=0.82) noaa=0.55(w=0.75)"
    We extract the probability values (0.62, 0.58, 0.55).
    """
    # Match patterns like "source_name=0.XX" where 0.XX is a probability
    matches = re.findall(r"=(\d+\.\d+)\(", source_desc)
    probs = []
    for m in matches:
        try:
            v = float(m)
            if 0.0 <= v <= 1.0:
                probs.append(v)
        except (TypeError, ValueError):
            continue
    return probs


def _std_dev(values: List[float]) -> float:
    """Standard deviation (population) for a list of floats."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5


# ---------------------------------------------------------------------------
# Factor 1: Edge
# ---------------------------------------------------------------------------

def _score_edge(
    ensemble_prob: float,
    market_prob: float,
    active_feedback: dict,
    market_data: dict,
) -> float:
    """Score the fee-adjusted edge on a 0-1 scale.

    15% fee-adjusted edge = 1.0, 7.5% = 0.5, 0% or negative = 0.0.
    """
    raw_edge = abs(ensemble_prob - market_prob)

    # Estimate round-trip cost using the money module.
    # Use 10 contracts as a representative size for the per-contract cost.
    price_cents = max(1, min(99, round(market_prob * 100)))
    try:
        rt = estimate_round_trip_cost(
            contracts=10,
            entry_price_cents=price_cents,
            exit_spread_cents=3,
            entry_maker=True,
            exit_maker=False,
        )
        cost_per_contract_frac = rt.per_contract_cents / 100.0  # convert cents to fraction
    except Exception:
        # Fallback: approximate 6% round-trip cost (3% fee + 3% spread)
        cost_per_contract_frac = 0.06

    fee_adjusted_edge = max(0.0, raw_edge - cost_per_contract_frac)

    # Apply category-specific edge threshold from active_feedback if available.
    # active_feedback may contain "edge_threshold_override" per category.
    category_threshold = None
    if active_feedback:
        cat_overrides = active_feedback.get("edge_thresholds", {})
        category = market_data.get("category", "")
        if category and category in cat_overrides:
            category_threshold = _safe_float(cat_overrides[category])

    if category_threshold is not None and category_threshold > 0:
        # If fee_adjusted_edge doesn't meet the category threshold, penalize.
        if fee_adjusted_edge < category_threshold:
            fee_adjusted_edge *= 0.5  # halve score when below category bar

    # Normalize: 15% edge = 1.0
    score = fee_adjusted_edge / 0.15
    return _clamp(score)


# ---------------------------------------------------------------------------
# Factor 2: Liquidity
# ---------------------------------------------------------------------------

def _score_liquidity(market_data: dict) -> float:
    """Score market liquidity on a 0-1 scale.

    Components (weighted):
      0.50 * volume_score   -- 200+ 24h volume = full credit
      0.30 * spread_score   -- 10c+ spread = 0
      0.20 * oi_score       -- 100+ open interest = full credit
    """
    # Volume: try multiple field names (API inconsistency)
    volume = _safe_float(
        market_data.get("volume")
        or market_data.get("volume_24h")
        or market_data.get("volume_24h_fp")
        or market_data.get("volume_fp"),
        default=0.0,
    )
    volume_score = _clamp(volume / 200.0)

    # Spread: yes_ask - yes_bid in cents
    yes_ask = _safe_float(market_data.get("yes_ask") or market_data.get("yes_ask_cents"), 99)
    yes_bid = _safe_float(market_data.get("yes_bid") or market_data.get("yes_bid_cents"), 0)

    # Normalise to cents if values look like dollar fractions (< 1.0)
    if yes_ask <= 1.0:
        yes_ask *= 100
    if yes_bid <= 1.0:
        yes_bid *= 100

    spread_cents = max(0.0, yes_ask - yes_bid)
    spread_score = _clamp(1.0 - spread_cents / 10.0)

    # Open interest
    open_interest = _safe_float(
        market_data.get("open_interest")
        or market_data.get("open_interest_fp"),
        default=0.0,
    )
    oi_score = _clamp(open_interest / 100.0)

    return 0.5 * volume_score + 0.3 * spread_score + 0.2 * oi_score


# ---------------------------------------------------------------------------
# Factor 3: Confidence
# ---------------------------------------------------------------------------

def _score_confidence(
    n_sources: int,
    source_desc: str,
    category_scores: dict,
    market_data: dict,
) -> float:
    """Score confidence on a 0-1 scale.

    Components (weighted):
      0.60 * base (source count / 3, capped at 1)
      0.20 * agreement (low std dev among source estimates)
      0.20 * category track record
    """
    # Base: more independent sources = more confidence (3+ = full credit)
    base = _clamp(n_sources / 3.0)

    # Agreement: parse source estimates from source_desc
    estimates = _parse_source_estimates(source_desc)
    if len(estimates) >= 2:
        sd = _std_dev(estimates)
        # Lower std dev = better agreement.  0.0 = perfect (1.0), 0.15+ = poor (0.0)
        agreement_score = _clamp(1.0 - sd / 0.15)
    elif len(estimates) == 1:
        # Single source: moderate agreement (we trust the one source somewhat)
        agreement_score = 0.5
    else:
        # No parseable estimates: conservative default
        agreement_score = 0.3

    # Category track record from category_scores
    # category_scores is a dict like {"weather": {"win_rate": 0.6, "n": 20}, ...}
    category = market_data.get("category", "")
    cat_score = 0.5  # neutral default
    if category and category_scores:
        cat_info = category_scores.get(category, {})
        if isinstance(cat_info, dict):
            win_rate = _safe_float(cat_info.get("win_rate"), 0.5)
            sample = _safe_float(cat_info.get("n"), 0)
            if sample >= 5:
                # Scale: 40% win rate = 0.0, 60%+ = 1.0
                cat_score = _clamp((win_rate - 0.4) / 0.2)
            # else: not enough samples, keep neutral
        elif isinstance(cat_info, (int, float)):
            # Some callers pass just a win_rate float
            cat_score = _clamp((_safe_float(cat_info, 0.5) - 0.4) / 0.2)

    return 0.6 * base + 0.2 * agreement_score + 0.2 * cat_score


# ---------------------------------------------------------------------------
# Factor 4: Timing + Regime
# ---------------------------------------------------------------------------

def _score_timing(
    market_data: dict,
    regime: Regime,
    active_feedback: dict,
) -> float:
    """Score timing and regime alignment on a 0-1 scale.

    Components (weighted):
      0.50 * time_score   -- based on time to expiry
      0.30 * hour_score   -- from active_feedback hour quality
      0.20 * regime_score -- regime match to directional strategy
    """
    # Time to expiry score
    days = _days_to_expiry(market_data)
    if days is None:
        time_score = 0.3  # unknown expiry -> conservative
    elif days < (1.0 / 24.0):
        # < 1 hour: near-resolution edge is high
        time_score = 0.8 + _clamp(1.0 - days * 24.0) * 0.2  # 0.8 - 1.0
    elif days < 1.0:
        # 1-24 hours
        time_score = 0.6
    elif days < 7.0:
        # 1-7 days
        time_score = 0.5
    elif days < 30.0:
        # 7-30 days
        time_score = 0.4
    else:
        # 30+ days: long-dated markets have more uncertainty
        time_score = 0.3

    # Hour quality from active_feedback
    # active_feedback may contain "hour_quality" as a 0-1 score for current hour
    hour_score = 0.5  # neutral default
    if active_feedback:
        hq = active_feedback.get("hour_quality")
        if hq is not None:
            hour_score = _clamp(_safe_float(hq, 0.5))
        else:
            # Check for hour-specific scoring
            try:
                current_hour = datetime.now(timezone.utc).hour
                hour_scores = active_feedback.get("hour_scores", {})
                if str(current_hour) in hour_scores:
                    hour_score = _clamp(_safe_float(hour_scores[str(current_hour)], 0.5))
                elif current_hour in hour_scores:
                    hour_score = _clamp(_safe_float(hour_scores[current_hour], 0.5))
            except Exception:
                pass  # keep neutral default

    # Regime score: how well does the current regime match directional trading?
    regime_scores: Dict[Regime, float] = {
        Regime.TRENDING: 0.8,       # trending markets = good for directional
        Regime.RANGE_BOUND: 0.4,    # choppy = harder for directional
        Regime.VOLATILE: 0.6,       # volatile = opportunity but risky
        Regime.QUIET: 0.5,          # quiet = neutral, edges may persist longer
        Regime.UNKNOWN: 0.5,        # unknown = neutral default
    }
    regime_score = regime_scores.get(regime, 0.5)

    return 0.5 * time_score + 0.3 * hour_score + 0.2 * regime_score


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def score_four_factors(
    market_data: dict,
    ensemble_prob: float,
    market_prob: float,
    n_sources: int,
    source_desc: str,
    regime: Regime,
    active_feedback: dict,
    category_scores: dict,
    conn: Optional[sqlite3.Connection] = None,
) -> FourFactorScore:
    """Evaluate a trade candidate on all four factors.

    Args:
        market_data: Full market dict from the Kalshi API.
        ensemble_prob: Our ensemble probability estimate (0-1).
        market_prob: Current market probability (0-1).
        n_sources: Number of independent data sources used.
        source_desc: Description string with per-source estimates.
        regime: Current market regime (Regime enum).
        active_feedback: Dict of adaptive feedback / learned parameters.
        category_scores: Per-category historical win rates and sample sizes.
        conn: SQLite connection (optional, for logging).

    Returns:
        FourFactorScore with all four factor scores populated.
        Use .passes to check the gate, .composite for ranking.
    """
    # Defensive defaults for bad inputs
    if active_feedback is None:
        active_feedback = {}
    if category_scores is None:
        category_scores = {}
    if market_data is None:
        market_data = {}
    if regime is None:
        regime = Regime.UNKNOWN
    if source_desc is None:
        source_desc = ""

    # Clamp probability inputs to valid range
    ensemble_prob = _clamp(ensemble_prob)
    market_prob = _clamp(market_prob, lo=0.01, hi=0.99)
    n_sources = max(0, n_sources)

    edge = _score_edge(ensemble_prob, market_prob, active_feedback, market_data)
    liquidity = _score_liquidity(market_data)
    confidence = _score_confidence(n_sources, source_desc, category_scores, market_data)
    timing = _score_timing(market_data, regime, active_feedback)

    return FourFactorScore(
        edge=round(edge, 4),
        liquidity=round(liquidity, 4),
        confidence=round(confidence, 4),
        timing=round(timing, 4),
    )


def log_four_factor_decision(
    ticker: str,
    score: FourFactorScore,
    action: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Log a four-factor gate decision to the decision_log table.

    Args:
        ticker: Market ticker.
        score: The FourFactorScore result.
        action: What happened -- e.g. "pass_gate", "fail_edge", "trade_yes", "skip".
        conn: SQLite connection. If None, logging is silently skipped.
    """
    if conn is None:
        return
    try:
        now = datetime.now(timezone.utc).isoformat()
        four_factor_json = json.dumps(score.to_dict())
        conn.execute(
            """INSERT INTO decision_log
               (timestamp, ticker, action, strategy, four_factor)
               VALUES (?, ?, ?, ?, ?)""",
            (now, ticker, action, "four_factor_gate", four_factor_json),
        )
        conn.commit()
    except Exception:
        # Logging must never break the trading pipeline.
        # If the table doesn't exist or schema mismatches, silently skip.
        pass
