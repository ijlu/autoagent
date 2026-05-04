"""Cross-bracket performance scoreboard CLI.

Thin wrapper over ``bot.observability.cross_bracket_scoreboard``. Run on
the VPS for ad-hoc inspection. The daemon emits the same scoreboard
daily to the cron log via a scheduled task.

Usage:
  python3 tools/cross_bracket_scoreboard.py            # human-readable
  python3 tools/cross_bracket_scoreboard.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

from bot.observability.cross_bracket_scoreboard import (
    build_scoreboard,
    render_scoreboard,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="kalshi_trades.db")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of human-readable")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    scoreboard = build_scoreboard(conn)

    if args.json:
        print(json.dumps(scoreboard, indent=2, default=str))
        return 0

    header = (
        f"Cross-bracket scoreboard "
        f"(now: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})"
    )
    print(render_scoreboard(scoreboard, header=header))
    return 0


if __name__ == "__main__":
    sys.exit(main())
