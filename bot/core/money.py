"""Canonical fee formulas and P&L math for Kalshi trading.

This is the ONE source of truth for all money math in the bot.
Every other module must import from here — never inline fee or P&L calculations.

Kalshi Fee Schedule (as of 2026):
  Taker fee: roundup(0.07 * contracts * price * (1 - price))
  Maker fee: roundup(0.0175 * contracts * price * (1 - price))

  Where price is in dollars (0-1 range), and roundup is math.ceil on the
  TOTAL fee (not per-contract), in cents.

  Maker fees are 4x cheaper than taker fees. Always prefer limit (maker) orders.

Known bug regression watchlist:
  1. _apply_trade() short-close P&L was inverted (avg_entry - price, not price - avg_entry)
  2. record_settlements() missing fee subtraction
  3. int(float(...)) rounding — always use round(float(...))
  4. Exit paths zeroing inventory without settlement confirmation (original
     offender mm_liquidate_expiring was removed; pattern still applies to
     manage_positions and any future exit policy)
  5. MM spread not checked against expected maker fees
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple


# ---------------------------------------------------------------------------
# Fee calculation — exact Kalshi formulas
# ---------------------------------------------------------------------------

def kalshi_taker_fee(contracts: int, price_cents: int) -> int:
    """Compute Kalshi taker fee in cents.

    Args:
        contracts: Number of contracts.
        price_cents: Price per contract in cents (1-99).

    Returns:
        Fee in cents (always >= 0). Rounded up on the TOTAL, not per-contract.

    Example: 10 contracts at 50¢ → ceil(7 * 10 * 50 * 50 / 10000) = ceil(17.5) = 18¢

    Uses integer arithmetic to avoid floating-point ceil() errors
    (e.g., ceil(28.000000000000004) → 29 instead of 28).
    """
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    # Formula: ceil(0.07 * C * P/100 * (1-P/100) * 100)
    #        = ceil(7 * C * P * (100-P) / 10000)
    # Integer ceil: (n + d - 1) // d
    numerator = 7 * contracts * price_cents * (100 - price_cents)
    return (numerator + 9999) // 10000


def kalshi_maker_fee(contracts: int, price_cents: int) -> int:
    """Compute Kalshi maker fee in cents.

    Args:
        contracts: Number of contracts.
        price_cents: Price per contract in cents (1-99).

    Returns:
        Fee in cents (always >= 0). Rounded up on the TOTAL, not per-contract.

    Example: 10 contracts at 50¢ → ceil(175 * 10 * 50 * 50 / 1000000) = ceil(4.375) = 5¢

    Uses integer arithmetic to avoid floating-point ceil() errors.
    """
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    # Formula: ceil(0.0175 * C * P/100 * (1-P/100) * 100)
    #        = ceil(175 * C * P * (100-P) / 1000000)
    numerator = 175 * contracts * price_cents * (100 - price_cents)
    return (numerator + 999999) // 1000000


def fee_per_contract_cents(price_cents: int, maker: bool = True) -> float:
    """Average fee per contract at a given price level.

    Useful for quick estimates. For actual order fees, use kalshi_maker_fee()
    or kalshi_taker_fee() which handle the ceil-on-total correctly.
    """
    if price_cents <= 0 or price_cents >= 100:
        return 0.0
    p = price_cents / 100.0
    rate = 0.0175 if maker else 0.07
    return rate * p * (1.0 - p) * 100  # in cents


def min_profitable_spread_cents(price_cents: int, contracts: int = 10) -> int:
    """Minimum spread (in cents) needed for a round-trip MM trade to be profitable.

    Both sides are maker orders, so total fee = 2 * maker_fee.
    Spread must exceed total fees for the trade to be profitable.
    """
    fee_per_side = kalshi_maker_fee(contracts, price_cents)
    total_fees = 2 * fee_per_side
    # Spread needed = total_fees / contracts, rounded up
    return math.ceil(total_fees / max(1, contracts))


# ---------------------------------------------------------------------------
# Position math: _apply_trade (extracted from trade.py:5813)
# ---------------------------------------------------------------------------

def apply_trade(
    net: int,
    avg_entry: float,
    side: str,
    qty: int,
    price_cents: float,
) -> Tuple[int, float, float]:
    """Pure math: compute new (net_position, avg_entry, realized_pnl) after a trade.

    Convention: avg_entry is always YES-equivalent cost basis (in cents).
      Long YES (net > 0):  avg_entry = what we paid per YES contract
      Short YES (net < 0): avg_entry = 100 - what_we_paid_per_NO (YES-equivalent)

    Args:
        net: Current net position (positive = long YES, negative = short YES/long NO).
        avg_entry: Current YES-equivalent average entry price in cents.
        side: "yes" or "no" — the side of the new trade.
        qty: Number of contracts traded (always positive).
        price_cents: The SIDE-APPROPRIATE price in cents:
            YES fill -> yes_price in cents
            NO fill  -> no_price in cents (NOT yes_price!)

    Returns:
        (new_net, new_avg_entry, realized_pnl_cents)

    Handles 4 cases per side:
      1. Adding to existing same-direction position  -> weighted avg
      2. Reducing opposite position without flipping -> keep old avg, realize P&L
      3. Flipping from opposite to same direction    -> realize P&L, new avg for excess
      4. Opening fresh position                      -> avg = trade price
    """
    realized_pnl = 0.0

    if side == "yes":
        new_net = net + qty
        if net >= 0:
            # Case 1/4: adding to long or opening fresh long
            total_cost = avg_entry * abs(net) + price_cents * qty
            new_avg = total_cost / max(1, abs(new_net))
        elif qty <= abs(net):
            # Case 2: reducing short without flipping
            closed = qty
            # Short had YES-equiv avg_entry (e.g. 60 means we effectively sold YES at 60c).
            # Buying YES at price_cents to close. P&L = avg_entry - price_cents per contract.
            # Example: short at avg_entry=60, close by buying YES at 45 -> profit = 60-45 = +15c
            realized_pnl = closed * (avg_entry - price_cents)
            new_avg = avg_entry  # remaining short keeps its avg
        else:
            # Case 3: flipping from short to long
            closed = abs(net)
            realized_pnl = closed * (avg_entry - price_cents)
            new_avg = price_cents  # new long position at trade price
    else:
        # NO side: convert to YES-equivalent for storage
        yes_equiv = 100.0 - price_cents
        new_net = net - qty
        if net <= 0:
            # Case 1/4: adding to short or opening fresh short
            total_cost = avg_entry * abs(net) + yes_equiv * qty
            new_avg = total_cost / max(1, abs(new_net)) if new_net != 0 else 0.0
        elif qty <= net:
            # Case 2: reducing long without flipping
            closed = qty
            exit_price = 100.0 - price_cents  # what we get for exiting YES via NO sale
            realized_pnl = closed * (exit_price - avg_entry)
            new_avg = avg_entry  # remaining long keeps its avg
        else:
            # Case 3: flipping from long to short
            closed = net
            exit_price = 100.0 - price_cents
            realized_pnl = closed * (exit_price - avg_entry)
            new_avg = yes_equiv  # new short at YES-equivalent

    return new_net, new_avg, realized_pnl


# ---------------------------------------------------------------------------
# Settlement P&L
# ---------------------------------------------------------------------------

def settlement_pnl(
    net: int,
    avg_entry: float,
    result: str,
    fee_cents: int = 0,
) -> int:
    """Compute P&L in cents when a market settles.

    Args:
        net: Net position at settlement (positive = long YES, negative = short YES).
        avg_entry: YES-equivalent average entry price in cents.
        result: "yes" or "no" — the market outcome.
        fee_cents: Total fees paid (from Kalshi API), in cents.

    Returns:
        Net profit in cents (negative = loss). Fee is subtracted.

    Settlement math (YES-equivalent convention):
      Result = YES: pnl = net * (100 - avg_entry) - fees
      Result = NO:  pnl = -net * avg_entry - fees

    For long YES (net > 0):
      YES wins: profit = net * (100 - avg_entry)  [we bought cheap, settle at 100]
      NO wins:  loss   = -net * avg_entry          [we paid avg_entry, get nothing]

    For short YES / long NO (net < 0):
      YES wins: loss   = net * (100 - avg_entry)   [net is negative, so this is a loss]
      NO wins:  profit = -net * avg_entry           [net is negative, so -net is positive]
    """
    if net == 0:
        return -fee_cents

    if result == "yes":
        gross = net * (100.0 - avg_entry)
    elif result == "no":
        gross = -net * avg_entry
    else:
        return -fee_cents  # unknown result

    return round(gross) - fee_cents


# ---------------------------------------------------------------------------
# Round-trip cost estimation (for trade screening)
# ---------------------------------------------------------------------------

@dataclass
class RoundTripCost:
    """Estimated round-trip cost for entering and exiting a position."""

    entry_fee_cents: int
    exit_fee_cents: int
    exit_spread_cents: int
    total_cents: int

    @property
    def per_contract_cents(self) -> float:
        return self.total_cents / max(1, self._contracts)

    _contracts: int = 1


def estimate_round_trip_cost(
    contracts: int,
    entry_price_cents: int,
    exit_spread_cents: int = 3,
    entry_maker: bool = True,
    exit_maker: bool = False,
) -> RoundTripCost:
    """Estimate total round-trip cost for a directional trade.

    Args:
        contracts: Number of contracts.
        entry_price_cents: Entry price in cents.
        exit_spread_cents: Expected exit slippage in cents (default 3).
        entry_maker: Whether entry is a maker (limit) order.
        exit_maker: Whether exit is a maker (limit) order.

    Returns:
        RoundTripCost with itemized breakdown.
    """
    entry_fee = (kalshi_maker_fee if entry_maker else kalshi_taker_fee)(
        contracts, entry_price_cents
    )
    # Estimate exit price as entry_price +/- spread (doesn't matter for fee calc
    # since fee depends on P*(1-P) which is symmetric around 50)
    exit_fee = (kalshi_maker_fee if exit_maker else kalshi_taker_fee)(
        contracts, entry_price_cents
    )
    spread_cost = exit_spread_cents * contracts
    total = entry_fee + exit_fee + spread_cost

    result = RoundTripCost(
        entry_fee_cents=entry_fee,
        exit_fee_cents=exit_fee,
        exit_spread_cents=spread_cost,
        total_cents=total,
    )
    result._contracts = contracts
    return result


def edge_after_costs(
    edge_cents: float,
    contracts: int,
    price_cents: int,
    exit_spread_cents: int = 3,
    entry_maker: bool = True,
    exit_maker: bool = False,
) -> float:
    """Compute edge remaining after all estimated round-trip costs.

    Args:
        edge_cents: Raw edge in cents (our_prob - market_prob) * 100.
        contracts: Number of contracts.
        price_cents: Entry price in cents.
        exit_spread_cents: Expected exit slippage.
        entry_maker: Maker entry.
        exit_maker: Maker exit.

    Returns:
        Net edge per contract in cents. Negative = unprofitable.
    """
    rt = estimate_round_trip_cost(
        contracts, price_cents, exit_spread_cents, entry_maker, exit_maker
    )
    return edge_cents - (rt.total_cents / max(1, contracts))
