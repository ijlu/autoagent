"""Directional exit decisions anchored on edge-decay.

The entry thesis for a directional trade is always "ensemble fair value X,
market Y, edge = X-Y." The correct exit is the inverse: close when that
edge no longer exists. Anything else — fixed %, time-based, P&L stop — is
a proxy for edge-decay that decouples from the thesis.

This module is the hard-trigger evaluator. It runs after take_profit /
stop_loss (which are catastrophic backstops) and before the composite
health score. The composite still applies when entry_edge is unavailable
(positions entered before entry_edge was persisted) or when edge-decay
rules abstain.

Pure-function surface by design — no DB, no API, no wall clock. Caller
passes snapshot state; we return (trigger_reason, should_exit). That
keeps edge-decay behavior testable without mocking the daemon.

Rule order (first match wins):
    1. edge_flipped          — thesis inverted; exit now
    2. edge_decayed          — thesis weakened below retention floor
    3. time_backstop         — near expiry with no conviction left
    4. stale_hold_backstop   — holding too long without improvement

Returns None when no rule fires — callers fall through to health-score.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EdgeDecayDecision:
    """Result of the edge-decay evaluator.

    trigger is one of:
        "edge_flipped" | "edge_decayed" | "time_backstop" |
        "stale_hold_backstop" | None (abstain)
    detail is a short human-readable explanation suitable for log lines.
    """
    trigger: Optional[str]
    detail: str


def evaluate_edge_decay_exit(
    *,
    entry_edge: Optional[float],
    remaining_edge: float,
    hours_to_expiry: float,
    hours_held: float,
    trend_score: float,
    decay_ratio: float = 0.33,
    time_backstop_hours: float = 0.25,
    time_backstop_edge_abs: float = 0.02,
    stale_hold_hours: float = 24.0,
) -> EdgeDecayDecision:
    """Determine whether edge-decay rules trigger an exit.

    Parameters
    ----------
    entry_edge : float or None
        Edge at the moment the position was opened (positive float for a
        thesis-aligned entry). If None or zero, the evaluator abstains —
        no edge-decay rule can fire without an anchor.
    remaining_edge : float
        Current edge net of exit fees. Same sign convention as entry_edge.
    hours_to_expiry : float
        Wall-clock hours until the Kalshi market settles.
    hours_held : float
        Wall-clock hours since entry.
    trend_score : float
        Edge trend in [-1, +1] (−1 = deteriorating, +1 = improving).
        Produced by the rolling edge-history comparison in manage_positions.
    decay_ratio : float
        Exit when remaining_edge < entry_edge * decay_ratio. 0.33 retains
        positions with at least a third of original edge; lower = more
        patient, higher = more trigger-happy.
    time_backstop_hours : float
        Near-expiry window below which low-conviction holds get closed.
    time_backstop_edge_abs : float
        |remaining_edge| below this counts as "no conviction" for the
        time backstop.
    stale_hold_hours : float
        Hours after which a non-improving position is considered stale.

    Returns
    -------
    EdgeDecayDecision
        trigger = None means no rule fired — the caller should fall
        through to composite health-score evaluation.
    """
    if entry_edge is None or entry_edge <= 0:
        return EdgeDecayDecision(trigger=None, detail="no_entry_edge_anchor")

    # Rule 1: thesis inverted — exit immediately. This is different from
    # edge_decayed because it doesn't depend on the ratio — any negative
    # edge after a positive-edge entry means the market moved against us.
    if remaining_edge < 0:
        return EdgeDecayDecision(
            trigger="edge_flipped",
            detail=(
                f"entry_edge={entry_edge:+.3f} → now={remaining_edge:+.3f} "
                f"(thesis inverted)"
            ),
        )

    # Rule 2: edge decayed below retention floor. Floor is a fraction of
    # the original edge — scales naturally with the strength of the
    # initial conviction.
    retention_floor = entry_edge * decay_ratio
    if remaining_edge < retention_floor:
        return EdgeDecayDecision(
            trigger="edge_decayed",
            detail=(
                f"entry_edge={entry_edge:+.3f} → now={remaining_edge:+.3f} "
                f"< {retention_floor:+.3f} (floor={decay_ratio:.0%} of entry)"
            ),
        )

    # Rule 3: near expiry + no conviction. Covers the case where the
    # ensemble didn't update as new information arrived (signal froze).
    # Without this backstop we'd ride flat-edge positions into settlement
    # variance.
    if (hours_to_expiry < time_backstop_hours
            and abs(remaining_edge) < time_backstop_edge_abs):
        return EdgeDecayDecision(
            trigger="time_backstop",
            detail=(
                f"hrs_left={hours_to_expiry:.2f}h < {time_backstop_hours:.2f}h "
                f"AND |edge|={abs(remaining_edge):.3f} < "
                f"{time_backstop_edge_abs:.3f} (no conviction)"
            ),
        )

    # Rule 4: stale hold. Position aged past the window and edge trend is
    # flat-or-worse. Catches positions where edge-decay didn't trip
    # because the ensemble is stuck; trend_score measures "is edge moving
    # or frozen?"
    if hours_held > stale_hold_hours and trend_score <= 0:
        return EdgeDecayDecision(
            trigger="stale_hold_backstop",
            detail=(
                f"held={hours_held:.1f}h > {stale_hold_hours:.1f}h "
                f"AND trend={trend_score:+.2f} ≤ 0 (not improving)"
            ),
        )

    # No rule fired — composite health-score takes over.
    return EdgeDecayDecision(trigger=None, detail="")
