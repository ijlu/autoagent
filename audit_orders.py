#!/usr/bin/env python3
"""Audit current open orders and inventory — check if every position has data backing."""
import os, sys, sqlite3

# Load .env
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k] = v

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade

# 1. Fetch open orders from Kalshi API
print("=" * 80)
print("  OPEN ORDERS AUDIT")
print("=" * 80)
try:
    orders_resp = trade.api_get("/portfolio/orders?status=resting&limit=200")
    orders = orders_resp.get("orders", [])
    print(f"\n  {len(orders)} resting orders on Kalshi\n")

    # Group by ticker
    by_ticker = {}
    for o in orders:
        t = o.get("ticker", "")
        by_ticker.setdefault(t, []).append(o)

    for ticker in sorted(by_ticker.keys()):
        ords = by_ticker[ticker]
        title = ""
        # Fetch market for data source check
        try:
            mkt_resp = trade.api_get(f"/markets/{ticker}")
            mkt = mkt_resp.get("market", mkt_resp)
            title = (mkt.get("title") or mkt.get("subtitle") or "")[:60]
            yes_ask = float(mkt.get("yes_ask") or mkt.get("yes_ask_dollars") or 0)
            if yes_ask > 1: yes_ask /= 100
            vol = float(mkt.get("volume") or mkt.get("volume_fp") or 0)
            cat = trade.categorize_market(ticker, title.lower())

            # Check which data sources would fire
            sources_that_fire = []
            # Test each source individually (without calling full ensemble)
            ticker_upper = ticker.upper()
            if "KXHIGH" in ticker_upper or any(w in title.lower() for w in ["temperature", "weather", "degrees"]):
                sources_that_fire.append("weather")
            if any(w in title.lower() for w in ["cpi", "inflation", "fed fund", "unemployment", "gdp"]) or \
               any(p in ticker_upper for p in ["KXCPI", "KXFED", "KXGDP", "KXJOB"]):
                sources_that_fire.append("fred")
            if any(w in title.lower() for w in ["bitcoin", "btc", "ethereum", "eth"]) or \
               any(p in ticker_upper for p in ["KXBTC", "KXETH"]):
                sources_that_fire.append("crypto")
            if any(w in title.lower() for w in ["nba", "nfl", "mlb", "nhl"]) or \
               any(p in ticker_upper for p in ["KXNBA", "KXNFL", "KXMLB", "KXNHL"]):
                sources_that_fire.append("odds")
            # Company KPI sources
            company_tickers = ["KXHOOD", "KXLYFT", "KXDASH", "KXSPOTIFY", "KXUBER", "KXMTCH",
                             "KXPLTR", "KXRACE", "KXPM", "KXABNB", "KXTESLA", "KXBOEING",
                             "KXMETA", "KXSTRIPE", "KXEARNINGS", "KXISMPMI"]
            if any(p in ticker_upper for p in company_tickers):
                sources_that_fire.append("company_kpi/finnhub")

            has_data = len(sources_that_fire) > 0
            status = "✓ DATA" if has_data else "⚠️  NO DATA"

        except Exception as e:
            title = f"(fetch error: {e})"
            cat = "?"
            sources_that_fire = []
            has_data = False
            status = "? ERROR"

        print(f"\n  {ticker}  [{cat}]  {status}")
        print(f"    Title: {title}")
        if sources_that_fire:
            print(f"    Sources: {', '.join(sources_that_fire)}")
        else:
            print(f"    Sources: NONE — bot has no data edge on this market")
        for o in ords:
            side = o.get("side", "?")
            price = o.get("yes_price") or o.get("no_price") or 0
            count = o.get("remaining_count", o.get("count", 0))
            action = o.get("action", "?")
            print(f"    Order: {action} {side} x{count} @ {price}¢")

except Exception as e:
    print(f"  Error fetching orders: {e}")
    import traceback; traceback.print_exc()

