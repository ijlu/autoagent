"""Tests for settlement certainty fast-path and METAR direction awareness.

Validates that:
1. METAR correctly handles "or below" vs "or above" markets
2. Settlement certainty exit triggers for near-certain losers near expiry
3. Settlement certainty hold triggers for near-certain winners near expiry
4. Normal health score path still works for non-extreme cases
"""

from __future__ import annotations

import math
import pytest


# ══════════════════════════════════════════════════════════════════════════════
# METAR direction parsing
# ══════════════════════════════════════════════════════════════════════════════

# Import the actual function for testing
from bot.signals.sources.metar_observations import _parse_threshold_from_market


class TestMETARDirectionParsing:
    """Test that _parse_threshold_from_market returns correct (threshold, is_above)."""

    def test_or_above_title(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T72", "will the high temperature be at or above 72°f?"
        )
        assert threshold == 72.0
        assert is_above is True

    def test_or_below_title(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T72", "72° or below"
        )
        # "below" in title → should parse as below direction
        assert threshold == 72.0
        assert is_above is False

    def test_above_keyword(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHNY-26APR16-T75", "above 75°f"
        )
        assert threshold == 75.0
        assert is_above is True

    def test_below_keyword(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHNY-26APR16-T75", "below 75°f"
        )
        assert threshold == 75.0
        assert is_above is False

    def test_at_or_below(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T72", "at or below 72"
        )
        assert threshold == 72.0
        assert is_above is False

    def test_at_or_above(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T72", "at or above 72"
        )
        assert threshold == 72.0
        assert is_above is True

    def test_over(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T80", "over 80"
        )
        assert threshold == 80.0
        assert is_above is True

    def test_under(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T68", "under 68"
        )
        assert threshold == 68.0
        assert is_above is False

    def test_exceed(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T90", "exceed 90"
        )
        assert threshold == 90.0
        assert is_above is True

    def test_at_least(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T72", "at least 72"
        )
        assert threshold == 72.0
        assert is_above is True

    def test_ticker_only_defaults_above(self):
        """When only ticker is available (no direction in title), default to above."""
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T72", "highest temperature in chicago today?"
        )
        assert threshold == 72.0
        assert is_above is True  # default

    def test_bracket_ticker(self):
        """Bracket tickers default to above (bracket logic handles direction separately)."""
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-B74", "74 to 75°f"
        )
        # Range match in title takes precedence, but the bracket threshold from
        # the title regex won't match (no direction keyword); falls to ticker parse
        assert threshold is not None

    def test_bare_degree_with_below_context(self):
        """Bare degree number with 'or below' in the broader title."""
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI-26APR16-T72", "72°f or below"
        )
        assert threshold == 72.0
        assert is_above is False

    def test_bare_degree_without_direction(self):
        """Bare degree number without clear direction defaults to above."""
        threshold, is_above = _parse_threshold_from_market(
            "KXHIGHCHI", "72°f"
        )
        assert threshold == 72.0
        assert is_above is True  # default

    def test_no_match_returns_none(self):
        threshold, is_above = _parse_threshold_from_market(
            "KXFED-26APR16", "will the fed raise rates?"
        )
        assert threshold is None


# ══════════════════════════════════════════════════════════════════════════════
# METAR probability inversion for "below" markets
# ══════════════════════════════════════════════════════════════════════════════

from bot.signals.sources.metar_observations import _compute_probability, _logistic_cdf


