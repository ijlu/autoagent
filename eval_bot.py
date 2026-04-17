#!/usr/bin/env python3
"""Comprehensive bot performance evaluation."""
import os, sys, sqlite3, json
from datetime import datetime, timezone
sys.path.insert(0, '.')
os.chdir(os.path.expanduser('~/autoagent'))

conn = sqlite3.connect('kalshi_trades.db')
conn.row_factory = sqlite3.Row

print("=" * 70)
print("  KALSHI BOT — PERFORMANCE EVALUATION")
print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("=" * 70)

# 1. Balance history
print("\n═══ BALANCE HISTORY ═══")
rows = conn.execute("SELECT timestamp, balance_cents, COALESCE(portfolio_cents,0) as pv FROM sessions ORDER BY id").fetchall()
if rows:
    first = rows[0]
    last = rows[-1]
    first_equity = first['balance_cents'] + first['pv']
    last_equity = last['balance_cents'] + last['pv']
    print(f"First session:  {first['timestamp'][:19]}  balance=${first['balance_cents']/100:.2f}  portfolio=${first['pv']/100:.2f}  equity=${first_equity/100:.2f}")
    print(f"Latest session: {last['timestamp'][:19]}  balance=${last['balance_cents']/100:.2f}  portfolio=${last['pv']/100:.2f}  equity=${last_equity/100:.2f}")
    pnl = last_equity - first_equity
    pnl_pct = (pnl / first_equity * 100) if first_equity else 0
    print(f"Total P&L:      ${pnl/100:.2f}  ({pnl_pct:+.1f}%)")
    print(f"Sessions:       {len(rows)} total")

    # Daily breakdown
    daily = {}
    for r in rows:
        day = r['timestamp'][:10]
        eq = r['balance_cents'] + r['pv']
        if day not in daily:
            daily[day] = {'first': eq, 'last': eq, 'count': 0}
        daily[day]['last'] = eq
        daily[day]['count'] += 1

    print("\n  Day-by-day equity:")
    prev_eq = None
    for day in sorted(daily.keys()):
        d = daily[day]
        day_pnl = d['last'] - d['first']
        if prev_eq:
            day_pnl = d['last'] - prev_eq
        print(f"    {day}: ${d['last']/100:.2f}  (day P&L: ${day_pnl/100:+.2f}, {d['count']} runs)")
        prev_eq = d['last']

# 2. MM Orders & Fills
print("\n═══ MARKET MAKING ═══")
try:
    # Get column names first
    cols = [d[0] for d in conn.execute("SELECT * FROM mm_orders LIMIT 1").description]
    total_orders = conn.execute("SELECT COUNT(*) FROM mm_orders").fetchone()[0]
    print(f"Total MM orders placed: {total_orders}")

    # Try to count fills
    if 'status' in cols:
        fills = conn.execute("SELECT COUNT(*) FROM mm_orders WHERE status = 'filled'").fetchone()[0]
        print(f"Total fills: {fills}")

    # Orders by ticker
    print("\n  Orders by market series:")
    ticker_col = 'ticker' if 'ticker' in cols else cols[1]
    rows = conn.execute(f"SELECT substr({ticker_col}, 1, instr({ticker_col}||'-','-')-1) as series, COUNT(*) as cnt FROM mm_orders GROUP BY series ORDER BY cnt DESC LIMIT 20").fetchall()
    for r in rows:
        print(f"    {r[0]}: {r[1]} orders")
except Exception as e:
    print(f"  Error reading mm_orders: {e}")

# 3. MM Inventory
print("\n═══ CURRENT INVENTORY ═══")
try:
    rows = conn.execute("SELECT ticker, net_position, avg_entry_cents, realized_pnl_cents FROM mm_inventory WHERE net_position != 0 ORDER BY ABS(net_position) DESC").fetchall()
    total_unrealized = 0
    total_realized = 0
    if rows:
        for r in rows:
            rpnl = r['realized_pnl_cents'] or 0
            total_realized += rpnl
            print(f"  {r['ticker']}: net={r['net_position']:+d}  avg_entry={r['avg_entry_cents']:.0f}¢  realized=${rpnl/100:.2f}")
        print(f"\n  Total realized P&L: ${total_realized/100:.2f}")
    else:
        print("  No open positions")

    # Also check positions with 0 net but realized P&L
    closed = conn.execute("SELECT ticker, realized_pnl_cents FROM mm_inventory WHERE net_position = 0 AND realized_pnl_cents != 0").fetchall()
    if closed:
        closed_pnl = sum(r['realized_pnl_cents'] for r in closed)
        print(f"  Closed position realized P&L: ${closed_pnl/100:.2f}")
