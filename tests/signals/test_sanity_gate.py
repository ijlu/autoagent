"""Tests for the v2 ensemble sanity gate.

The gate excludes sources whose μ is more than 5°F off the METAR/HRRR
consensus. Discovered 2026-04-29 that Open-Meteo and NWS Point regularly
produce μ 5-15°F cold at low TTE while reporting σ=2.0°F — too tight for
that error magnitude. The combine drags cold; METAR+HRRR-alone Brier is
44% better at TTE ≤1h.

Pin behavior so a future change doesn't accidentally drop the gate or
loosen its threshold to the point where the cold-bias problem returns.
"""

from __future__ import annotations

import pytest

from bot.signals.weather_ensemble_v2 import _apply_sanity_gate, _SANITY_GATE_F
from bot.signals.weather_forecast import GaussianForecast


def _g(name, mu, sigma=1.5):
    return GaussianForecast(
        mean_f=mu, sigma_f=sigma, horizon_hours=1.0,
        source_name=name, source_tag=f"{name}:test",
    )


class TestSanityGate:
    def test_excludes_cold_outlier_when_metar_hrrr_anchor(self):
        # The Apr 29 KXHIGHNY ≤1h scenario: Open-Meteo at 60°F, others ≈68-74.
        gs = [
            _g("metar", 74.0),
            _g("hrrr", 68.0),
            _g("nws_point", 53.0),    # 18°F off anchor (71) → drop
            _g("weather", 60.0),      # 11°F off anchor → drop
        ]
        kept = _apply_sanity_gate(gs)
        names = sorted(g.source_name for g in kept)
        assert names == ["hrrr", "metar"], (
            f"Expected only METAR + HRRR survived, got {names}. "
            f"Gate threshold may have loosened."
        )

    def test_keeps_all_when_aligned(self):
        # Normal case: all sources within a couple degrees of each other.
        gs = [
            _g("metar", 73.0),
            _g("hrrr", 72.0),
            _g("nws_point", 71.0),
            _g("weather", 74.0),
        ]
        kept = _apply_sanity_gate(gs)
        assert len(kept) == 4

    def test_metar_always_kept_even_if_outlier(self):
        # Edge case: METAR itself is the outlier (e.g., end-of-day current
        # temp instead of running max). We still keep it because the
        # downstream H3 truncation can correct, and dropping METAR at
        # low TTE is an even bigger problem.
        gs = [
            _g("metar", 50.0),   # end-of-day reading, far from real peak
            _g("hrrr", 72.0),
            _g("nws_point", 73.0),
            _g("weather", 71.0),
        ]
        kept = _apply_sanity_gate(gs)
        assert any(g.source_name == "metar" for g in kept), (
            "METAR is the anchor source — must never be dropped by the "
            "sanity gate even when it's the outlier"
        )

    def test_no_anchor_falls_back_to_median(self):
        # When METAR and HRRR are both absent, use median of remaining sources.
        gs = [
            _g("nws_point", 70.0),
            _g("weather", 72.0),
            _g("madis", 50.0),    # outlier vs median 71
        ]
        kept = _apply_sanity_gate(gs)
        names = sorted(g.source_name for g in kept)
        assert "madis" not in names

    def test_no_action_with_few_sources(self):
        # Can't triangulate with ≤2 sources; keep them all.
        gs = [_g("metar", 50.0), _g("hrrr", 70.0)]
        kept = _apply_sanity_gate(gs)
        assert len(kept) == 2

    def test_threshold_pinned_at_5F(self):
        # If the threshold drifts wider, the cold-bias filter weakens.
        # If narrower, normal forecast disagreements get dropped.
        # 5°F is the value validated by the METAR+HRRR-only retro-replay.
        assert _SANITY_GATE_F == 5.0

    def test_just_within_threshold_kept(self):
        gs = [
            _g("metar", 70.0),
            _g("hrrr", 72.0),
            _g("nws_point", 75.9),  # 4.9°F off anchor 71 → kept
        ]
        kept = _apply_sanity_gate(gs)
        assert any(g.source_name == "nws_point" for g in kept)

    def test_just_beyond_threshold_dropped(self):
        gs = [
            _g("metar", 70.0),
            _g("hrrr", 72.0),
            _g("nws_point", 76.5),  # 5.5°F off anchor 71 → drop
        ]
        kept = _apply_sanity_gate(gs)
        assert not any(g.source_name == "nws_point" for g in kept)