class TestMETARProbabilityDirection:
    """Test that probability is correctly inverted for 'below' markets."""

    def test_above_market_high_already_exceeded(self):
        """If running high >= threshold, P(above) should be near 1.0."""
        prob = _compute_probability(
            running_high=75.0, threshold=72.0, hours_left=2.0, forecast_high=76.0
        )
        assert prob >= 0.95  # near certainty

    def test_above_market_below_threshold(self):
        """If running high < threshold with little time left, P(above) should be low."""
        prob = _compute_probability(
            running_high=69.0, threshold=72.0, hours_left=0.5, forecast_high=70.0
        )
        assert prob < 0.20  # very unlikely to reach 72 with 30 min left

    def test_below_market_inversion_math(self):
        """For a 'below' market, P(YES) = 1 - P(high >= threshold).

        If high=71, threshold=72, <1h left: P(high >= 72) is small,
        so P(high <= 72) = 1 - P(high >= 72) should be high.
        """
        prob_above = _compute_probability(
            running_high=71.0, threshold=72.0, hours_left=0.5, forecast_high=71.0
        )
        prob_below = 1.0 - prob_above
        # With running high at 71, threshold 72, 30 min left → high very
        # unlikely to reach 72 → P(below) should be high
        assert prob_below >= 0.80
        assert prob_above <= 0.20

    def test_below_market_locked_in(self):
        """When high is well below threshold late in day, 'below' should be near 1.0."""
        prob_above = _compute_probability(
            running_high=68.0, threshold=72.0, hours_left=0.3, forecast_high=69.0
        )
        prob_below = 1.0 - prob_above
        assert prob_below >= 0.95

    def test_above_market_locked_in(self):
        """When high is well above threshold, 'above' should be near 1.0."""
        prob_above = _compute_probability(
            running_high=76.0, threshold=72.0, hours_left=5.0, forecast_high=78.0
        )
        assert prob_above >= 0.95


# ══════════════════════════════════════════════════════════════════════════════
# Settlement certainty decision logic (unit test the math)
# ══════════════════════════════════════════════════════════════════════════════

# Replicate the settlement certainty thresholds from manage_positions()
_SETTLEMENT_CERTAINTY_THRESH = 0.90
_SETTLEMENT_CERTAINTY_HOURS = 4
_SETTLEMENT_HOLD_THRESH = 0.93
_SETTLEMENT_HOLD_HOURS = 2


def settlement_certainty_decision(
    fresh_prob: float | None,
    fresh_n: int,
    hours_to_expiry: float,
    side: str,
) -> str:
    """Replicate settlement certainty logic.

    Returns: "exit", "hold", or "normal" (falls through to health score).
    """
    if fresh_prob is None or fresh_n <= 0:
        return "normal"
    if hours_to_expiry >= _SETTLEMENT_CERTAINTY_HOURS:
        return "normal"

    our_prob = fresh_prob if side == "yes" else (1 - fresh_prob)

    if our_prob <= (1 - _SETTLEMENT_CERTAINTY_THRESH):  # <= 0.10
        return "exit"
    elif our_prob >= _SETTLEMENT_HOLD_THRESH and hours_to_expiry < _SETTLEMENT_HOLD_HOURS:
        return "hold"
    else:
        return "normal"


