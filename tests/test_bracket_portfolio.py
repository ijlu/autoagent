"""Tests for cross-bracket portfolio scoring (Phase B.3).

Pin the per-leg decision logic and the multi-bracket projection. A bug
here → wrong-bracket bets, which cascade into catastrophic Brier.

Test cases match the worked example from the design doc — predicted
distribution N(68, 1.5), 5 brackets, expect:
  B62.5 → buy NO (we predict ~0% probability vs 5¢ market = NO mispriced)
  B68.5 → buy YES (we predict ~50% vs 30¢ market = YES underpriced)
"""

from __future__ import annotations

import pytest

from bot.scoring.bracket_portfolio import (
    BracketDecision,
    _ncdf,
    group_markets_by_settlement,
    project_gaussian_above,
    project_gaussian_to_bracket,
    score_market_portfolio,
)


def _market(ticker: str, yes_bid: int, yes_ask: int, **extra) -> dict:
    """Construct a Kalshi market_data dict with realistic shape."""
    bracket_lo = None
    bracket_hi = None
    if "-B" in ticker:
        b_value = float(ticker.rsplit("-B", 1)[1])
        bracket_lo = b_value - 0.5
        bracket_hi = b_value + 1.5
    threshold = bracket_lo if bracket_lo is not None else None
    if "-T" in ticker:
        threshold = float(ticker.rsplit("-T", 1)[1])

    base = {
        "ticker": ticker,
        "title": f"high temp test for {ticker}",
        "subtitle": "high temp",
        "yes_sub_title": f"{threshold or 75} or above",
        "yes_bid_dollars": f"0.{yes_bid:02d}",
        "yes_ask_dollars": f"0.{yes_ask:02d}",
        "close_time": "2030-04-30T23:59:59Z",
    }
    if bracket_lo is not None:
        base["floor_strike"] = bracket_lo
        base["cap_strike"] = bracket_hi
    base.update(extra)
    return base


# ── Pure projection math ──────────────────────────────────────────────
class TestProjection:
    def test_bracket_centered_around_mu_high_prob(self):
        # μ=68, bracket [68, 70] — N(68, 1.5) puts ~25% in [68, 70]
        p = project_gaussian_to_bracket(68.0, 1.5, 68.0, 70.0)
        # Pretty broad: [μ, μ+2σ] ≈ 0.477 of mass; [μ, μ+1.33σ] more like 0.41
        # P(68 ≤ X < 70) = Φ((70-68)/1.5) - Φ(0) = Φ(1.33) - 0.5 ≈ 0.41
        assert 0.38 < p < 0.45

    def test_bracket_far_from_mu_low_prob(self):
        # μ=68, bracket [62, 64] — far below; very low probability
        p = project_gaussian_to_bracket(68.0, 1.5, 62.0, 64.0)
        assert p < 0.05

    def test_above_threshold_50pct_at_mu(self):
        # P(X > μ) = 0.5 by symmetry
        p = project_gaussian_above(68.0, 1.5, 68.0)
        assert abs(p - 0.5) < 0.01

    def test_above_threshold_far_below_mu_high_prob(self):
        # P(X > 50) when μ=68 is essentially 1.0 — clipped to 0.995
        p = project_gaussian_above(68.0, 1.5, 50.0)
        assert p == 0.995

    def test_clipped_to_995_max(self):
        p = project_gaussian_to_bracket(68.0, 1.5, 60.0, 80.0)
        assert p == 0.995  # essentially the entire distribution

    def test_clipped_to_005_min(self):
        p = project_gaussian_to_bracket(68.0, 1.5, 90.0, 92.0)
        assert p == 0.005


