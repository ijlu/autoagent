"""Comprehensive tests for bot.daemon.smart_gates.

Coverage targets:
- Time gate: before 7am, during peak, each sub-window, after 7pm
- Bracket proximity: near, far above, far below, already passed
- Trajectory: warming fast, cooling, flat, extreme, side-specific
- Settlement certainty: blown bracket, inside bracket late, close call
- Forecast confidence: agreement, disagreement, early vs late
- Composite: multiple gates interact correctly, multiplicative multipliers, cap
- Spread computation: multiplier math, floor/cap
"""
from __future__ import annotations

import pytest

from bot.daemon.smart_gates import (
    bracket_proximity_gate,
    compute_smart_spread,
    evaluate_all_gates,
    forecast_confidence_gate,
    settlement_certainty_gate,
    time_of_day_gate,
    trajectory_gate,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 1: Time-of-Day
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeOfDayGate:
    """Test the time-of-day gate."""

    def test_before_7am_rejected(self):
        # hours_left=19 -> lst_hour=5 -> before 7am
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=19.0)
        assert ok is False
        assert "pre-7am" in reason

    def test_midnight_rejected(self):
        # hours_left=24 -> lst_hour=0
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=24.0)
        assert ok is False
        assert "pre-7am" in reason

    def test_6am_rejected(self):
        # hours_left=18 -> lst_hour=6
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=18.0)
        assert ok is False
        assert "pre-7am" in reason

    def test_7am_accepted_tight_spread(self):
        # hours_left=17 -> lst_hour=7
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=17.0)
        assert ok is True
        assert mult == 1.0
        assert "morning" in reason

    def test_9am_morning_ramp(self):
        # hours_left=15 -> lst_hour=9
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=15.0)
        assert ok is True
        assert mult == 1.0

    def test_noon_peak_heating(self):
        # hours_left=12 -> lst_hour=12
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=12.0)
        assert ok is True
        assert mult == 1.0
        assert "peak" in reason

    def test_10am_boundary_peak(self):
        # hours_left=14 -> lst_hour=10
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=14.0)
        assert ok is True
        assert mult == 1.0
        assert "peak" in reason

    def test_3pm_afternoon_widen(self):
        # hours_left=9 -> lst_hour=15
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=9.0)
        assert ok is True
        assert mult == 1.2
        assert "afternoon" in reason

    def test_5pm_late_afternoon_wide(self):
        # hours_left=7 -> lst_hour=17
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=7.0)
        assert ok is True
        assert mult == 1.5
        assert "late afternoon" in reason

    def test_6pm_late_afternoon_wide(self):
        # hours_left=6 -> lst_hour=18
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=6.0)
        assert ok is True
        assert mult == 1.5

    def test_7pm_rejected(self):
        # hours_left=5 -> lst_hour=19
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=5.0)
        assert ok is False
        assert "past-7pm" in reason

    def test_10pm_rejected(self):
        # hours_left=2 -> lst_hour=22
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=2.0)
        assert ok is False
        assert "past-7pm" in reason

    def test_different_station_accepted(self):
        # Gate doesn't currently use station for LST calc (hours_left is pre-computed).
        ok, reason, mult = time_of_day_gate("KDEN", hours_left=12.0)
        assert ok is True
        assert mult == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 2: Bracket Proximity
# ═══════════════════════════════════════════════════════════════════════════════

