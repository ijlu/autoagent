"""Regression guard for CLAUDE.md Known Bug Pattern #10.

Any MM quoting path MUST post a spread wide enough that even a perfect round
trip (we get hit on both sides at our quoted prices) covers the round-trip
maker fees. Otherwise we book negative expected value on every fill pair.

``WeatherQuoter.compute_quote_prices`` enforces this via
``_fee_floor_half_spread`` (which is computed from ``kalshi_maker_fee``).
This test pins the invariant: at every plausible fair value, the post-floor
``ask - bid`` exceeds ``2 * kalshi_maker_fee`` for the full quote size.
"""

from __future__ import annotations

import pytest

from bot.config import MM_ORDER_SIZE
from bot.core.money import kalshi_maker_fee
from bot.daemon.weather_quoter import WeatherQuoter


@pytest.mark.parametrize("fair_value_cents", [5, 10, 25, 40, 50, 60, 75, 90, 95])
@pytest.mark.parametrize("requested_half_spread", [0, 1, 2, 5])
def test_compute_quote_prices_covers_round_trip_maker_fee(
    fair_value_cents: int, requested_half_spread: int
) -> None:
    bid, ask, effective_hs = WeatherQuoter.compute_quote_prices(
        fair_value_cents=fair_value_cents,
        half_spread=requested_half_spread,
        inventory=0,
    )
    spread_cents = ask - bid
    round_trip_fee_cents = 2 * kalshi_maker_fee(MM_ORDER_SIZE, fair_value_cents)
    # Spread is in cents-per-contract; fee is total cents on a full quote of
    # MM_ORDER_SIZE. Convert fee to cents-per-contract for an apples-to-apples
    # comparison. (Spread × order_size = total cents earned on a perfect round
    # trip; that must dominate the total round-trip fee.)
    spread_total_cents = spread_cents * MM_ORDER_SIZE
    assert spread_total_cents >= round_trip_fee_cents, (
        f"fv={fair_value_cents}c req_hs={requested_half_spread}c: "
        f"effective_hs={effective_hs}c bid={bid} ask={ask} spread={spread_cents}c "
        f"× size={MM_ORDER_SIZE} = {spread_total_cents}c earned on perfect "
        f"round-trip, but round-trip maker fee = {round_trip_fee_cents}c. "
        f"_fee_floor_half_spread is letting through a negative-EV quote "
        f"(CLAUDE.md Known Bug Pattern #10)."
    )


def test_compute_quote_prices_respects_inventory_skew_invariants() -> None:
    """Sanity: skew never inverts bid/ask or pushes them outside [1, 99]."""
    for inv in (-50, -10, 0, 10, 50):
        bid, ask, _ = WeatherQuoter.compute_quote_prices(
            fair_value_cents=50, half_spread=2, inventory=inv,
        )
        assert 1 <= bid < ask <= 99, f"inv={inv} produced bid={bid} ask={ask}"