# ── End-to-end: portfolio decisions ───────────────────────────────────
class TestPortfolio:
    """The worked-example test — predicted N(68, 1.5), several brackets,
    confirm the decisions match expectation."""

    def test_buys_yes_on_high_edge_bracket(self):
        # B68.5 = bracket [68, 70], we predict ~41%, market mid 30¢ →
        # ~11% YES edge → buy YES
        markets = [_market("KXHIGHNY-26APR30-B68.5", yes_bid=28, yes_ask=32)]
        decisions = score_market_portfolio(markets, combined_mu=68.0,
                                            combined_sigma=1.5)
        assert len(decisions) == 1
        d = decisions[0]
        assert d.action == "buy_yes"
        assert d.side == "yes"
        assert d.price_cents == 32
        assert d.edge_yes is not None and d.edge_yes > 0.05

    def test_buys_no_on_far_bracket(self):
        # B62.5 = bracket [62, 64], we predict ~3% YES (i.e., 97% NO)
        # If market_no_ask is much less than 0.97 → buy NO
        # YES bid 5, YES ask 8 → NO ask = 100 - 5 = 95¢
        # Our edge_no = (1 - 0.03) - 0.95 = 0.02 → too small at min_edge=0.07
        # Try yes_bid=15: NO ask = 85; edge_no = 0.97 - 0.85 = 0.12 → buy NO
        markets = [_market("KXHIGHNY-26APR30-B62.5", yes_bid=15, yes_ask=18)]
        decisions = score_market_portfolio(markets, combined_mu=68.0,
                                            combined_sigma=1.5)
        d = decisions[0]
        assert d.action == "buy_no"
        assert d.side == "no"
        assert d.price_cents == 85  # 100 - yes_bid
        assert d.edge_no is not None and d.edge_no > 0.05

    def test_skips_when_no_side_clears_edge(self):
        # Pick a bracket / μ pair where P(YES) lands in the "small" regime.
        # μ=72, σ=1.5, B66.5 = [66, 68]: z_lo=-4, z_hi=-2.67 → p_yes ≈ 0.004
        # Market YES bid 1, ask 2 (penny floor) — both sides skip via price band
        # Use yes_bid=20, yes_ask=22: edge_yes = 0.004-0.22 = -0.22 (skip)
        # NO ask = 100-20 = 80; edge_no = 0.996-0.80 = 0.196 → BUY NO
        # So: pick a market where neither side has edge.
        # μ=68, σ=3.0 (wider), B66.5 = [66, 68]: p_yes ≈ 0.25
        # Market 22¢ YES: edge_yes = 0.25-0.22 = 0.03 < 0.07 (skip)
        # NO ask = 80; edge_no = 0.75-0.80 = -0.05 < 0.07 (skip)
        markets = [_market("KXHIGHNY-26APR30-B66.5", yes_bid=20, yes_ask=22)]
        decisions = score_market_portfolio(markets, combined_mu=68.0,
                                            combined_sigma=3.0)
        d = decisions[0]
        assert d.action == "skip"

    def test_extreme_tail_market_at_penny_skipped_by_price_band(self):
        # Predicted P(YES) ≈ 0 (μ=80, bracket=B40.5 = [40, 42]).
        # Market YES at 1-2¢ — penny price band catches both sides.
        markets = [_market("KXHIGHNY-26APR30-B40.5", yes_bid=1, yes_ask=2)]
        decisions = score_market_portfolio(markets, combined_mu=80.0,
                                            combined_sigma=1.5,
                                            min_price_cents=5)
        d = decisions[0]
        # YES ask 2 < 5 → skip
        # NO ask = 100-1 = 99 > 95 → skip
        assert d.action == "skip"

    def test_full_portfolio_5_brackets(self):
        """The headline worked example: 5 brackets, predicted N(68, 1.5)."""
        markets = [
            _market("KXHIGHNY-26APR30-B62.5", yes_bid=2, yes_ask=4),    # cheap; edge_no should NOT be huge (penny)
            _market("KXHIGHNY-26APR30-B64.5", yes_bid=8, yes_ask=12),
            _market("KXHIGHNY-26APR30-B66.5", yes_bid=20, yes_ask=24),  # ~16% pred vs 22% mid → small edge
            _market("KXHIGHNY-26APR30-B68.5", yes_bid=28, yes_ask=32),  # ~41% pred vs 30% mid → buy YES
            _market("KXHIGHNY-26APR30-B70.5", yes_bid=12, yes_ask=16),
        ]
        decisions = score_market_portfolio(markets, combined_mu=68.0,
                                            combined_sigma=1.5)
        assert len(decisions) == 5

        # B68.5 should be buy YES
        b685 = next(d for d in decisions if "B68.5" in d.ticker)
        assert b685.action == "buy_yes"

        # The portfolio should have at least one fire (B68.5 confirmed)
        n_fired = sum(1 for d in decisions if d.action != "skip")
        assert n_fired >= 1


