"""Regression tests for P&L math: apply_trade() and settlement_pnl().

Every known bug from audit history is captured as a regression fixture here.
If any of these tests break, we've reintroduced a fixed bug.

Known bug history:
  - BUG #1 (2026-04-10): _apply_trade() short-close used (price - avg_entry) instead
    of (avg_entry - price). Fixed: P&L = avg_entry - price_cents for short close.
  - BUG #2 (2026-04-10): record_settlements() missing fee_cost subtraction.
  - BUG #3 (2026-04-10): settlement won = revenue > 0 instead of profit > 0.
  - BUG #4 (2026-04-10): mm_liquidate_expiring() zeroed inventory before settlement.
  - BUG #5 (2026-05-12): record_settlements() trusted Kalshi's ``revenue`` field,
    which has returned 0 for valid winning settlements since 2026-04-12. This
    made every hedged cross-bracket position (1 YES + 1 NO) show as a ~$0.90
    loss instead of a ~$0.10 win, because the $1.00 payout never landed in the
    revenue field. Fix: derive revenue locally from {yes,no}_count_fp × winning-
    side payout via settlement_revenue_cents.
"""

import pytest
from bot.core.money import (
    apply_trade,
    estimate_round_trip_cost,
    settlement_pnl,
    settlement_revenue_cents,
)


# ═══════════════════════════════════════════════════════════════════════════════
# apply_trade() — YES side
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplyTradeYes:

    def test_open_fresh_long(self):
        """Buy 10 YES at 40c with no existing position."""
        net, avg, pnl = apply_trade(0, 0, "yes", 10, 40)
        assert net == 10
        assert avg == 40.0
        assert pnl == 0.0

    def test_add_to_long(self):
        """Buy 5 more YES at 50c, already long 10 at 40c."""
        net, avg, pnl = apply_trade(10, 40, "yes", 5, 50)
        assert net == 15
        # Weighted avg: (40*10 + 50*5) / 15 = 650/15 = 43.33...
        assert abs(avg - 43.333) < 0.01
        assert pnl == 0.0

    def test_reduce_short_without_flip(self):
        """Short 10 at avg_entry=60, buy 5 YES at 45c to reduce."""
        net, avg, pnl = apply_trade(-10, 60, "yes", 5, 45)
        assert net == -5
        assert avg == 60.0  # remaining short keeps avg
        # P&L: 5 * (60 - 45) = +75c profit
        assert pnl == 75.0

    def test_reduce_short_exact_close(self):
        """Close entire short by buying exactly the right amount."""
        net, avg, pnl = apply_trade(-10, 60, "yes", 10, 45)
        assert net == 0
        # P&L: 10 * (60 - 45) = +150c
        assert pnl == 150.0

    def test_flip_short_to_long(self):
        """Short 5 at 60, buy 8 YES at 45 — flip to long 3."""
        net, avg, pnl = apply_trade(-5, 60, "yes", 8, 45)
        assert net == 3
        assert avg == 45.0  # new long at trade price
        # Close 5 short: 5 * (60 - 45) = +75c
        assert pnl == 75.0

    def test_regression_short_close_pnl_direction(self):
        """REGRESSION: BUG #1 — short close P&L must be (avg_entry - price), NOT (price - avg_entry).

        Short at avg_entry=70, close by buying YES at 50 -> profit = 70-50 = +20/contract.
        The bug had this inverted: (50-70) = -20, turning profitable close into a loss.
        """
        net, avg, pnl = apply_trade(-10, 70, "yes", 10, 50)
        assert pnl == 200.0  # 10 * (70 - 50) = +200c PROFIT
        assert pnl > 0, "Short close at lower price MUST be profitable"

    def test_regression_short_close_at_loss(self):
        """Short at 40 (bad entry), close by buying YES at 60 -> loss = 40-60 = -20/contract."""
        net, avg, pnl = apply_trade(-10, 40, "yes", 10, 60)
        assert pnl == -200.0  # 10 * (40 - 60) = -200c LOSS
        assert pnl < 0, "Short close at higher price MUST be a loss"


