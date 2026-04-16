"""Market-maker fill detection and order cancellation.

Extracted from trade.py (lines ~5921-6141).
"""

import time
from datetime import datetime, timezone, timedelta

from bot.api import api_get, api_delete
from bot.market_maker.inventory import mm_get_inventory, mm_update_inventory, _parse_fill_price_cents


def mm_cancel_all_orders(conn):
    """Cancel all resting MM orders. We re-post fresh ones each cycle.
    Returns count of orders cancelled."""
    cancelled = 0
    try:
        orders = api_get("/portfolio/orders?status=resting&limit=200").get("orders", [])
        # Identify MM orders by checking our DB
        mm_order_ids = set()
        rows = conn.execute(
            "SELECT order_id FROM mm_orders WHERE status='posted'"
        ).fetchall()
        for row in rows:
            mm_order_ids.add(row[0])

        for o in orders:
            oid = o.get("order_id", "")
            if oid in mm_order_ids:
                try:
                    r = api_delete(f"/portfolio/orders/{oid}")
                    if r.status_code in (200, 204):
                        conn.execute("UPDATE mm_orders SET status='cancelled' WHERE order_id=?", (oid,))
                        cancelled += 1
                    else:
                        # Cancel failed — do NOT mark as cancelled, order may still be live
                        print(f"[mm] Cancel {oid} got HTTP {r.status_code} — order may still be live")
                        conn.execute("UPDATE mm_orders SET status='cancel_failed' WHERE order_id=?", (oid,))
                except Exception as e:
                    print(f"[mm] Failed to cancel {oid}: {e}")
                    # Network error — do NOT mark as cancelled, order may still be live
                    conn.execute("UPDATE mm_orders SET status='cancel_failed' WHERE order_id=?", (oid,))

        # Also mark any old 'posted' orders not found in API as expired/stale
        for oid in mm_order_ids:
            api_oids = {o.get("order_id", "") for o in orders}
            if oid not in api_oids:
                conn.execute("UPDATE mm_orders SET status='expired' WHERE order_id=? AND status='posted'", (oid,))
        conn.commit()
    except Exception as e:
        print(f"[mm] Error cancelling orders: {e}")
    return cancelled


