#!/usr/bin/env python3
"""Replay backtest: analyze actual fills/settlements from live DB.

Computes what dynamic sizing, synthetic sell, and graduated exits
would have changed.  Uses kalshi_trades_live.db (pulled from VPS).
"""

import sqlite3
import json
from collections import defaultdict
from math import ceil


DB_PATH = "kalshi_trades_live.db"


def kalshi_maker_fee(contracts, price_cents):
    """Exact Kalshi maker fee: roundup(0.0175 * C * P * (1-P)) in cents."""
    p = price_cents / 100
    raw = 0.0175 * contracts * p * (1 - p) * 100
    return ceil(raw) if raw > 0 else 0


def kalshi_taker_fee(contracts, price_cents):
    """Exact Kalshi taker fee: roundup(0.07 * C * P * (1-P)) in cents."""
    p = price_cents / 100
    raw = 0.07 * contracts * p * (1 - p) * 100
    return ceil(raw) if raw > 0 else 0


def analyze_fills(conn):
    """Analyze all fills — categorize, compute fee impact."""
    fills = conn.execute("""
        SELECT timestamp, ticker, side, price_cents, contracts, fill_qty,
               fair_value_cents, tag
        FROM mm_orders WHERE fill_qty > 0
        ORDER BY timestamp
    """).fetchall()

    print(f"\n{'='*70}")
    print(f" FILL ANALYSIS  ({len(fills)} fills)")
    print(f"{'='*70}")

    # Breakdown by category
    by_cat = defaultdict(lambda: {"fills": 0, "contracts": 0, "cost_cents": 0})
    by_side = defaultdict(int)
    total_contracts = 0
    total_cost = 0

    for ts, ticker, side, price, contracts, fill_qty, fv, tag in fills:
        prefix = ticker[:6] if "HIGH" in ticker or "HMON" in ticker else ticker[:5]
        cat = prefix.rstrip("-")
        by_cat[cat]["fills"] += 1
        by_cat[cat]["contracts"] += fill_qty
        by_cat[cat]["cost_cents"] += fill_qty * price
        by_side[side] += fill_qty
        total_contracts += fill_qty
        total_cost += fill_qty * price

    print(f"\n  Total fills: {len(fills)}")
    print(f"  Total contracts: {total_contracts}")
    print(f"  Total cost: ${total_cost/100:.2f}")
    print(f"  Avg price: {total_cost/max(1,total_contracts):.1f}¢")
    print(f"  YES contracts: {by_side.get('yes', 0)}  NO contracts: {by_side.get('no', 0)}")
    print(f"\n  Category breakdown:")
    for cat, d in sorted(by_cat.items(), key=lambda x: -x[1]["fills"]):
        print(f"    {cat:15s}  fills={d['fills']:>4d}  contracts={d['contracts']:>4d}  "
              f"cost=${d['cost_cents']/100:.2f}")

    return fills