# ── Settlement grouping ───────────────────────────────────────────────
class TestGroupBySettlement:
    def test_groups_brackets_correctly(self):
        markets = [
            {"ticker": "KXHIGHNY-26APR30-B62.5"},
            {"ticker": "KXHIGHNY-26APR30-B64.5"},
            {"ticker": "KXHIGHNY-26APR30-B66.5"},
            {"ticker": "KXHIGHCHI-26APR30-B66.5"},  # different city
            {"ticker": "KXHIGHNY-26MAY01-B68.5"},   # different date
        ]
        grouped = group_markets_by_settlement(markets)
        assert len(grouped) == 3
        assert len(grouped["KXHIGHNY-26APR30"]) == 3
        assert len(grouped["KXHIGHCHI-26APR30"]) == 1
        assert len(grouped["KXHIGHNY-26MAY01"]) == 1

    def test_groups_thresholds_too(self):
        markets = [
            {"ticker": "KXHIGHNY-26APR30-B68.5"},
            {"ticker": "KXHIGHNY-26APR30-T70"},
            {"ticker": "KXHIGHNY-26APR30-T75"},
        ]
        grouped = group_markets_by_settlement(markets)
        assert len(grouped["KXHIGHNY-26APR30"]) == 3

    def test_empty_input(self):
        assert group_markets_by_settlement([]) == {}

    def test_handles_unparseable_ticker(self):
        # No bracket / threshold suffix — falls into its own group
        grouped = group_markets_by_settlement([{"ticker": "KXFED-26JUN-T425"}])
        assert "KXFED" in str(grouped)


# ── Edge-band guard ───────────────────────────────────────────────────
class TestEdgeBandGuard:
    def test_skips_when_market_at_penny_floor(self):
        # YES ask 3¢, our p_yes 30% → huge edge but penny-floor blocks it
        markets = [_market("KXHIGHNY-26APR30-B68.5", yes_bid=2, yes_ask=3)]
        decisions = score_market_portfolio(markets, combined_mu=68.0,
                                            combined_sigma=1.5,
                                            min_price_cents=5)
        assert decisions[0].action == "skip"

    def test_skips_when_market_at_penny_ceiling(self):
        markets = [_market("KXHIGHNY-26APR30-B68.5", yes_bid=96, yes_ask=98)]
        decisions = score_market_portfolio(markets, combined_mu=68.0,
                                            combined_sigma=1.5,
                                            max_price_cents=95)
        # YES side: ask 98 > 95 → can't trade YES side
        # NO side: ask = 100-96 = 4, less than min_price 5 → can't trade NO either
        assert decisions[0].action == "skip"


# ── One-sided quotes ──────────────────────────────────────────────────
class TestOneSidedQuotes:
    """Regression: when one side of the quote is missing, the no-edge skip
    path used to crash on ``f"{None:+.3f}"`` (caught by the per-settlement
    try/except in cross_bracket_shadow, but masked the real fix). Ensure
    the formatter handles None on either side."""

    def test_skip_reason_handles_missing_yes_bid(self):
        # P_yes ≈ 0.5 (μ=68, B68.5=[68,70]); edge_yes is computable from
        # yes_ask alone, but edge_no needs yes_bid → None.
        from bot.scoring.bracket_portfolio import _decide_leg
        action, side, price, reason = _decide_leg(
            p_yes=0.30, yes_bid=None, yes_ask=40,  # edge_yes negative
            min_edge=0.07, min_price_cents=5, max_price_cents=95,
        )
        # edge_yes = 0.30 - 0.40 = -0.10 (skip), edge_no None → no crash
        assert action == "skip"
        assert "n/a" in reason or reason == "no_market_quote"

    def test_skip_reason_handles_missing_yes_ask(self):
        from bot.scoring.bracket_portfolio import _decide_leg
        action, side, price, reason = _decide_leg(
            p_yes=0.30, yes_bid=20, yes_ask=None,
            min_edge=0.50, min_price_cents=5, max_price_cents=95,
        )
        # edge_no = 0.70 - 0.80 = -0.10 (skip), edge_yes None → no crash
        assert action == "skip"
        assert "n/a" in reason or reason == "no_market_quote"


# ── σ floor ───────────────────────────────────────────────────────────
class TestSigmaFloor:
    def test_floor_enforced_when_sigma_too_tight(self):
        # σ=0.1 would put 99% on a single bracket; force 1.0 via floor
        markets = [_market("KXHIGHNY-26APR30-B68.5", yes_bid=2, yes_ask=4)]
        decisions = score_market_portfolio(markets, combined_mu=68.0,
                                            combined_sigma=0.1,
                                            sigma_floor=1.0)
        # With σ=1.0 floor, B68.5 = [68, 70] gets ~50% probability
        # not the 99% it would with σ=0.1. So edge_yes ≈ 0.50 - 0.04 = 0.46
        assert decisions[0].p_yes > 0.4
        assert decisions[0].p_yes < 0.55  # not 0.99