# 2. Check inventory
print("\n" + "=" * 80)
print("  MM INVENTORY AUDIT")
print("=" * 80)
db_path = os.environ.get("DB_PATH", "kalshi_trades.db")
try:
    conn = sqlite3.connect(db_path)
    inv_rows = conn.execute(
        "SELECT ticker, net_position, avg_entry_cents, realized_pnl_cents FROM mm_inventory WHERE abs(net_position) > 0 ORDER BY abs(net_position) * avg_entry_cents DESC"
    ).fetchall()
    print(f"\n  {len(inv_rows)} positions with inventory\n")

    total_exposure = 0
    no_data_exposure = 0
    for ticker, net, avg_e, rpnl in inv_rows:
        ticker_upper = ticker.upper()
        exposure = abs(net) * int(avg_e)
        total_exposure += exposure

        # Quick data source check
        has_data = False
        sources = []
        if any(p in ticker_upper for p in ["KXHIGH", "KXHMONTH"]):
            has_data = True; sources.append("weather")
        if any(p in ticker_upper for p in ["KXFED", "KXCPI", "KXGDP", "KXJOB", "KXISMPMI"]):
            has_data = True; sources.append("fred")
        if any(p in ticker_upper for p in ["KXBTC", "KXETH"]):
            has_data = True; sources.append("crypto")
        if any(p in ticker_upper for p in ["KXNBA", "KXNFL", "KXMLB", "KXNHL"]):
            has_data = True; sources.append("odds")
        company_tickers = ["KXHOOD", "KXLYFT", "KXDASH", "KXSPOTIFY", "KXUBER", "KXMTCH",
                         "KXPLTR", "KXRACE", "KXPM", "KXABNB", "KXTESLA", "KXBOEING",
                         "KXMETA", "KXSTRIPE", "KXEARNINGS"]
        if any(p in ticker_upper for p in company_tickers):
            has_data = True; sources.append("company_kpi")

        if not has_data:
            no_data_exposure += exposure

        status = "✓" if has_data else "⚠️ NO DATA"
        src_str = ",".join(sources) if sources else "NONE"
        print(f"  {status} {ticker}: net={net:+d} entry={int(avg_e)}¢ "
              f"exposure=${exposure/100:.2f} rpnl=${rpnl/100:+.2f} [{src_str}]")

    print(f"\n  Total inventory exposure: ${total_exposure/100:.2f}")
    print(f"  Exposure WITH data: ${(total_exposure - no_data_exposure)/100:.2f}")
    print(f"  Exposure WITHOUT data: ${no_data_exposure/100:.2f} "
          f"({no_data_exposure/max(total_exposure,1)*100:.0f}%)")

    if no_data_exposure > 0:
        print(f"\n  ⚠️  ${no_data_exposure/100:.2f} in positions with NO data source edge!")
        print(f"  These were likely accumulated before the no-data failsafe was deployed.")

    conn.close()
except Exception as e:
    print(f"  Error reading DB: {e}")

# 3. Summary
print("\n" + "=" * 80)
print("  RECOMMENDATION")
print("=" * 80)
print("""
  The bot should ONLY quote markets where it has at least 1 data source.
  Current data coverage:
    ✓ Weather (KXHIGH*) → Open-Meteo forecasts
    ✓ Economics (KXFED, KXCPI, KXGDP) → FRED API
    ✓ Crypto (KXBTC, KXETH) → CoinGecko + Deribit
    ✗ Company KPIs → Finnhub returning 403, SensorTower limited
    ✗ Sports → Odds API working but rarely matches Kalshi markets
    ✗ Polymarket → loaded but fuzzy matching rarely triggers
    ✗ Metaculus → requires auth (403)
    ✗ Cleveland Fed → API endpoint moved (404)

  Markets with NO data source should be SKIPPED (the failsafe does this now).
  Legacy inventory from before the failsafe will age out as markets settle.
""")
