"""Tests for the METAR-required gate in directional_shadow.evaluate().

Validation 2026-04-29 showed that without fresh METAR our combined μ is
1-3°F cold-biased, producing catastrophic Brier on bracket-edge calls.
The new gate refuses to trade weather families when METAR is stale.

This is the trade-decision-layer counterpart to the combine-layer
sanity gate (test_sanity_gate.py): one filters bad sources from the
combined output, the other refuses to trade when the combine has lost
its anchor source entirely.
"""

from __future__ import annotations

import pytest

from bot.learning.directional_shadow import (
    ShadowOutcome,
    evaluate,
)


def _kw(**overrides):
    base = dict(
        ticker="KXHIGHNY-26APR30-B68.5",
        side="no",
        indep_prob=0.6,
        contracts=2,
        price_cents=58,
        market_mid_cents=42,
        min_edge=0.05,
    )
    base.update(overrides)
    return base


class TestMetarRequiredGate:
    def test_metar_required_off_by_default(self):
        # Backwards-compat: existing callers that don't set metar_required
        # behave as before.
        d = evaluate(**_kw())
        assert d.outcome != ShadowOutcome.METAR_STALE

    def test_fires_when_required_and_stale(self):
        d = evaluate(**_kw(metar_required=True, metar_fresh=False))
        assert d.outcome == ShadowOutcome.METAR_STALE
        assert d.skip_reason == "metar_stale_or_missing"
        assert d.contracts == 0

    def test_passthrough_when_required_and_fresh(self):
        d = evaluate(**_kw(metar_required=True, metar_fresh=True))
        assert d.outcome != ShadowOutcome.METAR_STALE

    def test_runs_after_blocklist(self):
        # Blocklist (KXBTC) must take precedence — even with stale METAR
        # the family-blocked outcome should fire first since blocklist is
        # the harder constraint.
        d = evaluate(**_kw(
            ticker="KXBTC-26APR30-B50000",
            metar_required=True, metar_fresh=False,
        ))
        assert d.outcome == ShadowOutcome.BLOCKED

    def test_runs_before_kelly(self):
        # METAR check is gate #2 (after blocklist, before Kelly). With
        # contracts=0 (would be KELLY_ZERO) AND metar_fresh=False, METAR
        # gate fires first because it's the more informative skip reason.
        d = evaluate(**_kw(
            contracts=0, metar_required=True, metar_fresh=False,
        ))
        assert d.outcome == ShadowOutcome.METAR_STALE

    def test_metar_stale_in_outcome_enum(self):
        # The outcome string must be in the canonical _ALL_OUTCOMES set so
        # that downstream sql filters / promotion gates recognize it.
        from bot.learning.directional_shadow import _ALL_OUTCOMES
        assert ShadowOutcome.METAR_STALE in _ALL_OUTCOMES


class TestTTEGate:
    """The TTE window gate refuses to trade weather brackets when the
    settlement horizon is shorter than ``min_tte_hours``.

    Pinned by the 2026-04-29 validation: at TTE <9h, the daily peak has
    already happened (3pm LST peak vs midnight LST settle) and the market
    is at trivial Brier ~0.0001 — unwinnable. We use 12h as the floor
    because the 9-12h "peak forming" band is too marginal to commit on.
    """

    def test_no_gate_when_tte_hours_none(self):
        # Default: no TTE constraint. Backwards-compat with non-weather callers.
        d = evaluate(**_kw())  # tte_hours not passed
        assert d.outcome != ShadowOutcome.TTE_OUT_OF_WINDOW

    def test_blocks_when_below_min(self):
        d = evaluate(**_kw(tte_hours=8.0, min_tte_hours=12.0))
        assert d.outcome == ShadowOutcome.TTE_OUT_OF_WINDOW
        assert "tte_8.0h<12.0h_min" == d.skip_reason
        assert d.contracts == 0

    def test_passes_when_above_min(self):
        d = evaluate(**_kw(tte_hours=20.0, min_tte_hours=12.0))
        assert d.outcome != ShadowOutcome.TTE_OUT_OF_WINDOW

    def test_blocks_at_exact_boundary(self):
        # TTE exactly at the threshold counts as below — be conservative.
        d = evaluate(**_kw(tte_hours=11.999, min_tte_hours=12.0))
        assert d.outcome == ShadowOutcome.TTE_OUT_OF_WINDOW

    def test_runs_after_blocklist_and_metar(self):
        # Blocklist must take priority even with bad TTE
        d = evaluate(**_kw(
            ticker="KXBTC-26APR30-B50000",
            tte_hours=2.0, min_tte_hours=12.0,
        ))
        assert d.outcome == ShadowOutcome.BLOCKED

        # METAR-stale takes priority over TTE
        d = evaluate(**_kw(
            metar_required=True, metar_fresh=False,
            tte_hours=2.0, min_tte_hours=12.0,
        ))
        assert d.outcome == ShadowOutcome.METAR_STALE

    def test_runs_before_kelly(self):
        # Even with contracts=0, TTE should report first (it's a hard
        # market-condition gate, not a sizing issue).
        d = evaluate(**_kw(
            contracts=0, tte_hours=2.0, min_tte_hours=12.0,
        ))
        assert d.outcome == ShadowOutcome.TTE_OUT_OF_WINDOW

    def test_outcome_in_canonical_set(self):
        from bot.learning.directional_shadow import _ALL_OUTCOMES
        assert ShadowOutcome.TTE_OUT_OF_WINDOW in _ALL_OUTCOMES

    def test_caller_can_disable_via_zero_min(self):
        # Default min_tte_hours=0 means caller didn't opt in. Even with
        # tte_hours passed, the gate is a no-op when min=0.
        d = evaluate(**_kw(tte_hours=2.0, min_tte_hours=0.0))
        assert d.outcome != ShadowOutcome.TTE_OUT_OF_WINDOW
