#!/usr/bin/env python3
"""Comprehensive MM diagnostic: what markets exist, which pass filters, why others fail."""
import os, sys, json
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("MM_DRY_RUN", "true")
os.environ.setdefault("DB_PATH", "/tmp/mm_debug.db")

# Source .env
from pathlib import Path
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import trade

# ── Phase 1: Fetch ALL markets (deep pagination) ──────────────────────────
print("=" * 70)
print("  MM DIAGNOSTIC — Deep Market Scan")
print("=" * 70)
print("\nFetching markets (deep pagination)...")
markets = []
cursor = None
for page in range(20):  # up to 10,000 markets
    url = "/markets?limit=500&status=open"
    if cursor:
        url += f"&cursor={cursor}"
    resp = trade.api_get(url)
    batch = resp.get("markets", [])
    markets.extend(batch)
    cursor = resp.get("cursor")
    parlay_count = sum(1 for m in batch if "KXMVE" in m.get("ticker", ""))
    real_count = len(batch) - parlay_count
    print(f"  Page {page+1}: {len(batch)} markets ({real_count} real, {parlay_count} parlays) — total: {len(markets)}")
    if not cursor or len(batch) < 500:
        break

# ── Phase 1b: Targeted series fetching ────────────────────────────────────
print("\nFetching targeted series...")
TARGET_SERIES = [
    "KXBTC", "KXETH", "KXINX", "KXGDP", "KXCPI", "KXJOB", "KXUNRATE",
    "KXFED", "KXGAS", "KXTEMP", "KXWEATHER", "KXHURR", "KXNBA", "KXNFL",
    "KXMLB", "KXNHL", "KXMMA", "KXSOCCER", "KXFOOTBALL", "KXNCAA",
]
seen_tickers = {m.get("ticker") for m in markets}
targeted_count = 0
for series in TARGET_SERIES:
    try:
        resp = trade.api_get(f"/markets?limit=200&status=open&series_ticker={series}")
        added = 0
        for m in resp.get("markets", []):
            t = m.get("ticker", "")
            if t and t not in seen_tickers:
                markets.append(m)
                seen_tickers.add(t)
                targeted_count += 1
                added += 1
        if added:
            print(f"  {series}: +{added} markets")
    except Exception as e:
        pass

print(f"  Targeted fetch added {targeted_count} markets")
print(f"\nTotal markets: {len(markets)}")

# ── Phase 2: Separate parlays from real markets ───────────────────────────
parlays = []
real_markets = []
for m in markets:
    ticker = m.get("ticker", "")
    if "KXMVE" in ticker or "MULTIGAME" in ticker or m.get("mve_collection_ticker"):
        parlays.append(m)
    else:
        real_markets.append(m)

print(f"\n{'='*70}")
print(f"  MARKET BREAKDOWN")
print(f"{'='*70}")
print(f"  Parlay/combo markets: {len(parlays)}")
print(f"  Real single-event markets: {len(real_markets)}")

# ── Phase 3: Analyze real markets ─────────────────────────────────────────
def _pc(v):
    """Convert price to cents. Handles both integer (cents) and dollar string formats."""
    v = float(v or 0)
    return int(v) if v > 1 else int(v * 100)

def get_vol(m):
    return float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)

def get_oi(m):
    return float(m.get("open_interest") or m.get("open_interest_fp") or 0)

# All non-null fields
print(f"\n{'='*70}")
print(f"  FIELD CENSUS (real markets only)")
print(f"{'='*70}")
all_fields = {}
for m in real_markets:
    for k, v in m.items():
        if v and v != 0 and v != "" and v != [] and v != "0" and v != "0.00":
            all_fields[k] = all_fields.get(k, 0) + 1

for k, cnt in sorted(all_fields.items(), key=lambda x: -x[1]):
    pct = cnt / len(real_markets) * 100 if real_markets else 0
    print(f"  {k:30s}: {cnt:5d}/{len(real_markets)} ({pct:.0f}%)")

# Price and volume stats
has_ask = has_bid = has_both = has_vol = has_oi = 0
spreads = []
volumes = []
categories = {}