class TestBracketProximityGate:
    """Test the bracket proximity gate."""

    def test_bracket_near_expected_tight(self):
        # Bracket [78-80], running=79, forecast=80 -> expected=80, mid=79, dist=1
        ok, reason, mult = bracket_proximity_gate(78, 80, 79.0, 80.0, 12.0)
        assert ok is True
        assert mult == 1.0
        assert "near expected" in reason

    def test_bracket_moderate_distance(self):
        # Bracket [84-86], running=79, forecast=80 -> expected=80, mid=85, dist=5
        ok, reason, mult = bracket_proximity_gate(84, 86, 79.0, 80.0, 12.0)
        assert ok is True
        assert mult == 1.2
        assert "moderate distance" in reason

    def test_bracket_far_but_quotable(self):
        # Bracket [86-88], running=79, forecast=80 -> expected=80, mid=87, dist=7
        ok, reason, mult = bracket_proximity_gate(86, 88, 79.0, 80.0, 12.0)
        assert ok is True
        assert mult == 1.5
        assert "far from" in reason

    def test_bracket_unreachable_skip(self):
        # Bracket [90-92], running=79, forecast=80 -> expected=80, floor 90 > 80+8
        ok, reason, mult = bracket_proximity_gate(90, 92, 79.0, 80.0, 12.0)
        assert ok is False
        assert "unreachable" in reason

    def test_bracket_already_passed_through_late(self):
        # Running high=85, bracket [78-80] cap=80, running > cap, hours_left < 8
        ok, reason, mult = bracket_proximity_gate(78, 80, 85.0, 86.0, 6.0)
        assert ok is True
        assert mult == 2.0
        assert "already passed" in reason

    def test_bracket_already_passed_through_early(self):
        # Running high=85, bracket [78-80], hours_left=10 (>8)
        ok, reason, mult = bracket_proximity_gate(78, 80, 85.0, 86.0, 10.0)
        assert ok is False
        assert "already passed" in reason

    def test_bracket_far_below_running(self):
        # Running high=90, bracket [70-72] -> cap 72 < 90-8=82 -> skip
        ok, reason, mult = bracket_proximity_gate(70, 72, 90.0, 90.0, 12.0)
        assert ok is False
        assert "already passed" in reason

    def test_bracket_just_above_running_forecast_higher(self):
        # Running=75, forecast=82 -> expected=82, bracket [80-82], mid=81, dist=1
        ok, reason, mult = bracket_proximity_gate(80, 82, 75.0, 82.0, 12.0)
        assert ok is True
        assert mult == 1.0

    def test_bracket_exact_match(self):
        # Bracket [79-81], running=80, forecast=80 -> expected=80, mid=80, dist=0
        ok, reason, mult = bracket_proximity_gate(79, 81, 80.0, 80.0, 12.0)
        assert ok is True
        assert mult == 1.0

    def test_bracket_running_equals_cap_early(self):
        # running_high == bracket_cap, early day: running > cap is False (not >),
        # floor 78 > 80+8=88? No. cap 80 < 80-8=72? No. mid=79, expected=80, dist=1
        ok, reason, mult = bracket_proximity_gate(78, 80, 80.0, 80.0, 12.0)
        assert ok is True
        assert mult == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 3: Trajectory
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrajectoryGate:
    """Test the METAR trajectory gate."""

    def test_mild_warming_normal(self):
        ok, reason, mult = trajectory_gate(0.5, 80, 82, 78.0, "yes")
        assert ok is True
        assert mult == 1.0
        assert "mild" in reason

    def test_flat_normal(self):
        ok, reason, mult = trajectory_gate(0.0, 80, 82, 78.0, "yes")
        assert ok is True
        assert mult == 1.0

    def test_moderate_warming_widens(self):
        ok, reason, mult = trajectory_gate(1.5, 80, 82, 78.0, "yes")
        assert ok is True
        assert mult == 1.2
        assert "moderate warming" in reason

    def test_fast_warming_sell_yes_bracket_above_rejected(self):
        # Bracket above running high, selling YES, fast warming -> reject
        ok, reason, mult = trajectory_gate(3.0, 80, 82, 78.0, "yes")
        assert ok is False
        assert "don't sell YES" in reason

    def test_fast_warming_sell_no_bracket_above_ok(self):
        # Bracket above running high, selling NO side -- OK but wide
        ok, reason, mult = trajectory_gate(3.0, 80, 82, 78.0, "no")
        assert ok is True
        assert mult == 1.5
        assert "sell NO only" in reason

    def test_fast_warming_bracket_below_ok(self):
        # Bracket below running high -- fast warming doesn't matter,
        # bracket is already passed
        ok, reason, mult = trajectory_gate(3.0, 70, 72, 78.0, "yes")
        assert ok is True
        assert mult == 1.5

    def test_extreme_warming_skip(self):
        # Threshold raised to 12 F/hr (Phase 0 fix) -- normal spring mornings
        # can easily hit 8-10 F/hr, so only truly anomalous rates reject.
        ok, reason, mult = trajectory_gate(13.0, 80, 82, 78.0, "yes")
        assert ok is False
        assert "extreme" in reason

    def test_extreme_cooling_skip(self):
        ok, reason, mult = trajectory_gate(-13.0, 80, 82, 78.0, "yes")
        assert ok is False
        assert "extreme" in reason

    def test_moderate_cooling_normal(self):
        ok, reason, mult = trajectory_gate(-1.5, 80, 82, 78.0, "yes")
        assert ok is True
        assert mult == 1.0
        assert "cooling" in reason

    def test_fast_cooling_normal(self):
        # Fast cooling doesn't affect daily high (it's a running max)
        ok, reason, mult = trajectory_gate(-3.0, 80, 82, 78.0, "yes")
        assert ok is True
        assert mult == 1.0
        assert "doesn't affect daily high" in reason

    def test_side_case_insensitive(self):
        ok, reason, mult = trajectory_gate(3.0, 80, 82, 78.0, "YES")
        assert ok is False  # should still reject selling YES

    def test_boundary_4f_per_hr(self):
        # Exactly 4.0 should NOT be extreme (>4.0 triggers extreme)
        ok, reason, mult = trajectory_gate(4.0, 80, 82, 78.0, "no")
        assert ok is True  # 4.0 is not > 4.0
        assert mult == 1.5

    def test_boundary_negative_4f_per_hr(self):
        # -4.0 should NOT be extreme (abs(-4.0) == 4.0 which is not > 4.0)
        ok, reason, mult = trajectory_gate(-4.0, 80, 82, 78.0, "yes")
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 4: Settlement Certainty
# ═══════════════════════════════════════════════════════════════════════════════

