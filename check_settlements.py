#!/usr/bin/env python3
"""Check Kalshi settlements from the API."""
import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Load .env
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k] = v

import trade

# Check settlements
print("=== KALSHI SETTLEMENTS ===")
resp = trade.api_get("/portfolio/settlements?limit=50")
settlements = resp.get("settlements", [])
print(f"Total settlements from API: {len(settlements)}")
for s in settlements[:50]:
    ticker = s.get("ticker", "?")
    revenue = s.get("revenue", 0)
    yes_price = s.get("yes_price", "?")
    no_price = s.get("no_price", "?")
    settled_time = s.get("settled_time", "?")
    print(f"  {ticker}  revenue={revenue}  yes={yes_price}  no={no_price}  settled={settled_time}")

# Check portfolio positions
print("\n=== OPEN POSITIONS (from API) ===")
resp2 = trade.api_get("/portfolio/positions?limit=50&settlement_status=unsettled")
positions = resp2.get("market_positions", resp2.get("positions", []))
print(f"Open positions: {len(positions)}")
for p in positions[:20]:
    ticker = p.get("ticker", "?")
    qty = p.get("total_traded", p.get("position", "?"))
    side = "YES" if p.get("market_exposure", 0) > 0 else "NO"
    print(f"  {ticker}  {side}  qty={qty}")

# Check settled positions
print("\n=== SETTLED POSITIONS (from API) ===")
resp3 = trade.api_get("/portfolio/positions?limit=50&settlement_status=settled")
settled_pos = resp3.get("market_positions", resp3.get("positions", []))
print(f"Settled positions: {len(settled_pos)}")
for p in settled_pos[:20]:
    ticker = p.get("ticker", "?")
    payout = p.get("total_payout", p.get("realized_pnl", "?"))
    print(f"  {ticker}  payout={payout}")
