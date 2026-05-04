"""Cross-bracket shadow-vs-realized diagnostic CLI.

Run after overnight settles to check whether real-money cross-bracket
P&L matches the shadow EV the strategy predicted at decision time.

Usage:
  python3 tools/cross_bracket_diagnostic.py
  python3 tools/cross_bracket_diagnostic.py --since 2026-05-04
  python3 tools/cross_bracket_diagnostic.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

from bot.observability.cross_bracket_diagnostic import (
    CROSS_BRACKET_EPOCH_ISO,
    build_diagnostic,
    render_diagnostic,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="kalshi_trades.db")
    ap.add_argument("--since", default=CROSS_BRACKET_EPOCH_ISO,
                    help=f"ISO date (default: {CROSS_BRACKET_EPOCH_ISO})")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of human-readable")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows, summary = build_diagnostic(conn, since_iso=args.since)

    if args.json:
        print(json.dumps({"rows": rows, "summary": summary}, indent=2, default=str))
        return 0

    header = (
        f"Cross-bracket diagnostic — shadow vs realized "
        f"(since {args.since}, now {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})"
    )
    print(render_diagnostic(rows, summary, header=header))
    return 0


if __name__ == "__main__":
    sys.exit(main())
