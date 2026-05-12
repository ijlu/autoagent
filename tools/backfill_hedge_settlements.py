#!/usr/bin/env python3
"""One-time backfill: correct historical settlements rows that were
written before the 2026-05-12 hedge-accounting fix (BUG #5).

Background
----------
Kalshi's /portfolio/settlements endpoint has been returning ``revenue=0``
for valid winning settlements since at least 2026-04-12. The bot's
``record_settlements`` used this field directly in
``profit = revenue - cost - fees``, so:

* Pure winning positions (held YES, market settled YES) had revenue
  read as 0 instead of the real payout. profit understated by 100¢
  per winning contract.
* Hedged positions (1 YES + 1 NO on the same bracket — cross-bracket
  exit pattern) showed up as ~$0.90 losses when they were actually
  ~$0.10 wins, because the hedge guarantees a $1 payout the buggy code
  never saw.

The 2026-05-12 fix lands ``settlement_revenue_cents`` in
``bot.core.money`` and wires it into ``record_settlements``. This
script extends the same correction to historical rows already in the
``settlements`` table.

Strategy
--------
For every ``settlements`` row:
  1. Look up the bot's positions on that ticker from ``fills_ledger``
     (yes_qty, no_qty, yes_paid, no_paid, fees).
  2. Determine the authoritative market_result from
     ``alpha_backtest.settlement_result`` (or, fallback, derive from
     the row's own (side, won) when alpha_backtest has no entry).
  3. Recompute revenue via ``settlement_revenue_cents``.
  4. Recompute profit = revenue - total_cost - fees.
  5. Recompute won = profit > 0.
  6. If any of (revenue, profit, won) differ from the stored values
     AND the bot actually had a position on this ticker, UPDATE.

Idempotent: re-runs only change rows whose new computation differs
from the stored values, so running twice is safe.

Usage
-----
::

    # Dry run (default) — print every change without writing
    python3 tools/backfill_hedge_settlements.py --db kalshi_trades.db

    # Apply changes
    python3 tools/backfill_hedge_settlements.py --db kalshi_trades.db --apply

    # On the VPS:
    sudo -u kalshi python3 tools/backfill_hedge_settlements.py \\
        --db /home/kalshi/autoagent/kalshi_trades.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.core.money import settlement_revenue_cents


def _market_result_for_ticker(conn: sqlite3.Connection, ticker: str) -> str | None:
    """Return the authoritative ``yes``/``no`` outcome for a ticker.

    Priority:
      1. ``alpha_backtest.settlement_result`` (any single row — we
         verified earlier these are internally consistent per ticker)
      2. ``kalshi_market_snapshots.result`` (rarely populated)

    Returns None if no source has a non-null answer; caller must skip.
    """
    row = conn.execute(
        "SELECT settlement_result FROM alpha_backtest "
        "WHERE ticker=? AND settlement_result IN ('yes','no') "
        "LIMIT 1",
        (ticker,),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT result FROM kalshi_market_snapshots "
        "WHERE ticker=? AND result IN ('yes','no') "
        "LIMIT 1",
        (ticker,),
    ).fetchone()
    if row:
        return row[0]
    return None


def _bot_positions_for_ticker(
    conn: sqlite3.Connection, ticker: str,
) -> tuple[int, int, int, int, int]:
    """Return (yes_qty, no_qty, yes_paid_cents, no_paid_cents, fees_cents)
    aggregated across every fill the bot has on this ticker in
    fills_ledger. Returns all zeros if the bot has no fills.

    Caller should treat ``yes_qty == 0 and no_qty == 0`` as "skip this
    row — not a bot position" rather than reaching for revenue.
    """
    row = conn.execute(
        """SELECT
              COALESCE(SUM(CASE WHEN side='yes' THEN contracts ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN side='no'  THEN contracts ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN side='yes' THEN contracts*yes_price_cents ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN side='no'  THEN contracts*no_price_cents  ELSE 0 END), 0),
              COALESCE(SUM(fee_cents), 0)
           FROM fills_ledger WHERE ticker=?""",
        (ticker,),
    ).fetchone()
    return tuple(int(x) for x in row)


def backfill(db_path: str, apply: bool) -> int:
    """Walk settlements; recompute revenue/profit/won where wrong.

    Returns the count of rows that needed (or got) an update. With
    ``apply=False`` (dry run) the count reflects what *would* change.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 30000")

    changes = 0
    no_position_skips = 0
    no_result_skips = 0
    fills_ledger_empty = (
        conn.execute("SELECT COUNT(*) FROM fills_ledger").fetchone()[0] == 0
    )
    if fills_ledger_empty:
        print(
            "[backfill] fills_ledger is empty — nothing to backfill. "
            "(May 11+ rows still need A.3 source attribution before "
            "this script can find their fills.)",
            file=sys.stderr,
        )
        return 0

    rows = conn.execute(
        """SELECT id, ticker, side, contracts, revenue_cents,
                  profit_cents, won, strategy, recorded_at
             FROM settlements ORDER BY recorded_at""",
    ).fetchall()

    print(f"[backfill] scanning {len(rows)} settlement rows...")
    for (sid, ticker, side, n_c, old_revenue, old_profit, old_won,
         strategy, recorded_at) in rows:
        yes_qty, no_qty, yes_paid, no_paid, fees = _bot_positions_for_ticker(
            conn, ticker,
        )
        if yes_qty == 0 and no_qty == 0:
            # No bot fills on this ticker → can't recompute; leave alone.
            # Could be a pre-fills_ledger settlement (most of the legacy
            # mm:mm_v1 rows are from before the table existed).
            no_position_skips += 1
            continue

        result = _market_result_for_ticker(conn, ticker)
        if result not in ("yes", "no"):
            no_result_skips += 1
            continue

        new_revenue = settlement_revenue_cents(yes_qty, no_qty, result)
        total_cost = yes_paid + no_paid
        new_profit = new_revenue - total_cost - fees
        new_won = 1 if new_profit > 0 else 0

        if (
            new_revenue == old_revenue
            and new_profit == old_profit
            and new_won == old_won
        ):
            continue

        changes += 1
        print(
            f"  {ticker:32s} result={result} "
            f"yes={yes_qty} no={no_qty} cost={total_cost} fees={fees}\n"
            f"    revenue: {old_revenue} → {new_revenue}\n"
            f"    profit:  {old_profit} → {new_profit}\n"
            f"    won:     {old_won} → {new_won}"
        )

        if apply:
            conn.execute(
                """UPDATE settlements
                      SET revenue_cents=?, profit_cents=?, won=?
                    WHERE id=?""",
                (new_revenue, new_profit, new_won, sid),
            )

    if apply:
        conn.commit()

    print()
    print(f"[backfill] changes:                {changes}")
    print(f"[backfill] skipped (no bot fills): {no_position_skips}")
    print(f"[backfill] skipped (no result):    {no_result_skips}")
    if not apply:
        print("[backfill] DRY RUN — pass --apply to write the updates.")
    else:
        print("[backfill] applied.")
    conn.close()
    return changes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="Path to kalshi_trades.db")
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write the updates (default: dry run)",
    )
    args = p.parse_args()
    backfill(args.db, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
