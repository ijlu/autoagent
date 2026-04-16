"""Bracket constraint arbitrage -- exploits violation of sum-to-100% invariant.

In a bracket series (e.g., Fed rate brackets), YES prices for all brackets
should sum to ~100%. When they sum to >102% (overpriced) or <98% (underpriced),
we can profit by selling the overpriced set or buying the underpriced set.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from bot.types import Regime, Side, TradeSignal
from bot.market_maker.series_profitability import _get_series_prefix


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The YES prices across all brackets in a mutually-exclusive series must sum
# to ~100 cents.  We only trade when the deviation exceeds these thresholds
# (in absolute percentage points of the sum).
OVERPRICED_THRESHOLD = 102.0   # sum > 102 => sell opportunity
UNDERPRICED_THRESHOLD = 98.0   # sum < 98  => buy opportunity
MIN_BRACKETS = 3               # ignore series with fewer brackets
MIN_EDGE_AFTER_FEES = 2.0      # require at least 2% edge per bracket after fees
ESTIMATED_FEE_PER_SIDE = 0.7   # ~0.7 cents estimated fee per contract per side (maker)
MAX_CONTRACTS_PER_LEG = 10     # cap per-leg size


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_price_cents(market: dict, field: str) -> Optional[float]:
    """Extract a price in cents from a market dict, tolerating missing/bad data.

    Handles both cent-denominated (int-ish) and dollar-denominated (0.xx)
    values.  Returns None when the field is absent or unparseable.
    """
    raw = market.get(field)
    if raw is None:
        return None
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return None
    # Kalshi sometimes returns dollar values (0.35) or cent values (35)
    if 0 < val < 1:
        return round(val * 100, 2)
    return round(val, 2)


def _estimate_round_trip_fee(price_cents: float, contracts: int = 1) -> float:
    """Conservative estimate of round-trip maker fees in cents per contract.

    Kalshi maker fee formula: roundup(0.0175 * C * P * (1 - P))
    We estimate entry + exit fees.
    """
    p = price_cents / 100.0
    if p <= 0 or p >= 1:
        return 0.0
    per_side = max(0.5, 0.0175 * contracts * p * (1 - p) * 100)
    return per_side * 2  # entry + exit


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

def scan_bracket_arbs(
    conn: sqlite3.Connection,
    markets: list[dict],
) -> list[TradeSignal]:
    """Scan a list of markets for bracket-sum arbitrage opportunities.

    Parameters
    ----------
    conn : sqlite3.Connection
        Database connection (used for position lookups if needed).
    markets : list[dict]
        Market dicts from Kalshi API, each containing at least ``ticker``,
        ``yes_ask``, ``no_ask``, and ``title`` fields.

    Returns
    -------
    list[TradeSignal]
        One signal per actionable bracket in a mispriced series.
    """
    signals: list[TradeSignal] = []

    # ── 1. Group markets by bracket series ────────────────────────────────
    series: dict[str, list[dict]] = {}
    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker:
            continue
        prefix, is_bracket = _get_series_prefix(ticker)
        if not is_bracket:
            continue
        series.setdefault(prefix, []).append(m)

    # ── 2. Evaluate each series ───────────────────────────────────────────
    for prefix, bracket_markets in series.items():
        if len(bracket_markets) < MIN_BRACKETS:
            continue

        # Collect YES ask prices for each bracket
        priced_brackets: list[tuple[dict, float]] = []
        for m in bracket_markets:
            yes_ask = _safe_price_cents(m, "yes_ask")
            if yes_ask is None:
                # Also try yes_ask_dollars (some API responses)
                yes_ask = _safe_price_cents(m, "yes_ask_dollars")
            if yes_ask is None or yes_ask <= 0:
                continue
            priced_brackets.append((m, yes_ask))

        if len(priced_brackets) < MIN_BRACKETS:
            continue

        total_yes = sum(p for _, p in priced_brackets)
        n = len(priced_brackets)
        deviation = total_yes - 100.0

        # ── 3a. Overpriced series (sum > threshold) ──────────────────────
        if total_yes > OVERPRICED_THRESHOLD:
            # Strategy: sell YES on the most overpriced brackets.
            # Edge per bracket = deviation / n (if we could sell the full set).
            edge_per_bracket = deviation / n

            # Sort by YES price descending -- the most expensive brackets
            # contribute the most to the overshoot.
            priced_brackets.sort(key=lambda x: x[1], reverse=True)

            for m, yes_ask in priced_brackets:
                ticker = m.get("ticker", "")
                fee_est = _estimate_round_trip_fee(yes_ask, 1)
                net_edge = edge_per_bracket - fee_est

                if net_edge < MIN_EDGE_AFTER_FEES:
                    continue

                signals.append(TradeSignal(
                    ticker=ticker,
                    side=Side.NO,  # selling YES = buying NO
                    strategy="bracket_arb",
                    ensemble_prob=(100.0 - yes_ask) / 100.0,  # implied NO prob
                    market_prob=yes_ask / 100.0,
                    edge=net_edge / 100.0,  # as a fraction
                    confidence=min(0.9, 0.5 + deviation / 20.0),
                    n_sources=n,  # number of brackets informing the signal
                    source_desc=(
                        f"bracket_arb:overpriced:{prefix} "
                        f"sum={total_yes:.1f} dev={deviation:+.1f} "
                        f"n={n}"
                    ),
                    regime=Regime.UNKNOWN,
                    suggested_contracts=min(MAX_CONTRACTS_PER_LEG, 5),
                    suggested_price_cents=round(100 - yes_ask),
                    metadata={
                        "arb_type": "overpriced",
                        "series_prefix": prefix,
                        "series_sum": round(total_yes, 2),
                        "deviation": round(deviation, 2),
                        "n_brackets": n,
                        "edge_per_bracket": round(edge_per_bracket, 2),
                        "fee_estimate": round(fee_est, 2),
                        "yes_ask_cents": round(yes_ask, 2),
                    },
                ))

        # ── 3b. Underpriced series (sum < threshold) ─────────────────────
        elif total_yes < UNDERPRICED_THRESHOLD:
            # Strategy: buy YES on the cheapest brackets.
            # Edge per bracket = |deviation| / n.
            edge_per_bracket = abs(deviation) / n

            # Sort by YES price ascending -- the cheapest brackets offer
            # the best risk/reward.
            priced_brackets.sort(key=lambda x: x[1])

            for m, yes_ask in priced_brackets:
                ticker = m.get("ticker", "")
                fee_est = _estimate_round_trip_fee(yes_ask, 1)
                net_edge = edge_per_bracket - fee_est

                if net_edge < MIN_EDGE_AFTER_FEES:
                    continue

                signals.append(TradeSignal(
                    ticker=ticker,
                    side=Side.YES,
                    strategy="bracket_arb",
                    ensemble_prob=yes_ask / 100.0,
                    market_prob=yes_ask / 100.0,
                    edge=net_edge / 100.0,
                    confidence=min(0.9, 0.5 + abs(deviation) / 20.0),
                    n_sources=n,
                    source_desc=(
                        f"bracket_arb:underpriced:{prefix} "
                        f"sum={total_yes:.1f} dev={deviation:+.1f} "
                        f"n={n}"
                    ),
                    regime=Regime.UNKNOWN,
                    suggested_contracts=min(MAX_CONTRACTS_PER_LEG, 5),
                    suggested_price_cents=round(yes_ask),
                    metadata={
                        "arb_type": "underpriced",
                        "series_prefix": prefix,
                        "series_sum": round(total_yes, 2),
                        "deviation": round(deviation, 2),
                        "n_brackets": n,
                        "edge_per_bracket": round(edge_per_bracket, 2),
                        "fee_estimate": round(fee_est, 2),
                        "yes_ask_cents": round(yes_ask, 2),
                    },
                ))

    return signals
