"""Regression tests for Kalshi fee calculations.

Tests the exact Kalshi fee formulas against known values.
Maker fee: ceil(0.0175 * C * P * (1-P) * 100)  — result in cents
Taker fee: ceil(0.07  * C * P * (1-P) * 100)   — result in cents

Uses integer arithmetic internally to avoid floating-point ceil() errors.
"""

import math
import pytest
from bot.core.money import (
    kalshi_maker_fee,
    kalshi_taker_fee,
    fee_per_contract_cents,
    min_profitable_spread_cents,
)


class TestKalshiTakerFee:
    """Taker fee: ceil(7 * C * P * (100-P) / 10000)"""

    def test_at_50_cents_1_contract(self):
        # 7 * 1 * 50 * 50 / 10000 = 1.75 -> ceil = 2 cents
        assert kalshi_taker_fee(1, 50) == 2

    def test_at_50_cents_10_contracts(self):
        # 7 * 10 * 50 * 50 / 10000 = 17.5 -> ceil = 18 cents
        assert kalshi_taker_fee(10, 50) == 18

    def test_at_50_cents_100_contracts(self):
        # 7 * 100 * 50 * 50 / 10000 = 175 -> ceil = 175 cents
        assert kalshi_taker_fee(100, 50) == 175

    def test_at_20_cents_10_contracts(self):
        # 7 * 10 * 20 * 80 / 10000 = 11.2 -> ceil = 12 cents
        assert kalshi_taker_fee(10, 20) == 12

    def test_at_80_cents_10_contracts(self):
        # Symmetric with 20 cents
        assert kalshi_taker_fee(10, 80) == 12

    def test_at_5_cents_10_contracts(self):
        # 7 * 10 * 5 * 95 / 10000 = 3.325 -> ceil = 4 cents
        assert kalshi_taker_fee(10, 5) == 4

    def test_at_95_cents_10_contracts(self):
        # Symmetric with 5 cents
        assert kalshi_taker_fee(10, 95) == 4

    def test_max_fee_at_50_cents(self):
        # 7 * 1000 * 50 * 50 / 10000 = 1750 -> ceil = 1750
        assert kalshi_taker_fee(1000, 50) == 1750

    def test_zero_contracts(self):
        assert kalshi_taker_fee(0, 50) == 0

    def test_zero_price(self):
        assert kalshi_taker_fee(10, 0) == 0

    def test_price_100(self):
        assert kalshi_taker_fee(10, 100) == 0

    def test_negative_contracts(self):
        assert kalshi_taker_fee(-5, 50) == 0

    def test_symmetry(self):
        """Fee at price P equals fee at price (100-P)."""
        for p in range(1, 100):
            assert kalshi_taker_fee(10, p) == kalshi_taker_fee(10, 100 - p)


class TestKalshiMakerFee:
    """Maker fee: ceil(175 * C * P * (100-P) / 1000000). 4x cheaper than taker."""

    def test_at_50_cents_1_contract(self):
        # 175 * 1 * 50 * 50 / 1000000 = 0.4375 -> ceil = 1 cent
        assert kalshi_maker_fee(1, 50) == 1

    def test_at_50_cents_10_contracts(self):
        # 175 * 10 * 50 * 50 / 1000000 = 4.375 -> ceil = 5
        assert kalshi_maker_fee(10, 50) == 5

    def test_at_50_cents_100_contracts(self):
        # 175 * 100 * 50 * 50 / 1000000 = 43.75 -> ceil = 44
        assert kalshi_maker_fee(100, 50) == 44

    def test_at_50_cents_1000_contracts(self):
        # 175 * 1000 * 50 * 50 / 1000000 = 437.5 -> ceil = 438
        assert kalshi_maker_fee(1000, 50) == 438

    def test_at_20_cents_100_contracts(self):
        # 175 * 100 * 20 * 80 / 1000000 = 28.0 -> ceil = 28 (NOT 29!)
        # This case verifies integer arithmetic avoids the float ceil bug
        assert kalshi_maker_fee(100, 20) == 28

    def test_much_cheaper_than_taker(self):
        """Maker fees should always be <= taker fees."""
        for c in [1, 10, 50, 100, 500]:
            for p in range(5, 96, 5):
                maker = kalshi_maker_fee(c, p)
                taker = kalshi_taker_fee(c, p)
                assert maker <= taker, f"Maker ({maker}) > Taker ({taker}) at c={c}, p={p}"

    def test_zero_edge_cases(self):
        assert kalshi_maker_fee(0, 50) == 0
        assert kalshi_maker_fee(10, 0) == 0
        assert kalshi_maker_fee(10, 100) == 0

    def test_symmetry(self):
        for p in range(1, 100):
            assert kalshi_maker_fee(10, p) == kalshi_maker_fee(10, 100 - p)