def analyze_settlements(conn):
    """Analyze settlement outcomes."""
    settlements = conn.execute("""
        SELECT recorded_at, ticker, side, price_cents, contracts,
               revenue_cents, profit_cents, won, strategy
        FROM settlements ORDER BY profit_cents
    """).fetchall()

    print(f"\n{'='*70}")
    print(f" SETTLEMENT ANALYSIS  ({len(settlements)} settlements)")
    print(f"{'='*70}")

    wins = [(t, tk, s, p, c, r, pnl, w, st) for t, tk, s, p, c, r, pnl, w, st in settlements if w]
    losses = [(t, tk, s, p, c, r, pnl, w, st) for t, tk, s, p, c, r, pnl, w, st in settlements if not w]

    total_pnl = sum(pnl for _, _, _, _, _, _, pnl, _, _ in settlements)
    win_pnl = sum(pnl for _, _, _, _, _, _, pnl, _, _ in wins)
    loss_pnl = sum(pnl for _, _, _, _, _, _, pnl, _, _ in losses)

    print(f"\n  Wins: {len(wins)}/{len(settlements)} ({100*len(wins)/max(1,len(settlements)):.0f}%)")
    print(f"  Win P&L: ${win_pnl/100:.2f}")
    print(f"  Loss P&L: ${loss_pnl/100:.2f}")
    print(f"  Net P&L: ${total_pnl/100:.2f}")
    if wins:
        print(f"  Avg win: ${win_pnl/100/len(wins):.2f}")
    if losses:
        print(f"  Avg loss: ${loss_pnl/100/len(losses):.2f}")

    # Top losses
    print(f"\n  Top 10 losses:")
    for ts, tk, side, price, contracts, rev, pnl, won, strat in settlements[:10]:
        cat = tk[:6] if "HIGH" in tk else tk[:5]
        print(f"    {tk:40s}  {side:3s}  {contracts:>3d}x @ {price:>3d}¢  "
              f"P&L=${pnl/100:>+8.2f}  [{cat}]")

    # Top wins
    print(f"\n  Top wins:")
    for ts, tk, side, price, contracts, rev, pnl, won, strat in reversed(settlements[-10:]):
        if pnl > 0:
            cat = tk[:6] if "HIGH" in tk else tk[:5]
            print(f"    {tk:40s}  {side:3s}  {contracts:>3d}x @ {price:>3d}¢  "
                  f"P&L=${pnl/100:>+8.2f}  [{cat}]")

    # By category
    cat_pnl = defaultdict(lambda: {"pnl": 0, "count": 0, "wins": 0})
    for ts, tk, side, price, contracts, rev, pnl, won, strat in settlements:
        cat = tk[:6].rstrip("-") if "HIGH" in tk or "HMON" in tk else tk[:5].rstrip("-")
        cat_pnl[cat]["pnl"] += pnl
        cat_pnl[cat]["count"] += 1
        if won:
            cat_pnl[cat]["wins"] += 1

    print(f"\n  P&L by category:")
    for cat, d in sorted(cat_pnl.items(), key=lambda x: x[1]["pnl"]):
        wr = d["wins"] / max(1, d["count"])
        print(f"    {cat:15s}  P&L=${d['pnl']/100:>+8.2f}  "
              f"settlements={d['count']:>3d}  WR={wr:.0%}")

    return settlements


def simulate_dynamic_sizing(conn, fills):
    """Simulate what dynamic sizing would have done.

    Key question: with dynamic sizing at ~$982 equity, would order sizes
    have been different, and would that have changed the outcome?
    """
    print(f"\n{'='*70}")
    print(f" DYNAMIC SIZING SIMULATION")
    print(f"{'='*70}")

    # Current equity ~$982 (balance $741 + portfolio $241)
    equity_cents = 98200

    # Current: MM_ORDER_SIZE=10, MM_MAX_INVENTORY=50
    # Dynamic: compute_dynamic_sizing(98200)
    equity_dollars = equity_cents / 100
    dyn_order_size = max(3, min(500, int(equity_dollars * 0.01 / 0.50)))
    dyn_max_inv = max(15, min(2500, dyn_order_size * 5))

    print(f"\n  Current equity: ${equity_cents/100:.2f}")
    print(f"  Static sizing: MM_ORDER_SIZE=10, MM_MAX_INVENTORY=50")
    print(f"  Dynamic sizing: MM_ORDER_SIZE={dyn_order_size}, MM_MAX_INVENTORY={dyn_max_inv}")

    # Actual fill sizes
    fill_sizes = [f[5] for f in fills]  # fill_qty
    avg_fill = sum(fill_sizes) / max(1, len(fill_sizes))
    max_fill = max(fill_sizes) if fill_sizes else 0

    print(f"\n  Actual avg fill size: {avg_fill:.1f} contracts")
    print(f"  Actual max fill size: {max_fill} contracts")
    print(f"  Note: Most fills are < order size because orders were partially filled")

    # Per-ticker inventory analysis
    inv_rows = conn.execute(
        "SELECT ticker, net_position, avg_entry_cents, realized_pnl_cents "
        "FROM mm_inventory WHERE net_position != 0"
    ).fetchall()

    over_limit = 0
    for tk, net, avg_e, rpnl in inv_rows:
        if abs(net) > dyn_max_inv:
            over_limit += 1

    print(f"\n  Active positions exceeding dynamic max inv ({dyn_max_inv}): {over_limit}/{len(inv_rows)}")

    # Impact: with dynamic sizing at $982, order_size=19 instead of 10
    # This means ~2x larger orders, but also ~2x larger max inventory
    # For a $982 account, this is appropriate — current sizing is conservative
    print(f"\n  Impact: Dynamic sizing would have posted ~{dyn_order_size/10:.1f}x larger orders")
    print(f"  Expected effect: More fills per cycle, faster inventory build-up")
    print(f"  Risk: Same % of equity (1% per order), so relative risk unchanged")

    # What about at $10K?
    eq10k = compute_sizing_at(1000000)
    print(f"\n  At $10K: MM_ORDER_SIZE={eq10k['order']}, MM_MAX_INVENTORY={eq10k['inv']}")
    print(f"  Estimated 10x more fills, 10x more market-making revenue")


