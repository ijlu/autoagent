"""Per-category edge threshold learning from settlement data.

Categories where we lose money get higher edge requirements; categories where
we consistently win can have slightly relaxed requirements.
"""

from __future__ import annotations

from bot.market_maker.selection import categorize_market

_CATEGORY_EDGES = None  # cached per run


def compute_category_edge_thresholds(conn):
    """Learn per-category minimum edge thresholds from settlement data.
    Categories where we lose money need higher edge requirements.
    Returns dict of {category: min_edge_multiplier}."""
    global _CATEGORY_EDGES
    if _CATEGORY_EDGES is not None:
        return _CATEGORY_EDGES

    MIN_CAT_SAMPLES = 8

    rows = conn.execute(
        "SELECT settlements.ticker, settlements.won, settlements.profit_cents, trades.edge, "
        "trades.reason "
        "FROM settlements "
        "JOIN trades ON settlements.order_id = trades.order_id "
        "WHERE trades.edge IS NOT NULL"
    ).fetchall()

    if len(rows) < 20:
        _CATEGORY_EDGES = {}
        return _CATEGORY_EDGES

    cat_stats = {}  # {category: {"wins": n, "losses": n, "profit": x, "edges": [...]}}
    for ticker, won, profit, edge, reason in rows:
        # Use reason/detail field for title hints since we don't store raw title
        cat = categorize_market(ticker, reason or "")
        if cat not in cat_stats:
            cat_stats[cat] = {"wins": 0, "losses": 0, "profit": 0, "edges": []}
        if won:
            cat_stats[cat]["wins"] += 1
        else:
            cat_stats[cat]["losses"] += 1
        cat_stats[cat]["profit"] += (profit or 0)
        cat_stats[cat]["edges"].append(edge)

    thresholds = {}
    for cat, stats in cat_stats.items():
        n = stats["wins"] + stats["losses"]
        if n < MIN_CAT_SAMPLES:
            continue
        wr = stats["wins"] / n
        avg_profit = stats["profit"] / n

        if wr < 0.50 or avg_profit < 0:
            # Losing category — require 50% more edge
            thresholds[cat] = 1.5
            print(f"[cat_edge] {cat}: LOSING (wr={wr:.0%}, profit={avg_profit:+.0f}¢) "
                  f"→ 1.5x edge required")
        elif wr > 0.58 and avg_profit > 0:
            # Strong category — can reduce edge requirement slightly
            thresholds[cat] = 0.85
            print(f"[cat_edge] {cat}: STRONG (wr={wr:.0%}, profit={avg_profit:+.0f}¢) "
                  f"→ 0.85x edge required")
        else:
            thresholds[cat] = 1.0

    _CATEGORY_EDGES = thresholds
    return thresholds