# ═══════════════════════════════════════════════════════════════════════════════
# apply_trade() — NO side
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplyTradeNo:

    def test_open_fresh_short(self):
        """Buy 10 NO at 40c (yes_equiv = 60c) with no existing position."""
        net, avg, pnl = apply_trade(0, 0, "no", 10, 40)
        assert net == -10
        assert avg == 60.0  # YES-equivalent: 100 - 40 = 60
        assert pnl == 0.0

    def test_add_to_short(self):
        """Buy 5 more NO at 30c, already short 10 at avg_entry=60 (YES-equiv)."""
        net, avg, pnl = apply_trade(-10, 60, "no", 5, 30)
        assert net == -15
        # YES-equiv of new: 100 - 30 = 70
        # Weighted: (60*10 + 70*5) / 15 = 950/15 = 63.33
        assert abs(avg - 63.333) < 0.01
        assert pnl == 0.0

    def test_reduce_long_without_flip(self):
        """Long 10 at avg_entry=40, sell via buying 5 NO at 55c."""
        net, avg, pnl = apply_trade(10, 40, "no", 5, 55)
        assert net == 5
        assert avg == 40.0  # remaining long keeps avg
        # Exit price YES-equiv: 100 - 55 = 45
        # P&L: 5 * (45 - 40) = +25c
        assert pnl == 25.0

    def test_reduce_long_at_loss(self):
        """Long 10 at avg_entry=60, exit via NO at 50c (yes_equiv=50)."""
        net, avg, pnl = apply_trade(10, 60, "no", 5, 50)
        assert net == 5
        # Exit price: 100 - 50 = 50
        # P&L: 5 * (50 - 60) = -50c loss
        assert pnl == -50.0

    def test_flip_long_to_short(self):
        """Long 5 at 40, buy 8 NO at 55c — flip to short 3."""
        net, avg, pnl = apply_trade(5, 40, "no", 8, 55)
        assert net == -3
        # Close 5 long: exit_price = 100-55 = 45. P&L = 5*(45-40) = +25c
        assert pnl == 25.0
        # New short avg: YES-equiv of 55c NO = 45
        assert avg == 45.0


# ═══════════════════════════════════════════════════════════════════════════════
# settlement_pnl()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSettlementPnl:

    def test_long_yes_wins(self):
        """Long 10 YES at 40c, settles YES -> profit = 10 * (100-40) = 600c."""
        pnl = settlement_pnl(10, 40, "yes", fee_cents=0)
        assert pnl == 600

    def test_long_yes_loses(self):
        """Long 10 YES at 40c, settles NO -> loss = -10 * 40 = -400c."""
        pnl = settlement_pnl(10, 40, "no", fee_cents=0)
        assert pnl == -400

    def test_short_yes_loses(self):
        """Short 10 (net=-10) at avg_entry=60, settles YES -> loss = -10 * (100-60) = -400c."""
        pnl = settlement_pnl(-10, 60, "yes", fee_cents=0)
        assert pnl == -400

    def test_short_yes_wins(self):
        """Short 10 (net=-10) at avg_entry=60, settles NO -> profit = 10 * 60 = 600c."""
        pnl = settlement_pnl(-10, 60, "no", fee_cents=0)
        assert pnl == 600

    def test_regression_fee_subtraction(self):
        """REGRESSION: BUG #2 — fees MUST be subtracted from settlement P&L.

        Without fee subtraction, a $6.00 gross profit with $0.50 fees would
        incorrectly show as $6.00 instead of $5.50.
        """
        pnl_no_fee = settlement_pnl(10, 40, "yes", fee_cents=0)
        pnl_with_fee = settlement_pnl(10, 40, "yes", fee_cents=50)
        assert pnl_no_fee == 600
        assert pnl_with_fee == 550
        assert pnl_with_fee < pnl_no_fee, "Fees MUST reduce P&L"

    def test_regression_won_is_profit_based(self):
        """REGRESSION: BUG #3 — 'won' should be profit > 0, not revenue > 0.

        A trade with $1 revenue but $2 fees is a LOSS, not a win.
        """
        # Scenario: long 1 YES at 99c, settles YES
        # Revenue = 1 * (100 - 99) = 1c, but with 5c fee -> profit = -4c
        pnl = settlement_pnl(1, 99, "yes", fee_cents=5)
        assert pnl == -4  # 1 - 5 = -4
        assert pnl < 0, "Small revenue with large fee MUST be a loss"

    def test_zero_position(self):
        pnl = settlement_pnl(0, 50, "yes", fee_cents=10)
        assert pnl == -10  # just fees

    def test_unknown_result(self):
        pnl = settlement_pnl(10, 50, "unknown", fee_cents=5)
        assert pnl == -5  # just fees


