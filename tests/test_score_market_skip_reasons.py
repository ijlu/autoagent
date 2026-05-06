"""Tests for the 2026-04-22 score_market observability refactor.

``score_market`` now returns a **10-tuple** whose trailing element is a
``skip_reason`` string. For a successful score the reason is ``""``; for
each of the six EMPTY exits it's one of the ``SKIP_*`` constants so the
cycle funnel can partition ``score_zero`` into actionable buckets.

These tests pin the return shape and the reason-per-gate mapping so the
partitioning can't silently regress. The gate logic itself is covered
indirectly — we only care here that the correct SKIP_* is returned when
a specific gate trips.

Note: ``event_driven``, ``cross_market``, and ``near_resolution`` are
disabled via ``disabled_strategies`` so they don't accidentally fire on
the fixture market and mask a skip-reason we're trying to assert.
"""

from __future__ import annotations

import pytest

from bot.scoring import market_scorer as ms
from bot.scoring.market_scorer import (
    SKIP_EDGE_BELOW_THRESHOLD,
    SKIP_NO_ENSEMBLE,
    SKIP_NO_STRATEGY_FIRED,
    SKIP_PRICE_BOUNDS,
    SKIP_SPREAD_ZERO,
    SKIP_VOLUME,
    score_market,
)


# Disable every non-info_edge strategy so they can't fire on the fixture
# and mask the skip_reason we're trying to assert.
_DISABLE_OTHERS = {"event_driven", "cross_market", "near_resolution"}


def _mkt(**overrides):
    """Baseline "valid" market — caller mutates one field per test."""
    base = {
        "ticker": "KXTEST-26APR-T1",
        "title": "Test market",
        "yes_ask": 0.50,
        "yes_bid": 0.47,   # spread = 0.03, < 0.08 gate
        "no_ask": 0.53,
        "no_bid": 0.50,
        "volume": 1000,
    }
    base.update(overrides)
    return base


def _patch_ensemble(monkeypatch, prob, n_sources=3, source_label="test"):
    """Pin ``get_independent_estimate`` to a controlled value.

    ``score_market`` imports the symbol at module scope, so patch the
    reference *inside* ``bot.scoring.market_scorer`` — patching the
    original in ``bot.signals.ensemble`` wouldn't take effect.
    """
    def fake(*_a, **_kw):
        return (prob, source_label, n_sources)
    monkeypatch.setattr(ms, "get_independent_estimate", fake)


# ---------------------------------------------------------------------------
# Shape pin
# ---------------------------------------------------------------------------

class TestReturnShape:
    def test_return_shape_is_10_tuple_on_skip(self, monkeypatch):
        _patch_ensemble(monkeypatch, 0.5)
        out = score_market(_mkt(yes_ask=0.05),
                           disabled_strategies=_DISABLE_OTHERS)
        assert isinstance(out, tuple)
        assert len(out) == 10, f"expected 10-tuple, got len={len(out)}: {out}"

    def test_return_shape_is_10_tuple_on_success(self, monkeypatch):
        # Strong edge → info_edge fires → success path.
        _patch_ensemble(monkeypatch, 0.80, n_sources=3)
        out = score_market(
            _mkt(yes_ask=0.50, yes_bid=0.47),
            disabled_strategies=_DISABLE_OTHERS,
        )
        assert len(out) == 10
        assert out[0] > 0, f"expected non-zero score, got {out}"
        # On success the skip_reason must be empty.
        assert out[9] == ""


# ---------------------------------------------------------------------------
# One test per SKIP_* bucket
# ---------------------------------------------------------------------------

class TestSkipReasons:
    def test_skip_price_bounds_low(self, monkeypatch):
        _patch_ensemble(monkeypatch, 0.5)
        out = score_market(_mkt(yes_ask=0.05),
                           disabled_strategies=_DISABLE_OTHERS)
        assert out[0] == 0
        assert out[9] == SKIP_PRICE_BOUNDS

    def test_skip_price_bounds_high(self, monkeypatch):
        _patch_ensemble(monkeypatch, 0.5)
        out = score_market(_mkt(yes_ask=0.95, yes_bid=0.92),
                           disabled_strategies=_DISABLE_OTHERS)
        assert out[0] == 0
        assert out[9] == SKIP_PRICE_BOUNDS

    def test_skip_volume(self, monkeypatch):
        _patch_ensemble(monkeypatch, 0.5)
        out = score_market(_mkt(volume=30),
                           disabled_strategies=_DISABLE_OTHERS)
        assert out[0] == 0
        assert out[9] == SKIP_VOLUME

    def test_skip_spread_zero(self, monkeypatch):
        # yes_ask == yes_bid → spread <= 0.
        _patch_ensemble(monkeypatch, 0.5)
        out = score_market(_mkt(yes_ask=0.50, yes_bid=0.50),
                           disabled_strategies=_DISABLE_OTHERS)
        assert out[0] == 0
        assert out[9] == SKIP_SPREAD_ZERO

    def test_skip_no_ensemble_none_prob(self, monkeypatch):
        # Ensemble explicitly returns None → no data.
        _patch_ensemble(monkeypatch, None, n_sources=0, source_label="")
        out = score_market(_mkt(),
                           disabled_strategies=_DISABLE_OTHERS)
        assert out[0] == 0
        assert out[9] == SKIP_NO_ENSEMBLE

    def test_skip_no_ensemble_zero_sources(self, monkeypatch):
        # Ensemble has a prob but 0 sources → also treated as no data.
        _patch_ensemble(monkeypatch, 0.5, n_sources=0)
        out = score_market(_mkt(),
                           disabled_strategies=_DISABLE_OTHERS)
        assert out[0] == 0
        assert out[9] == SKIP_NO_ENSEMBLE

    def test_skip_edge_below_threshold(self, monkeypatch):
        # Ensemble agrees with market (indep_prob ≈ yes_ask) → edge
        # vanishes after fees on both sides.
        _patch_ensemble(monkeypatch, 0.50, n_sources=3)
        out = score_market(_mkt(yes_ask=0.50, yes_bid=0.47),
                           disabled_strategies=_DISABLE_OTHERS)
        assert out[0] == 0
        assert out[9] == SKIP_EDGE_BELOW_THRESHOLD

    def test_skip_no_strategy_fired(self, monkeypatch):
        # Edge is huge, but spread >= 0.08 kills info_edge. Other
        # strategies are disabled → nothing fires. Distinct from
        # SKIP_EDGE_BELOW_THRESHOLD because the fix surface is
        # different (spread gate, not MIN_EDGE).
        _patch_ensemble(monkeypatch, 0.90, n_sources=3)
        out = score_market(
            _mkt(yes_ask=0.50, yes_bid=0.40),   # spread=0.10 ≥ 0.08
            disabled_strategies=_DISABLE_OTHERS,
        )
        assert out[0] == 0
        assert out[9] == SKIP_NO_STRATEGY_FIRED

    def test_successful_score_has_empty_skip_reason(self, monkeypatch):
        _patch_ensemble(monkeypatch, 0.80, n_sources=3)
        out = score_market(
            _mkt(yes_ask=0.50, yes_bid=0.47),
            disabled_strategies=_DISABLE_OTHERS,
        )
        assert out[0] > 0
        assert out[9] == ""
