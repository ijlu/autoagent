"""Edge convergence tracking.

After we identify an edge, does the market price converge toward our estimate?
If edges consistently don't converge, the bot is noise-trading, not edge-trading.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from bot.api import api_get
from bot.db import db_write_ctx


def check_edge_convergence(conn):
    """For recent trades, check if market prices moved toward our estimates.
    This validates whether the bot is actually smarter than the market."""
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    # Find trades from 6-48h ago that haven't been checked yet
    window_start = (now - timedelta(hours=48)).isoformat()
    window_end = (now - timedelta(hours=6)).isoformat()

    trades = conn.execute("""
        SELECT t.order_id, t.ticker, t.side, t.independent_prob, t.market_prob,
               t.timestamp, t.price_cents
        FROM trades t
        WHERE t.action = 'buy'
          AND t.timestamp BETWEEN ? AND ?
          AND t.independent_prob IS NOT NULL
          AND t.ticker NOT IN (SELECT ticker FROM edge_convergence)
    """, (window_start, window_end)).fetchall()

    if not trades:
        return 0

    checked = 0
    convergences = []
    with db_write_ctx(conn):
        for oid, ticker, side, est_prob, mkt_prob, trade_ts, entry_price in trades:
            # Fetch current market price for this ticker
            try:
                mkt = api_get(f"/markets/{ticker}")
                current_yes = float(mkt.get("yes_ask") or mkt.get("yes_ask_dollars") or mkt.get("last_price") or mkt.get("last_price_dollars") or 0)
                if current_yes > 1:
                    current_yes /= 100
            except Exception:
                continue

            if current_yes <= 0 or mkt_prob is None or est_prob is None:
                continue

            entry_price_frac = (entry_price / 100) if entry_price and entry_price > 1 else (entry_price or 0)

            # Did the market move toward our estimate?
            original_gap = abs(est_prob - mkt_prob)
            current_gap = abs(est_prob - current_yes)

            if original_gap > 0.01:  # only check if we had meaningful edge
                convergence_pct = (original_gap - current_gap) / original_gap
                converged = 1 if convergence_pct > 0.1 else 0  # >10% closer = convergence

                conn.execute("""INSERT INTO edge_convergence
                    (recorded_at, ticker, side, our_estimate, market_price_at_entry,
                     market_price_after_24h, converged, convergence_pct)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (now_str, ticker, side, est_prob, mkt_prob, current_yes,
                     converged, convergence_pct))

                convergences.append(convergence_pct)
                checked += 1

    if convergences:
        avg_conv = sum(convergences) / len(convergences)
        n_converged = sum(1 for c in convergences if c > 0.1)
        print(f"[convergence] Checked {checked} trades: {n_converged}/{checked} converged "
              f"({n_converged/checked:.0%}), avg convergence={avg_conv:+.1%}")

        # Strategic assessment
        all_conv = conn.execute(
            "SELECT convergence_pct, converged FROM edge_convergence"
        ).fetchall()
        if len(all_conv) >= 20:
            total_conv_rate = sum(c[1] for c in all_conv) / len(all_conv)
            if total_conv_rate < 0.30:
                print(f"[convergence] ⚠️  WARNING: Only {total_conv_rate:.0%} of edges converge. "
                      f"The bot may be trading noise, not signal.")
                # Log to strategy journal
                conn.execute("""INSERT INTO strategy_journal
                    (timestamp, entry_type, category, title, detail, metric_value, metric_name)
                    VALUES (?,?,?,?,?,?,?)""",
                    (now_str, "observation", "convergence",
                     "Low edge convergence rate",
                     f"Only {total_conv_rate:.0%} of identified edges show market convergence. "
                     f"This suggests our estimates may not contain real information.",
                     total_conv_rate, "convergence_rate"))

    return checked