class TestSettlementCertaintyGate:
    """Test the settlement certainty gate."""

    def test_bracket_blown_late(self):
        # Running 85F >= cap 82F, hours_left=3 < 4 -> blown
        ok, reason, mult = settlement_certainty_gate(85.0, 78, 82, 3.0)
        assert ok is False
        assert "blown" in reason

    def test_bracket_blown_still_early(self):
        # Running 85F >= cap 82F, hours_left=5 >= 4 -> not triggered by blown check
        ok, reason, mult = settlement_certainty_gate(85.0, 78, 82, 5.0)
        assert ok is True

    def test_inside_bracket_late(self):
        # Running 80F inside [78,82], hours_left=1.5 < 2 -> skip
        ok, reason, mult = settlement_certainty_gate(80.0, 78, 82, 1.5)
        assert ok is False
        assert "inside bracket" in reason or "too close" in reason

    def test_inside_bracket_enough_time(self):
        # Running 80F inside [78,82], hours_left=3 >= 2 -> OK
        ok, reason, mult = settlement_certainty_gate(80.0, 78, 82, 3.0)
        assert ok is True

    def test_coin_flip_last_hour(self):
        # Running 77F, bracket floor 78F, |77-78|=1 < 2, hours_left=0.5 < 1
        # (running is BELOW bracket floor, so "inside bracket" check doesn't fire)
        ok, reason, mult = settlement_certainty_gate(77.0, 78, 82, 0.5)
        assert ok is False
        assert "coin flip" in reason

    def test_inside_bracket_very_late(self):
        # Running 79F is inside [78,82], hours_left=0.5 < 2 -> "inside bracket" fires
        ok, reason, mult = settlement_certainty_gate(79.0, 78, 82, 0.5)
        assert ok is False
        assert "inside bracket" in reason

    def test_not_coin_flip_if_enough_time(self):
        # Same temps but hours_left=2 -> not triggered
        ok, reason, mult = settlement_certainty_gate(79.0, 78, 82, 2.0)
        assert ok is True

    def test_below_bracket_late_ok(self):
        # Running 75F below bracket [78,82], hours_left=0.5
        # Not inside bracket, not blown, distance=3 > 2 -> not coin flip
        ok, reason, mult = settlement_certainty_gate(75.0, 78, 82, 0.5)
        assert ok is True

    def test_close_call_above_bracket_floor(self):
        # Running 79.5F, bracket floor 80F, |79.5-80|=0.5 < 2, hours_left=0.8 < 1
        # running < floor so "inside bracket" doesn't fire, but coin flip does
        ok, reason, mult = settlement_certainty_gate(79.5, 80, 84, 0.8)
        assert ok is False
        assert "coin flip" in reason

    def test_running_exactly_at_cap_late(self):
        # Running == cap (82), hours_left=3 < 4 -> blown (>=)
        ok, reason, mult = settlement_certainty_gate(82.0, 78, 82, 3.0)
        assert ok is False
        assert "blown" in reason

    def test_running_exactly_at_floor_late(self):
        # Running == floor (78), hours_left=1 < 2,
        # inside bracket check: 78 >= 78 and 78 < 82 -> inside, skip
        ok, reason, mult = settlement_certainty_gate(78.0, 78, 82, 1.0)
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 5: Forecast Confidence
# ═══════════════════════════════════════════════════════════════════════════════

