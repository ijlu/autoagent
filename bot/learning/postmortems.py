"""Loss post-mortem classification.

Classifies every loss into actionable categories so we learn WHY we lose,
not just that we lost.

Directional loss categories (from trades table):
  - bad_source: our estimate was >30% off from market consensus at entry
  - efficient_market: edge was thin (<7%) and market was right
  - adverse_selection: price moved against us right after entry (informed traders on other side)
  - timing: direction was right but market hadn't converged yet (early entry)
  - fee_erosion: would have been profitable pre-fees but fees ate the edge

MM loss categories (from mm_inventory / mm_orders):
  - mm_adverse_selection: fills consistently on wrong side of fair value
  - mm_inventory_decay: position aged out without paired exit
  - mm_fee_erosion: spread capture eaten by fees
  - mm_directional_loss: net inventory held to settlement on wrong side
"""

from __future__ import annotations

from datetime import datetime, timezone

from bot.market_maker.selection import categorize_market

# Fee calculations — use exact Kalshi formulas
from bot.core.money import fee_per_contract_cents as _fee_per_contract_cents
ESTIMATED_EXIT_SPREAD = 0.03  # 3¢ expected exit slippage

def _round_trip_fee_dollars(price_dollars: float) -> float:
    """Round-trip fee per contract in dollars (maker entry + taker exit)."""
    pc = max(1, min(99, round(price_dollars * 100)))
    entry = _fee_per_contract_cents(pc, maker=True)
    exit_ = _fee_per_contract_cents(pc, maker=False)
    return (entry + exit_) / 100


