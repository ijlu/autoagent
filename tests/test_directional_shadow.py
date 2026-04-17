"""Tests for bot/learning/directional_shadow.py (step 7)."""
from __future__ import annotations

import pytest

from bot.db import init_db
from bot.learning.directional_shadow import (
    DEFAULT_MIN_SETTLED,
    ShadowDecision,
    ShadowOutcome,
    _our_side_market_prob,
    evaluate,
    set_live_flag,
    should_go_live,
    should_trade_live,
)


# ── Pure evaluator ──────────────────────────────────────────────────────────
class TestBlocklist:
    def test_hard_blocked_family_returns_blocked(self):
        dec = evaluate(
            ticker="KXBTC-26MAY01-T100000", side="yes",
            indep_prob=0.9, contracts=10, price_cents=50,
            market_mid_cents=30, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.BLOCKED
        assert dec.family == "KXBTC"
        assert dec.skip_reason == "family_blocked:KXBTC"
        assert dec.contracts == 0

    def test_blocked_short_circuits_kelly_and_edge(self):
        # Contracts==0 AND big negative edge — blocklist still wins first.
        dec = evaluate(
            ticker="KXETH-26MAY01-T3000", side="no",
            indep_prob=0.1, contracts=0, price_cents=70,
            market_mid_cents=80, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.BLOCKED

    def test_custom_blocklist_overrides_default(self):
        dec = evaluate(
            ticker="KXHIGHMIA-26APR18-T75", side="yes",
            indep_prob=0.7, contracts=5, price_cents=50,
            market_mid_cents=50, min_edge=0.05,
            blocklist=frozenset({"KXHIGHMIA"}),
        )
        assert dec.outcome == ShadowOutcome.BLOCKED
        assert dec.skip_reason == "family_blocked:KXHIGHMIA"


class TestKellyZero:
    def test_zero_contracts_returns_kelly_zero(self):
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="yes",
            indep_prob=0.6, contracts=0, price_cents=55,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.KELLY_ZERO
        assert dec.skip_reason == "kelly_zero"
        assert dec.family == "KXHIGHNY"

    def test_negative_contracts_also_kelly_zero(self):
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="yes",
            indep_prob=0.6, contracts=-2, price_cents=55,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.KELLY_ZERO


class TestBelowEdge:
    def test_small_edge_below_threshold(self):
        # Our side 52%, market 50% → edge 2pp, threshold 5pp → below.
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="yes",
            indep_prob=0.52, contracts=5, price_cents=51,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.BELOW_EDGE
        assert dec.edge_vs_mid == pytest.approx(0.02, abs=1e-6)
        assert "edge_vs_mid=+0.020<0.050" in dec.skip_reason

    def test_negative_edge_also_below(self):
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="yes",
            indep_prob=0.40, contracts=5, price_cents=51,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.BELOW_EDGE
        assert dec.edge_vs_mid == pytest.approx(-0.10, abs=1e-6)

    def test_no_side_compares_to_one_minus_yes_mid(self):
        # NO trade: our P(NO)=0.70, market YES-mid=50¢ → P(NO)-mid = 0.70-0.50 = 0.20.
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="no",
            indep_prob=0.70, contracts=5, price_cents=40,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.SHADOW_PASS
        assert dec.market_prob == pytest.approx(0.50, abs=1e-6)
        assert dec.edge_vs_mid == pytest.approx(0.20, abs=1e-6)

    def test_no_side_below_edge(self):
        # NO trade with our P(NO)=0.52, market YES-mid=50¢ → P(NO)=0.50, edge=0.02.
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="no",
            indep_prob=0.52, contracts=5, price_cents=49,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.BELOW_EDGE
        assert dec.market_prob == pytest.approx(0.50, abs=1e-6)


class TestShadowPass:
    def test_clean_pass_with_mid(self):
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="yes",
            indep_prob=0.70, contracts=4, price_cents=55,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.SHADOW_PASS
        assert dec.skip_reason is None
        assert dec.edge_vs_mid == pytest.approx(0.20, abs=1e-6)
        assert dec.contracts == 4
        assert dec.our_prob == pytest.approx(0.70)
        assert dec.market_prob == pytest.approx(0.50)

    def test_missing_mid_bypasses_edge_gate(self):
        # When market mid is unknown we pass through (caller decides how to handle).
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="yes",
            indep_prob=0.51, contracts=4, price_cents=50,
            market_mid_cents=None, min_edge=0.05,
        )
        assert dec.outcome == ShadowOutcome.SHADOW_PASS
        assert dec.edge_vs_mid is None
        assert dec.market_prob is None


class TestOurSideMarketProbHelper:
    def test_yes_returns_yes_mid(self):
        assert _our_side_market_prob("yes", 50) == pytest.approx(0.50)

    def test_no_returns_complement(self):
        assert _our_side_market_prob("no", 40) == pytest.approx(0.60)

    def test_none_mid_is_none(self):
        assert _our_side_market_prob("yes", None) is None

    def test_clamps_out_of_range(self):
        # Kalshi shouldn't give >100 or <0 but be defensive.
        assert _our_side_market_prob("yes", 150) == pytest.approx(1.0)
        assert _our_side_market_prob("no", -10) == pytest.approx(1.0)


# ── Live flag (kv_cache) ────────────────────────────────────────────────────
@pytest.fixture()
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


class TestShouldTradeLive:
    def test_default_is_false(self, conn):
        assert should_trade_live(conn, "KXHIGHNY") is False

    def test_blocked_family_never_live(self, conn):
        # Even if someone writes True to the kv row, block list wins.
        set_live_flag(conn, "KXBTC", True)
        assert should_trade_live(conn, "KXBTC") is False

    def test_set_flag_flips_live(self, conn):
        assert should_trade_live(conn, "KXHIGHNY") is False
        set_live_flag(conn, "KXHIGHNY", True)
        assert should_trade_live(conn, "KXHIGHNY") is True

    def test_set_flag_false_shadows_prior_true(self, conn):
        set_live_flag(conn, "KXHIGHNY", True)
        assert should_trade_live(conn, "KXHIGHNY") is True
        set_live_flag(conn, "KXHIGHNY", False)
        assert should_trade_live(conn, "KXHIGHNY") is False

    def test_case_insensitive(self, conn):
        set_live_flag(conn, "kxhighny", True)
        assert should_trade_live(conn, "KXHIGHNY") is True
        assert should_trade_live(conn, "kxhighny") is True


class TestShouldGoLive:
    def test_stub_always_false(self, conn):
        assert should_go_live(conn, "KXHIGHNY") is False

    def test_blocked_family_never_graduates(self, conn):
        assert should_go_live(conn, "KXBTC") is False

    def test_stub_exposes_default_thresholds(self):
        assert DEFAULT_MIN_SETTLED == 50


# ── Integration shape ──────────────────────────────────────────────────────
class TestShadowDecisionImmutable:
    def test_dataclass_is_frozen(self):
        dec = evaluate(
            ticker="KXHIGHNY-26APR18-T75", side="yes",
            indep_prob=0.70, contracts=4, price_cents=55,
            market_mid_cents=50, min_edge=0.05,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            dec.outcome = ShadowOutcome.BLOCKED  # type: ignore[misc]

    def test_family_uppercased(self):
        dec = evaluate(
            ticker="kxhighny-26apr18-t75", side="yes",
            indep_prob=0.7, contracts=4, price_cents=55,
            market_mid_cents=50, min_edge=0.05,
        )
        assert dec.family == "KXHIGHNY"