for m in real_markets:
    ya = _pc(m.get("yes_ask") or m.get("yes_ask_dollars"))
    yb = _pc(m.get("yes_bid") or m.get("yes_bid_dollars"))
    vol = get_vol(m)
    oi = get_oi(m)
    ticker = m.get("ticker", "")
    title = (m.get("title", "") or m.get("subtitle", "") or "")
    cat = trade.categorize_market(ticker, title)
    categories[cat] = categories.get(cat, 0) + 1

    if ya > 0: has_ask += 1
    if yb > 0: has_bid += 1
    if ya > 0 and yb > 0:
        has_both += 1
        spreads.append(ya - yb)
    if vol > 0: has_vol += 1
    if oi > 0: has_oi += 1
    volumes.append(vol)

n = max(len(real_markets), 1)
print(f"\n{'='*70}")
print(f"  PRICE & VOLUME STATS (real markets)")
print(f"{'='*70}")
print(f"  Has yes_ask:       {has_ask:5d}/{n} ({has_ask/n:.0%})")
print(f"  Has yes_bid:       {has_bid:5d}/{n} ({has_bid/n:.0%})")
print(f"  Has both bid+ask:  {has_both:5d}/{n} ({has_both/n:.0%})")
print(f"  Has volume > 0:    {has_vol:5d}/{n} ({has_vol/n:.0%})")
print(f"  Has open interest: {has_oi:5d}/{n} ({has_oi/n:.0%})")

print(f"\n  Categories:")
for cat, cnt in sorted(categories.items(), key=lambda x: -x[1]):
    in_mm = "✓" if cat in trade.MM_PREFERRED_CATS else "✗"
    print(f"    {cat:15s}: {cnt:5d} [{in_mm} MM preferred]")

print(f"\n  Spread Distribution (markets with both bid+ask):")
if spreads:
    for threshold in [1, 2, 3, 5, 8, 10, 15, 20, 30, 50]:
        count = sum(1 for s in spreads if s >= threshold)
        print(f"    Spread >= {threshold:2d}¢: {count:5d} markets")

print(f"\n  Volume Distribution:")
for threshold in [0, 1, 5, 10, 20, 50, 100, 500, 1000, 5000]:
    count = sum(1 for v in volumes if v > threshold)
    print(f"    Volume > {threshold:5d}: {count:5d} markets")

# ── Phase 4: Simulate MM filter funnel ────────────────────────────────────
print(f"\n{'='*70}")
print(f"  MM FILTER FUNNEL SIMULATION")
print(f"{'='*70}")
MM_MIN_SPREAD = int(os.environ.get("MM_MIN_SPREAD_CENTS", "8"))
MM_MIN_VOLUME = int(os.environ.get("MM_MIN_VOLUME", "5"))
print(f"  Config: MM_MIN_SPREAD={MM_MIN_SPREAD}¢, MM_MIN_VOLUME={MM_MIN_VOLUME}")
print(f"  MM_PREFERRED_CATS={trade.MM_PREFERRED_CATS}")

funnel = {"start": 0, "parlay": 0, "cat_skip": 0, "no_prices": 0,
          "tight_spread": 0, "low_activity": 0, "extreme_price": 0, "passed": 0}
passed = []

for m in real_markets:
    funnel["start"] += 1
    ticker = m.get("ticker", "")
    title = (m.get("title", "") or m.get("subtitle", "") or "").lower()
    cat = trade.categorize_market(ticker, title)

    if cat not in trade.MM_PREFERRED_CATS:
        funnel["cat_skip"] += 1
        continue

    ya = _pc(m.get("yes_ask") or m.get("yes_ask_dollars"))
    yb = _pc(m.get("yes_bid") or m.get("yes_bid_dollars"))

    if ya <= 0 and yb <= 0:
        vol = get_vol(m)
        oi = get_oi(m)
        if max(vol, oi) < MM_MIN_VOLUME:
            funnel["no_prices"] += 1
            continue
        spread = 99; mid = 50
    elif ya <= 0:
        spread = 99 - yb; mid = yb + 10
    elif yb <= 0:
        spread = ya; mid = ya // 2
    else:
        spread = ya - yb; mid = (ya + yb) // 2

    if spread < MM_MIN_SPREAD:
        funnel["tight_spread"] += 1
        continue

    vol = get_vol(m)
    oi = get_oi(m)
    activity = max(vol, oi)
    if activity < MM_MIN_VOLUME:
        funnel["low_activity"] += 1
        continue

    if mid < 10 or mid > 90:
        funnel["extreme_price"] += 1
        continue

    funnel["passed"] += 1
    score = spread * (1 + vol)
    passed.append((score, spread, vol, oi, mid, cat, ticker, (m.get("title") or "")[:60]))