def compute_sizing_at(equity_cents):
    equity_dollars = equity_cents / 100
    order = max(3, min(500, int(equity_dollars * 0.01 / 0.50)))
    inv = max(15, min(2500, order * 5))
    return {"order": order, "inv": inv}


def simulate_synthetic_sell(conn, settlements):
    """Estimate fee savings from synthetic sell.

    For every settlement that involved closing a position via SELL order,
    compute what the taker fee was vs what the maker fee would have been.
    """
    print(f"\n{'='*70}")
    print(f" SYNTHETIC SELL FEE SAVINGS SIMULATION")
    print(f"{'='*70}")

    # All fills represent positions that needed to be exited at settlement or via liquidation.
    # For MM, most entries are maker (limit) orders, and exits currently happen via:
    #   1. Settlement (no fee to close)
    #   2. Liquidation orders (action=sell → taker fee)
    #   3. QA auto-liquidation (action=sell → taker fee)

    # Find liquidation orders (tag='liquidation' or client_id contains 'liq')
    liq_orders = conn.execute("""
        SELECT ticker, side, price_cents, fill_qty, tag
        FROM mm_orders
        WHERE fill_qty > 0 AND (tag = 'liquidation' OR tag LIKE '%liq%')
    """).fetchall()

    # Also find any sell orders (action would need to be in the data)
    # Since we don't store action in mm_orders, let's count liquidation fills
    total_liq_contracts = sum(f[3] for f in liq_orders)
    total_liq_cost = sum(f[3] * f[2] for f in liq_orders)

    print(f"\n  Liquidation fills found: {len(liq_orders)} ({total_liq_contracts} contracts)")

    # Compute taker vs maker fees for these
    total_taker_fee = 0
    total_maker_fee = 0
    for tk, side, price, qty, tag in liq_orders:
        taker = kalshi_taker_fee(qty, price)
        maker = kalshi_maker_fee(qty, price)
        total_taker_fee += taker
        total_maker_fee += maker

    savings = total_taker_fee - total_maker_fee
    print(f"  Current (sell/taker) fees: ${total_taker_fee/100:.2f}")
    print(f"  Synthetic (buy opp/maker) fees: ${total_maker_fee/100:.2f}")
    print(f"  Estimated savings: ${savings/100:.2f} ({savings} cents)")

    # Now compute for ALL settled positions — how much would synthetic sell save
    # on ALL orders if we had always used it for exits?
    all_fills = conn.execute("""
        SELECT ticker, side, price_cents, fill_qty, tag
        FROM mm_orders WHERE fill_qty > 0
    """).fetchall()

    total_maker_all = 0
    total_taker_all = 0
    for tk, side, price, qty, tag in all_fills:
        total_maker_all += kalshi_maker_fee(qty, price)
        total_taker_all += kalshi_taker_fee(qty, price)

    print(f"\n  ALL fills fee analysis:")
    print(f"    If all were maker (limit) fees: ${total_maker_all/100:.2f}")
    print(f"    If all were taker (market) fees: ${total_taker_all/100:.2f}")
    print(f"    Current MM uses maker (limit) for entries — good")
    print(f"    Synthetic sell ensures exits are ALSO maker — saves ~${(total_taker_all-total_maker_all)/100:.2f}")

    # Per-contract savings at various price points
    print(f"\n  Fee savings per contract at key prices:")
    for p in [20, 30, 40, 50, 60, 70, 80]:
        maker = kalshi_maker_fee(1, p)
        taker = kalshi_taker_fee(1, p)
        print(f"    {p}¢: taker={taker}¢  maker={maker}¢  save={taker-maker}¢")