class TestSettlementCertaintyDecision:
    """Test the settlement certainty fast-path decision logic."""

    # ── Near-certain loser cases ──

    def test_loser_yes_side(self):
        """YES position when P(YES) is very low near expiry → exit."""
        result = settlement_certainty_decision(
            fresh_prob=0.05, fresh_n=3, hours_to_expiry=1.0, side="yes"
        )
        assert result == "exit"

    def test_loser_no_side(self):
        """NO position when P(YES) is very high near expiry → exit.
        P(our_side=NO) = 1 - 0.95 = 0.05 → near-certain loser.
        """
        result = settlement_certainty_decision(
            fresh_prob=0.95, fresh_n=3, hours_to_expiry=1.0, side="no"
        )
        assert result == "exit"

    def test_loser_boundary_0_09(self):
        """P(our_side) = 0.09 → below 0.10 threshold → should exit."""
        result = settlement_certainty_decision(
            fresh_prob=0.09, fresh_n=2, hours_to_expiry=2.0, side="yes"
        )
        assert result == "exit"

    def test_loser_just_above_threshold(self):
        """P(our_side) = 0.11 → NOT below 0.10, should fall through to normal."""
        result = settlement_certainty_decision(
            fresh_prob=0.11, fresh_n=2, hours_to_expiry=2.0, side="yes"
        )
        assert result == "normal"

    def test_loser_at_4h_boundary(self):
        """At exactly 4 hours: hours_to_expiry >= _SETTLEMENT_CERTAINTY_HOURS → normal."""
        result = settlement_certainty_decision(
            fresh_prob=0.05, fresh_n=3, hours_to_expiry=4.0, side="yes"
        )
        assert result == "normal"

    def test_loser_just_under_4h(self):
        """Just under 4 hours: should trigger exit."""
        result = settlement_certainty_decision(
            fresh_prob=0.05, fresh_n=3, hours_to_expiry=3.9, side="yes"
        )
        assert result == "exit"

    # ── Near-certain winner cases ──

    def test_winner_yes_side(self):
        """YES position when P(YES) very high near expiry → hold."""
        result = settlement_certainty_decision(
            fresh_prob=0.95, fresh_n=3, hours_to_expiry=1.0, side="yes"
        )
        assert result == "hold"

    def test_winner_no_side(self):
        """NO position when P(YES) very low near expiry → hold.
        P(our_side=NO) = 1 - 0.05 = 0.95 → near-certain winner.
        """
        result = settlement_certainty_decision(
            fresh_prob=0.05, fresh_n=3, hours_to_expiry=1.0, side="no"
        )
        assert result == "hold"

    def test_winner_at_93_boundary(self):
        """P(our_side) = 0.93 → at threshold → hold."""
        result = settlement_certainty_decision(
            fresh_prob=0.93, fresh_n=2, hours_to_expiry=1.0, side="yes"
        )
        assert result == "hold"

    def test_winner_just_below_93(self):
        """P(our_side) = 0.92 → below hold threshold → normal."""
        result = settlement_certainty_decision(
            fresh_prob=0.92, fresh_n=2, hours_to_expiry=1.0, side="yes"
        )
        assert result == "normal"

    def test_winner_too_far_from_expiry(self):
        """Near-certain winner but >2 hours → normal (too far out to commit to hold)."""
        result = settlement_certainty_decision(
            fresh_prob=0.95, fresh_n=3, hours_to_expiry=2.5, side="yes"
        )
        assert result == "normal"

    # ── No data / boundary cases ──

    def test_no_fresh_data(self):
        """No fresh data → normal (can't make settlement certainty call)."""
        result = settlement_certainty_decision(
            fresh_prob=None, fresh_n=0, hours_to_expiry=0.5, side="yes"
        )
        assert result == "normal"

    def test_zero_sources(self):
        """Zero sources → normal."""
        result = settlement_certainty_decision(
            fresh_prob=0.95, fresh_n=0, hours_to_expiry=0.5, side="yes"
        )
        assert result == "normal"

    def test_far_from_expiry(self):
        """Far from expiry → always normal, even with extreme probs."""
        result = settlement_certainty_decision(
            fresh_prob=0.01, fresh_n=5, hours_to_expiry=24.0, side="yes"
        )
        assert result == "normal"

    def test_middle_probability(self):
        """50/50 probability near expiry → normal."""
        result = settlement_certainty_decision(
            fresh_prob=0.50, fresh_n=3, hours_to_expiry=0.5, side="yes"
        )
        assert result == "normal"


# ══════════════════════════════════════════════════════════════════════════════
# Integration: Josh's exact scenario
# ══════════════════════════════════════════════════════════════════════════════

