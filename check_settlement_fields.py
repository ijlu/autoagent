#!/usr/bin/env python3
"""Check exact fields in Kalshi settlement API response."""
import os, sys, json
os.chdir(os.path.dirname(os.path.abspath(__file__)))
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k] = v

import trade
resp = trade.api_get("/portfolio/settlements?limit=5")
settlements = resp.get("settlements", [])
print(f"Settlement count: {len(settlements)}")
for s in settlements[:3]:
    print(json.dumps(s, indent=2))
    print("---")

# Also check what the positions endpoint returns for settled
print("\n=== Settled positions fields ===")
resp2 = trade.api_get("/portfolio/positions?limit=3&settlement_status=settled")
for key in resp2:
    if isinstance(resp2[key], list) and resp2[key]:
        print(json.dumps(resp2[key][0], indent=2))
        break