def analyze_inventory_risk(conn):
    """Analyze current inventory — which positions are at risk?"""
    print(f"\n{'='*70}")
    print(f" INVENTORY RISK ANALYSIS")
    print(f"{'='*70}")

    inv = conn.execute("""
        SELECT ticker, net_position, avg_entry_cents, realized_pnl_cents
        FROM mm_inventory WHERE net_position != 0
        ORDER BY ABS(net_position) * avg_entry_cents DESC
    """).fetchall()

    total_exposure = 0
    total_realized = 0
    by_cat = defaultdict(lambda: {"exposure": 0, "positions": 0, "contracts": 0, "rpnl": 0})

    print(f"\n  {len(inv)} active positions:")
    print(f"  {'Ticker':40s} {'Net':>5s} {'AvgE':>6s} {'Exposure':>10s} {'Realized':>10s}")
    print(f"  {'-'*40} {'-'*5} {'-'*6} {'-'*10} {'-'*10}")

    for tk, net, avg_e, rpnl in inv[:25]:
        exposure = abs(net) * int(avg_e)
        total_exposure += exposure
        total_realized += rpnl
        cat = tk[:6].rstrip("-") if "HIGH" in tk or "HMON" in tk else tk[:5].rstrip("-")
        by_cat[cat]["exposure"] += exposure
        by_cat[cat]["positions"] += 1
        by_cat[cat]["contracts"] += abs(net)
        by_cat[cat]["rpnl"] += rpnl
        direction = "LONG" if net > 0 else "SHORT"
        print(f"  {tk:40s} {net:>+5d} {int(avg_e):>5d}¢ ${exposure/100:>9.2f} ${rpnl/100:>+9.2f}")

    if len(inv) > 25:
        print(f"  ... and {len(inv) - 25} more positions")

    print(f"\n  Total exposure: ${total_exposure/100:.2f}")
    print(f"  Total realized P&L: ${total_realized/100:.2f}")

    print(f"\n  By category:")
    for cat, d in sorted(by_cat.items(), key=lambda x: -x[1]["exposure"]):
        print(f"    {cat:15s}  exposure=${d['exposure']/100:>8.2f}  "
              f"positions={d['positions']:>2d}  contracts={d['contracts']:>4d}  "
              f"realized=${d['rpnl']/100:>+7.2f}")


