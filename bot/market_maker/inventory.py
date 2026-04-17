"""Market-maker inventory tracking and quote calculation.

Extracted from trade.py (lines ~5806-5985).
"""

from datetime import datetime, timezone

from bot.core.money import apply_trade
from bot.config import MM_SKEW_PER_10


def mm_get_inventory(conn, ticker):
    """Get current net position for a market. Positive = long YES, negative = short YES (long NO)."""
    row = conn.execute(
        "SELECT net_position, avg_entry_cents FROM mm_inventory WHERE ticker=?", (ticker,)
    ).fetchone()
    return (row[0], row[1]) if row else (0, 0.0)


def mm_update_inventory(conn, ticker, side, qty, price_cents):
    """Update inventory after a fill is detected.
    price_cents must be the SIDE-APPROPRIATE price:
      YES fill → yes_price in cents
      NO fill  → no_price in cents
    NOTE: Does NOT commit — caller must commit the transaction."""
    now = datetime.now(timezone.utc).isoformat()
    net, avg_entry = mm_get_inventory(conn, ticker)

    new_net, new_avg, realized_pnl = apply_trade(net, avg_entry, side, qty, price_cents)

    if round(realized_pnl) != 0:
        conn.execute("""UPDATE mm_inventory SET realized_pnl_cents = realized_pnl_cents + ?
                       WHERE ticker = ?""", (round(realized_pnl), ticker))

    if side == "yes":
        conn.execute("""INSERT INTO mm_inventory (updated_at, ticker, net_position, total_bought, avg_entry_cents)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(ticker) DO UPDATE SET
                       updated_at=?, net_position=?, total_bought=total_bought+?, avg_entry_cents=?""",
                     (now, ticker, new_net, qty, new_avg,
                      now, new_net, qty, new_avg))
    else:
        conn.execute("""INSERT INTO mm_inventory (updated_at, ticker, net_position, total_sold, avg_entry_cents)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(ticker) DO UPDATE SET
                       updated_at=?, net_position=?, total_sold=total_sold+?, avg_entry_cents=?""",
                     (now, ticker, new_net, qty, new_avg,
                      now, new_net, qty, new_avg))
    # NOTE: no conn.commit() here — caller handles transaction boundaries


def mm_calculate_quotes(fair_value_cents, inventory, half_spread):
    """Calculate bid/ask prices, skewed by inventory to encourage rebalancing.
    If we're long (inventory > 0), lower BID (reluctant to buy more) and lower ASK (eager to sell).
    If we're short (inventory < 0), raise ASK (reluctant to sell more) and raise BID (eager to buy).
    The skew moves the MIDPOINT, so both sides shift together — the direction incentivizes
    the counterparty to take the other side of our position."""
    # Continuous skew instead of integer truncation — even 1 contract matters
    skew = int(round(inventory * MM_SKEW_PER_10 / 10.0))
    bid = fair_value_cents - half_spread - skew
    ask = fair_value_cents + half_spread - skew
    # Clamp to valid Kalshi range [1, 99]
    bid = max(1, min(98, bid))
    ask = max(bid + 1, min(99, ask))  # ask must be > bid
    return bid, ask


def _parse_fill_price_cents(fill, side):
    """Extract the correct side-appropriate price from a fill response.
    For YES fills → yes_price. For NO fills → no_price.
    Returns price in cents (integer)."""
    if side == "no":
        # Use no_price_dollars first, then derive from yes_price if missing
        no_raw = fill.get("no_price_dollars") or fill.get("no_price")
        if no_raw:
            v = float(no_raw)
            return int(round(v * 100)) if 0 < v <= 1.0 else int(v)
        # Fallback: derive from yes_price (no_price = 100 - yes_price)
        yes_raw = fill.get("yes_price_dollars") or fill.get("yes_price")
        if yes_raw:
            v = float(yes_raw)
            yes_cents = int(round(v * 100)) if 0 < v <= 1.0 else int(v)
            return 100 - yes_cents
        return 0
    else:
        yes_raw = fill.get("yes_price_dollars") or fill.get("yes_price")
        if yes_raw:
            v = float(yes_raw)
            return int(round(v * 100)) if 0 < v <= 1.0 else int(v)
        return 0
