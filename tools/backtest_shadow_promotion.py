#!/usr/bin/env python3
"""
Backtest shadow-to-canary promotion using /markets?status=settled
instead of /portfolio/settlements.

Why this exists
---------------
The daemon's existing settlement driver reads /portfolio/settlements,
which only returns tickers where we had bot-placed orders. As of
2026-04-22 the weather_mm_shadow table has 243 distinct *unsettled*
tickers with zero legacy mm_orders history — so /portfolio/settlements
will never return them, and annotate_shadow_pnl will never fire.

This one-off pulls settlement outcomes directly from the market catalog
(Kalshi's /markets?series_ticker=X&status=settled), intersects with
our shadow tickers, and — in --commit mode — stamps the annotations
and runs evaluate_mm_promotion per family.

Dry-run by default. Use --commit to actually write.

Usage (VPS):
    cd /home/kalshi/autoagent
    .venv/bin/python tools/backtest_shadow_promotion.py            # dry run
    .venv/bin/python tools/backtest_shadow_promotion.py --commit   # apply
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

# Make sibling modules importable when invoked from repo root
sys.path.insert(0, ".")

from bot.api import api_get
from bot.db import init_db
from bot.learning.mm_promotion import (
    annotate_shadow_pnl,
    evaluate_mm_promotion,
)


# Weather MM families we shadow-quote on. Keep aligned with
# bot/daemon/stations.py::SERIES_TO_STATION.
WEATHER_SERIES = [
    "KXHIGHNY",
    "KXHIGHCHI",
    "KXHIGHMIA",
    "KXHIGHLAX",
    "KXHIGHAUS",
    "KXHIGHDEN",
    "KXHIGHATL",
    "KXHIGHBOS",
    "KXHIGHPHIL",
    "KXHIGHHOU",
    "KXHIGHPHX",
]


def _parse_close_ts(close_time: str) -> Optional[float]:
    """ISO8601 → unix epoch float. Returns None on parse failure."""
    if not close_time:
        return None
    try:
        # Kalshi returns e.g. "2026-04-21T20:00:00Z"
        ts = close_time.rstrip("Z")
        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def fetch_settled_markets(series: str, max_pages: int = 50) -> dict[str, tuple[str, float]]:
    """
    Fetch all settled markets for a series.
    Returns {ticker: (result_lower, close_ts_unix)}.
    `result_lower` ∈ {"yes", "no"}; other outcomes are dropped.
    """
    out: dict[str, tuple[str, float]] = {}
    cursor: Optional[str] = None
    pages = 0
    while pages < max_pages:
        params = {
            "series_ticker": series,
            "status": "settled",
            "limit": "200",
        }
        if cursor:
            params["cursor"] = cursor
        path = "/markets?" + urllib.parse.urlencode(params)
        try:
            resp = api_get(path)
        except Exception as e:
            print(f"  [{series}] API error page {pages}: {e}")
            break
        markets = resp.get("markets", [])
        if not markets:
            break
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue
            result = (m.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue
            close_time = m.get("close_time") or m.get("expiration_time") or ""
            close_ts = _parse_close_ts(close_time)
            if close_ts is None:
                continue
            out[ticker] = (result, close_ts)
        cursor = resp.get("cursor")
        pages += 1
        if not cursor:
            break
        time.sleep(0.25)
    return out


def distinct_unsettled_shadow_tickers(conn, series: str) -> set[str]:
    """Tickers in weather_mm_shadow for this series with ts_settle_unix IS NULL."""
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM weather_mm_shadow "
        "WHERE series=? AND ts_settle_unix IS NULL",
        (series.upper(),),
    ).fetchall()
    return {r[0] for r in rows}


def count_unsettled_rows(conn, ticker: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM weather_mm_shadow "
        "WHERE ticker=? AND ts_settle_unix IS NULL",
        (ticker,),
    ).fetchone()
    return int(row[0] or 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit", action="store_true",
        help="Write annotations to DB and run evaluate_mm_promotion. "
             "Without this flag, only reports what WOULD be annotated.",
    )
    parser.add_argument(
        "--series", nargs="+", default=None,
        help=f"Series to include (default: {WEATHER_SERIES})",
    )
    args = parser.parse_args()

    series_list = [s.upper() for s in (args.series or WEATHER_SERIES)]

    print(f"{'='*72}")
    print(f" SHADOW PROMOTION BACKTEST  (mode: {'COMMIT' if args.commit else 'DRY-RUN'})")
    print(f" series: {series_list}")
    print(f"{'='*72}\n")

    conn = init_db()

    grand_tickers = 0
    grand_rows = 0
    per_series_summary: list[dict[str, Any]] = []

    for series in series_list:
        print(f"─── {series} ──────────────────────────────────────")
        shadow_tickers = distinct_unsettled_shadow_tickers(conn, series)
        print(f"  unsettled shadow tickers in DB: {len(shadow_tickers)}")
        if not shadow_tickers:
            per_series_summary.append({
                "series": series, "matched_tickers": 0, "matched_rows": 0,
                "gate_pass": None, "reason": "no_unsettled_shadow",
            })
            print()
            continue

        settled = fetch_settled_markets(series)
        print(f"  settled markets returned by Kalshi: {len(settled)}")

        matched = shadow_tickers & set(settled.keys())
        print(f"  intersection (shadow_unsettled ∩ settled): {len(matched)}")

        total_rows = 0
        yes_count = 0
        no_count = 0
        for ticker in sorted(matched):
            result, close_ts = settled[ticker]
            n = count_unsettled_rows(conn, ticker)
            total_rows += n
            if result == "yes":
                yes_count += 1
            else:
                no_count += 1

        print(f"  rows pending annotation: {total_rows}  "
              f"(yes_settled={yes_count}, no_settled={no_count})")

        grand_tickers += len(matched)
        grand_rows += total_rows

        if args.commit and matched:
            print(f"  [COMMIT] calling annotate_shadow_pnl …")
            annotated = 0
            for ticker in sorted(matched):
                result, close_ts = settled[ticker]
                try:
                    n = annotate_shadow_pnl(
                        conn, ticker,
                        won_yes=(result == "yes"),
                        ts_settle_unix=float(close_ts),
                    )
                    annotated += n
                except Exception as e:
                    print(f"    {ticker}: annotate FAILED: {e}")
            print(f"  [COMMIT] annotated {annotated} rows")

            # Evaluate the canary gate on freshly annotated data
            passed, reason, metrics = evaluate_mm_promotion(conn, series)
            print(f"  [GATE] pass={passed}  reason={reason}")
            print(f"  [GATE] metrics={metrics}")
            per_series_summary.append({
                "series": series,
                "matched_tickers": len(matched),
                "matched_rows": total_rows,
                "gate_pass": passed,
                "reason": reason,
                "metrics": metrics,
            })
        else:
            per_series_summary.append({
                "series": series,
                "matched_tickers": len(matched),
                "matched_rows": total_rows,
                "gate_pass": None,
                "reason": "dry_run",
            })
        print()

    print(f"{'='*72}")
    print(f" SUMMARY")
    print(f"{'='*72}")
    print(f"  total tickers matched: {grand_tickers}")
    print(f"  total shadow rows to annotate: {grand_rows}")
    print()
    print(f"  {'series':<14} {'tickers':>8} {'rows':>6} {'gate':>6} {'reason'}")
    for s in per_series_summary:
        gate = (
            "PASS" if s["gate_pass"] is True
            else ("FAIL" if s["gate_pass"] is False else "—")
        )
        print(f"  {s['series']:<14} {s['matched_tickers']:>8d} "
              f"{s['matched_rows']:>6d} {gate:>6}  {s['reason']}")

    if not args.commit:
        print()
        print(f"  Dry-run only. Re-run with --commit to apply annotations.")


if __name__ == "__main__":
    main()