# ═══════════════════════════════════════════════════════════════════════════════
# settlement_revenue_cents() — Kalshi-revenue-field-drift recovery (BUG #5)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSettlementRevenueCents:
    """Pin the canonical revenue formula. Kalshi's /portfolio/settlements
    endpoint has been returning ``revenue=0`` even for valid winning
    settlements (verified live 2026-05-12 + historical settlements DB).
    The bot must derive revenue locally to avoid silently reporting all
    hedged winners as ~$0.90 losses.
    """

    def test_pure_yes_winner(self):
        """Held 10 YES, market settled YES → payout = 10 × $1 = 1000¢."""
        assert settlement_revenue_cents(10, 0, "yes") == 1000

    def test_pure_yes_loser(self):
        """Held 10 YES, market settled NO → 0¢."""
        assert settlement_revenue_cents(10, 0, "no") == 0

    def test_pure_no_winner(self):
        """Held 5 NO, market settled NO → 500¢."""
        assert settlement_revenue_cents(0, 5, "no") == 500

    def test_pure_no_loser(self):
        """Held 5 NO, market settled YES → 0¢."""
        assert settlement_revenue_cents(0, 5, "yes") == 0

    def test_balanced_hedge_yes_outcome(self):
        """REGRESSION (BUG #5): Hedged 1 YES + 1 NO, settles YES → 100¢
        (the YES leg). Pre-fix, ``revenue=0`` from Kalshi made this look
        like a 0-payout total loss, costing ~$1.00 per hedged ticker.
        13 cross-bracket positions on 2026-05-04 → -12 were mis-reported
        as such; cross-bracket strategy's reported -$13.39 P&L was
        actually closer to -$3.35 once the hedge math was honest.
        """
        assert settlement_revenue_cents(1, 1, "yes") == 100

    def test_balanced_hedge_no_outcome(self):
        """Same hedge, settles NO → 100¢ (the NO leg)."""
        assert settlement_revenue_cents(1, 1, "no") == 100

    def test_balanced_hedge_payout_is_outcome_invariant(self):
        """The hedge guarantee: payout is the same regardless of which
        side wins, because exactly one side wins fully. The bot uses
        this to lock in spread when it has paper profit on a NO.
        """
        yes_outcome = settlement_revenue_cents(3, 3, "yes")
        no_outcome = settlement_revenue_cents(3, 3, "no")
        assert yes_outcome == no_outcome == 300

    def test_asymmetric_hedge(self):
        """Held 1 YES + 2 NO, settles YES → 100¢ (1×100).
        Held 1 YES + 2 NO, settles NO  → 200¢ (2×100).
        Different total payout, but each side's contribution is its own
        count × $1 on win."""
        assert settlement_revenue_cents(1, 2, "yes") == 100
        assert settlement_revenue_cents(1, 2, "no") == 200

    def test_no_contracts_held(self):
        """No position at settlement → 0¢ (shouldn't happen if the
        settlement is even being recorded, but safe default)."""
        assert settlement_revenue_cents(0, 0, "yes") == 0
        assert settlement_revenue_cents(0, 0, "no") == 0

    def test_unknown_market_result(self):
        """Empty / unknown market_result → 0¢. Conservative: report a
        full loss when the API hasn't told us the outcome. Caller's
        downstream profit math will then show ``profit = -cost - fees``,
        which is the safe assumption."""
        assert settlement_revenue_cents(10, 0, "") == 0
        assert settlement_revenue_cents(10, 0, "unknown") == 0

    def test_fractional_counts_round_to_nearest_cent(self):
        """Kalshi reports counts as fixed-point strings (``"3.00"``);
        callers pass float() of those. round() handles edge cases like
        ``2.9999...`` from float conversion of ``"3.00"`` cleanly.
        Same defensive rounding as the dual-format parser fix."""
        assert settlement_revenue_cents(2.9999999, 0, "yes") == 300
        assert settlement_revenue_cents(0, 1.0000001, "no") == 100

    def test_record_settlements_call_pattern(self):
        """Models the exact call shape from trade.py:record_settlements.
        If this test breaks, that call site needs a matching change."""
        # Hedged winner — the case that motivated the fix
        kalshi_payload = {
            "yes_count_fp": "1.00",
            "no_count_fp": "1.00",
            "market_result": "yes",
            "revenue": 0,  # this is the lie Kalshi tells us
            "yes_total_cost_dollars": "0.12",
            "no_total_cost_dollars": "0.75",
            "fee_cost": "0.03",
        }
        yes_count = float(kalshi_payload["yes_count_fp"])
        no_count = float(kalshi_payload["no_count_fp"])
        result = kalshi_payload["market_result"]
        revenue = settlement_revenue_cents(yes_count, no_count, result)
        assert revenue == 100  # not 0 (which is what Kalshi reported)

        # Full profit math the way record_settlements computes it
        yes_cost = float(kalshi_payload["yes_total_cost_dollars"]) * 100
        no_cost = float(kalshi_payload["no_total_cost_dollars"]) * 100
        total_cost = round(yes_cost + no_cost)  # 87
        fee_cents = round(float(kalshi_payload["fee_cost"]) * 100)  # 3
        profit = revenue - total_cost - fee_cents  # 100 - 87 - 3
        assert profit == 10
        assert profit > 0, "hedged position with hedge LOCKED IN positive P&L"


