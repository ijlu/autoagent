"""Timing pattern learning.

Tracks what time of day and day of week our trades are most/least profitable.
Over time, this reveals when our data sources have the freshest information.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bot.learning.adaptive_weights import _parse_sources_from_strategy
from bot.core.categorization import categorize_market
from bot.db import db_write_ctx


def record_timing_data(conn):
    """Record timing metadata for settled trades that don't have timing data yet."""
    now_str = datetime.now(timezone.utc).isoformat()

    # Find settled trades without timing records (deduplicate by order_id)
    rows = conn.execute("""
        SELECT s.order_id, s.ticker, s.won, s.profit_cents,
               t.timestamp, t.strategy, t.edge
        FROM settlements s
        JOIN trades t ON s.order_id = t.order_id
        WHERE t.timestamp IS NOT NULL
          AND s.order_id NOT IN (SELECT order_id FROM timing_patterns WHERE order_id IS NOT NULL)
    """).fetchall()

    # Also find MM settlements without timing records (mm_orders, not trades table)
    mm_rows = conn.execute("""
        SELECT s.order_id, s.ticker, s.won, s.profit_cents,
               m.timestamp, s.strategy, NULL as edge
        FROM settlements s
        JOIN mm_orders m ON s.ticker = m.ticker AND m.fill_qty > 0
        WHERE s.strategy LIKE 'mm:%'
          AND m.timestamp IS NOT NULL
          AND s.order_id NOT IN (SELECT order_id FROM timing_patterns WHERE order_id IS NOT NULL)
        GROUP BY s.order_id
    """).fetchall()

    all_rows = rows + mm_rows
    if not all_rows:
        return 0

    recorded = 0
    with db_write_ctx(conn):
        for oid, ticker, won, profit, trade_ts, strategy, edge in all_rows:
            try:
                dt = datetime.fromisoformat(trade_ts.replace("Z", "+00:00"))
            except Exception:
                continue

            hour_utc = dt.hour
            dow = dt.weekday()  # 0=Monday
            cat = categorize_market(ticker, "")

            # Extract primary source from strategy string
            sources = _parse_sources_from_strategy(strategy)
            primary_source = sources[0] if sources else "unknown"

            conn.execute("""INSERT INTO timing_patterns
                (recorded_at, order_id, hour_utc, day_of_week, category, source, edge, won, profit_cents)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now_str, oid, hour_utc, dow, cat, primary_source, edge, won, profit))
            recorded += 1

    # Analyze timing patterns if we have enough data
    if recorded > 0:
        analyze_timing_patterns(conn)

    return recorded


def analyze_timing_patterns(conn):
    """Identify profitable/unprofitable time windows."""
    total = conn.execute("SELECT COUNT(*) FROM timing_patterns").fetchone()[0]
    if total < 30:
        return  # need more data

    # Best/worst hours
    hours = conn.execute("""
        SELECT hour_utc, COUNT(*) as n, AVG(won) as wr,
               SUM(profit_cents) as total_profit
        FROM timing_patterns
        GROUP BY hour_utc
        HAVING n >= 3
        ORDER BY wr DESC
    """).fetchall()

    if hours:
        best = hours[0]
        worst = hours[-1]
        print(f"[timing] Best hour: {best[0]}:00 UTC (wr={best[2]:.0%}, n={best[1]})")
        print(f"[timing] Worst hour: {worst[0]}:00 UTC (wr={worst[2]:.0%}, n={worst[1]})")

    # Best/worst day of week
    days = conn.execute("""
        SELECT day_of_week, COUNT(*) as n, AVG(won) as wr,
               SUM(profit_cents) as total_profit
        FROM timing_patterns
        GROUP BY day_of_week
        HAVING n >= 3
        ORDER BY wr DESC
    """).fetchall()

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if days:
        best_d = days[0]
        worst_d = days[-1]
        print(f"[timing] Best day: {day_names[best_d[0]]} (wr={best_d[2]:.0%}, n={best_d[1]})")
        print(f"[timing] Worst day: {day_names[worst_d[0]]} (wr={worst_d[2]:.0%}, n={worst_d[1]})")

    # Best/worst source by time
    src_time = conn.execute("""
        SELECT source, CASE WHEN hour_utc BETWEEN 6 AND 18 THEN 'day' ELSE 'night' END as period,
               COUNT(*) as n, AVG(won) as wr
        FROM timing_patterns
        GROUP BY source, period
        HAVING n >= 5
        ORDER BY source, period
    """).fetchall()

    for src, period, n, wr in src_time:
        if wr < 0.40 or wr > 0.65:
            print(f"[timing] {src} during {period}: wr={wr:.0%} (n={n}) "
                  f"{'← strong' if wr > 0.60 else '← weak'}")
