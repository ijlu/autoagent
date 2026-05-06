"""Tests for synthetic exit pricing and urgency logic.

Validates that:
- Urgency 0 (patient) uses post_only=True at non-crossing price
- Urgency 1+ (moderate/aggressive) does NOT use post_only (would be rejected)
- Price computation: base = 100 - bid, plus urgency adjustment
- Exit escalation: repeated patient exits escalate urgency
"""

from __future__ import annotations

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Synthetic exit price computation (unit test the math, not the API call)
# ══════════════════════════════════════════════════════════════════════════════

def compute_exit_price(side: str, yes_bid_cents: int, no_bid_cents: int,
                       exit_urgency: int) -> tuple[str, int, bool]:
    """Replicate the exit price logic from manage_positions().

    Returns (opp_side, price_cents, use_post_only).
    """
    opp_side = "no" if side == "yes" else "yes"

    if side == "yes":
        base_no_price = max(1, 100 - yes_bid_cents)
        if exit_urgency >= 2:
            opp_price_cents = min(99, base_no_price + 3)
        elif exit_urgency == 1:
            opp_price_cents = min(99, base_no_price + 1)
        else:
            opp_price_cents = min(99, base_no_price)
    else:
        base_yes_price = max(1, 100 - no_bid_cents)
        if exit_urgency >= 2:
            opp_price_cents = min(99, base_yes_price + 3)
        elif exit_urgency == 1:
            opp_price_cents = min(99, base_yes_price + 1)
        else:
            opp_price_cents = min(99, base_yes_price)

    use_post_only = (exit_urgency == 0)
    return opp_side, opp_price_cents, use_post_only


class TestExitPriceComputation:
    def test_urgency_0_yes_position(self):
        """Patient exit of YES position: buy NO at ask (non-crossing), post_only."""
        opp_side, price, post_only = compute_exit_price("yes", 47, 53, 0)
        assert opp_side == "no"
        assert price == 53  # 100 - 47 = 53 = NO ask
        assert post_only is True

    def test_urgency_1_yes_position(self):
        """Moderate exit of YES position: buy NO at ask+1, NO post_only."""
        opp_side, price, post_only = compute_exit_price("yes", 47, 53, 1)
        assert opp_side == "no"
        assert price == 54  # ask + 1 (crosses spread)
        assert post_only is False

    def test_urgency_2_yes_position(self):
        """Aggressive exit of YES position: buy NO at ask+3, NO post_only."""
        opp_side, price, post_only = compute_exit_price("yes", 47, 53, 2)
        assert opp_side == "no"
        assert price == 56  # ask + 3 (crosses spread deeply)
        assert post_only is False

    def test_urgency_0_no_position(self):
        """Patient exit of NO position: buy YES at ask (non-crossing), post_only."""
        opp_side, price, post_only = compute_exit_price("no", 47, 53, 0)
        assert opp_side == "yes"
        assert price == 47  # 100 - 53 = 47 = YES ask
        assert post_only is True

    def test_urgency_1_no_position(self):
        opp_side, price, post_only = compute_exit_price("no", 47, 53, 1)
        assert opp_side == "yes"
        assert price == 48  # ask + 1
        assert post_only is False

    def test_urgency_2_no_position(self):
        opp_side, price, post_only = compute_exit_price("no", 47, 53, 2)
        assert opp_side == "yes"
        assert price == 50  # ask + 3
        assert post_only is False

    def test_extreme_yes_bid_high(self):
        """YES bid at 97: NO price = 3, urgency 2 = 6."""
        opp_side, price, post_only = compute_exit_price("yes", 97, 3, 2)
        assert price == 6  # 3 + 3

    def test_price_capped_at_99(self):
        """Price cannot exceed 99."""
        opp_side, price, post_only = compute_exit_price("yes", 1, 99, 2)
        assert price == 99  # min(99, 99+3) = 99

    def test_price_floor_at_1(self):
        """Price cannot be below 1."""
        opp_side, price, post_only = compute_exit_price("yes", 100, 0, 0)
        # 100 - 100 = 0, max(1, 0) = 1
        assert price == 1


# ══════════════════════════════════════════════════════════════════════════════
# Post-only correctness: the key regression test
# ══════════════════════════════════════════════════════════════════════════════

class TestPostOnlyRegression:
    """The critical bug: urgency>0 exits must NOT use post_only.

    With post_only=True, any order that would immediately fill (i.e., crosses
    the spread) is rejected by Kalshi. Since urgency>0 exit prices are
    intentionally aggressive (above the ask), post_only would cause rejection.
    """

    def test_urgency_0_uses_post_only(self):
        _, _, post_only = compute_exit_price("yes", 50, 50, 0)
        assert post_only is True, "Patient exits should use post_only for maker fee"

    def test_urgency_1_no_post_only(self):
        _, _, post_only = compute_exit_price("yes", 50, 50, 1)
        assert post_only is False, "Moderate exits must NOT use post_only (would be rejected)"

    def test_urgency_2_no_post_only(self):
        _, _, post_only = compute_exit_price("yes", 50, 50, 2)
        assert post_only is False, "Aggressive exits must NOT use post_only (would be rejected)"

    def test_urgency_3_no_post_only(self):
        """Edge case: urgency > 2 should also not use post_only."""
        _, _, post_only = compute_exit_price("yes", 50, 50, 3)
        assert post_only is False


# ══════════════════════════════════════════════════════════════════════════════
# Exit escalation logic
# ══════════════════════════════════════════════════════════════════════════════

def escalate_urgency(current_urgency: int, attempt_count: int) -> int:
    """Replicate the exit escalation logic from manage_positions()."""
    if current_urgency == 0 and attempt_count >= 2:
        return 1
    if current_urgency == 1 and attempt_count >= 4:
        return 2
    return current_urgency


class TestExitEscalation:
    def test_first_attempt_no_escalation(self):
        assert escalate_urgency(0, 1) == 0

    def test_second_attempt_escalates_to_moderate(self):
        assert escalate_urgency(0, 2) == 1

    def test_third_attempt_stays_moderate(self):
        assert escalate_urgency(1, 3) == 1

    def test_fourth_attempt_escalates_to_aggressive(self):
        assert escalate_urgency(1, 4) == 2

    def test_already_aggressive_stays_aggressive(self):
        assert escalate_urgency(2, 5) == 2
