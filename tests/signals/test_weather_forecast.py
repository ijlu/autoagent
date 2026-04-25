"""Tests for bot.signals.weather_forecast — Gaussian temperature type + combiner.

These tests pin down the CDF math and the precision-weighted combine
behaviour so the v2 ensemble can rely on them without re-deriving.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from bot.signals.weather_forecast import (
    GaussianForecast,
    WeightedForecast,
    combine_gaussian,
    hours_until_settlement_end,
    probability_for_market,
)


# ══════════════════════════════════════════════════════════════════════
# GaussianForecast construction
# ══════════════════════════════════════════════════════════════════════


def test_gaussian_forecast_construction_valid():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.mean_f == 75.0
    assert f.sigma_f == 2.0
    assert f.horizon_hours == 6.0
    assert f.source_name == "hrrr"
    assert f.source_tag == ""


def test_gaussian_forecast_rejects_zero_sigma():
    with pytest.raises(ValueError):
        GaussianForecast(mean_f=75.0, sigma_f=0.0, horizon_hours=6.0, source_name="hrrr")


def test_gaussian_forecast_rejects_negative_sigma():
    with pytest.raises(ValueError):
        GaussianForecast(mean_f=75.0, sigma_f=-0.5, horizon_hours=6.0, source_name="hrrr")


def test_gaussian_forecast_rejects_nan_mean():
    with pytest.raises(ValueError):
        GaussianForecast(mean_f=float("nan"), sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")


def test_gaussian_forecast_rejects_empty_source_name():
    with pytest.raises(ValueError):
        GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="")


def test_gaussian_forecast_precision():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.precision == pytest.approx(0.25)


# ══════════════════════════════════════════════════════════════════════
# Probability projection (exact values against math.erf)
# ══════════════════════════════════════════════════════════════════════


def test_prob_above_at_mean_is_half():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.prob_above(75.0) == pytest.approx(0.5, abs=1e-9)


def test_prob_below_at_mean_is_half():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.prob_below(75.0) == pytest.approx(0.5, abs=1e-9)


def test_prob_above_one_sigma_up():
    """P(X > μ + σ) = 1 - Φ(1) ≈ 0.1587 for a Normal(μ, σ)."""
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.prob_above(77.0) == pytest.approx(0.15865525, abs=1e-6)


def test_prob_above_far_right_tail():
    """Five sigma above should be ~3e-7, never negative, never > 1."""
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    p = f.prob_above(85.0)
    assert 0 <= p <= 1e-5


def test_prob_between_symmetric_bracket():
    """[μ - σ, μ + σ] should capture ~68% of a Normal."""
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.prob_between(73.0, 77.0) == pytest.approx(0.6826895, abs=1e-6)


def test_prob_between_swapped_args():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.prob_between(77.0, 73.0) == f.prob_between(73.0, 77.0)


def test_prob_between_zero_width():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert f.prob_between(75.0, 75.0) == pytest.approx(0.0, abs=1e-12)


# ══════════════════════════════════════════════════════════════════════
# Adjustments
# ══════════════════════════════════════════════════════════════════════


def test_shifted_preserves_sigma_and_horizon():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    g = f.shifted(+1.5)
    assert g.mean_f == pytest.approx(76.5)
    assert g.sigma_f == 2.0
    assert g.horizon_hours == 6.0
    assert g.source_name == "hrrr"


def test_with_sigma_preserves_mean_and_horizon():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    g = f.with_sigma(3.5)
    assert g.mean_f == 75.0
    assert g.sigma_f == pytest.approx(3.5)
    assert g.horizon_hours == 6.0


# ══════════════════════════════════════════════════════════════════════
# probability_for_market
# ══════════════════════════════════════════════════════════════════════


def test_probability_for_market_threshold_above():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    p = probability_for_market(
        f, is_bracket=False, threshold_f=77.0, is_above=True
    )
    assert p == pytest.approx(0.15865525, abs=1e-6)


def test_probability_for_market_threshold_below():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    p = probability_for_market(
        f, is_bracket=False, threshold_f=77.0, is_above=False
    )
    assert p == pytest.approx(1 - 0.15865525, abs=1e-6)


def test_probability_for_market_bracket_centered():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    p = probability_for_market(
        f, is_bracket=True, bracket_lo_f=74.0, bracket_hi_f=76.0
    )
    # ±0.5σ → Φ(0.5) - Φ(-0.5) ≈ 0.3829
    assert p == pytest.approx(0.382924923, abs=1e-6)


def test_probability_for_market_clamps_low():
    """5σ above mean should clamp to the low bound, not round to 0."""
    f = GaussianForecast(mean_f=60.0, sigma_f=1.0, horizon_hours=1.0, source_name="metar")
    p = probability_for_market(
        f, is_bracket=False, threshold_f=80.0, is_above=True
    )
    assert p == pytest.approx(0.02, abs=1e-9)


def test_probability_for_market_clamps_high():
    f = GaussianForecast(mean_f=80.0, sigma_f=1.0, horizon_hours=1.0, source_name="metar")
    p = probability_for_market(
        f, is_bracket=False, threshold_f=60.0, is_above=True
    )
    assert p == pytest.approx(0.98, abs=1e-9)


def test_probability_for_market_bracket_requires_both_bounds():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    with pytest.raises(ValueError):
        probability_for_market(f, is_bracket=True, bracket_lo_f=74.0)


def test_probability_for_market_threshold_requires_threshold():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    with pytest.raises(ValueError):
        probability_for_market(f, is_bracket=False)


# ══════════════════════════════════════════════════════════════════════
# combine_gaussian
# ══════════════════════════════════════════════════════════════════════


def test_combine_empty_returns_none():
    assert combine_gaussian([]) is None


def test_combine_all_zero_weights_returns_none():
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    assert combine_gaussian([WeightedForecast(f, 0.0)]) is None


def test_combine_single_source_equals_itself():
    """With one source and weight 1, precision-weighted combine = that source's mean.

    Sigma: total_precision = 1 × 1/σ² = 1/σ², so combined_sigma = σ. ✓
    """
    f = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    combined = combine_gaussian([WeightedForecast(f, 1.0)])
    assert combined is not None
    assert combined.mean_f == pytest.approx(75.0)
    assert combined.sigma_f == pytest.approx(2.0)
    assert combined.source_name == "combined"


def test_combine_equal_sigma_equal_weight_averages_mean():
    """Two sources, same σ and weight: combined mean = average of means,
    combined σ = σ/√2."""
    a = GaussianForecast(mean_f=74.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    b = GaussianForecast(mean_f=76.0, sigma_f=2.0, horizon_hours=6.0, source_name="nbm")
    combined = combine_gaussian(
        [WeightedForecast(a, 1.0), WeightedForecast(b, 1.0)]
    )
    assert combined is not None
    assert combined.mean_f == pytest.approx(75.0)
    assert combined.sigma_f == pytest.approx(2.0 / math.sqrt(2.0), abs=1e-9)


def test_combine_higher_precision_source_dominates_mean():
    """Source with tiny σ should pull combined mean toward it."""
    tight = GaussianForecast(mean_f=74.0, sigma_f=0.5, horizon_hours=1.0, source_name="metar")
    loose = GaussianForecast(mean_f=80.0, sigma_f=4.0, horizon_hours=48.0, source_name="weather")
    combined = combine_gaussian(
        [WeightedForecast(tight, 1.0), WeightedForecast(loose, 1.0)]
    )
    assert combined is not None
    # precision_tight = 4, precision_loose = 1/16 = 0.0625
    # combined_mean = (4*74 + 0.0625*80) / (4.0625) ≈ 74.09
    assert combined.mean_f == pytest.approx(74.0923, abs=1e-3)


def test_combine_horizon_is_minimum():
    """Combined horizon should be the minimum of contributing horizons
    (nearest-in-time source drives learning stratification)."""
    a = GaussianForecast(mean_f=74.0, sigma_f=2.0, horizon_hours=2.0, source_name="metar")
    b = GaussianForecast(mean_f=76.0, sigma_f=2.0, horizon_hours=24.0, source_name="hrrr")
    combined = combine_gaussian(
        [WeightedForecast(a, 1.0), WeightedForecast(b, 1.0)]
    )
    assert combined is not None
    assert combined.horizon_hours == 2.0


def test_combine_skip_zero_weight():
    """Zero-weight inputs should be dropped entirely, not just de-emphasized."""
    a = GaussianForecast(mean_f=74.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    b = GaussianForecast(mean_f=80.0, sigma_f=2.0, horizon_hours=6.0, source_name="noise")
    combined = combine_gaussian(
        [WeightedForecast(a, 1.0), WeightedForecast(b, 0.0)]
    )
    assert combined is not None
    assert combined.mean_f == pytest.approx(74.0)


def test_combine_weight_affects_precision_not_mean_directly():
    """Doubling one source's weight doubles its precision contribution."""
    a = GaussianForecast(mean_f=74.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    b = GaussianForecast(mean_f=76.0, sigma_f=2.0, horizon_hours=6.0, source_name="nbm")
    w_equal = combine_gaussian(
        [WeightedForecast(a, 1.0), WeightedForecast(b, 1.0)]
    )
    w_skewed = combine_gaussian(
        [WeightedForecast(a, 2.0), WeightedForecast(b, 1.0)]
    )
    assert w_equal is not None and w_skewed is not None
    # Equal: mean = 75. Skewed: precision 2 vs 1 → mean = (2*74 + 1*76)/3 ≈ 74.67
    assert w_equal.mean_f == pytest.approx(75.0)
    assert w_skewed.mean_f == pytest.approx(74.6666, abs=1e-3)


def test_combine_correlation_discount_widens_sigma():
    """effective_n_fraction < 1 should widen combined sigma (less info)."""
    a = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    b = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="nbm")
    no_discount = combine_gaussian(
        [WeightedForecast(a, 1.0), WeightedForecast(b, 1.0)],
        effective_n_fraction=1.0,
    )
    with_discount = combine_gaussian(
        [WeightedForecast(a, 1.0), WeightedForecast(b, 1.0)],
        effective_n_fraction=0.5,
    )
    assert no_discount is not None and with_discount is not None
    assert with_discount.sigma_f > no_discount.sigma_f


