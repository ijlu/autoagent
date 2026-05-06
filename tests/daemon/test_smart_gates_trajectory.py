"""Regression tests for the Phase 0 trajectory-gate threshold fix.

Pre-fix behavior: any |trajectory| > 4 F/hr was labeled "extreme" and
rejected, which killed ~168 quotes/hour during normal morning warmup
(spring/summer morning peaks often hit 8-10 F/hr).

Post-fix behavior:
  - |trajectory| > 12 F/hr    -> extreme, always reject
  - 6 < trajectory <= 12      -> "very fast warming", widen 2.0x, reject YES
                                  if bracket is above the running high
  - 2 < trajectory <= 6       -> "fast warming", widen 1.5x, reject YES if
                                  bracket is above running high
  - 1 < trajectory <= 2       -> "moderate warming", widen 1.2x
  - mild/flat/cooling         -> normal 1.0x
"""
from __future__ import annotations

from bot.daemon.smart_gates import trajectory_gate


# ═══════════════════════════════════════════════════════════════════════════════
# Normal morning warmup (previously rejected as "extreme", now accepted)
# ═══════════════════════════════════════════════════════════════════════════════

def test_morning_warmup_8f_per_hr_no_side_ok():
    """A typical spring morning: temp rising 8 F/hr, selling NO while
    bracket is above running high. Should be OK with 2.0x spread."""
    ok, reason, mult = trajectory_gate(
        trajectory_f_per_hr=8.0,
        bracket_floor=80,
        bracket_cap=82,
        running_high=72.0,
        side="no",
    )
    assert ok is True
    assert mult == 2.0
    assert "very fast warming" in reason


def test_morning_warmup_8f_per_hr_yes_side_bracket_above_rejected():
    """Selling YES on a bracket that temp is rapidly heading INTO is the
    exact adverse-selection scenario -- must still reject."""
    ok, reason, mult = trajectory_gate(
        trajectory_f_per_hr=8.0,
        bracket_floor=80,
        bracket_cap=82,
        running_high=72.0,
        side="yes",
    )
    assert ok is False
    assert "don't sell YES" in reason


def test_morning_warmup_8f_per_hr_yes_side_bracket_below_ok():
    """If bracket is already below running high, YES side is fine even
    with fast warming -- the bracket is already settled one way."""
    ok, reason, mult = trajectory_gate(
        trajectory_f_per_hr=8.0,
        bracket_floor=60,
        bracket_cap=62,
        running_high=72.0,
        side="yes",
    )
    assert ok is True
    assert mult == 2.0


# ═══════════════════════════════════════════════════════════════════════════════
# Extreme threshold boundary (new threshold is 12, not 4)
# ═══════════════════════════════════════════════════════════════════════════════

def test_12f_per_hr_not_extreme():
    """12.0 exactly is the upper limit of 'very fast warming' -- still OK."""
    ok, reason, mult = trajectory_gate(
        trajectory_f_per_hr=12.0,
        bracket_floor=80,
        bracket_cap=82,
        running_high=72.0,
        side="no",
    )
    assert ok is True
    assert mult == 2.0
    assert "very fast warming" in reason


def test_13f_per_hr_extreme_reject():
    """Above 12 is genuinely anomalous -- skip entirely regardless of side."""
    ok, reason, mult = trajectory_gate(
        trajectory_f_per_hr=13.0,
        bracket_floor=80,
        bracket_cap=82,
        running_high=72.0,
        side="no",
    )
    assert ok is False
    assert "extreme" in reason


def test_negative_13f_per_hr_extreme_reject():
    """Extreme cooling also rejected (model may be wrong)."""
    ok, reason, mult = trajectory_gate(
        trajectory_f_per_hr=-13.0,
        bracket_floor=80,
        bracket_cap=82,
        running_high=72.0,
        side="yes",
    )
    assert ok is False
    assert "extreme" in reason