def mm_check_fills(conn):
    """Check for new fills using Kalshi's /portfolio/fills endpoint (ground truth).
    Falls back to order-status inference if fills endpoint unavailable.
    Returns number of new fills detected.

    ATOMICITY: inventory update, fill-id insert, and order-state update are
    wrapped in a single transaction per fill batch. mm_update_inventory() does
    NOT commit internally."""
    fills = 0

    # Ensure tables exist
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_processed_fills (
        fill_id TEXT PRIMARY KEY, processed_at TEXT, fee_cents REAL DEFAULT 0,
        order_id TEXT DEFAULT '', ticker TEXT DEFAULT '')""")
    # Add columns if upgrading from old schema
    for col, coldef in [("fee_cents", "REAL DEFAULT 0"), ("order_id", "TEXT DEFAULT ''"), ("ticker", "TEXT DEFAULT ''")]:
        try:
            conn.execute(f"SELECT {col} FROM mm_processed_fills LIMIT 1")
        except Exception:
            try:
                conn.execute(f"ALTER TABLE mm_processed_fills ADD COLUMN {col} {coldef}")
            except Exception:
                pass

    try:
        # --- Paginated fill retrieval with high-water mark ---
        # Get last-seen fill timestamp for incremental sync
        last_ts_row = conn.execute(
            "SELECT MAX(processed_at) FROM mm_processed_fills").fetchone()
        min_ts_param = ""
        if last_ts_row and last_ts_row[0]:
            # Subtract 5 minutes for safety overlap (dedupe handles repeats)
            try:
                last_dt = datetime.fromisoformat(last_ts_row[0].replace("Z", "+00:00"))
                safe_dt = last_dt - timedelta(minutes=5)
                min_ts_param = f"&min_ts={int(safe_dt.timestamp())}"
            except Exception:
                pass

        # Get ALL known MM order IDs (not just 'posted' — catches late fills on canceled orders)
        mm_oids = set(r[0] for r in conn.execute(
            "SELECT order_id FROM mm_orders").fetchall())
        processed = set(r[0] for r in conn.execute(
            "SELECT fill_id FROM mm_processed_fills").fetchall())

        # Paginate through fills using cursor
        cursor = ""
        total_api_fills = 0
        while True:
            url = f"/portfolio/fills?limit=1000{min_ts_param}"
            if cursor:
                url += f"&cursor={cursor}"
            resp = api_get(url)
            api_fills = resp.get("fills", [])
            total_api_fills += len(api_fills)

            for f in api_fills:
                fill_id = f.get("fill_id") or f.get("trade_id") or ""
                if not fill_id or fill_id in processed:
                    continue
                order_id = f.get("order_id", "")
                if order_id not in mm_oids:
                    continue

                # Parse fill details
                ticker = f.get("ticker") or f.get("market_ticker", "")
                side = f.get("side", "")
                count_raw = f.get("count_fp") or f.get("count", 0)
                fill_qty = max(1, round(float(count_raw))) if count_raw else 0
                # CRITICAL: use side-appropriate price (audit fix #1)
                price_cents = _parse_fill_price_cents(f, side)
                # Actual fee from Kalshi (in dollars → cents)
                fee_raw = f.get("fee_cost") or f.get("fee_cost_dollars") or 0
                fee_cents = float(fee_raw) * 100 if fee_raw and float(fee_raw) < 100 else float(fee_raw or 0)

                if fill_qty <= 0:
                    continue

                # ATOMIC: inventory + fill record + order state in one transaction
                mm_update_inventory(conn, ticker, side, fill_qty, price_cents)
                conn.execute("INSERT OR IGNORE INTO mm_processed_fills (fill_id, processed_at, fee_cents, order_id, ticker) VALUES (?, ?, ?, ?, ?)",
                            (fill_id, datetime.now(timezone.utc).isoformat(), fee_cents, order_id, ticker))
                conn.execute(
                    "UPDATE mm_orders SET fill_qty = fill_qty + ?, "
                    "status = CASE WHEN fill_qty + ? >= contracts THEN 'filled' ELSE status END "
                    "WHERE order_id = ?",
                    (fill_qty, fill_qty, order_id))
                fills += 1
                processed.add(fill_id)  # avoid re-processing within same batch
                is_taker = f.get("is_taker", False)
                print(f"[mm] Fill: {ticker} {side} x{fill_qty} @ {price_cents}\u00a2 "
                      f"({'taker' if is_taker else 'maker'}) fee={fee_cents:.1f}\u00a2 [fill_id={fill_id[:12]}]")

            # Commit after each page (atomic per page)
            conn.commit()

            # Check for next page
            next_cursor = resp.get("cursor", "")
            if not next_cursor or not api_fills:
                break
            cursor = next_cursor

        if fills > 0 or total_api_fills > 0:
            return fills  # fills endpoint worked — trust it

    except Exception as e:
        print(f"[mm] Fills endpoint error ({e}), falling back to order-status inference")

    # Fallback: infer fills from order status (less reliable but works pre-migration)
    try:
        orders = api_get("/portfolio/orders?status=executed&limit=200").get("orders", [])
        executed_ids = {o.get("order_id"): o for o in orders if o.get("order_id")}

        rows = conn.execute(
            "SELECT id, order_id, ticker, side, price_cents, contracts, fill_qty FROM mm_orders WHERE status='posted'"
        ).fetchall()

        for row_id, oid, ticker, side, price_cents, contracts, prev_fill in rows:
            if oid in executed_ids:
                exec_order = executed_ids[oid]
                fill_count_raw = exec_order.get("fill_count_fp") or exec_order.get("count_fp") or exec_order.get("count", contracts)
                total_filled = round(float(fill_count_raw))
                new_fills = total_filled - (prev_fill or 0)
                if new_fills > 0:
                    mm_update_inventory(conn, ticker, side, new_fills, price_cents)
                    conn.execute(
                        "UPDATE mm_orders SET status='filled', fill_qty=? WHERE id=?",
                        (total_filled, row_id))
                    fills += 1
                    print(f"[mm] Fill (fallback): {ticker} {side} x{new_fills} @ {price_cents}\u00a2")

        # Check partially filled resting orders
        resting = api_get("/portfolio/orders?status=resting&limit=200").get("orders", [])
        resting_ids = {o.get("order_id"): o for o in resting if o.get("order_id")}
        rows2 = conn.execute(
            "SELECT id, order_id, ticker, side, price_cents, contracts, fill_qty FROM mm_orders WHERE status='posted'"
        ).fetchall()
        for row_id, oid, ticker, side, price_cents, contracts, prev_fill in rows2:
            if oid in resting_ids:
                o = resting_ids[oid]
                remaining_raw = o.get("remaining_count_fp") or o.get("remaining_count", contracts)
                remaining = round(float(remaining_raw))
                new_fills = contracts - remaining - (prev_fill or 0)
                if new_fills > 0:
                    mm_update_inventory(conn, ticker, side, new_fills, price_cents)
                    conn.execute(
                        "UPDATE mm_orders SET fill_qty=? WHERE id=?",
                        ((prev_fill or 0) + new_fills, row_id))
                    fills += 1
                    print(f"[mm] Partial fill (fallback): {ticker} {side} x{new_fills} @ {price_cents}\u00a2")

        conn.commit()
    except Exception as e:
        print(f"[mm] Error in fallback fill check: {e}")
    return fills