# ═══════════════════════════════════════════════════════════════════════════════
# Round-trip cost estimation
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundTripCost:

    def test_maker_entry_taker_exit(self):
        rt = estimate_round_trip_cost(10, 50, exit_spread_cents=3,
                                      entry_maker=True, exit_maker=False)
        assert rt.entry_fee_cents >= 0
        assert rt.exit_fee_cents >= rt.entry_fee_cents  # taker >= maker
        assert rt.exit_spread_cents == 30  # 3c * 10 contracts
        assert rt.total_cents == rt.entry_fee_cents + rt.exit_fee_cents + rt.exit_spread_cents

    def test_both_maker(self):
        rt = estimate_round_trip_cost(10, 50, exit_spread_cents=0,
                                      entry_maker=True, exit_maker=True)
        # Both sides maker, no spread
        assert rt.total_cents == rt.entry_fee_cents + rt.exit_fee_cents

    def test_much_cheaper_than_old_estimate(self):
        """The old bot estimated 3c/contract/side + 3c exit spread = ~9c/contract round-trip.
        Real maker costs are 0.44c/contract/side at 50c -> total ~1c + spread."""
        rt = estimate_round_trip_cost(10, 50, exit_spread_cents=3,
                                      entry_maker=True, exit_maker=True)
        old_estimate = 10 * (3 + 3 + 3)  # 3c entry + 3c exit + 3c spread per contract
        assert rt.total_cents < old_estimate


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases and boundary conditions
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_apply_trade_single_contract(self):
        """Minimal trade: 1 contract."""
        net, avg, pnl = apply_trade(0, 0, "yes", 1, 50)
        assert net == 1
        assert avg == 50.0

    def test_apply_trade_extreme_prices(self):
        """Prices near boundaries."""
        net, avg, pnl = apply_trade(0, 0, "yes", 1, 1)
        assert net == 1
        assert avg == 1.0

        net, avg, pnl = apply_trade(0, 0, "yes", 1, 99)
        assert net == 1
        assert avg == 99.0

    def test_settlement_extreme_entry(self):
        """Entry at 1c, settles YES -> huge profit."""
        pnl = settlement_pnl(10, 1, "yes")
        assert pnl == 990  # 10 * (100-1) = 990

    def test_settlement_near_100(self):
        """Entry at 99c, settles YES -> tiny profit."""
        pnl = settlement_pnl(10, 99, "yes")
        assert pnl == 10  # 10 * (100-99) = 10

    def test_apply_trade_no_side_zero_net(self):
        """NO trade that exactly zeros a long position."""
        net, avg, pnl = apply_trade(10, 40, "no", 10, 55)
        assert net == 0
        # exit_price = 100 - 55 = 45
        # P&L = 10 * (45 - 40) = 50c
        assert pnl == 50.0