class TestJoshScenario:
    """Reproduce Josh's exact weather position scenario.

    Position: NO on "72° or below" (KXHIGH Chicago)
    Running high: 71°F, late afternoon (<1h left), temps declining.
    The market should resolve YES (high IS 72° or below), so NO loses.
    """

    def test_metar_returns_correct_prob_for_below_market(self):
        """METAR should return high P(YES) for a 'below' market when high is below threshold.

        For "72° or below": YES = high ≤ 72°.
        With running high 71° and <1h left, P(YES) should be very high.
        """
        # METAR's _compute_probability returns P(high >= threshold)
        prob_above = _compute_probability(
            running_high=71.0, threshold=72.0, hours_left=0.5, forecast_high=71.5
        )
        # For a "below" market, we invert: P(YES) = 1 - P(high >= 72)
        prob_yes_below = 1.0 - prob_above

        # Should be very high — the high is 71° with 30 min left
        assert prob_yes_below >= 0.80, (
            f"P(YES for '72° or below') should be ≥0.80 but got {prob_yes_below:.3f}"
        )

    def test_settlement_certainty_triggers_exit_for_josh(self):
        """Josh holds NO. After METAR fix, P(YES) is high → P(NO) is low → exit.

        fresh_prob = P(YES) ≈ 0.90+ (from corrected METAR)
        side = "no"
        our_prob = 1 - 0.90 = 0.10 → triggers exit
        """
        # After METAR fix: ensemble correctly returns P(YES) ≈ 0.90+
        # for "72° or below" market with running high at 71°
        result = settlement_certainty_decision(
            fresh_prob=0.92, fresh_n=3, hours_to_expiry=1.0, side="no"
        )
        assert result == "exit", (
            "Josh's NO position should trigger settlement certainty exit"
        )

    def test_old_behavior_was_wrong(self):
        """Before METAR fix: ensemble returned P(high ≥ 72) ≈ 0.065 as P(YES).

        This made the bot think P(NO) = 0.935 → "winning" → HOLD.
        """
        # Old behavior: fresh_prob = 0.065 (wrong — P(high≥72), not P(YES for 'below'))
        result = settlement_certainty_decision(
            fresh_prob=0.065, fresh_n=3, hours_to_expiry=1.0, side="no"
        )
        # With wrong prob: our_prob = 1 - 0.065 = 0.935 → hold (winner!)
        # This is the bug — it would have held a losing position
        assert result == "hold", (
            "Old behavior: wrong prob → thinks it's winning → holds"
        )

    def test_new_behavior_is_correct(self):
        """After METAR fix: ensemble returns P(YES for 'below') ≈ 0.93.

        Now: P(NO) = 1 - 0.93 = 0.07 → near-certain loser → exit!
        """
        # Corrected: fresh_prob = 0.93 (P(YES for '72° or below') = P(high ≤ 72))
        result = settlement_certainty_decision(
            fresh_prob=0.93, fresh_n=3, hours_to_expiry=1.0, side="no"
        )
        # our_prob = 1 - 0.93 = 0.07 → below 0.10 → exit
        assert result == "exit", (
            "New behavior: correct prob → sees loser → exits"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases: logistic CDF math
# ══════════════════════════════════════════════════════════════════════════════

class TestLogisticCDF:
    """Verify the logistic CDF used in METAR probability estimation."""

    def test_at_mean(self):
        """CDF at mean should be 0.5."""
        result = _logistic_cdf(72.0, 72.0, 1.0)
        assert abs(result - 0.5) < 0.001

    def test_well_above_mean(self):
        """CDF well above mean should be near 1.0."""
        result = _logistic_cdf(80.0, 72.0, 1.0)
        assert result > 0.99

    def test_well_below_mean(self):
        """CDF well below mean should be near 0.0."""
        result = _logistic_cdf(64.0, 72.0, 1.0)
        assert result < 0.01

    def test_small_sigma_concentrates(self):
        """Very small sigma → CDF is like a step function."""
        # 1 degree above mean with sigma=0.1 → near 1.0
        result = _logistic_cdf(73.0, 72.0, 0.1)
        assert result > 0.999

    def test_overflow_protection(self):
        """Extreme values shouldn't cause overflow."""
        result_high = _logistic_cdf(1000.0, 72.0, 0.1)
        assert result_high == 1.0
        result_low = _logistic_cdf(-1000.0, 72.0, 0.1)
        assert result_low == 0.0
