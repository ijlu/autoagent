"""Backfill market_id and portfolio_leg_count for legacy cross_bracket
alpha_backtest rows.

One-shot. Pre-Phase-B.3-column-promotion, cross-bracket rows stuffed both
fields into the freeform `notes` column as
``cross_bracket;market_id=KEY;leg=N/M;...``. New rows write the dedicated
columns directly. This script back-fills the legacy rows by parsing notes.

Usage:
    DB_PATH=/home/kalshi/autoagent/kalshi_trades.db \
    python3 tools/backfill_cross_bracket_columns.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

NOTES_RE = re.compile(
    r"cross_bracket;market_id=(?P<mid>[^;]+);leg=\d+/(?P<count>\d+)"
)


def main() -> int:
    db_path = os.environ.get("DB_PATH", "kalshi_trades.db")
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, notes FROM alpha_backtest "
        "WHERE notes LIKE 'cross_bracket;market_id=%' "
        "AND (market_id IS NULL OR portfolio_leg_count IS NULL)"
    ).fetchall()

    print(f"Found {len(rows)} rows needing backfill")
    if not rows:
        return 0

    updates: list[tuple[str, int, int]] = []
    skipped = 0
    for rid, notes in rows:
        m = NOTES_RE.match(notes or "")
        if m is None:
            skipped += 1
            continue
        updates.append((m.group("mid"), int(m.group("count")), rid))

    if skipped:
        print(f"  ⚠ {skipped} rows had unparseable notes — left untouched")

    cur = conn.executemany(
        "UPDATE alpha_backtest SET market_id = ?, portfolio_leg_count = ? "
        "WHERE id = ?",
        updates,
    )
    conn.commit()
    print(f"  ✓ Updated {cur.rowcount} rows")

    # Spot-check
    sample = conn.execute(
        "SELECT id, market_id, portfolio_leg_count, notes "
        "FROM alpha_backtest "
        "WHERE notes LIKE 'cross_bracket;%' "
        "ORDER BY id DESC LIMIT 3"
    ).fetchall()
    print("Spot-check (3 most recent):")
    for r in sample:
        print(f"  id={r[0]} market_id={r[1]} leg_count={r[2]}  notes={r[3][:60]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
