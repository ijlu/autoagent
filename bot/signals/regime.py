"""Market regime detection from recent trading data.

Classifies the current market environment into one of:
TRENDING, RANGE_BOUND, VOLATILE, QUIET, UNKNOWN.

Uses settlements and MM fill data from SQLite to compute
win rate trends, P&L volatility, spread compression, and fill rate.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone, timedelta

from bot.types import Regime


# ══════════════════════════════════════════════════════════════════════════════
# Thresholds (tuned conservatively — prefer UNKNOWN over wrong label)
# ══════════════════════════════════════════════════════════════════════════════

_MIN_SETTLEMENTS = 10          # Need at least this many to classify
_HIGH_PNL_VOL_CENTS = 150      # Std dev of profit_cents above this → volatile
_LOW_PNL_VOL_CENTS = 30        # Below this → quiet
_WIN_RATE_IMPROVING_DELTA = 0.05   # Recent - older win rate delta
_WIN_RATE_DECLINING_DELTA = -0.05
_GOOD_FILL_RATE = 0.15        # fills / orders posted ratio
_LOW_FILL_RATE = 0.03
_TIGHT_SPREAD_CENTS = 4        # avg spread at or below this → compressed


def detect_regime(conn: sqlite3.Connection) -> Regime:
    """Classify the current market regime from recent trading data.

    Args:
        conn: Active SQLite connection with settlements and mm_processed_fills tables.

    Returns:
        A Regime enum value. Returns UNKNOWN if insufficient data.
    """
    # ── 1. Query recent settlements ──
    settlements = conn.execute(
        """SELECT profit_cents, won, recorded_at
           FROM settlements
           ORDER BY id DESC LIMIT 50"""
    ).fetchall()

    if len(settlements) < _MIN_SETTLEMENTS:
        return Regime.UNKNOWN

    # ── 2. Query recent MM fills ──
    fills = conn.execute(
        """SELECT price_cents, side, recorded_at
           FROM mm_processed_fills
           ORDER BY id DESC LIMIT 100"""
    ).fetchall()

    # ── 3. Compute metrics ──

    # 3a. Win rate trend: compare first half vs second half of settlements
    # settlements are ordered newest-first, so "recent" is the first half
    mid = len(settlements) // 2
    recent_settlements = settlements[:mid]
    older_settlements = settlements[mid:]

    recent_wins = sum(1 for _, won, _ in recent_settlements if won)
    recent_win_rate = recent_wins / len(recent_settlements) if recent_settlements else 0.0

    older_wins = sum(1 for _, won, _ in older_settlements if won)
    older_win_rate = older_wins / len(older_settlements) if older_settlements else 0.0

    win_rate_delta = recent_win_rate - older_win_rate

    # 3b. P&L volatility: std dev of profit_cents across all settlements
    profits = [row[0] for row in settlements if row[0] is not None]
    pnl_vol = statistics.stdev(profits) if len(profits) >= 2 else 0.0

    # 3c. Spread compression: average spread between bid/ask fills
    # We approximate by looking at the range of fill prices per ticker
    spread_avg = _compute_avg_spread(fills) if fills else None

    # 3d. Fill rate: recent fills vs recent orders posted
    fill_rate = _compute_fill_rate(conn)

    # ── 4. Classification rules ──

    # High P&L vol + declining win rate → VOLATILE
    if pnl_vol > _HIGH_PNL_VOL_CENTS and win_rate_delta < _WIN_RATE_DECLINING_DELTA:
        return Regime.VOLATILE

    # High P&L vol alone (even without declining win rate) → VOLATILE
    if pnl_vol > _HIGH_PNL_VOL_CENTS * 1.5:
        return Regime.VOLATILE

    # Increasing win rate + good fill rate → TRENDING
    if win_rate_delta > _WIN_RATE_IMPROVING_DELTA and fill_rate >= _GOOD_FILL_RATE:
        return Regime.TRENDING

    # Stable win rate + tight spreads → RANGE_BOUND
    if (abs(win_rate_delta) <= _WIN_RATE_IMPROVING_DELTA
            and spread_avg is not None
            and spread_avg <= _TIGHT_SPREAD_CENTS):
        return Regime.RANGE_BOUND

    # Low activity + few fills → QUIET
    if pnl_vol < _LOW_PNL_VOL_CENTS and fill_rate < _LOW_FILL_RATE:
        return Regime.QUIET

    # Low fill rate alone can indicate quiet conditions
    if fill_rate < _LOW_FILL_RATE and len(fills) < 10:
        return Regime.QUIET

    # If nothing matches clearly, return UNKNOWN rather than guess
    return Regime.UNKNOWN


def _compute_avg_spread(fills: list[tuple]) -> float | None:
    """Estimate average spread from MM fills by looking at per-ticker price ranges.

    Fills are (price_cents, side, recorded_at). For each ticker's fills within
    the batch, spread = max_price - min_price. Average across tickers.
    """
    if not fills:
        return None

    # Group fill prices by side to approximate bid-ask
    yes_prices = [row[0] for row in fills if row[1] == "yes" and row[0] is not None]
    no_prices = [row[0] for row in fills if row[1] == "no" and row[0] is not None]

    if yes_prices and no_prices:
        avg_yes = statistics.mean(yes_prices)
        avg_no = statistics.mean(no_prices)
        # In a two-sided market, spread ~ |avg_yes + avg_no - 100|
        # But simpler: spread ~ max(yes) - min(yes) as a proxy
        if len(yes_prices) >= 2:
            return statistics.stdev(yes_prices)
        return abs(avg_yes - avg_no)

    # Fallback: just look at price dispersion
    all_prices = [row[0] for row in fills if row[0] is not None]
    if len(all_prices) >= 2:
        return max(all_prices) - min(all_prices)

    return None


def _compute_fill_rate(conn: sqlite3.Connection) -> float:
    """Compute fill rate: recent fills / recent orders posted.

    Uses the last 7 days of data from mm_orders and mm_processed_fills.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    try:
        orders_row = conn.execute(
            "SELECT COUNT(*) FROM mm_orders WHERE timestamp >= ?", (cutoff,)
        ).fetchone()
        orders_posted = orders_row[0] if orders_row else 0

        fills_row = conn.execute(
            "SELECT COUNT(*) FROM mm_processed_fills WHERE recorded_at >= ?", (cutoff,)
        ).fetchone()
        fills_detected = fills_row[0] if fills_row else 0
    except Exception:
        return 0.0

    if orders_posted == 0:
        return 0.0

    return fills_detected / orders_posted