print(f"\n  Filter funnel:")
print(f"    Start:           {funnel['start']:5d}")
print(f"    - Category skip: {funnel['cat_skip']:5d}")
print(f"    - No prices:     {funnel['no_prices']:5d}")
print(f"    - Tight spread:  {funnel['tight_spread']:5d}")
print(f"    - Low activity:  {funnel['low_activity']:5d}")
print(f"    - Extreme price: {funnel['extreme_price']:5d}")
print(f"    = PASSED:        {funnel['passed']:5d}")

# ── Phase 5: Top MM candidates ────────────────────────────────────────────
passed.sort(reverse=True)
print(f"\n{'='*70}")
print(f"  TOP 30 MM CANDIDATES")
print(f"{'='*70}")
if not passed:
    print("  (none)")
for score, spread, vol, oi, mid, cat, ticker, title in passed[:30]:
    print(f"  {ticker:45s} spread={spread:2d}¢ vol={vol:7.0f} OI={oi:6.0f} mid={mid:2d} [{cat}] {title}")

# ── Phase 6: Show some near-misses ───────────────────────────────────────
print(f"\n{'='*70}")
print(f"  NEAR-MISS ANALYSIS (markets that almost passed)")
print(f"{'='*70}")

# Markets with good volume but tight spread
tight_but_active = []
for m in real_markets:
    ticker = m.get("ticker", "")
    title = (m.get("title", "") or m.get("subtitle", "") or "").lower()
    cat = trade.categorize_market(ticker, title)
    if cat not in trade.MM_PREFERRED_CATS:
        continue
    ya = _pc(m.get("yes_ask") or m.get("yes_ask_dollars"))
    yb = _pc(m.get("yes_bid") or m.get("yes_bid_dollars"))
    vol = get_vol(m)
    if ya > 0 and yb > 0 and vol > 50:
        spread = ya - yb
        mid = (ya + yb) // 2
        if 0 < spread < MM_MIN_SPREAD and 10 <= mid <= 90:
            tight_but_active.append((vol, spread, mid, cat, ticker, (m.get("title") or "")[:50]))

tight_but_active.sort(reverse=True)
print(f"\n  Active markets with tight spreads (spread < {MM_MIN_SPREAD}¢, volume > 50):")
if not tight_but_active:
    print("    (none)")
for vol, spread, mid, cat, ticker, title in tight_but_active[:10]:
    print(f"    {ticker:40s} spread={spread:2d}¢ vol={vol:7.0f} mid={mid:2d} [{cat}] {title}")

# Show sample of markets filtered by category
cat_filtered = []
for m in real_markets:
    ticker = m.get("ticker", "")
    title = (m.get("title", "") or m.get("subtitle", "") or "").lower()
    cat = trade.categorize_market(ticker, title)
    if cat not in trade.MM_PREFERRED_CATS:
        vol = get_vol(m)
        ya = _pc(m.get("yes_ask") or m.get("yes_ask_dollars"))
        yb = _pc(m.get("yes_bid") or m.get("yes_bid_dollars"))
        if vol > 20 and ya > 0:
            spread = (ya - yb) if yb > 0 else ya
            cat_filtered.append((vol, spread, cat, ticker, (m.get("title") or "")[:50]))

cat_filtered.sort(reverse=True)
print(f"\n  Active markets excluded by category filter:")
if not cat_filtered:
    print("    (none)")
for vol, spread, cat, ticker, title in cat_filtered[:10]:
    print(f"    {ticker:40s} vol={vol:7.0f} spread={spread:2d}¢ [{cat}] {title}")

print(f"\n{'='*70}")
print(f"  DIAGNOSTIC COMPLETE")
print(f"{'='*70}")
