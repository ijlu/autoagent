"""Tests for settlement-driven learning population from alpha_backtest.

Covers:
- fill_settlement_for_ticker: per-row won_yes + counterfactual P&L,
  idempotency, cross-side correctness
- populate_calibration: bucket assignment by p(our-side), provenance tag,
  idempotency
- populate_timing_patterns: hour/dow/category/edge computation
- populate_edge_convergence: convergence_pct computation against settlement
- populate_postmortems: loss classification and provenance
- populate_all: total counts + second-run no-op
"""

from __future__ import annotations

import pytest

from bot.db import init_db
from bot.learning.alpha_log import (
    DecisionOutcome, DecisionType, EnsembleSnapshot, MarketSnapshot,
    fill_settlement_for_ticker, log_decision,
)
from bot.learning.populate_from_alpha import (
    populate_all, populate_calibration, populate_edge_convergence,
    populate_postmortems, populate_timing_patterns,
)


@pytest.fixture
def conn():
    return init_db(":memory:")


def _seed(conn, *, ticker, side, price=52, contracts=10,
          p_yes=0.60, market=None, dtype=DecisionType.DIRECTIONAL_SHADOW,
          outcome=DecisionOutcome.SHADOW_ONLY):
    market = market if market is not None else MarketSnapshot(
        yes_bid_cents=48, yes_ask_cents=52,
    )
    return log_decision(
        conn, ticker=ticker, decision_type=dtype, decision_outcome=outcome,
        ensemble=EnsembleSnapshot(p_yes=p_yes, source_count=3),
        market=market, side=side, price_cents=price, contracts=contracts,
    )