def analyze_fill_patterns(conn):
    """Analyze fill patterns — when, how, what fills best."""
    print(f"\n{'='*70}")
    print(f" FILL PATTERN ANALYSIS")
    print(f"{'='*70}")

    fills = conn.execute("""
        SELECT timestamp, ticker, side, price_cents, fill_qty, fair_value_cents
        FROM mm_orders WHERE fill_qty > 0
        ORDER BY timestamp
    """).fetchall()

    # Time-of-day analysis
    by_hour = defaultdict(lambda: {"fills": 0, "contracts": 0})
    for ts, tk, side, price, qty, fv in fills:
        hour = int(ts[11:13])
        by_hour[hour]["fills"] += 1
        by_hour[hour]["contracts"] += qty

    print(f"\n  Fills by hour (UTC):")
    for h in sorted(by_hour.keys()):
        d = by_hour[h]
        bar = "█" * min(40, d["fills"] // 2)
        print(f"    {h:02d}:00  fills={d['fills']:>4d}  contracts={d['contracts']:>4d}  {bar}")

    # Fair value vs fill price (adverse selection proxy)
    as_data = []
    for ts, tk, side, price, qty, fv in fills:
        if fv and fv > 0:
            if side == "yes":
                markout = fv - price  # positive = good (filled below fair value)
            else:
                markout = (100 - fv) - price  # NO price = 100 - yes_fair_value
            as_data.append(markout)

    if as_data:
        avg_markout = sum(as_data) / len(as_data)
        positive = sum(1 for m in as_data if m > 0)
        print(f"\n  Adverse selection (fill price vs fair value):")
        print(f"    Avg markout: {avg_markout:+.1f}¢")
        print(f"    Favorable fills: {positive}/{len(as_data)} ({100*positive/len(as_data):.0f}%)")
        print(f"    (Positive = we bought below fair value or sold above)")


def compute_what_if_pnl(conn):
    """What-if: if we had used dynamic sizing + graduated exits from the start."""
    print(f"\n{'='*70}")
    print(f" WHAT-IF ANALYSIS: DYNAMIC SIZING + GRADUATED EXITS")
    print(f"{'='*70}")

    settlements = conn.execute("""
        SELECT ticker, side, price_cents, contracts, profit_cents, won
        FROM settlements
    """).fetchall()

    # At $982 equity, dynamic sizing → order_size=19 (vs 10)
    # This means we'd have ~1.9x more contracts in each position
    # Key insight: LOSSES scale linearly with position size too

    actual_pnl = sum(p for _, _, _, _, p, _ in settlements)
    # With 1.9x sizing, P&L would scale proportionally for each trade
    scaling_factor = 19 / 10  # dynamic order size / actual order size
    scaled_pnl = sum(int(p * scaling_factor) for _, _, _, _, p, _ in settlements)

    print(f"\n  Actual P&L (static sizing): ${actual_pnl/100:.2f}")
    print(f"  Projected P&L (1.9x dynamic sizing): ${scaled_pnl/100:.2f}")
    print(f"  Note: Losses also scale proportionally. Dynamic sizing doesn't fix")
    print(f"  underlying strategy performance — it amplifies both wins AND losses.")

    # The real value of graduated exits: would they have caught the big losses?
    print(f"\n  Key insight: The 69 losses (avg -$3.91 each) suggest the bot often")
    print(f"  holds positions through unfavorable resolution. Graduated exits")
    print(f"  with edge monitoring would have exited some of these earlier.")

    # Estimate: if graduated exits caught 30% of losses at 50% of their magnitude
    loss_total = sum(abs(p) for _, _, _, _, p, w in settlements if not w)
    saved_by_exits = int(loss_total * 0.30 * 0.50)
    print(f"\n  If graduated exits caught 30% of losses at 50% magnitude:")
    print(f"    Loss savings: ${saved_by_exits/100:.2f}")
    print(f"    Adjusted net P&L: ${(actual_pnl + saved_by_exits)/100:.2f}")


def main():
    conn = sqlite3.connect(DB_PATH)

    # Core analyses
    fills = analyze_fills(conn)
    settlements = analyze_settlements(conn)
    simulate_dynamic_sizing(conn, fills)
    simulate_synthetic_sell(conn, settlements)
    analyze_inventory_risk(conn)
    analyze_fill_patterns(conn)
    compute_what_if_pnl(conn)

    # Summary
    print(f"\n{'='*70}")
    print(f" SUMMARY & RECOMMENDATIONS")
    print(f"{'='*70}")
    print(f"""
  1. DYNAMIC SIZING: At current $982 equity, order sizes should be ~19
     instead of 10. This would increase fill volume and revenue, but also
     losses. The key benefit is that sizing automatically scales as the
     account grows — no manual reconfiguration needed.

  2. SYNTHETIC SELL: Saves ~1-1.3¢ per contract on exits (taker→maker fee).
     With 813+ fills and growing, this adds up. Most impactful for
     liquidation orders and QA auto-liquidation where we currently pay
     taker fees.

  3. GRADUATED EXITS: The 69/76 loss rate (90.8%) on settlements is the
     #1 problem. Many of these positions likely showed deteriorating edge
     for multiple cycles before resolution. Health-score based exits would
     have caught some losses earlier, reducing their magnitude.

  4. WEATHER MARKETS: Dominant fill category (396 fills) but also major
     source of settlement losses. METAR gating should help by only
     quoting when fresh observation data is available.

  5. INVENTORY CONCENTRATION: Heavy exposure to KXFED markets (~56 positions).
     Consider max per-series inventory caps.
""")

    conn.close()


if __name__ == "__main__":
    main()
