"""Phase C — when did weather_forecast_snapshots stop being written?

If 26APR26 tickers have zero rows but 26APR25 does, narrow down whether
the issue is "predict_v2 broken on April 26" vs "predict_v2 never wrote
snapshots for tickers settling on the same UTC day they were predicted".
"""
from __future__ import annotations

import sqlite3

conn = sqlite3.connect("/home/kalshi/autoagent/kalshi_trades.db")

# By settle date: number of forecast_snapshot rows + first/last recorded_at
print("=" * 80)
print("weather_forecast_snapshots — coverage by settle date suffix in ticker")
print("=" * 80)
rows = conn.execute("""
    SELECT
        substr(ticker, instr(ticker, '-')+1, 7) AS settle_suf,
        COUNT(*) AS row_count,
        COUNT(DISTINCT ticker) AS distinct_tickers,
        MIN(recorded_at) AS first_seen,
        MAX(recorded_at) AS last_seen
    FROM weather_forecast_snapshots
    WHERE ticker LIKE 'KXHIGH%'
    GROUP BY settle_suf
    ORDER BY settle_suf
""").fetchall()
for r in rows:
    print(f"  {r[0]:10s}  rows={r[1]:5d}  tickers={r[2]:3d}  "
          f"first={r[3]}  last={r[4]}")

# By recorded_at hour bucket on April 25–28
print()
print("=" * 80)
print("weather_forecast_snapshots — write rate by UTC hour, Apr 25–28")
print("=" * 80)
rows = conn.execute("""
    SELECT
        substr(recorded_at, 1, 13) AS hour_utc,
        COUNT(*) AS rows
    FROM weather_forecast_snapshots
    WHERE recorded_at LIKE '2026-04-2_%'
      AND recorded_at >= '2026-04-25'
      AND recorded_at <  '2026-04-29'
    GROUP BY hour_utc
    ORDER BY hour_utc
""").fetchall()
for r in rows:
    bar = "#" * min(60, r[1] // 5)
    print(f"  {r[0]}  rows={r[1]:5d}  {bar}")

# Same for shadow rows
print()
print("=" * 80)
print("weather_mm_shadow — write rate by UTC hour, Apr 25–28")
print("=" * 80)
rows = conn.execute("""
    SELECT
        strftime('%Y-%m-%d %H', datetime(ts_unix, 'unixepoch')) AS hour_utc,
        COUNT(*) AS rows
    FROM weather_mm_shadow
    WHERE ts_unix >= strftime('%s', '2026-04-25')
      AND ts_unix <  strftime('%s', '2026-04-29')
    GROUP BY hour_utc
    ORDER BY hour_utc
""").fetchall()
for r in rows:
    bar = "#" * min(60, r[1] // 5)
    print(f"  {r[0]}  rows={r[1]:4d}  {bar}")

# Concrete: did snapshots get written for 26APR27 tickers BEFORE Apr 27 began?
# That tests "is the issue future-day vs today-day predicts"
print()
print("=" * 80)
print("Test: were 26APR27 tickers being snapshotted ON April 26?")
print("(if YES: April 26 was simply broken; predict_v2 was running but writes failed)")
print("(if NO:  predict_v2 only writes for future-settle tickers — the 'today' market was always missing)")
print("=" * 80)
r = conn.execute("""
    SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at)
    FROM weather_forecast_snapshots
    WHERE ticker LIKE 'KXHIGH%-26APR27-%'
      AND recorded_at < '2026-04-27'
""").fetchone()
print(f"  26APR27 tickers snapshotted before 2026-04-27 UTC: {r[0]} rows  "
      f"first={r[1]} last={r[2]}")

r = conn.execute("""
    SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at)
    FROM weather_forecast_snapshots
    WHERE ticker LIKE 'KXHIGH%-26APR27-%'
      AND recorded_at >= '2026-04-27'
""").fetchone()
print(f"  26APR27 tickers snapshotted on/after 2026-04-27 UTC: {r[0]} rows  "
      f"first={r[1]} last={r[2]}")

r = conn.execute("""
    SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at)
    FROM weather_forecast_snapshots
    WHERE ticker LIKE 'KXHIGH%-26APR26-%'
""").fetchone()
print(f"  26APR26 tickers snapshotted at any time:           {r[0]} rows  "
      f"first={r[1]} last={r[2]}")

r = conn.execute("""
    SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at)
    FROM weather_forecast_snapshots
    WHERE ticker LIKE 'KXHIGH%-26APR25-%'
""").fetchone()
print(f"  26APR25 tickers snapshotted at any time:           {r[0]} rows  "
      f"first={r[1]} last={r[2]}")

# Same question for shadow
print()
r = conn.execute("""
    SELECT COUNT(*) FROM weather_mm_shadow
    WHERE ticker LIKE 'KXHIGH%-26APR26-%'
""").fetchone()
print(f"  26APR26 shadow rows (any time):                    {r[0]}")
r = conn.execute("""
    SELECT COUNT(*) FROM weather_mm_shadow
    WHERE ticker LIKE 'KXHIGH%-26APR25-%'
""").fetchone()
print(f"  26APR25 shadow rows (any time):                    {r[0]}")
r = conn.execute("""
    SELECT COUNT(*) FROM weather_mm_shadow
    WHERE ticker LIKE 'KXHIGH%-26APR27-%'
""").fetchone()
print(f"  26APR27 shadow rows (any time):                    {r[0]}")
