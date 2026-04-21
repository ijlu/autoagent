"""Tests for the edge-decay exit evaluator.

Pure-function surface makes this trivial — no mocks, no fixtures,
just deterministic inputs and asserted triggers.
"""
from __future__ import annotations

import pytest

from bot.core.exit_model import evaluate_edge_decay_exit


def _call(**overrides):
    """Build a call with benign defaults and targeted overrides.

    Defaults represent a healthy position: entered with +10¢ edge, still
    holds +8¢, 6 hours to expiry, held 2 hours, trend flat. None of the
    four rules should fire.
    """
    base = dict(
        entry_edge=0.10,
        remaining_edge=0.08,
        hours_to_expiry=6.0,
        hours_held=2.0,
        trend_score=0.0,
    )
    base.update(overrides)
    return evaluate_edge_decay_exit(**base)


class TestAbstention:
    def test_abstain_when_entry_edge_is_none(self):
        d = _call(entry_edge=None, remaining_edge=-0.5)  # would otherwise flip
        assert d.trigger is None
        assert "no_entry_edge_anchor" in d.detail

    def test_abstain_when_entry_edge_is_zero(self):
        d = _call(entry_edge=0.0, remaining_edge=-0.5)
        assert d.trigger is None

    def test_abstain_when_entry_edge_is_negative(self):
        # Shouldn't happen in practice — guard against it anyway.
        d = _call(entry_edge=-0.05, remaining_edge=-0.10)
        assert d.trigger is None

    def test_no_rule_fires_on_healthy_position(self):
        d = _call()
        assert d.trigger is None
        assert d.detail == ""


class TestEdgeFlipped:
    def test_negative_remaining_edge_flips(self):
        d = _call(remaining_edge=-0.01)
        assert d.trigger == "edge_flipped"

    def test_large_negative_flips(self):
        d = _call(entry_edge=0.12, remaining_edge=-0.20)
        assert d.trigger == "edge_flipped"
        assert "thesis inverted" in d.detail

    def test_exactly_zero_does_not_flip(self):
        # Strict < 0; zero edge falls through to other rules.
        d = _call(entry_edge=0.10, remaining_edge=0.0)
        # 0 < 0.033 retention floor → decayed, not flipped
        assert d.trigger == "edge_decayed"

    def test_flip_takes_precedence_over_time_backstop(self):
        d = _call(remaining_edge=-0.005, hours_to_expiry=0.1,
                  hours_held=0.1)
        assert d.trigger == "edge_flipped"


class TestEdgeDecayed:
    def test_decayed_below_floor_fires(self):
        # entry=0.10, floor=0.033, remaining=0.02 < 0.033
        d = _call(entry_edge=0.10, remaining_edge=0.02)
        assert d.trigger == "edge_decayed"
        assert "floor=33%" in d.detail

    def test_at_exact_floor_does_not_fire(self):
        # Strict < floor.
        d = _call(entry_edge=0.10, remaining_edge=0.0333)  # ≥ 0.033
        assert d.trigger is None

    def test_custom_decay_ratio_tightens(self):
        # With decay_ratio=0.5, floor=0.05 — remaining=0.04 triggers.
        d = _call(entry_edge=0.10, remaining_edge=0.04, decay_ratio=0.5)
        assert d.trigger == "edge_decayed"

    def test_custom_decay_ratio_loosens(self):
        # With decay_ratio=0.1, floor=0.01 — remaining=0.02 survives.
        d = _call(entry_edge=0.10, remaining_edge=0.02, decay_ratio=0.1)
        assert d.trigger is None


class TestTimeBackstop:
    def test_near_expiry_no_conviction_fires(self):
        d = _call(entry_edge=0.10, remaining_edge=0.015,
                  hours_to_expiry=0.1)
        # 0.015 >= floor=0.033? No — so edge_decayed fires first.
        # Rewrite to land in time_backstop window:
        d = _call(entry_edge=0.05, remaining_edge=0.019,
                  hours_to_expiry=0.1)
        # floor = 0.05 * 0.33 = 0.0165; 0.019 > 0.0165 → not decayed.
        # |0.019| < 0.02 AND 0.1 < 0.25 → time_backstop.
        assert d.trigger == "time_backstop"
        assert "no conviction" in d.detail

    def test_near_expiry_with_conviction_holds(self):
        d = _call(entry_edge=0.05, remaining_edge=0.04,
                  hours_to_expiry=0.1)
        # floor = 0.0165; 0.04 > 0.0165 → not decayed.
        # |0.04| >= 0.02 → time_backstop does not fire.
        assert d.trigger is None

    def test_far_from_expiry_no_conviction_holds(self):
        d = _call(entry_edge=0.05, remaining_edge=0.019,
                  hours_to_expiry=5.0)
        # Not near expiry → time_backstop does not fire.
        assert d.trigger is None

    def test_custom_thresholds(self):
        d = _call(entry_edge=0.10, remaining_edge=0.05,
                  hours_to_expiry=1.0, time_backstop_hours=2.0,
                  time_backstop_edge_abs=0.1)
        # |0.05| < 0.1 AND 1.0 < 2.0 → fires.
        # But need to clear the decay floor first: floor = 0.10 * 0.33 = 0.033.
        # 0.05 > 0.033 → not decayed. Time backstop fires.
        assert d.trigger == "time_backstop"