except Exception as e:
    print(f"  Error reading inventory: {e}")

# 4. Directional trades
print("\n═══ DIRECTIONAL TRADING ═══")
try:
    trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    settlements = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    print(f"Directional trades: {trades}")
    print(f"Settlements: {settlements}")
    if settlements > 0:
        won = conn.execute("SELECT COUNT(*) FROM settlements WHERE won = 1").fetchone()[0]
        print(f"Win rate: {won}/{settlements} = {won/settlements*100:.0f}%")
        total_profit = conn.execute("SELECT SUM(profit_cents) FROM settlements").fetchone()[0] or 0
        print(f"Total directional P&L: ${total_profit/100:.2f}")
    else:
        print("No settlements yet — learning systems dormant")
except Exception as e:
    print(f"  Error: {e}")

# 5. Fill analysis
print("\n═══ FILL ANALYSIS ═══")
try:
    total_filled = conn.execute("SELECT COUNT(*) FROM mm_orders WHERE status = 'filled'").fetchone()[0]
    total_completed = conn.execute("SELECT COUNT(*) FROM mm_orders WHERE status IN ('filled', 'canceled', 'cancelled')").fetchone()[0]
    if total_completed > 0:
        fill_rate = total_filled / total_completed * 100
        print(f"Fill rate: {total_filled}/{total_completed} = {fill_rate:.0f}%")
except:
    pass

# 6. Pipeline health
print("\n═══ DATA SOURCE HEALTH ═══")
try:
    rows = conn.execute("""
        SELECT source,
               COUNT(*) as total,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successes,
               SUM(CASE WHEN status IN ('broken','degraded') THEN 1 ELSE 0 END) as failures,
               AVG(CASE WHEN avg_latency_ms > 0 THEN avg_latency_ms END) as avg_latency
        FROM pipeline_health
        GROUP BY source
        ORDER BY successes DESC
    """).fetchall()
    if rows:
        for r in rows:
            rate = (r['successes'] / r['total'] * 100) if r['total'] else 0
            lat = r['avg_latency'] or 0
            print(f"  {r['source']:15s}: {r['successes']}/{r['total']} success ({rate:.0f}%)  avg_latency={lat:.0f}ms")
    else:
        print("  No pipeline health data (recently reset)")
except Exception as e:
    print(f"  Error: {e}")

# 7. Fee analysis
print("\n═══ FEE ESTIMATION ═══")
try:
    # Estimate fees from fills
    total_filled = conn.execute("SELECT COUNT(*) FROM mm_orders WHERE status = 'filled'").fetchone()[0]
    # Kalshi maker fee: 0.0175 * P * (1-P) per contract
    # Average P around 0.50 = max fee of 0.004375 per contract = 0.44 cents
    est_fee_per_fill = 0.44  # cents average
    est_total_fees = total_filled * est_fee_per_fill
    print(f"Estimated total maker fees: ~${est_total_fees/100:.2f} ({total_filled} fills)")
except:
    pass

# 8. Key observations
print("\n═══ SUMMARY ═══")
if rows:
    pass
last_session = conn.execute("SELECT balance_cents, COALESCE(portfolio_cents,0) FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
if last_session:
    equity = last_session[0] + last_session[1]
    first_session = conn.execute("SELECT balance_cents, COALESCE(portfolio_cents,0) FROM sessions ORDER BY id ASC LIMIT 1").fetchone()
    start_equity = first_session[0] + first_session[1] if first_session else equity
    total_pnl = equity - start_equity
    print(f"Starting equity: ${start_equity/100:.2f}")
    print(f"Current equity:  ${equity/100:.2f}")
    print(f"Net P&L:         ${total_pnl/100:+.2f} ({total_pnl/start_equity*100:+.1f}%)")
    print(f"Current balance: ${last_session[0]/100:.2f}")
    print(f"Current portfolio value: ${last_session[1]/100:.2f}")

conn.close()