class TestForecastConfidenceGate:
    """Test the forecast confidence gate."""

    def test_strong_agreement_late(self):
        # Gap=2, hours_left=4 (<= 6) -> high confidence
        ok, reason, mult = forecast_confidence_gate(78.0, 80.0, 4.0)
        assert ok is True
        assert mult == 1.0
        assert "high confidence" in reason or "agrees" in reason

    def test_strong_disagreement_late(self):
        # Gap=13, hours_left=4 (<= 6) -> wide spread
        ok, reason, mult = forecast_confidence_gate(72.0, 85.0, 4.0)
        assert ok is True
        assert mult == 2.0
        assert "something is off" in reason

    def test_moderate_disagreement_late(self):
        # Gap=4, hours_left=4 -> slight widen
        ok, reason, mult = forecast_confidence_gate(76.0, 80.0, 4.0)
        assert ok is True
        assert mult == 1.3

    def test_big_gap_early(self):
        # Gap=10, hours_left=10 (>8) -> trust forecast, normal spread
        ok, reason, mult = forecast_confidence_gate(70.0, 80.0, 10.0)
        assert ok is True
        assert mult == 1.0
        assert "early" in reason or "trust forecast" in reason

    def test_small_gap_early(self):
        # Gap=2, hours_left=10 -> normal
        ok, reason, mult = forecast_confidence_gate(78.0, 80.0, 10.0)
        assert ok is True
        assert mult == 1.0

    def test_big_gap_mid_day(self):
        # Gap=6, hours_left=7 (>6, <=8) -> widen
        ok, reason, mult = forecast_confidence_gate(74.0, 80.0, 7.0)
        assert ok is True
        assert mult == 1.5
        assert "mid-day" in reason

    def test_small_gap_mid_day(self):
        # Gap=2, hours_left=7 -> normal
        ok, reason, mult = forecast_confidence_gate(78.0, 80.0, 7.0)
        assert ok is True
        assert mult == 1.0

    def test_forecast_below_running(self):
        # Running > forecast: gap is the same regardless of direction
        ok, reason, mult = forecast_confidence_gate(85.0, 78.0, 4.0)
        assert ok is True
        assert mult == 2.0  # gap=7 > 5


