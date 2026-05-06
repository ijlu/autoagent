"""Tests for bot/core/sizing.py — Thompson sizing evaluator.

Pure-function surface: deterministic via seeded RNG, no fixtures.
"""
from __future__ import annotations

import random

import pytest

from bot.core.sizing import ThompsonSizeDecision, thompson_mm_size_multiplier


class TestInsufficientN:
    def test_empty_list_returns_zero(self):
        d = thompson_mm_size_multiplier([])
        assert d.multiplier == 0.0
        assert d.n == 0
        assert d.reason == "insufficient_n"

    def test_below_min_n_returns_zero(self):
        d = thompson_mm_size_multiplier([10.0, 10.0, 10.0])
        assert d.multiplier == 0.0
        assert d.reason == "insufficient_n"

    def test_at_min_n_samples(self):
        # Default min_n=5; exactly 5 constant rows triggers degenerate path.
        d = thompson_mm_size_multiplier([10.0] * 5)
        assert d.reason == "degenerate_variance"
        assert d.multiplier == 1.0  # 10/2=5 clamped to cap


class TestDegenerateVariance:
    def test_all_same_returns_clamped_mean(self):
        d = thompson_mm_size_multiplier(
            [4.0] * 10, target_edge_cents=2.0, cap_multiplier=1.0,
        )
        assert d.std_cents == 0.0
        assert d.mu_sample_cents == 4.0
        assert d.multiplier == 1.0  # 4/2 = 2.0, clamped to 1.0

    def test_all_zero_returns_zero(self):
        d = thompson_mm_size_multiplier([0.0] * 10)
        assert d.multiplier == 0.0
        assert d.reason == "degenerate_variance"

    def test_all_negative_clamped_to_zero(self):
        d = thompson_mm_size_multiplier([-3.0] * 10)
        assert d.multiplier == 0.0


class TestClamping:
    def test_caps_at_one(self):
        d = thompson_mm_size_multiplier(
            [100.0] * 20, target_edge_cents=2.0, cap_multiplier=1.0,
            rng=random.Random(0),
        )
        assert d.multiplier == 1.0

    def test_custom_cap(self):
        d = thompson_mm_size_multiplier(
            [20.0] * 20, target_edge_cents=2.0, cap_multiplier=2.5,
            rng=random.Random(0),
        )
        # Mean=20/2=10, clamped to cap=2.5
        assert d.multiplier == 2.5

    def test_floor_at_zero(self):
        # Seed that produces a negative mu_sample clamps to 0.
        rng = random.Random(0)
        d = thompson_mm_size_multiplier(
            [-5.0, -4.0, -6.0, -3.0, -7.0, -4.0, -5.0, -6.0, -5.0, -4.0],
            target_edge_cents=2.0, rng=rng,
        )
        assert d.multiplier == 0.0


class TestDiagnostics:
    def test_reports_n_and_mean(self):
        d = thompson_mm_size_multiplier(
            [1.0, 3.0, 5.0, 7.0, 9.0, 11.0], rng=random.Random(0),
        )
        assert d.n == 6
        assert d.mean_cents == pytest.approx(6.0)
        assert d.std_cents > 0

    def test_reason_on_success(self):
        d = thompson_mm_size_multiplier(
            [1.0, 3.0, 5.0, 7.0, 9.0, 11.0], rng=random.Random(0),
        )
        assert d.reason == "sampled"

    def test_se_shrinks_with_n(self):
        rng = random.Random(0)
        d_small = thompson_mm_size_multiplier(
            [5.0, 10.0, 15.0, 5.0, 10.0], rng=rng,
        )
        rng = random.Random(0)
        d_large = thompson_mm_size_multiplier(
            [5.0, 10.0, 15.0] * 20, rng=rng,
        )
        assert d_large.se_cents < d_small.se_cents


class TestDeterminismWithSeededRng:
    def test_same_seed_same_result(self):
        data = [1.0, 3.0, 5.0, 7.0, 9.0, 11.0]
        d1 = thompson_mm_size_multiplier(data, rng=random.Random(42))
        d2 = thompson_mm_size_multiplier(data, rng=random.Random(42))
        assert d1.multiplier == d2.multiplier
        assert d1.mu_sample_cents == d2.mu_sample_cents

    def test_different_seeds_different_draws(self):
        data = [1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0]
        draws = [
            thompson_mm_size_multiplier(data, rng=random.Random(i)).mu_sample_cents
            for i in range(20)
        ]
        assert len(set(draws)) > 10  # lots of distinct samples


class TestExplorationMagnitude:
    """Smoke test — under Thompson, the multiplier should vary across draws
    for a noisy series, concentrating near mean/target on average.
    """

    def test_many_draws_concentrate_near_mean(self):
        # Mean +5, std ~2 → posterior mean for multiplier ≈ 5/2 = 2.5, but
        # clamped to cap=1.0 on most draws.
        data = [3.0, 5.0, 7.0, 4.0, 6.0, 5.0, 5.0, 6.0, 4.0, 5.0,
                3.0, 5.0, 7.0, 4.0, 6.0]
        mults = [
            thompson_mm_size_multiplier(data, rng=random.Random(i)).multiplier
            for i in range(200)
        ]
        avg = sum(mults) / len(mults)
        # High mean relative to target + sizable n → should almost always cap.
        assert avg > 0.8
        assert max(mults) == 1.0

    def test_marginal_mean_produces_middle_multipliers(self):
        # Mean matches target exactly → expected multiplier ≈ 0.5 (clamped
        # draws around 1.0 offset by clamped-at-0 draws).
        data = [2.0] * 5 + [2.0] * 5  # degenerate → exactly 1.0
        d = thompson_mm_size_multiplier(data)
        assert d.reason == "degenerate_variance"
        assert d.multiplier == 1.0

        # Noisy around mean=2 with wide variance → expect broad spread.
        import statistics
        noisy = [0.0, 4.0] * 10  # mean=2, std≈2
        mults = [
            thompson_mm_size_multiplier(noisy, rng=random.Random(i)).multiplier
            for i in range(500)
        ]
        avg = sum(mults) / len(mults)
        # Mean 2, target 2, so posterior expectation ≈ 1.0 but SE shrinks
        # with n, so draws are tightly centered and most clamp near cap.
        assert 0.3 < avg <= 1.0
        # Spread: at minimum, not every draw caps — some sample below 1.
        assert min(mults) < 1.0


class TestMinNOverride:
    def test_smaller_min_n_allows_tiny_sample(self):
        d = thompson_mm_size_multiplier(
            [10.0, 10.0], min_n=2,
        )
        assert d.reason == "degenerate_variance"
        assert d.multiplier == 1.0

    def test_larger_min_n_holds_mid_sized_sample(self):
        d = thompson_mm_size_multiplier(
            [10.0] * 10, min_n=20,
        )
        assert d.multiplier == 0.0
        assert d.reason == "insufficient_n"


class TestTargetEdgeCentsScaling:
    def test_higher_target_lowers_multiplier(self):
        data = [5.0] * 10
        d1 = thompson_mm_size_multiplier(data, target_edge_cents=2.0)
        d2 = thompson_mm_size_multiplier(data, target_edge_cents=10.0)
        # target=2 → 5/2=2.5 clamped to 1; target=10 → 5/10=0.5
        assert d1.multiplier == 1.0
        assert d2.multiplier == pytest.approx(0.5)
