"""Tests for the per-city LST+stability post-peak detector (Phase 3e).

The METAR fast-path arms when ``is_post_peak_safe(series, lst_hour,
stability_hours)`` returns True. This encodes Josh Lu's hypothesis
(2026-05-05) that later LST hours need less stability proof because
solar heating has decayed enough to make late-day spikes implausible.

Per-city rules derived from ~100 days/city METAR backfill — see
reports/PHASE_3E_STABILITY_VALIDATION_2026-05-05.md.
"""

from __future__ import annotations

import pytest

from bot.learning.cross_bracket_lst_gate import (
    POST_PEAK_RULE_BY_SERIES,
    is_post_peak_safe,
)


class TestRuleStructure:
    def test_all_six_cities_have_rules(self):
        for series in (
            "KXHIGHNY", "KXHIGHLAX", "KXHIGHCHI",
            "KXHIGHAUS", "KXHIGHMIA", "KXHIGHDEN",
        ):
            assert series in POST_PEAK_RULE_BY_SERIES, f"{series} missing"

    def test_rules_have_required_fields(self):
        for series, rule in POST_PEAK_RULE_BY_SERIES.items():
            assert "always_arm_lst_hour" in rule
            assert "k_required_before" in rule
            assert "min_lst_for_k" in rule
            assert 0 <= rule["always_arm_lst_hour"] <= 23
            assert rule["k_required_before"] >= 0
            assert rule["min_lst_for_k"] < rule["always_arm_lst_hour"], (
                f"{series}: min_lst_for_k must be below always_arm_lst_hour"
            )


class TestNyRule:
    """NY: always_arm@17, else K>=3 from LST 14."""

    def test_arms_at_17_with_zero_stability(self):
        assert is_post_peak_safe("KXHIGHNY", lst_hour=17, stability_hours=0) is True

    def test_arms_above_17_regardless(self):
        for h in range(17, 24):
            assert is_post_peak_safe("KXHIGHNY", lst_hour=h, stability_hours=0) is True

    def test_does_not_arm_below_min_lst_even_with_stability(self):
        # 2026-05-05 cross-season tightening: NY min_lst_for_k 14→15
        # because K=3 at LST 14 has 8% summer (heat-wave) risk. So at
        # LST 14 fast-path can't fire regardless of stability.
        assert is_post_peak_safe("KXHIGHNY", lst_hour=14, stability_hours=10) is False
        assert is_post_peak_safe("KXHIGHNY", lst_hour=13, stability_hours=10) is False

    def test_arms_at_lst_15_with_3h_stability(self):
        # NY rule post-tightening: K≥3 at LST 15-16; always-arm at 17+.
        assert is_post_peak_safe("KXHIGHNY", lst_hour=15, stability_hours=3) is True
        assert is_post_peak_safe("KXHIGHNY", lst_hour=15, stability_hours=2) is False

    def test_arms_at_lst_16_with_3h_stability(self):
        assert is_post_peak_safe("KXHIGHNY", lst_hour=16, stability_hours=3) is True


class TestLaxRule:
    """LAX: always_arm@14, else K>=2 from LST 13. Marine layer = sharp
    early peak. K=1 raised to K=2 after Santa Ana fall 2024 data
    showed 7% risk at K=1@LST13."""

    def test_arms_at_lst_14(self):
        assert is_post_peak_safe("KXHIGHLAX", lst_hour=14, stability_hours=0) is True

    def test_arms_at_lst_13_with_2h(self):
        assert is_post_peak_safe("KXHIGHLAX", lst_hour=13, stability_hours=2) is True

    def test_does_not_arm_at_lst_13_with_1h(self):
        # Tightened from K=1 → K=2 after Santa Ana fall 2024 validation
        assert is_post_peak_safe("KXHIGHLAX", lst_hour=13, stability_hours=1) is False
        assert is_post_peak_safe("KXHIGHLAX", lst_hour=13, stability_hours=0) is False


class TestAusRule:
    """AUS: always_arm@16, else K>=1 from LST 15. Texas convective afternoons."""

    def test_arms_at_lst_15_with_1h(self):
        assert is_post_peak_safe("KXHIGHAUS", lst_hour=15, stability_hours=1) is True

    def test_does_not_arm_at_lst_14_even_with_high_K(self):
        # AUS pre-LST 15 has 80% K=0 chance of new high — too risky
        assert is_post_peak_safe("KXHIGHAUS", lst_hour=14, stability_hours=10) is False


class TestMiaRule:
    """MIA: always_arm@16, K>=3 from LST 13. Convective + marine.
    Tightened from K=2 → K=3 after summer 2024 data showed 8% risk
    at K=2@LST15 (afternoon thunderstorm regime)."""

    def test_arms_at_lst_13_with_3h(self):
        assert is_post_peak_safe("KXHIGHMIA", lst_hour=13, stability_hours=3) is True

    def test_does_not_arm_at_lst_13_with_2h(self):
        # Tightened from K=2 → K=3 — summer afternoon thunderstorm risk.
        assert is_post_peak_safe("KXHIGHMIA", lst_hour=13, stability_hours=2) is False


class TestUnknownSeries:
    def test_unknown_series_falls_back_to_lst_18(self):
        # Conservative fallback for unmapped series
        assert is_post_peak_safe("KXMADEUP", lst_hour=18, stability_hours=0) is True
        assert is_post_peak_safe("KXMADEUP", lst_hour=17, stability_hours=10) is False