# ══════════════════════════════════════════════════════════════════════════
# fill_settlement_for_ticker
# ══════════════════════════════════════════════════════════════════════════
class TestFillSettlementForTicker:
    def test_yes_side_yes_result(self, conn):
        rid = _seed(conn, ticker="KXFED-26MAY-T425", side="yes",
                   price=52, contracts=10)
        n = fill_settlement_for_ticker(conn, ticker="KXFED-26MAY-T425",
                                       settlement_result="yes")
        assert n == 1
        row = conn.execute(
            "SELECT won_yes, realized_pnl_cents, settlement_result "
            "FROM alpha_backtest WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == 1
        assert row[1] == 10 * (100 - 52)  # 480¢
        assert row[2] == "yes"

    def test_yes_side_no_result_loses(self, conn):
        _seed(conn, ticker="KXFED-26MAY-T425", side="yes",
              price=52, contracts=10)
        fill_settlement_for_ticker(conn, ticker="KXFED-26MAY-T425",
                                   settlement_result="no")
        row = conn.execute(
            "SELECT won_yes, realized_pnl_cents FROM alpha_backtest WHERE ticker=?",
            ("KXFED-26MAY-T425",),
        ).fetchone()
        assert row[0] == 0
        assert row[1] == -10 * 52  # -520¢

    def test_no_side_no_result_wins(self, conn):
        _seed(conn, ticker="KXHIGHMIA-26APR18-T75", side="no",
              price=40, contracts=5)
        fill_settlement_for_ticker(conn, ticker="KXHIGHMIA-26APR18-T75",
                                   settlement_result="no")
        row = conn.execute(
            "SELECT won_yes, realized_pnl_cents FROM alpha_backtest WHERE ticker=?",
            ("KXHIGHMIA-26APR18-T75",),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == 5 * (100 - 40)  # 300¢

    def test_both_sides_same_ticker(self, conn):
        _seed(conn, ticker="KXETH-B2500", side="yes", price=55, contracts=4)
        _seed(conn, ticker="KXETH-B2500", side="no", price=45, contracts=6)
        n = fill_settlement_for_ticker(conn, ticker="KXETH-B2500",
                                       settlement_result="yes")
        assert n == 2
        rows = conn.execute(
            "SELECT side, won_yes, realized_pnl_cents FROM alpha_backtest "
            "WHERE ticker=? ORDER BY side", ("KXETH-B2500",),
        ).fetchall()
        # alphabetical: no, yes
        assert rows[0] == ("no", 0, -6 * 45)
        assert rows[1] == ("yes", 1, 4 * (100 - 55))

    def test_idempotent(self, conn):
        _seed(conn, ticker="KXBTC-B40000", side="yes")
        n1 = fill_settlement_for_ticker(conn, ticker="KXBTC-B40000",
                                        settlement_result="yes")
        n2 = fill_settlement_for_ticker(conn, ticker="KXBTC-B40000",
                                        settlement_result="yes")
        assert n1 == 1
        assert n2 == 0

    def test_bad_result_noop(self, conn):
        _seed(conn, ticker="KXFOO", side="yes")
        n = fill_settlement_for_ticker(conn, ticker="KXFOO",
                                       settlement_result="void")
        assert n == 0


# ══════════════════════════════════════════════════════════════════════════
# populate_calibration
# ══════════════════════════════════════════════════════════════════════════
class TestPopulateCalibration:
    def test_yes_side_bucket(self, conn):
        _seed(conn, ticker="KXFED-26MAY-T425", side="yes", p_yes=0.62)
        fill_settlement_for_ticker(conn, ticker="KXFED-26MAY-T425",
                                   settlement_result="yes")
        n = populate_calibration(conn)
        assert n == 1
        row = conn.execute(
            "SELECT estimated_prob, actual_outcome, bucket, source_desc, alpha_id "
            "FROM calibration WHERE alpha_id IS NOT NULL"
        ).fetchone()
        assert row[0] == pytest.approx(0.62)
        assert row[1] == 1
        assert row[2] == "0.6-0.7"
        assert row[3] == "alpha:shadow"
        assert row[4] is not None

    def test_no_side_bucket_uses_our_prob(self, conn):
        # ensemble_p_yes=0.3 → NO side thinks p(our win)=0.7 → bucket 0.7-0.8
        _seed(conn, ticker="KXHIGHMIA", side="no", p_yes=0.3)
        fill_settlement_for_ticker(conn, ticker="KXHIGHMIA",
                                   settlement_result="no")
        populate_calibration(conn)
        row = conn.execute(
            "SELECT estimated_prob, bucket, actual_outcome FROM calibration "
            "WHERE alpha_id IS NOT NULL"
        ).fetchone()
        assert row[0] == pytest.approx(0.7)
        assert row[1] == "0.7-0.8"
        assert row[2] == 1  # NO side + NO result = won

    def test_skips_unsettled(self, conn):
        _seed(conn, ticker="KXFOO", side="yes")
        # No settlement → no calibration row
        assert populate_calibration(conn) == 0

    def test_idempotent(self, conn):
        _seed(conn, ticker="KXFOO", side="yes")
        fill_settlement_for_ticker(conn, ticker="KXFOO",
                                   settlement_result="yes")
        assert populate_calibration(conn) == 1
        assert populate_calibration(conn) == 0


# ══════════════════════════════════════════════════════════════════════════
# populate_timing_patterns
# ══════════════════════════════════════════════════════════════════════════
class TestPopulateTimingPatterns:
    def test_hour_dow_from_ts_decision(self, conn):
        _seed(conn, ticker="KXFED-26MAY-T425", side="yes", p_yes=0.6)
        fill_settlement_for_ticker(conn, ticker="KXFED-26MAY-T425",
                                   settlement_result="yes")
        n = populate_timing_patterns(conn)
        assert n == 1
        row = conn.execute(
            "SELECT hour_utc, day_of_week, source, won, alpha_id "
            "FROM timing_patterns WHERE alpha_id IS NOT NULL"
        ).fetchone()
        assert 0 <= row[0] <= 23
        assert 0 <= row[1] <= 6
        assert row[2] == "alpha:directional_shadow"
        assert row[3] == 1

    def test_edge_computation(self, conn):
        # ensemble=0.7, mid market=0.5 (bid 48/ask 52 → 0.50), YES side
        # edge = 0.7 - 0.5 = 0.2
        _seed(conn, ticker="KXFED-26MAY-T425", side="yes", p_yes=0.7,
              market=MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52))
        fill_settlement_for_ticker(conn, ticker="KXFED-26MAY-T425",
                                   settlement_result="yes")
        populate_timing_patterns(conn)
        edge = conn.execute(
            "SELECT edge FROM timing_patterns WHERE alpha_id IS NOT NULL"
        ).fetchone()[0]
        assert edge == pytest.approx(0.2)

    def test_idempotent(self, conn):
        _seed(conn, ticker="KXFOO", side="yes")
        fill_settlement_for_ticker(conn, ticker="KXFOO",
                                   settlement_result="yes")
        assert populate_timing_patterns(conn) == 1
        assert populate_timing_patterns(conn) == 0


# ══════════════════════════════════════════════════════════════════════════
# populate_edge_convergence
# ══════════════════════════════════════════════════════════════════════════
class TestPopulateEdgeConvergence:
    def test_converged_true_when_closer_than_market(self, conn):
        # market_prob=0.50, our_prob=0.80, settlement=yes (1.0)
        # our_gap = 0.20, mkt_gap = 0.50
        # conv_pct = (0.50 - 0.20)/0.50 = 0.60 → converged
        _seed(conn, ticker="KXFED", side="yes", p_yes=0.80,
              market=MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52))
        fill_settlement_for_ticker(conn, ticker="KXFED",
                                   settlement_result="yes")
        n = populate_edge_convergence(conn)
        assert n == 1
        row = conn.execute(
            "SELECT converged, convergence_pct, our_estimate, "
            "market_price_at_entry, market_price_after_24h "
            "FROM edge_convergence WHERE alpha_id IS NOT NULL"
        ).fetchone()
        assert row[0] == 1
        assert row[1] == pytest.approx(0.60)
        assert row[2] == pytest.approx(0.80)
        assert row[3] == pytest.approx(0.50)
        assert row[4] == 1.0  # settlement yes → 1.0

    def test_diverged_negative(self, conn):
        # our_prob=0.20, market=0.50, settlement=yes (1.0)
        # our_gap=0.80, mkt_gap=0.50 → conv_pct = (0.50-0.80)/0.50 = -0.60
        _seed(conn, ticker="KXFED", side="yes", p_yes=0.20,
              market=MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52))
        fill_settlement_for_ticker(conn, ticker="KXFED",
                                   settlement_result="yes")
        populate_edge_convergence(conn)
        conv = conn.execute(
            "SELECT converged, convergence_pct FROM edge_convergence "
            "WHERE alpha_id IS NOT NULL"
        ).fetchone()
        assert conv[0] == 0
        assert conv[1] == pytest.approx(-0.60)

    def test_skips_when_market_at_truth(self, conn):
        # Market at 100¢ (= 1.0), settlement yes (1.0) → mkt_gap=0, skip
        _seed(conn, ticker="KXFED", side="yes", p_yes=0.95,
              market=MarketSnapshot(yes_bid_cents=100, yes_ask_cents=100))
        fill_settlement_for_ticker(conn, ticker="KXFED",
                                   settlement_result="yes")
        n = populate_edge_convergence(conn)
        assert n == 0


