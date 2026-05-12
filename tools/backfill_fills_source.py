#!/usr/bin/env python3
"""One-time backfill: re-tag ``fills_ledger.source`` for May 11+ rows
that landed as ``manual`` because Kalshi's /portfolio/fills response
stopped including ``client_order_id``.

Background
----------
The 2026-05-12 fix (commit 433ceb7) made ``FillsWriter.ingest_page``
fall back to a ``posted_orders`` lookup when Kalshi omits
``client_order_id``. But the ``posted_orders`` table wasn't being
written between roughly 2026-05-10 22:05 UTC (last historical entry)
and the fix deploy. Every fill in that gap landed in fills_ledger
with ``source='manual'``, polluting per-strategy attribution.

This script recovers source from two sources of truth:

* ``alpha_backtest`` rows with ``decision_outcome='posted'`` and
  ``decision_type='cross_bracket_live'`` — joined by exact
  (ticker, side) at the same ts_decision (cross_bracket_shadow writes
  the alpha_backtest row in the same code path as the api_post). A
  match → ``source='cross_bracket'``.

* For unmatched fills that look like exit hedges — buy YES at low
  price (≤15¢) immediately after a same-ticker NO entry — tag
  ``cross_bracket_exit``. The cross_bracket_exit code path doesn't
  write to alpha_backtest, so this is a heuristic, but the pattern
  is unambiguous when the price is sub-penny-floor and timing is
  within a few hours of the parent NO entry.

Anything not matched by either rule is left as ``manual``. This is
the conservative default for fills that may genuinely have been
human-placed.

Idempotent: only updates rows whose currently-stored source differs
from the inferred one.

Scope
-----
Strictly fills_ledger rows where ``source='manual'`` AND ticker
matches a KXHIGH* family pattern. Non-weather manual fills (NBA,
NCAA, etc.) are not bot trades and are left alone.

Usage
-----
::

    python3 tools/backfill_fills_source.py --db kalshi_trades.db          # dry run
    python3 tools/backfill_fills_source.py --db kalshi_trades.db --apply  # write
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# YES-buy prices at or below this threshold are treated as exit hedge
# candidates. cross_bracket_exit's pricing aims for cents in the
# 1-12¢ range (the "lock in NO's gain by buying back as a cheap YES").
_EXIT_HEDGE_MAX_YES_PRICE_CENTS = 15

# A YES exit hedge is plausible iff its parent NO entry landed within
# this many seconds before. 12 hours covers same-day flow with margin.
_EXIT_HEDGE_LOOKBACK_SECONDS = 12 * 3600


def _find_cross_bracket_match(
    conn: sqlite3.Connection, ticker: str, side: str, fill_ts_unix: float,
) -> bool:
    """True if alpha_backtest has a posted cross_bracket_live decision
    on this ticker/side within ±60s of the fill. Posts and fills happen
    in the same code path so timestamps are very tight; 60s leaves
    headroom for clock skew."""
    row = conn.execute(
        """SELECT 1 FROM alpha_backtest
            WHERE ticker=? AND side=?
              AND decision_type='cross_bracket_live'
              AND decision_outcome='posted'
              AND ABS(ts_decision_unix - ?) < 60
            LIMIT 1""",
        (ticker, side, fill_ts_unix),
    ).fetchone()
    return row is not None


def _find_exit_hedge_parent(
    conn: sqlite3.Connection, ticker: str, fill_ts_unix: float,
    yes_price: int,
) -> bool:
    """True if this fill looks like a cross_bracket_exit hedge:
    a YES buy at low price following a NO entry on the same ticker
    within the lookback window. The cross_bracket_exit path doesn't
    log to alpha_backtest so we infer from fill structure.
    """
    if yes_price > _EXIT_HEDGE_MAX_YES_PRICE_CENTS:
        return False
    row = conn.execute(
        """SELECT 1 FROM fills_ledger
            WHERE ticker=? AND side='no' AND action='buy'
              AND fill_ts_unix BETWEEN ? AND ?
            LIMIT 1""",
        (ticker,
         fill_ts_unix - _EXIT_HEDGE_LOOKBACK_SECONDS,
         fill_ts_unix - 0.1),  # strictly before this YES fill
    ).fetchone()
    return row is not None


def backfill(db_path: str, apply: bool) -> tuple[int, int]:
    """Returns (cross_bracket_count, cross_bracket_exit_count)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 30000")

    rows = conn.execute(
        """SELECT trade_id, ticker, side, contracts,
                  yes_price_cents, no_price_cents, fill_ts_iso, fill_ts_unix
             FROM fills_ledger
            WHERE source='manual' AND ticker LIKE 'KXHIGH%'
            ORDER BY fill_ts_unix""",
    ).fetchall()
    print(f"[backfill] scanning {len(rows)} manual KXHIGH* fills...")

    n_cb = 0
    n_cb_exit = 0
    n_skip = 0
    for (trade_id, ticker, side, contracts,
         yes_price, no_price, fill_ts_iso, fill_ts_unix) in rows:
        new_source = None
        # Rule 1: side matches a cross_bracket_live posted decision.
        if _find_cross_bracket_match(conn, ticker, side, fill_ts_unix):
            new_source = "cross_bracket"
        # Rule 2: YES buy at low price following a NO entry → exit hedge.
        elif (side == "yes" and
              _find_exit_hedge_parent(conn, ticker, fill_ts_unix, yes_price)):
            new_source = "cross_bracket_exit"
        else:
            n_skip += 1
            continue

        print(
            f"  {fill_ts_iso}  {ticker:32s} {side:3s} "
            f"y={yes_price:3d} n={no_price:3d} contracts={contracts}  "
            f"→ source='{new_source}' (was 'manual')"
        )
        if new_source == "cross_bracket":
            n_cb += 1
        else:
            n_cb_exit += 1

        if apply:
            conn.execute(
                "UPDATE fills_ledger SET source=? WHERE trade_id=?",
                (new_source, trade_id),
            )

    if apply:
        conn.commit()

    print()
    print(f"[backfill] re-tagged cross_bracket:       {n_cb}")
    print(f"[backfill] re-tagged cross_bracket_exit:  {n_cb_exit}")
    print(f"[backfill] left as manual (no match):     {n_skip}")
    if not apply:
        print("[backfill] DRY RUN — pass --apply to write the updates.")
    else:
        print("[backfill] applied.")
    conn.close()
    return n_cb, n_cb_exit


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    backfill(args.db, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
