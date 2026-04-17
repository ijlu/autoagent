"""Loss post-mortem classification.

Classifies every loss into actionable categories so we learn WHY we lose,
not just that we lost.

Directional loss categories (from trades table):
  - bad_source: our estimate was >30% off from market consensus at entry
  - efficient_market: edge was thin (<7%) and market was right
  - adverse_selection: price moved against us right after entry (informed traders on other side)
  - timing: direction was right but market hadn't converged yet (early entry)
  - fee_erosion: would have been profitable pre-fees but fees ate the edge
"""

from __future__ import annotations

from datetime import datetime, timezone

from bot.core.categorization import categorize_market

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
