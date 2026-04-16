"""Correlation arbitrage -- exploits logical implication violations.

When market A implies market B (e.g., "CPI > 3.0%" implies "CPI > 2.5%"),
then price(A) must be <= price(B). Violations are near risk-free arbs.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from bot.types import Regime, Side, TradeSignal
from bot.market_maker.series_profitability import _get_series_prefix


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_VIOLATION_CENTS = 2.0     # ignore tiny mispricings below 2 cents
MIN_EDGE_AFTER_FEES = 1.5     # require >= 1.5 cents net edge after fees
ESTIMATED_FEE_PER_SIDE = 0.7  # ~0.7 cents maker fee per contract per side
MAX_CONTRACTS_PER_LEG = 10    # cap position size per leg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_price_cents(market: dict, field: str) -> Optional[float]:
    """Extract a price in cents from a market dict, tolerating missing data.

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
    if 0 < val < 1:
        return round(val * 100, 2)
    return round(val, 2)


def _extract_threshold(ticker: str) -> Optional[float]:
    """Parse the numeric threshold from a threshold-type ticker.

    Examples
    --------
    >>> _extract_threshold("KXCPI-27APR-T3.00")
    3.0
    >>> _extract_threshold("KXFED-27APR-T2.50")
    2.5
    >>> _extract_threshold("KXHIGHDEN-26APR09-B69.5")  # bracket, not threshold
    """
    # Match -T followed by a numeric value at the end of the ticker
    m = re.search(r"-T([\d.]+)$", ticker)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _estimate_round_trip_fee(price_cents: float, contracts: int = 1) -> float:
    """Conservative estimate of round-trip maker fees in cents per contract.

    Kalshi maker fee: roundup(0.0175 * C * P * (1 - P))
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

def scan_correlation_arbs(
    conn: sqlite3.Connection,
    markets: list[dict],
) -> list[TradeSignal]:
    """Scan threshold markets for monotonicity violations.

    In a threshold series (e.g., ``KXCPI-27APR-T2.50``, ``KXCPI-27APR-T3.00``),
    higher thresholds must be priced *lower* than lower thresholds because
    "CPI > 3.0%" is a *subset* of "CPI > 2.5%".  If the market prices violate
    this ordering, we have a near risk-free arbitrage.

    Parameters
    ----------
    conn : sqlite3.Connection
        Database connection (available for position lookups if needed).
    markets : list[dict]
        Market dicts from Kalshi API, each containing at least ``ticker``,
        ``yes_ask``, ``no_ask``, and ``title`` fields.

    Returns
    -------
    list[TradeSignal]
        Paired signals: sell YES on the overpriced higher-threshold market,
        buy YES on the underpriced lower-threshold market.
    """
    signals: list[TradeSignal] = []

    # ── 1. Group threshold markets by series prefix ───────────────────────
    series: dict[str, list[tuple[dict, float, float]]] = {}
    #        prefix -> [(market_dict, threshold_value, yes_ask_cents), ...]

    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker:
            continue

        prefix, is_bracket = _get_series_prefix(ticker)
        if is_bracket:
            # We want threshold (-T) markets, not brackets (-B)
            continue

        threshold = _extract_threshold(ticker)
        if threshold is None:
            continue

        yes_ask = _safe_price_cents(m, "yes_ask")
        if yes_ask is None:
            yes_ask = _safe_price_cents(m, "yes_ask_dollars")
        if yes_ask is None or yes_ask <= 0:
            continue

        series.setdefault(prefix, []).append((m, threshold, yes_ask))

    # ── 2. Check each series for monotonicity violations ──────────────────
    for prefix, entries in series.items():
        if len(entries) < 2:
            continue

        # Sort by threshold ascending.  For "above X" markets, the YES price
        # should be monotonically *decreasing* as the threshold increases,
        # because a higher bar is harder to clear.
        entries.sort(key=lambda x: x[1])

        for i in range(len(entries) - 1):
            m_low, thresh_low, price_low = entries[i]
            m_high, thresh_high, price_high = entries[i + 1]

            # Violation: higher threshold should NOT have a higher YES price
            if price_high <= price_low:
                continue  # No violation -- monotonicity holds

            violation_cents = price_high - price_low

            if violation_cents < MIN_VIOLATION_CENTS:
                continue

            # Estimate fees for both legs
            fee_sell_high = _estimate_round_trip_fee(price_high, 1)
            fee_buy_low = _estimate_round_trip_fee(price_low, 1)
            total_fees = fee_sell_high + fee_buy_low

            net_edge_cents = violation_cents - total_fees

            if net_edge_cents < MIN_EDGE_AFTER_FEES:
                continue

            ticker_high = m_high.get("ticker", "")
            ticker_low = m_low.get("ticker", "")

            shared_metadata = {
                "arb_type": "correlation_monotonicity",
                "series_prefix": prefix,
                "threshold_low": thresh_low,
                "threshold_high": thresh_high,
                "price_low_cents": round(price_low, 2),
                "price_high_cents": round(price_high, 2),
                "violation_cents": round(violation_cents, 2),
                "fee_estimate_cents": round(total_fees, 2),
                "net_edge_cents": round(net_edge_cents, 2),
                "paired_ticker": "",  # filled per-leg below
            }

            # ── Leg 1: Sell YES on the overpriced higher threshold ────────
            meta_sell = {**shared_metadata, "leg": "sell_high", "paired_ticker": ticker_low}
            signals.append(TradeSignal(
                ticker=ticker_high,
                side=Side.NO,  # selling YES = buying NO
                strategy="correlation_arb",
                ensemble_prob=(100.0 - price_high) / 100.0,
                market_prob=price_high / 100.0,
                edge=net_edge_cents / 100.0,  # as fraction
                confidence=min(0.95, 0.6 + violation_cents / 20.0),
                n_sources=2,  # both legs inform the signal
                source_desc=(
                    f"corr_arb:sell_high:{ticker_high} "
                    f"T{thresh_high}@{price_high:.0f}c > "
                    f"T{thresh_low}@{price_low:.0f}c "
                    f"gap={violation_cents:.1f}c"
                ),
                regime=Regime.UNKNOWN,
                suggested_contracts=min(MAX_CONTRACTS_PER_LEG, 5),
                suggested_price_cents=round(100 - price_high),
                metadata=meta_sell,
            ))

            # ── Leg 2: Buy YES on the underpriced lower threshold ─────────
            meta_buy = {**shared_metadata, "leg": "buy_low", "paired_ticker": ticker_high}
            signals.append(TradeSignal(
                ticker=ticker_low,
                side=Side.YES,
                strategy="correlation_arb",
                ensemble_prob=price_low / 100.0,
                market_prob=price_low / 100.0,
                edge=net_edge_cents / 100.0,
                confidence=min(0.95, 0.6 + violation_cents / 20.0),
                n_sources=2,
                source_desc=(
                    f"corr_arb:buy_low:{ticker_low} "
                    f"T{thresh_low}@{price_low:.0f}c < "
                    f"T{thresh_high}@{price_high:.0f}c "
                    f"gap={violation_cents:.1f}c"
                ),
                regime=Regime.UNKNOWN,
                suggested_contracts=min(MAX_CONTRACTS_PER_LEG, 5),
                suggested_price_cents=round(price_low),
                metadata=meta_buy,
            ))

    return signals
