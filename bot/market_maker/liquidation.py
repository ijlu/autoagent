"""Liquidation of expiring MM positions.

Extracted from trade.py mm_liquidate_expiring() (lines 7120-7252).
Handles two cases:
  1. Markets already settled/closed with confirmed result -> record settlement P&L.
  2. Markets expiring within 1 hour -> post aggressive exit orders.

Safety invariant: NEVER zero inventory without confirmed settlement or confirmed
flattening fill.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from bot.api import api_get, api_post
from bot.market_maker.inventory import mm_get_inventory
from bot.config import MM_DRY_RUN, MM_ORDER_TAG


def mm_liquidate_expiring(conn):
    """Check for MM inventory in markets that are about to expire (<1h) or already expired.
    - Expiring (<1h): post aggressive exit orders to avoid holding through settlement.
    - Already expired/settled/closed with confirmed result: record settlement P&L.
    - SAFETY: never zero inventory without confirmed settlement or confirmed flattening fill.
    Returns count of liquidation orders posted."""
    liquidated = 0
    try:
        inv_rows = conn.execute(
            "SELECT ticker, net_position, avg_entry_cents FROM mm_inventory WHERE net_position != 0"
        ).fetchall()
        for ticker, net, avg_entry in inv_rows:
            try:
                m_data = api_get(f"/markets/{ticker}")
                market = m_data.get("market", m_data)

                # Check if market is already settled/closed
                status = (market.get("status") or "").lower()
                result = (market.get("result") or market.get("expiration_value") or "").lower()

                if status in ("settled", "closed", "finalized", "determined") and result in ("yes", "no"):
                    # Market settled with confirmed result — safe to zero + record P&L
                    # Convention: avg_entry is YES-equivalent cost basis
                    #   long YES (net>0): avg_entry = what we paid for YES
                    #   short YES (net<0): avg_entry = 100 - what_we_paid_for_NO
                    if result == "yes":
                        # YES pays $1: long YES profits, short YES loses
                        pnl_cents = net * (100 - avg_entry)
                    else:  # result == "no"
                        # NO pays $1: long YES loses, short YES profits
                        pnl_cents = -net * avg_entry

                    # Subtract actual fees from P&L (same as record_settlements path)
                    fee_cents = 0.0
                    try:
                        fee_row = conn.execute(
                            "SELECT COALESCE(SUM(fee_cents), 0) FROM mm_processed_fills WHERE ticker=?",
                            (ticker,)
                        ).fetchone()
                        if fee_row and fee_row[0]:
                            fee_cents = float(fee_row[0])
                    except Exception:
                        pass  # fee_cents column may not exist yet
                    pnl_cents -= fee_cents

                    conn.execute("""UPDATE mm_inventory SET net_position=0,
                                   realized_pnl_cents = realized_pnl_cents + ?
                                   WHERE ticker=?""", (int(pnl_cents), ticker))
                    conn.commit()
                    print(f"[mm] Settled {ticker}: net={net:+d} result={result} "
                          f"pnl=${pnl_cents/100:+.2f} (fees=${fee_cents/100:.2f})")
                    continue

                if status in ("settled", "closed", "finalized", "determined") and result not in ("yes", "no"):
                    # Market closed but no clear result yet — do NOT zero, wait for result
                    print(f"[mm] \u26a0\ufe0f  {ticker}: status={status} but result='{result}' — waiting for confirmed settlement")
                    continue

                close_time = market.get("close_time") or market.get("expiration_time")
                if not close_time:
                    continue
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                hours_left = (ct - datetime.now(timezone.utc)).total_seconds() / 3600

                if hours_left < -1:
                    # Market closed >1h ago but not yet marked settled in API
                    # Do NOT zero — let record_settlements() handle it with confirmed fill data
                    print(f"[mm] \u26a0\ufe0f  {ticker}: closed {-hours_left:.1f}h ago, awaiting settlement confirmation")
                    continue

                if hours_left > 1:
                    continue  # not expiring yet

                # Liquidate: post market-crossing order to exit
                qty = abs(net)
                liq_client_id = f"mm_liq_{ticker.replace('.', '_')}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
                if net > 0:
                    yes_bid = market.get("yes_bid") or market.get("yes_bid_dollars") or 0
                    if isinstance(yes_bid, str):
                        yes_bid = int(float(yes_bid) * 100)
                    elif isinstance(yes_bid, float) and yes_bid < 1:
                        yes_bid = int(yes_bid * 100)
                    no_price = max(1, 100 - yes_bid + 2)
                    order_body = {
                        "ticker": ticker, "side": "no", "type": "limit",
                        "count": qty, "no_price": no_price, "action": "buy",
                        "client_order_id": liq_client_id,
                        "expiration_ts": int(time.time() + 3600)
                    }
                else:
                    yes_ask = market.get("yes_ask") or market.get("yes_ask_dollars") or 99
                    if isinstance(yes_ask, str):
                        yes_ask = int(float(yes_ask) * 100)
                    elif isinstance(yes_ask, float) and yes_ask < 1:
                        yes_ask = int(yes_ask * 100)
                    order_body = {
                        "ticker": ticker, "side": "yes", "type": "limit",
                        "count": qty, "yes_price": min(99, yes_ask + 2), "action": "buy",
                        "client_order_id": liq_client_id,
                        "expiration_ts": int(time.time() + 3600)
                    }

                if not MM_DRY_RUN:
                    resp = api_post("/portfolio/orders", order_body)
                    liq_order_id = resp.get("order", {}).get("order_id", "")
                    # Track in mm_orders so fills are detected by mm_check_fills()
                    if liq_order_id:
                        liq_side = order_body["side"]
                        liq_price = order_body.get("no_price") or order_body.get("yes_price", 0)
                        conn.execute("""INSERT OR IGNORE INTO mm_orders
                            (timestamp, order_id, ticker, side, price_cents, contracts, tag, status, fill_qty, fair_value_cents)
                            VALUES (?, ?, ?, ?, ?, ?, 'liquidation', 'posted', 0, 0)""",
                            (datetime.now(timezone.utc).isoformat(), liq_order_id, ticker, liq_side, liq_price, qty))
                        conn.commit()
                    print(f"[mm] Liquidating {ticker}: {qty} contracts "
                          f"(net={net:+d}, {hours_left:.1f}h left) order={liq_order_id[:12] if liq_order_id else '?'}")
                    liquidated += 1
                else:
                    print(f"[mm] DRY: Would liquidate {ticker}: {qty} contracts")
                    liquidated += 1
            except Exception as e:
                err_str = str(e)
                if "404" in err_str:
                    # Market no longer exists — do NOT zero silently, log for manual review
                    print(f"[mm] \u26a0\ufe0f  {ticker}: market 404 (net={net:+d}) — awaiting settlement to zero")
                elif "400" in err_str:
                    # Cannot trade — market may be frozen/closed, let settlement handle it
                    print(f"[mm] \u26a0\ufe0f  {ticker}: order rejected 400 (net={net:+d}) — awaiting settlement")
                else:
                    print(f"[mm] Error liquidating {ticker}: {e}")
    except Exception as e:
        print(f"[mm] Error in liquidation scan: {e}")
    return liquidated
