"""Tests for the cross_bracket vs single-side retro-replay validator.

Pin the PnL/Brier/win-rate aggregation math against synthetic data with
known answers. Bug here → wrong go/no-go signal → ship a worse strategy.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot.db import init_db
from bot.learning.alpha_log import (
    DecisionOutcome, DecisionType, EnsembleSnapshot,
    MarketSnapshot, log_decision,
)
from tools.validate_cross_bracket import per_leg_summary, per_portfolio_summary


def _seed_settled(conn: sqlite3.Connection, *,
                  ticker: str, p_yes: float, side: str,
                  price_cents: int, our_trade_won: int,
                  yes_outcome: int | None = None,
                  market_id: str | None = None,
                  portfolio_leg_count: int | None = None) -> int:
    """Insert one alpha_backtest row + back-fill settlement fields with the
    PRODUCTION convention: ``won_yes`` stores "did our trade win" (not
    "did YES outcome happen"). For YES-side rows the two coincide; for
    NO-side rows they're flipped.

    Caller specifies ``our_trade_won`` (the column value) and optionally
    ``yes_outcome`` (overrides the implied settlement_result for testing
    Brier on cases where caller wants direct control).
    """
    rid = log_decision(
        conn, ticker=ticker,
        decision_type=DecisionType.DIRECTIONAL_SHADOW,
        decision_outcome=DecisionOutcome.SHADOW_ONLY,
        ensemble=EnsembleSnapshot(p_yes=p_yes, source_count=3),
        market=MarketSnapshot(yes_bid_cents=40, yes_ask_cents=50),
        side=side, price_cents=price_cents, contracts=1,
        market_id=market_id,
        portfolio_leg_count=portfolio_leg_count,
        notes=("cross_bracket;leg=0;p_yes=" if market_id else "single_side"),
    )
    assert rid is not None
    # Imply YES outcome from (side, our_trade_won) unless overridden.
    if yes_outcome is None:
        if side == "yes":
            yes_outcome = our_trade_won
        else:  # side == "no": we won iff YES did NOT happen
            yes_outcome = 1 - our_trade_won
    settlement_result = "yes" if yes_outcome else "no"
    conn.execute(
        "UPDATE alpha_backtest "
        "SET won_yes = ?, settlement_result = ?, "
        "ts_settle = '2026-04-30T00:00:00Z', ts_settle_unix = 1714435200 "
        "WHERE id = ?",
        (our_trade_won, settlement_result, rid),
    )
    conn.commit()
    return rid


@pytest.fixture
def conn():
    return init_db(":memory:")


class TestPerLegSummary:
    def test_yes_side_won(self, conn):
        # Bet YES at 40¢, YES outcome happened → trade won, +60¢
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.7, side="yes",
                      price_cents=40, our_trade_won=1)
        rows = per_leg_summary(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["cohort"] == "single_side"
        assert r["family"] == "KXHIGHNY"
        assert r["n"] == 1
        assert r["mean_gross_pnl_cents"] == pytest.approx(60.0)
        # Maker fee at price 40: ceil(0.0175 * 40 * 60 / 100) = ceil(0.42) = 1
        assert r["mean_net_pnl_cents"] == pytest.approx(59.0)
        assert r["win_rate"] == pytest.approx(1.0)
        # YES side: yes_outcome = our_trade_won = 1. Brier = (0.7-1)^2 = 0.09
        assert r["brier"] == pytest.approx(0.09)

    def test_yes_side_lost(self, conn):
        # Bet YES at 70¢, YES did NOT happen → trade lost, -70¢
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.6, side="yes",
                      price_cents=70, our_trade_won=0)
        r = per_leg_summary(conn)[0]
        assert r["mean_gross_pnl_cents"] == pytest.approx(-70.0)
        # Brier = (0.6 - 0)^2 = 0.36
        assert r["brier"] == pytest.approx(0.36)
        assert r["win_rate"] == pytest.approx(0.0)

    def test_no_side_won(self, conn):
        # Bet NO at 80¢, YES did NOT happen → trade won, +20¢
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.2, side="no",
                      price_cents=80, our_trade_won=1)
        r = per_leg_summary(conn)[0]
        assert r["mean_gross_pnl_cents"] == pytest.approx(20.0)
        assert r["win_rate"] == pytest.approx(1.0)
        # NO side: yes_outcome = 1 - our_trade_won = 0. Brier = (0.2-0)^2 = 0.04
        # Confirms NO-side flip: column says won, but YES outcome was 0, so
        # our 20% YES prediction was well-calibrated.
        assert r["brier"] == pytest.approx(0.04)

    def test_no_side_lost(self, conn):
        # Bet NO at 90¢, YES happened → trade lost, -90¢
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.1, side="no",
                      price_cents=90, our_trade_won=0)
        r = per_leg_summary(conn)[0]
        assert r["mean_gross_pnl_cents"] == pytest.approx(-90.0)
        assert r["win_rate"] == pytest.approx(0.0)
        # NO side losing: yes_outcome = 1 - 0 = 1. Brier = (0.1-1)^2 = 0.81
        # We predicted 10% YES, YES happened, big miss.
        assert r["brier"] == pytest.approx(0.81)

    def test_cohort_split(self, conn):
        # Two rows: one cross_bracket (market_id set), one single_side
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.5, side="yes",
                      price_cents=50, our_trade_won=1,
                      market_id="KXHIGHNY-26APR30",
                      portfolio_leg_count=6)
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.5, side="yes",
                      price_cents=50, our_trade_won=0)
        rows = per_leg_summary(conn)
        cohorts = {r["cohort"]: r for r in rows}
        assert "cross_bracket" in cohorts
        assert "single_side" in cohorts
        assert cohorts["cross_bracket"]["mean_gross_pnl_cents"] == pytest.approx(50.0)
        assert cohorts["single_side"]["mean_gross_pnl_cents"] == pytest.approx(-50.0)

    def test_average_over_multiple(self, conn):
        # 3 wins + 1 loss at 30¢ = (3*70 + 1*-30) / 4 = 180/4 = 45
        for _ in range(3):
            _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.7, side="yes",
                          price_cents=30, our_trade_won=1)
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.7, side="yes",
                      price_cents=30, our_trade_won=0)
        r = per_leg_summary(conn)[0]
        assert r["n"] == 4
        assert r["mean_gross_pnl_cents"] == pytest.approx(45.0)
        assert r["win_rate"] == pytest.approx(0.75)

    def test_no_side_brier_uses_flipped_outcome(self, conn):
        """Regression: production won_yes column stores "our trade won."
        For NO-side rows the Brier formula must flip it to recover the
        YES-outcome boolean, otherwise Brier is computed against the
        wrong target (this exact bug shipped in v1 of the validator)."""
        # NO-side at 80¢, YES happened (we lost): predicted p_yes=0.05.
        # Correct Brier = (0.05 - 1)^2 = 0.9025.
        # Buggy Brier (using won_yes directly) would be (0.05 - 0)^2 = 0.0025.
        _seed_settled(conn, ticker="KXHIGHNY-T70", p_yes=0.05, side="no",
                      price_cents=80, our_trade_won=0)
        r = per_leg_summary(conn)[0]
        assert r["brier"] == pytest.approx(0.9025), (
            "NO-side Brier must flip won_yes to recover YES outcome"
        )


class TestPerPortfolioSummary:
    def test_sums_legs_per_portfolio(self, conn):
        # Portfolio of 3 legs:
        #   leg 1: YES @ 30¢, our trade won → +70
        #   leg 2: NO  @ 80¢, our trade won → +20
        #   leg 3: YES @ 50¢, our trade lost → -50
        # Total portfolio gross PnL = +40
        for ticker, side, price, our_won in [
            ("KXHIGHNY-T70", "yes", 30, 1),
            ("KXHIGHNY-T75", "no", 80, 1),  # NO-side win = our_trade_won=1
            ("KXHIGHNY-T80", "yes", 50, 0),
        ]:
            _seed_settled(conn, ticker=ticker, p_yes=0.5, side=side,
                          price_cents=price, our_trade_won=our_won,
                          market_id="KXHIGHNY-26APR30",
                          portfolio_leg_count=6)
        rows = per_portfolio_summary(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["family"] == "KXHIGHNY"
        assert r["n_portfolios"] == 1
        assert r["mean_legs_fired"] == pytest.approx(3.0)
        assert r["mean_legs_total"] == pytest.approx(6.0)
        assert r["mean_portfolio_gross_cents"] == pytest.approx(40.0)

    def test_excludes_single_side_rows(self, conn):
        # market_id=NULL row should not appear in portfolio rollup
        _seed_settled(conn, ticker="KXFED-T425", p_yes=0.5, side="yes",
                      price_cents=50, our_trade_won=1)
        rows = per_portfolio_summary(conn)
        assert rows == []

    def test_empty_db(self, conn):
        assert per_leg_summary(conn) == []
        assert per_portfolio_summary(conn) == []
