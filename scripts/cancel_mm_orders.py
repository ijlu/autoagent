#!/usr/bin/env python3
"""Cancel all resting MM orders before MM code deletion.

Part of Phase 0 shutdown:
  1. Set BOT_MM_ENABLED=0 in .env
  2. Run this script -- cancels every resting order with client_order_id
     starting with "mm_"
  3. Verify 0 resting MM orders remain
  4. Tag git pre-mm-deletion and delete MM code

Safety: only cancels orders whose client_order_id starts with "mm_".
Manual/external orders are never touched. Idempotent: safe to re-run.

Usage:
    # On VPS (has API keys loaded):
    sudo -u kalshi python3 scripts/cancel_mm_orders.py
    sudo -u kalshi python3 scripts/cancel_mm_orders.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Iterator

# Make sure bot.* imports work when run as a script
sys.path.insert(0, ".")

from bot.api import api_get, api_delete


PAGE_LIMIT = 200


def _iter_resting_mm_orders() -> Iterator[dict]:
    """Yield every resting order whose client_order_id starts with 'mm_'.

    Paginates using cursor until Kalshi stops returning one.
    """
    cursor: str | None = None
    total_seen = 0
    while True:
        path = f"/portfolio/orders?status=resting&limit={PAGE_LIMIT}"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            resp = api_get(path)
        except Exception as e:
            print(f"[cancel_mm] ERROR fetching orders: {e}")
            return
        orders = resp.get("orders", []) or []
        for o in orders:
            total_seen += 1
            cid = (o.get("client_order_id") or "").strip()
            if cid.startswith("mm_"):
                yield o
        cursor = resp.get("cursor")
        if not cursor or not orders:
            break
    print(f"[cancel_mm] scanned {total_seen} resting orders")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cancel resting MM orders.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List orders that would be cancelled; don't cancel.")
    args = parser.parse_args()

    to_cancel = list(_iter_resting_mm_orders())
    if not to_cancel:
        print("[cancel_mm] 0 resting mm_ orders -- nothing to do")
        return 0

    print(f"[cancel_mm] found {len(to_cancel)} resting mm_ orders")
    for o in to_cancel:
        print(f"  - {o.get('order_id')} {o.get('ticker')} "
              f"{o.get('side')}@{o.get('yes_price') or o.get('no_price')}¢ "
              f"[{o.get('client_order_id')}]")

    if args.dry_run:
        print("[cancel_mm] --dry-run set; no cancellations performed")
        return 0

    cancelled = 0
    failed: list[tuple[str, str]] = []
    for o in to_cancel:
        oid = o.get("order_id")
        if not oid:
            continue
        try:
            r = api_delete(f"/portfolio/orders/{oid}")
            if getattr(r, "status_code", 0) in (200, 204):
                cancelled += 1
            else:
                failed.append((oid, f"HTTP {getattr(r, 'status_code', '?')}"))
        except Exception as e:
            failed.append((oid, str(e)))
        # mild throttle -- Kalshi rate limits are generous but we're not in a hurry
        time.sleep(0.05)

    print(f"[cancel_mm] cancelled {cancelled}/{len(to_cancel)}")
    if failed:
        print(f"[cancel_mm] {len(failed)} failures:")
        for oid, err in failed:
            print(f"  - {oid}: {err}")

    # Verify we're at zero
    remaining = list(_iter_resting_mm_orders())
    if remaining:
        print(f"[cancel_mm] WARN: {len(remaining)} mm_ orders still resting after cancel")
        return 2
    print("[cancel_mm] OK: 0 resting mm_ orders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