def run_loss_postmortems(conn):
    """Analyze all unsettled losses and classify them. Run after record_settlements()."""
    now_str = datetime.now(timezone.utc).isoformat()

    # Find losses that haven't been post-mortem'd yet
    losses = conn.execute("""
        SELECT s.order_id, s.ticker, s.revenue_cents, s.profit_cents, s.price_cents, s.contracts,
               t.independent_prob, t.market_prob, t.edge, t.strategy, t.price_cents as entry_price
        FROM settlements s
        LEFT JOIN trades t ON s.order_id = t.order_id
        WHERE s.won = 0
          AND s.order_id NOT IN (SELECT order_id FROM loss_postmortems WHERE order_id IS NOT NULL)
    """).fetchall()

    if not losses:
        return 0

    classified = 0
    for (oid, ticker, revenue, profit, settle_price, contracts,
         est_prob, mkt_prob, edge, strategy, entry_price) in losses:

        loss_type = "unknown"
        detail = ""
        title = ""
        cat = categorize_market(ticker, title)

        if est_prob is not None and mkt_prob is not None and edge is not None:
            # How wrong were we?
            # Compare our estimate to what the market was pricing at entry.
            # A large gap (est >> market) that still lost = bad source signal.
            estimation_error = est_prob - mkt_prob  # our estimate vs market consensus

            if abs(estimation_error) > 0.30:
                loss_type = "bad_source"
                detail = (f"Estimated {est_prob:.0%} probability but lost. "
                         f"Sources: {strategy}. Major estimation failure.")
            elif edge is not None and abs(edge) < 0.07:
                # Fee calculation: entry spread + exit spread + platform fees
                fee_cost = ESTIMATED_EXIT_SPREAD + _round_trip_fee_dollars(mkt_prob if mkt_prob else 0.5)
                if edge > 0 and edge < fee_cost:
                    loss_type = "fee_erosion"
                    detail = (f"Edge of {edge:.1%} was below fee cost ~{fee_cost:.1%}. "
                             f"Would need >{fee_cost:.1%} edge to be profitable after fees.")
                else:
                    loss_type = "efficient_market"
                    detail = (f"Edge was only {edge:.1%}. Market was approximately correct. "
                             f"Our estimate {est_prob:.2f} vs market {mkt_prob:.2f}.")
            elif est_prob is not None and est_prob > 0.55:
                # We were fairly confident but still lost — could be adverse selection
                loss_type = "adverse_selection"
                detail = (f"Confident estimate ({est_prob:.0%}) but lost. "
                         f"Possible informed traders on other side or stale data.")
            else:
                loss_type = "bad_source"
                detail = f"Estimate {est_prob:.2f}, edge {edge:.1%}. Sources: {strategy}"
        else:
            loss_type = "unknown"
            detail = "Missing estimation data for analysis"

        conn.execute("""INSERT INTO loss_postmortems
            (recorded_at, order_id, ticker, category, loss_type, source_combo,
             estimated_prob, market_prob, edge_at_entry, price_at_settlement, detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (now_str, oid, ticker, cat, loss_type, strategy,
             est_prob, mkt_prob, edge, settle_price, detail))
        classified += 1

    conn.commit()

    # Print summary
    if classified > 0:
        summary = conn.execute("""
            SELECT loss_type, COUNT(*) FROM loss_postmortems GROUP BY loss_type ORDER BY COUNT(*) DESC
        """).fetchall()
        print(f"[postmortem] Classified {classified} new losses. All-time breakdown:")
        for lt, count in summary:
            print(f"  {lt}: {count}")

    return classified


def run_mm_postmortems(conn):
    """Analyze MM-specific losses from mm_inventory realized P&L.

    Unlike directional postmortems (which join settlements -> trades),
    MM postmortems analyze mm_inventory tickers with negative realized P&L
    using data from mm_orders and mm_processed_fills.

    Classifies into:
      - mm_adverse_selection: avg fill price was consistently on the wrong side
        of fair value (taker-heavy fills)
      - mm_inventory_decay: position aged out (high inventory age, low turnover)
      - mm_fee_erosion: realized P&L would be positive without fees
      - mm_directional_loss: net directional bet that went wrong
    """
    now_str = datetime.now(timezone.utc).isoformat()

    # Find MM tickers with negative realized P&L not yet analyzed
    losses = conn.execute("""
        SELECT i.ticker, i.realized_pnl_cents, i.net_position,
               i.avg_entry_cents, i.total_bought, i.total_sold
        FROM mm_inventory i
        WHERE i.realized_pnl_cents < -10
          AND i.ticker NOT IN (
              SELECT DISTINCT ticker FROM loss_postmortems
              WHERE source_combo LIKE 'mm:%'
          )
    """).fetchall()

    if not losses:
        return 0

    classified = 0
    for (ticker, realized_pnl, net_pos, avg_entry, total_bought, total_sold) in losses:
        loss_type = "mm_directional_loss"
        detail = ""

        # Get fill count and fee data for this ticker
        fill_data = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(fee_cents), 0)
            FROM mm_processed_fills WHERE ticker = ?
        """, (ticker,)).fetchone()
        fill_count = fill_data[0] if fill_data else 0
        total_fees = fill_data[1] if fill_data else 0

        # Get fair value data from mm_orders
        fv_data = conn.execute("""
            SELECT AVG(fair_value_cents), COUNT(*)
            FROM mm_orders WHERE ticker = ? AND fill_qty > 0
        """, (ticker,)).fetchone()
        avg_fair_value = fv_data[0] if fv_data and fv_data[0] else None
        filled_orders = fv_data[1] if fv_data else 0

        # Classify based on available data
        turnover = total_bought + total_sold
        if total_fees > 0 and realized_pnl + total_fees > 0:
            # Would be profitable without fees
            loss_type = "mm_fee_erosion"
            detail = (f"Realized P&L={realized_pnl/100:.2f}$ but paid "
                     f"{total_fees/100:.2f}$ in fees. "
                     f"Pre-fee P&L would be +{(realized_pnl + total_fees)/100:.2f}$")
        elif turnover > 0 and abs(net_pos) > turnover * 0.5:
            # Most fills are still held (not turned over) — inventory decay
            loss_type = "mm_inventory_decay"
            detail = (f"Net position {net_pos} vs turnover {turnover}. "
                     f"Inventory not actively managed — position aged out.")
        elif avg_fair_value is not None and avg_entry > 0:
            # Check if fills systematically got picked off
            # For long YES: if avg_entry >> fair_value, adverse selection
            # For short YES: if avg_entry << fair_value, adverse selection
            fv_gap = abs(avg_entry - avg_fair_value)
            if fv_gap > 5:  # >5¢ gap between entry and fair value
                loss_type = "mm_adverse_selection"
                detail = (f"Avg entry {avg_entry:.0f}¢ vs avg fair value "
                         f"{avg_fair_value:.0f}¢ (gap={fv_gap:.0f}¢). "
                         f"Fills were systematically on wrong side of fair value.")
            else:
                loss_type = "mm_directional_loss"
                detail = (f"Net position {net_pos}, avg entry {avg_entry:.0f}¢, "
                         f"fair value {avg_fair_value:.0f}¢. "
                         f"Directional exposure on losing side.")
        else:
            detail = (f"Realized P&L={realized_pnl/100:.2f}$, "
                     f"net={net_pos}, fills={fill_count}. "
                     f"Insufficient data for detailed classification.")

        cat = categorize_market(ticker, "")
        conn.execute("""INSERT INTO loss_postmortems
            (recorded_at, order_id, ticker, category, loss_type, source_combo,
             estimated_prob, market_prob, edge_at_entry, price_at_settlement, detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (now_str, None, ticker, cat, loss_type, f"mm:{ticker}",
             avg_fair_value / 100 if avg_fair_value else None,
             avg_entry / 100 if avg_entry else None,
             None, None, detail))
        classified += 1

    conn.commit()

    if classified > 0:
        mm_summary = conn.execute("""
            SELECT loss_type, COUNT(*) FROM loss_postmortems
            WHERE source_combo LIKE 'mm:%'
            GROUP BY loss_type ORDER BY COUNT(*) DESC
        """).fetchall()
        print(f"[mm-postmortem] Classified {classified} new MM losses:")
        for lt, count in mm_summary:
            print(f"  {lt}: {count}")

    return classified