# ══════════════════════════════════════════════════════════════════════════
# populate_postmortems
# ══════════════════════════════════════════════════════════════════════════
class TestPopulatePostmortems:
    def test_bad_source_classification(self, conn):
        # our_prob=0.9, market=0.5 (diff 0.4 > 0.3 threshold) + lost
        _seed(conn, ticker="KXFED", side="yes", p_yes=0.90,
              market=MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52))
        fill_settlement_for_ticker(conn, ticker="KXFED",
                                   settlement_result="no")
        n = populate_postmortems(conn)
        assert n == 1
        row = conn.execute(
            "SELECT loss_type, source_combo, category FROM loss_postmortems "
            "WHERE alpha_id IS NOT NULL"
        ).fetchone()
        assert row[0] == "bad_source"
        assert row[1] == "alpha:directional_shadow"

    def test_efficient_market_classification(self, conn):
        # our_prob=0.52, market=0.50 (edge 0.02 < 0.07) + lost
        _seed(conn, ticker="KXFED", side="yes", p_yes=0.52,
              market=MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52))
        fill_settlement_for_ticker(conn, ticker="KXFED",
                                   settlement_result="no")
        populate_postmortems(conn)
        lt = conn.execute(
            "SELECT loss_type FROM loss_postmortems WHERE alpha_id IS NOT NULL"
        ).fetchone()[0]
        assert lt == "efficient_market"

    def test_skips_wins(self, conn):
        _seed(conn, ticker="KXFED", side="yes", p_yes=0.62)
        fill_settlement_for_ticker(conn, ticker="KXFED",
                                   settlement_result="yes")  # won
        assert populate_postmortems(conn) == 0


# ══════════════════════════════════════════════════════════════════════════
# populate_all orchestrator
# ══════════════════════════════════════════════════════════════════════════
class TestPopulateAll:
    def test_counts_and_idempotency(self, conn):
        _seed(conn, ticker="KXFED", side="yes", p_yes=0.85)
        _seed(conn, ticker="KXHIGHMIA", side="no", p_yes=0.4)
        fill_settlement_for_ticker(conn, ticker="KXFED", settlement_result="no")
        fill_settlement_for_ticker(conn, ticker="KXHIGHMIA", settlement_result="no")

        r1 = populate_all(conn)
        # Both are losses: KXFED lost (yes side, no result), KXHIGHMIA won
        # (no side, no result) → only 1 postmortem
        assert r1["calibration"] == 2
        assert r1["timing_patterns"] == 2
        # edge_convergence: both should be populated (market 0.50)
        assert r1["edge_convergence"] == 2
        assert r1["postmortems"] == 1  # only KXFED lost

        r2 = populate_all(conn)
        assert all(v == 0 for v in r2.values()), f"second run not idempotent: {r2}"
