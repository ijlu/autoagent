"""Tests for the trading-time probability cap (Phase 2 item 4).

Two layers of coverage:

1. Helper unit tests — cap_trading_prob() correctness and counter behavior.
2. Targeted regression tests — verify each callsite that consumes the helper
   actually caps trading-decision math while preserving raw values for logs.
"""

from __future__ import annotations

import pytest

from bot.scoring.trading_caps import (
    TRADING_PROB_CAP_HI,
    TRADING_PROB_CAP_LO,
    cap_trading_prob,
    get_hit_counts,
    reset_hit_counts,
)


@pytest.fixture(autouse=True)
def _reset_counters():
    reset_hit_counts()
    yield
    reset_hit_counts()


# ── Helper unit tests ─────────────────────────────────────────────────────


def test_cap_passthrough_for_normal_value():
    assert cap_trading_prob(0.5, source="t") == 0.5
    assert get_hit_counts() == {}


def test_cap_passthrough_at_hi_boundary():
    assert cap_trading_prob(TRADING_PROB_CAP_HI, source="t") == TRADING_PROB_CAP_HI
    assert get_hit_counts() == {}


def test_cap_passthrough_at_lo_boundary():
    assert cap_trading_prob(TRADING_PROB_CAP_LO, source="t") == TRADING_PROB_CAP_LO
    assert get_hit_counts() == {}


def test_cap_clips_high_value():
    assert cap_trading_prob(0.999, source="t") == TRADING_PROB_CAP_HI
    assert cap_trading_prob(1.0, source="t") == TRADING_PROB_CAP_HI
    assert get_hit_counts() == {"t.hi": 2}


def test_cap_clips_low_value():
    assert cap_trading_prob(0.001, source="t") == TRADING_PROB_CAP_LO
    assert cap_trading_prob(0.0, source="t") == TRADING_PROB_CAP_LO
    assert get_hit_counts() == {"t.lo": 2}


def test_cap_counters_per_source():
    cap_trading_prob(0.999, source="alpha")
    cap_trading_prob(0.999, source="beta")
    cap_trading_prob(0.001, source="alpha")
    counts = get_hit_counts()
    assert counts == {"alpha.hi": 1, "alpha.lo": 1, "beta.hi": 1}


def test_reset_hit_counts():
    cap_trading_prob(0.999, source="t")
    assert get_hit_counts() != {}
    reset_hit_counts()
    assert get_hit_counts() == {}


# ── Callsite regression tests ─────────────────────────────────────────────


def test_decide_leg_caps_extreme_p_yes():
    """_decide_leg sizing/edge math uses capped p_yes when input is extreme."""
    from bot.scoring.bracket_portfolio import _decide_leg

    # Raw p_yes=0.999 vs market YES at 90¢: raw edge=0.099, capped edge=0.09.
    # min_edge=0.07 → both fire YES, but the capped sizing input is 0.99.
    action_raw, side_raw, price_raw, _ = _decide_leg(
        0.999, yes_bid=88, yes_ask=90,
        min_edge=0.07, min_price_cents=5, max_price_cents=95,
    )
    assert action_raw == "buy_yes"
    assert side_raw == "yes"
    assert price_raw == 90
    counts = get_hit_counts()
    assert counts.get("decide_leg.hi", 0) >= 1


def test_decide_leg_cap_neutralizes_marginal_edge():
    """Raw edge that depends on p_yes>0.99 should be neutralized by the cap.

    Raw p_yes=0.995 vs YES ask at 99¢: raw edge=0.005 (passes if min_edge<0.005);
    capped p_yes=0.99: capped edge=0 (skip). Cap should win.
    """
    from bot.scoring.bracket_portfolio import _decide_leg

    action, side, _, _ = _decide_leg(
        0.995, yes_bid=98, yes_ask=99,
        min_edge=0.001, min_price_cents=5, max_price_cents=99,
    )
    # Capped edge_yes = 0.99 - 0.99 = 0.0, fails min_edge=0.001 → skip.
    assert action == "skip"


def test_kelly_contracts_caps_high_prob():
    """kelly_contracts caps independent_prob at entry (defense-in-depth)."""
    pytest.importorskip("cryptography", reason="trade.py requires cryptography RSA")
    try:
        from trade import kelly_contracts
    except ImportError:
        pytest.skip("trade.py unavailable in this env (e.g. arch mismatch)")

    # Raw 0.999 → capped to 0.99 → existing >=0.98 skip fires → returns 0.
    # (The cap is no-op here vs the existing skip, which is the documented
    # behavior; the test confirms the cap call wires up without error.)
    out = kelly_contracts(0.999, price_cents=80, balance_cents=10000)
    assert out == 0
    counts = get_hit_counts()
    assert counts.get("kelly.hi", 0) >= 1


def test_four_factor_uses_trading_cap():
    """four_factor.score_four_factors caps ensemble_prob before edge math."""
    from bot.scoring.four_factor import score_four_factors

    # Two calls with the same logical input: 0.999 (raw) vs 0.99 (cap).
    # Both should produce the same edge component because the cap normalizes.
    common = dict(
        market_data={"volume_24h_fp": 100000, "yes_ask": 0.85, "yes_bid": 0.80},
        market_prob=0.85, n_sources=3,
        source_desc="weather", regime=None,
        active_feedback={}, category_scores={},
    )
    score_a = score_four_factors(ensemble_prob=0.999, **common)
    score_b = score_four_factors(ensemble_prob=0.99, **common)
    # Edge component of the score must be identical: cap normalizes 0.999
    # down to 0.99 inside score_four_factors before the edge calc.
    assert score_a.edge == pytest.approx(score_b.edge)
    counts = get_hit_counts()
    assert counts.get("four_factor.hi", 0) >= 1


def test_bracket_decision_records_raw_p_yes():
    """BracketDecision.p_yes records the raw projected value, not the
    capped one used inside _decide_leg. (Choice (i) — calibration must
    see model truth.)"""
    from bot.scoring.bracket_portfolio import score_market_portfolio

    # Build a synthetic market at threshold 70°F with mu well above (84°F)
    # so the projection produces a near-certain YES (p > 0.99) for "above".
    market = {
        "ticker": "KXHIGHNY-26MAY01-T70.0",
        "title": "Will the high in NY exceed 70 degrees on May 1?",
        "yes_ask_dollars": "0.95",
        "yes_bid_dollars": "0.93",
    }
    decisions = score_market_portfolio(
        [market],
        combined_mu=84.0,
        combined_sigma=1.0,
        min_edge=0.01,
        sigma_floor=1.0,
    )
    if not decisions:
        pytest.skip("market parser rejected synthetic ticker; structural test")

    d = decisions[0]
    # Raw projected p_yes (subject only to the existing model-layer
    # [0.005, 0.995] numerical-safety clamp) should be recorded as-is —
    # NOT capped to 0.99 by the trading-time cap.
    assert d.p_yes > TRADING_PROB_CAP_HI, (
        f"Expected raw p_yes > {TRADING_PROB_CAP_HI}, got {d.p_yes}"
    )