class TestMakerVsOldEstimate:
    """The old bot used a flat 3c/contract estimate. Verify maker is much cheaper per contract."""

    def test_typical_mm_order(self):
        # 10 contracts at 50c: maker fee = 5c total (0.5c per contract)
        # Old estimate: 10 * 3 = 30c. Maker is 6x cheaper.
        fee = kalshi_maker_fee(10, 50)
        old_estimate = 10 * 3  # old flat 3c/contract
        assert fee < old_estimate
        assert fee == 5

    def test_typical_directional_taker(self):
        # 5 contracts at 30c: taker = ceil(7*5*30*70/10000) = ceil(7.35) = 8c
        # Old estimate: 5 * 3 = 15c. Taker is ~2x cheaper.
        fee = kalshi_taker_fee(5, 30)
        old_estimate = 5 * 3
        assert fee < old_estimate
        assert fee == 8


class TestMinProfitableSpread:

    def test_at_50_cents(self):
        # Maker fee at 50c for 10 contracts = 5c per side
        # Total fees = 10c. Spread per contract = 10/10 = 1c
        spread = min_profitable_spread_cents(50, 10)
        assert spread == 1

    def test_spread_increases_near_center(self):
        """Spread requirement should be highest near 50c (maximum P*(1-P))."""
        spread_50 = min_profitable_spread_cents(50, 100)
        spread_10 = min_profitable_spread_cents(10, 100)
        assert spread_50 >= spread_10


class TestFeePerContract:

    def test_maker_at_50(self):
        # Per contract: 0.0175 * 0.5 * 0.5 * 100 = 0.4375 cents
        result = fee_per_contract_cents(50, maker=True)
        assert abs(result - 0.4375) < 0.001

    def test_taker_at_50(self):
        # Per contract: 0.07 * 0.5 * 0.5 * 100 = 1.75 cents
        result = fee_per_contract_cents(50, maker=False)
        assert abs(result - 1.75) < 0.001

    def test_edge_cases(self):
        assert fee_per_contract_cents(0) == 0.0
        assert fee_per_contract_cents(100) == 0.0


class TestIntegerArithmeticPrecision:
    """Verify that integer arithmetic avoids floating-point ceil errors."""

    def test_no_floating_point_ceil_bug(self):
        """maker(100, 20) should be 28, not 29.

        With floating point: 0.0175 * 100 * 0.2 * 0.8 * 100 = 28.000000000000004
        math.ceil(28.000000000000004) = 29 (WRONG!)

        With integer arithmetic: 175 * 100 * 20 * 80 = 28000000
        (28000000 + 999999) // 1000000 = 28 (CORRECT!)
        """
        assert kalshi_maker_fee(100, 20) == 28
        assert kalshi_maker_fee(100, 80) == 28  # symmetric

    def test_exact_integer_results(self):
        """When the formula gives an exact integer, ceil should not round up."""
        # taker(10, 20): 7*10*20*80 = 112000 / 10000 = 11.2 -> ceil = 12 (not exact)
        assert kalshi_taker_fee(10, 20) == 12
        # maker(10, 50): 175*10*50*50 = 4375000 / 1000000 = 4.375 -> ceil = 5
        assert kalshi_maker_fee(10, 50) == 5

    def test_all_prices_consistent(self):
        """Fee should be monotonically related to P*(1-P)."""
        for c in [1, 10, 100]:
            # Fee should peak at P=50 and decrease toward extremes
            fee_50 = kalshi_maker_fee(c, 50)
            fee_10 = kalshi_maker_fee(c, 10)
            fee_90 = kalshi_maker_fee(c, 90)
            assert fee_50 >= fee_10, f"c={c}: fee_50={fee_50} < fee_10={fee_10}"
            assert fee_10 == fee_90, f"c={c}: asymmetry fee_10={fee_10} != fee_90={fee_90}"