# ═══════════════════════════════════════════════════════════════════════════════
# Composite: evaluate_all_gates
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompositeGate:
    """Test the composite gate evaluation."""

    def test_all_gates_pass_normal(self):
        # 10am, bracket near expected, flat trajectory, not settled, forecast agrees
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=78,
            bracket_cap=80,
            running_high=77.0,
            forecast_high=79.0,
            hours_left=14.0,  # lst_hour=10 -> peak
            trajectory_f_per_hr=0.5,
            side="yes",
        )
        assert ok is True
        assert mult == pytest.approx(1.0)

    def test_time_gate_rejects_first(self):
        # Before 7am -> time gate rejects, other gates don't matter
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=78,
            bracket_cap=80,
            running_high=77.0,
            forecast_high=79.0,
            hours_left=19.0,  # lst_hour=5 -> before 7am
            trajectory_f_per_hr=0.5,
        )
        assert ok is False
        assert "time_of_day" in reason

    def test_trajectory_rejects(self):
        # Extreme trajectory should cause rejection
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=78,
            bracket_cap=80,
            running_high=77.0,
            forecast_high=79.0,
            hours_left=14.0,
            trajectory_f_per_hr=5.0,  # extreme
        )
        assert ok is False
        assert "trajectory" in reason

    def test_multiplicative_spread(self):
        # Afternoon (1.2x) + moderate distance bracket (1.2x) + moderate warming (1.2x)
        # = 1.728x
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=84,
            bracket_cap=86,
            running_high=79.0,
            forecast_high=80.0,
            hours_left=9.0,   # lst_hour=15 -> afternoon (1.2x)
            trajectory_f_per_hr=1.5,  # moderate warming (1.2x)
            side="no",
        )
        assert ok is True
        # 1.2 (time) * 1.2 (bracket, dist=5) * 1.2 (trajectory) = 1.728
        assert mult == pytest.approx(1.728, abs=0.01)

    def test_multiplier_cap_rejects(self):
        # Stack multiple wide gates to exceed 3.0x cap
        # late afternoon (1.5x) + far bracket (1.5x) + fast warming NO side (1.5x)
        # = 3.375x > 3.0 -> reject
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=86,
            bracket_cap=88,
            running_high=79.0,
            forecast_high=80.0,
            hours_left=7.0,   # lst_hour=17 -> late afternoon (1.5x)
            trajectory_f_per_hr=3.0,  # fast warming (1.5x)
            side="no",
        )
        assert ok is False
        assert "exceeds cap" in reason

    def test_bracket_proximity_rejects(self):
        # Bracket far above expected -> bracket proximity rejects
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=95,
            bracket_cap=97,
            running_high=79.0,
            forecast_high=80.0,
            hours_left=14.0,
            trajectory_f_per_hr=0.5,
        )
        assert ok is False
        assert "bracket_proximity" in reason

    def test_settlement_certainty_rejects(self):
        # Bracket blown with little time left
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=70,
            bracket_cap=72,
            running_high=85.0,
            forecast_high=86.0,
            hours_left=8.0,  # lst_hour=16 -> afternoon, time gate passes
            trajectory_f_per_hr=0.0,
        )
        assert ok is False
        assert "settlement_certainty" in reason or "bracket_proximity" in reason

    def test_forecast_confidence_widens(self):
        # Forecast disagrees strongly in mid-afternoon with few hours left
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=78,
            bracket_cap=80,
            running_high=72.0,
            forecast_high=85.0,
            hours_left=7.0,  # lst_hour=17 -> late afternoon (1.5x)
            trajectory_f_per_hr=0.5,
            side="yes",
        )
        # Time: 1.5 (late afternoon)
        # Bracket: expected=max(72,85)=85, mid=79, dist=|79-85|=6 -> 1.2x (<=6)
        # Trajectory: mild (1.0x)
        # Settlement: OK (1.0x)
        # Forecast: hours_left=7 (>6, <=8 mid-day), gap=13>5 -> 1.5x
        # Total: 1.5 * 1.2 * 1.0 * 1.0 * 1.5 = 2.7
        assert ok is True
        assert mult == pytest.approx(2.7, abs=0.01)

    def test_default_side_is_yes(self):
        # When side is omitted, defaults to "yes"
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=78,
            bracket_cap=80,
            running_high=77.0,
            forecast_high=79.0,
            hours_left=14.0,
            trajectory_f_per_hr=0.5,
        )
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# Spread computation
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeSmartSpread:
    """Test the spread computation helper."""

    def test_identity_multiplier(self):
        assert compute_smart_spread(3, 1.0) == 3

    def test_widen(self):
        # 3 * 1.5 = 4.5 -> rounds to 4 (wait, round(4.5)=4 in Python banker's rounding)
        # Actually round(4.5) = 4 in Python 3 (banker's rounding).
        # Let's use a clear example: 3 * 2.0 = 6
        assert compute_smart_spread(3, 2.0) == 6

    def test_cap_at_15(self):
        # 3 * 10.0 = 30 -> capped at 15
        assert compute_smart_spread(3, 10.0) == 15

    def test_floor_at_base(self):
        # 5 * 0.5 = 2.5 -> rounds to 2, but floored at base 5
        assert compute_smart_spread(5, 0.5) == 5

    def test_multiplier_1_2(self):
        # 3 * 1.2 = 3.6 -> rounds to 4
        assert compute_smart_spread(3, 1.2) == 4

    def test_multiplier_1_5(self):
        # 4 * 1.5 = 6.0 -> 6
        assert compute_smart_spread(4, 1.5) == 6

    def test_large_base_capped(self):
        # base=10, mult=2.0 -> 20 capped at 15
        assert compute_smart_spread(10, 2.0) == 15

    def test_base_already_at_cap(self):
        # base=15, mult=1.0 -> 15
        assert compute_smart_spread(15, 1.0) == 15

    def test_base_above_cap(self):
        # Edge case: base=20 (shouldn't happen but test robustness)
        # 20 * 1.0 = 20, min(20,15)=15, max(20,15)=20
        # The floor is at base, so it returns 20 (base wins over cap)
        assert compute_smart_spread(20, 1.0) == 20

    def test_rounding(self):
        # 3 * 1.3 = 3.9 -> rounds to 4
        assert compute_smart_spread(3, 1.3) == 4
        # 3 * 1.1 = 3.3 -> rounds to 3
        assert compute_smart_spread(3, 1.1) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Integration / edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_zero_hours_left(self):
        # hours_left=0 -> lst_hour=24 -> past 7pm
        ok, reason, mult = time_of_day_gate("KJFK", hours_left=0.0)
        assert ok is False

    def test_negative_trajectory(self):
        # Negative but not extreme
        ok, reason, mult = trajectory_gate(-0.5, 80, 82, 78.0, "yes")
        assert ok is True
        assert mult == 1.0

    def test_bracket_with_zero_width(self):
        # Degenerate bracket where floor == cap
        ok, reason, mult = bracket_proximity_gate(80, 80, 80.0, 80.0, 12.0)
        # running_high (80) > bracket_cap (80) is False (not >)
        # floor > expected + 8? 80 > 88? No.
        # cap < running - 8? 80 < 72? No.
        # mid = 80, expected = 80, dist = 0 -> tight
        assert ok is True
        assert mult == 1.0

    def test_settlement_priority_blown_over_inside(self):
        # Running at cap boundary -- blown check (>=) fires before inside check
        ok, reason, mult = settlement_certainty_gate(82.0, 78, 82, 1.0)
        assert ok is False
        assert "blown" in reason

    def test_all_gates_pass_returns_all_reasons(self):
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=78,
            bracket_cap=80,
            running_high=77.0,
            forecast_high=79.0,
            hours_left=14.0,
            trajectory_f_per_hr=0.5,
        )
        assert ok is True
        assert "time_of_day" in reason
        assert "bracket_proximity" in reason
        assert "trajectory" in reason
        assert "settlement_certainty" in reason
        assert "forecast_confidence" in reason

    def test_first_rejection_stops_evaluation(self):
        # Time gate rejects -> reason only contains time gate
        ok, reason, mult = evaluate_all_gates(
            station="KJFK",
            bracket_floor=78,
            bracket_cap=80,
            running_high=77.0,
            forecast_high=79.0,
            hours_left=19.0,  # pre-7am
            trajectory_f_per_hr=5.0,  # extreme (would also reject)
        )
        assert ok is False
        assert "time_of_day" in reason
        # trajectory rejection should NOT appear since time gate fires first
        assert "trajectory" not in reason
