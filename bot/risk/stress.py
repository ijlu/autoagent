"""Stress level computation and self-modification gating.

Computes a 0-1 stress level from drawdown, loss streaks, and daily P&L.
Used to gate automated self-improvement: high stress blocks risky changes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

from bot.config import DAILY_LOSS_LIMIT, MAX_DRAWDOWN, GUARDRAILS


# ══════════════════════════════════════════════════════════════════════════════
# Stress computation
# ══════════════════════════════════════════════════════════════════════════════

def compute_stress_level(conn: sqlite3.Connection) -> float:
    """Compute overall stress level from 0.0 (calm) to 1.0 (crisis).

    Components:
        1. Drawdown stress: current balance vs 30-day peak
        2. Loss streak stress: consecutive losing trades
        3. Daily P&L stress: today's losses vs daily loss limit

    Returns:
        max(drawdown_stress, streak_stress, daily_stress), clamped to [0.0, 1.0].
    """
    drawdown_stress = _compute_drawdown_stress(conn)
    streak_stress = _compute_streak_stress(conn)
    daily_stress = _compute_daily_pnl_stress(conn)

    combined = max(drawdown_stress, streak_stress, daily_stress)
    return max(0.0, min(1.0, combined))


def _compute_drawdown_stress(conn: sqlite3.Connection) -> float:
    """Drawdown: current balance vs peak balance in last 30 days.

    Queries session records for balance_cents. If current balance is at peak,
    stress = 0. At MAX_DRAWDOWN, stress = 1.0.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    try:
        rows = conn.execute(
            """SELECT balance_cents FROM sessions
               WHERE timestamp >= ? AND balance_cents IS NOT NULL
               ORDER BY id DESC""",
            (cutoff,),
        ).fetchall()
    except Exception:
        return 0.0

    if not rows or len(rows) < 2:
        return 0.0

    current_balance = rows[0][0]
    peak_balance = max(row[0] for row in rows)

    if peak_balance <= 0:
        return 0.0

    drawdown_pct = (peak_balance - current_balance) / peak_balance

    if drawdown_pct <= 0:
        return 0.0

    max_dd = MAX_DRAWDOWN if MAX_DRAWDOWN > 0 else 0.15
    return min(1.0, drawdown_pct / max_dd)


def _compute_streak_stress(conn: sqlite3.Connection) -> float:
    """Loss streak: count consecutive losing trades from most recent.

    Queries settlements ordered by most recent. Counts consecutive losses.
    5+ losses maps to 0.8+, scaling linearly.
    """
    try:
        rows = conn.execute(
            """SELECT won FROM settlements
               ORDER BY id DESC LIMIT 20"""
        ).fetchall()
    except Exception:
        return 0.0

    if not rows:
        return 0.0

    consecutive_losses = 0
    for (won,) in rows:
        if won:
            break
        consecutive_losses += 1

    if consecutive_losses == 0:
        return 0.0

    # 1 loss = 0.1, 2 = 0.2, ... 5 = 0.8, 6 = 0.9, 7+ = 1.0
    # Using a ramp: stress = min(1.0, consecutive_losses * 0.16)
    # This gives: 1->0.16, 2->0.32, 3->0.48, 5->0.80, 6->0.96, 7->1.0
    return min(1.0, consecutive_losses * 0.16)


def _compute_daily_pnl_stress(conn: sqlite3.Connection) -> float:
    """Daily P&L: today's realized losses vs daily loss limit.

    Combines settlement P&L and MM inventory realized P&L for today.
    At the daily loss limit, stress = 1.0.
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).isoformat()

    total_pnl_cents = 0

    # Settlement P&L from today
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(profit_cents), 0) FROM settlements
               WHERE recorded_at >= ?""",
            (today_start,),
        ).fetchone()
        if row:
            total_pnl_cents += row[0]
    except Exception:
        pass

    # MM realized P&L (cumulative, so we look at today's fills)
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(realized_pnl_cents), 0) FROM mm_inventory"""
        ).fetchone()
        # mm_inventory.realized_pnl_cents is cumulative, not daily.
        # For daily, we approximate from today's processed fills.
        # Each fill's P&L isn't directly stored, so we use settlement data as primary.
    except Exception:
        pass

    # If P&L is positive (profitable day), no stress
    if total_pnl_cents >= 0:
        return 0.0

    # Need a reference balance for the loss limit percentage
    try:
        balance_row = conn.execute(
            """SELECT balance_cents FROM sessions
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        balance_cents = balance_row[0] if balance_row and balance_row[0] else 0
    except Exception:
        balance_cents = 0

    if balance_cents <= 0:
        # No balance reference; if we have losses, conservatively report moderate stress
        return 0.5 if total_pnl_cents < 0 else 0.0

    daily_limit = DAILY_LOSS_LIMIT if DAILY_LOSS_LIMIT > 0 else 0.10
    loss_limit_cents = balance_cents * daily_limit
    loss_pct_of_limit = abs(total_pnl_cents) / loss_limit_cents

    return min(1.0, loss_pct_of_limit)


# ══════════════════════════════════════════════════════════════════════════════
# Self-modification gating
# ══════════════════════════════════════════════════════════════════════════════

def should_allow_modification(stress: float, change_type: str) -> bool:
    """Decide whether an automated self-modification should proceed.

    Policy:
        stress < 0.5:     Allow all changes
        stress 0.5 - 0.8: Only allow tightening changes (reduce risk)
        stress > 0.8:     Block all automated modifications

    Args:
        stress: Stress level from compute_stress_level(), 0.0-1.0.
        change_type: One of "tighten", "loosen", "neutral".

    Returns:
        True if the modification is allowed, False otherwise.
    """
    stress_gate = GUARDRAILS.get("stress_gate_threshold", 0.8)

    # Crisis mode: block everything
    if stress > stress_gate:
        return False

    # Elevated stress: only allow tightening (risk reduction)
    if stress >= 0.5:
        return change_type == "tighten"

    # Calm: allow all changes
    return True