class TestStaleHold:
    def test_stale_with_flat_trend_fires(self):
        d = _call(entry_edge=0.10, remaining_edge=0.08,
                  hours_held=30.0, trend_score=0.0)
        assert d.trigger == "stale_hold_backstop"
        assert "not improving" in d.detail

    def test_stale_with_negative_trend_fires(self):
        d = _call(entry_edge=0.10, remaining_edge=0.08,
                  hours_held=30.0, trend_score=-0.4)
        assert d.trigger == "stale_hold_backstop"

    def test_stale_with_positive_trend_holds(self):
        d = _call(entry_edge=0.10, remaining_edge=0.08,
                  hours_held=30.0, trend_score=0.3)
        assert d.trigger is None

    def test_fresh_hold_does_not_fire(self):
        d = _call(entry_edge=0.10, remaining_edge=0.08,
                  hours_held=10.0, trend_score=-0.5)
        assert d.trigger is None

    def test_custom_stale_hours(self):
        d = _call(entry_edge=0.10, remaining_edge=0.08,
                  hours_held=5.0, trend_score=0.0,
                  stale_hold_hours=4.0)
        assert d.trigger == "stale_hold_backstop"


class TestRuleOrder:
    """First-match-wins: verify priority when multiple rules would fire."""

    def test_flipped_beats_decayed(self):
        d = _call(entry_edge=0.10, remaining_edge=-0.005)
        # Negative so flipped; also below floor so decayed would fire.
        assert d.trigger == "edge_flipped"

    def test_decayed_beats_time_backstop(self):
        d = _call(entry_edge=0.10, remaining_edge=0.01,
                  hours_to_expiry=0.1)
        # 0.01 < floor=0.033 → decayed. Also |0.01| < 0.02 near expiry.
        assert d.trigger == "edge_decayed"

    def test_decayed_beats_stale_hold(self):
        d = _call(entry_edge=0.10, remaining_edge=0.01,
                  hours_held=30.0, trend_score=-0.5)
        assert d.trigger == "edge_decayed"

    def test_time_backstop_beats_stale_hold(self):
        d = _call(entry_edge=0.10, remaining_edge=0.05,
                  hours_to_expiry=0.1, hours_held=30.0,
                  trend_score=-0.5)
        # 0.05 > floor=0.033 → not decayed.
        # |0.05| < ... wait default is 0.02, 0.05 > 0.02 → time_backstop also no.
        # Use smaller edge:
        d = _call(entry_edge=0.10, remaining_edge=0.035,
                  hours_to_expiry=0.1, hours_held=30.0,
                  trend_score=-0.5)
        # 0.035 > 0.033 → not decayed.
        # |0.035| > 0.02 → time_backstop no.
        # Stale fires.
        assert d.trigger == "stale_hold_backstop"

        # Now land in time_backstop window:
        d = _call(entry_edge=0.05, remaining_edge=0.019,
                  hours_to_expiry=0.1, hours_held=30.0,
                  trend_score=-0.5)
        # floor=0.0165; 0.019 > 0.0165 → not decayed.
        # |0.019| < 0.02 AND 0.1 < 0.25 → time_backstop.
        assert d.trigger == "time_backstop"


class TestDetailFormatting:
    def test_flipped_detail_contains_edges(self):
        d = _call(entry_edge=0.123, remaining_edge=-0.045)
        assert "+0.123" in d.detail
        assert "-0.045" in d.detail

    def test_decayed_detail_contains_floor(self):
        d = _call(entry_edge=0.10, remaining_edge=0.02, decay_ratio=0.33)
        assert "+0.033" in d.detail  # floor display
        assert "33%" in d.detail