def test_combine_prior_tightens_sigma():
    """A Bayesian prior adds precision — the combined sigma should not
    increase relative to the no-prior case. This pins down that the prior
    is additive in precision-space (not just a pull on the mean)."""
    a = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    no_prior = combine_gaussian([WeightedForecast(a, 1.0)])
    with_prior = combine_gaussian(
        [WeightedForecast(a, 1.0)],
        prior_mean_f=75.0,
        prior_sigma_f=5.0,
    )
    assert no_prior is not None and with_prior is not None
    # with_prior precision = 1/4 + 1/25 = 0.29, sigma = 1/√0.29 ≈ 1.857
    assert with_prior.sigma_f < no_prior.sigma_f
    assert with_prior.sigma_f == pytest.approx(1.857, abs=1e-3)


def test_combine_tight_prior_pulls_mean():
    """A tight prior far from the source mean should pull the combined mean."""
    a = GaussianForecast(mean_f=75.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr")
    # Prior at 70°F with σ=1.0: precision 1.0 vs source precision 0.25
    # combined_mean = (0.25*75 + 1.0*70) / 1.25 = 71
    combined = combine_gaussian(
        [WeightedForecast(a, 1.0)],
        prior_mean_f=70.0,
        prior_sigma_f=1.0,
    )
    assert combined is not None
    assert combined.mean_f == pytest.approx(71.0, abs=1e-6)


# ══════════════════════════════════════════════════════════════════════
# hours_until_settlement_end
# ══════════════════════════════════════════════════════════════════════


def test_horizon_full_day_at_midnight_lst():
    """At 00:00 LST on day 0, there are ~24h until 23:59:59 LST same day."""
    now_utc = datetime(2026, 4, 23, 5, 0, 0, tzinfo=timezone.utc)  # 00:00 EST
    h = hours_until_settlement_end(lst_offset_hours=-5, day_idx=0, now=now_utc)
    assert h == pytest.approx(24.0, abs=0.01)


def test_horizon_midday_today_is_about_twelve_hours():
    now_utc = datetime(2026, 4, 23, 17, 0, 0, tzinfo=timezone.utc)  # 12:00 EST
    h = hours_until_settlement_end(lst_offset_hours=-5, day_idx=0, now=now_utc)
    assert h == pytest.approx(12.0, abs=0.02)


def test_horizon_end_of_day_near_zero():
    now_utc = datetime(2026, 4, 24, 4, 59, 0, tzinfo=timezone.utc)  # 23:59 EST
    h = hours_until_settlement_end(lst_offset_hours=-5, day_idx=0, now=now_utc)
    assert 0 <= h <= 0.05


def test_horizon_tomorrow_adds_twentyfour():
    now_utc = datetime(2026, 4, 23, 17, 0, 0, tzinfo=timezone.utc)  # 12:00 EST today
    today = hours_until_settlement_end(lst_offset_hours=-5, day_idx=0, now=now_utc)
    tomorrow = hours_until_settlement_end(lst_offset_hours=-5, day_idx=1, now=now_utc)
    assert tomorrow - today == pytest.approx(24.0, abs=0.01)


def test_horizon_nonnegative_after_rollover():
    """After the settlement boundary has passed for day_idx=0, horizon
    clamps to 0 rather than going negative."""
    now_utc = datetime(2026, 4, 24, 5, 1, 0, tzinfo=timezone.utc)  # 00:01 EST next day
    h = hours_until_settlement_end(lst_offset_hours=-5, day_idx=-1, now=now_utc)
    assert h == 0.0


def test_horizon_respects_lst_offset():
    """Pacific (-8) at same UTC moment is 3 hours earlier in local time,
    so has 3 more hours to settlement close."""
    now_utc = datetime(2026, 4, 23, 17, 0, 0, tzinfo=timezone.utc)
    eastern = hours_until_settlement_end(lst_offset_hours=-5, day_idx=0, now=now_utc)
    pacific = hours_until_settlement_end(lst_offset_hours=-8, day_idx=0, now=now_utc)
    assert pacific - eastern == pytest.approx(3.0, abs=0.01)


# ══════════════════════════════════════════════════════════════════════
# combine preserves provenance
# ══════════════════════════════════════════════════════════════════════


def test_combine_preserves_source_tag_provenance():
    a = GaussianForecast(mean_f=74.0, sigma_f=2.0, horizon_hours=6.0, source_name="hrrr",
                         source_tag="hrrr:nyc_2026-04-23")
    b = GaussianForecast(mean_f=76.0, sigma_f=2.0, horizon_hours=6.0, source_name="nbm",
                         source_tag="nbm:nyc_2026-04-23")
    combined = combine_gaussian(
        [WeightedForecast(a, 1.0), WeightedForecast(b, 1.0)]
    )
    assert combined is not None
    assert "hrrr:nyc_2026-04-23" in combined.source_tag
    assert "nbm:nyc_2026-04-23" in combined.source_tag
