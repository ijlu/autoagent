"""Production-path Brier backtest.

Reads ``weather_mm_shadow.fair_value_cents`` directly — these are the
exact fair values the live ensemble produced at the moment WeatherQuoter
considered quoting. No re-running of ``predict_v2``, no monkey-patched
``_collect_gaussians``, no synthesized Gaussian list. Just the real
production output joined to settled outcomes.

Why this exists: the previous backtests (``diagnose_methodology``,
``diagnose_nowcast``, ``backfill_directional_shadow``) all build their
own Gaussian list from ``weather_gaussian_snapshots_backfill``, which
excludes NWS Point / MADIS / Tomorrow because those have only n=12 rows
each in the original Open-Meteo backfill. Those backtests therefore
measure a 3-source combine while production runs a 6-source combine
plus AFD. The 2026-04-27 audit found the NWS Point under-weighting bug
that no synthetic backtest could see — only this production-path
backtest can.

Usage::

    # On the VPS:
    python -m tools.backtest_production_path

Pulls all settled rows from ``weather_mm_shadow``, computes Brier per
series and pooled, and reports calibration buckets. No Kalshi calls;
finishes in seconds.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from typing import Optional

from bot.config import DB_PATH
from bot.db import init_db


def _bucket_label(diff_cents: float) -> str:
    a = abs(diff_cents)
    if a < 5: return "0-4c"
    if a < 15: return "5-14c"
    if a < 30: return "15-29c"
    if a < 60: return "30-59c"
    return "60c+"


def run(conn: sqlite3.Connection, since_unix: Optional[float] = None) -> None:
    where_clause = (
        " AND ts_unix >= ?" if since_unix is not None else ""
    )
    params: list = []
    if since_unix is not None:
        params.append(since_unix)

    # Per-series settled Brier: ours (production v2) vs market mid.
    print()
    print("=" * 92)
    print("Production-path Brier — fair_value_cents (live v2 output) vs market mid vs settled")
    print("=" * 92)
    rows = conn.execute(
        f"""SELECT
              series,
              COUNT(*) AS n,
              AVG((fair_value_cents/100.0 - ticker_settled_yes) *
                  (fair_value_cents/100.0 - ticker_settled_yes)) AS our_brier,
              AVG((market_mid/100.0 - ticker_settled_yes) *
                  (market_mid/100.0 - ticker_settled_yes)) AS mkt_brier,
              AVG(fair_value_cents/100.0) AS avg_fair,
              AVG(market_mid/100.0) AS avg_mkt,
              AVG(ticker_settled_yes*1.0) AS base_rate
            FROM weather_mm_shadow
           WHERE ticker_settled_yes IS NOT NULL
             AND fair_value_cents IS NOT NULL
             AND market_mid IS NOT NULL
             {where_clause}
        GROUP BY series ORDER BY n DESC""",
        params,
    ).fetchall()
    if not rows:
        print("  no settled rows yet — wait for tonight's settlement and re-run.")
        return

    print(f"  {'series':<11} {'n':>5} {'our_brier':>10} {'mkt_brier':>10} "
          f"{'edge':>8} {'avg_fair':>9} {'avg_mkt':>8} {'base_rate':>10}")
    print("  " + "-" * 80)
    for series, n, our_b, mkt_b, avg_f, avg_m, base in rows:
        edge = (mkt_b or 0) - (our_b or 0)
        print(f"  {series:<11} {n:>5} {our_b:>10.4f} {mkt_b:>10.4f} "
              f"{edge:>+7.4f} {avg_f:>9.3f} {avg_m:>8.3f} {base:>10.3f}")

    # Pooled
    pooled = conn.execute(
        f"""SELECT
              COUNT(*) AS n,
              AVG((fair_value_cents/100.0 - ticker_settled_yes) *
                  (fair_value_cents/100.0 - ticker_settled_yes)) AS our_brier,
              AVG((market_mid/100.0 - ticker_settled_yes) *
                  (market_mid/100.0 - ticker_settled_yes)) AS mkt_brier
            FROM weather_mm_shadow
           WHERE ticker_settled_yes IS NOT NULL
             AND fair_value_cents IS NOT NULL
             AND market_mid IS NOT NULL
             {where_clause}""",
        params,
    ).fetchone()
    print()
    print(f"  POOLED  n={pooled[0]:>5}  our={pooled[1]:.4f}  mkt={pooled[2]:.4f}  "
          f"edge={pooled[2]-pooled[1]:+.4f}")

    # Calibration buckets — for each bucket of our_fair, what was the actual
    # YES rate? A well-calibrated forecaster has actual ≈ predicted.
    print()
    print("=" * 92)
    print("Calibration buckets — when we said X¢, what fraction settled YES?")
    print("=" * 92)
    bucket_rows = conn.execute(
        f"""SELECT
              CASE
                WHEN fair_value_cents < 10 THEN '00-09c'
                WHEN fair_value_cents < 20 THEN '10-19c'
                WHEN fair_value_cents < 30 THEN '20-29c'
                WHEN fair_value_cents < 40 THEN '30-39c'
                WHEN fair_value_cents < 50 THEN '40-49c'
                WHEN fair_value_cents < 60 THEN '50-59c'
                WHEN fair_value_cents < 70 THEN '60-69c'
                WHEN fair_value_cents < 80 THEN '70-79c'
                WHEN fair_value_cents < 90 THEN '80-89c'
                ELSE '90-99c'
              END AS bucket,
              COUNT(*) AS n,
              AVG(fair_value_cents) AS avg_fair_c,
              AVG(ticker_settled_yes*100.0) AS actual_yes_pct,
              AVG(market_mid) AS avg_mkt_c
            FROM weather_mm_shadow
           WHERE ticker_settled_yes IS NOT NULL
             AND fair_value_cents IS NOT NULL
             {where_clause}
        GROUP BY bucket ORDER BY MIN(fair_value_cents)""",
        params,
    ).fetchall()
    print(f"  {'bucket':<10} {'n':>5} {'avg_fair':>9} {'actual_yes_pct':>14} {'avg_mkt':>8} {'verdict'}")
    print("  " + "-" * 75)
    for bucket, n, avg_f, actual, avg_m in bucket_rows:
        gap = (avg_f or 0) - (actual or 0)
        verdict = ("over-confident_HIGH" if gap > 10 else
                   "under-confident_HIGH" if gap < -10 else "well-calibrated")
        print(f"  {bucket:<10} {n:>5} {avg_f:>9.1f} {actual:>14.1f} "
              f"{avg_m:>8.1f}  {verdict}")

    # Disagreement bucket distribution
    print()
    print("=" * 92)
    print("Disagreement buckets — |our_fair − market_mid|")
    print("=" * 92)
    rows = conn.execute(
        f"""SELECT fair_value_cents, market_mid
            FROM weather_mm_shadow
           WHERE ticker_settled_yes IS NOT NULL
             AND fair_value_cents IS NOT NULL
             AND market_mid IS NOT NULL
             {where_clause}""",
        params,
    ).fetchall()
    bucket_counts: dict[str, int] = defaultdict(int)
    for fv, mm in rows:
        bucket_counts[_bucket_label(fv - mm)] += 1
    total = sum(bucket_counts.values())
    for label in ("0-4c", "5-14c", "15-29c", "30-59c", "60c+"):
        n = bucket_counts.get(label, 0)
        pct = 100.0 * n / total if total else 0
        print(f"  {label:<8} {n:>5} ({pct:>5.1f}%)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--since", default=None,
                   help="Only rows on/after this UTC timestamp "
                        "(e.g. '2026-04-27T14:18:00')")
    args = p.parse_args()

    since_unix = None
    if args.since:
        from datetime import datetime, timezone
        try:
            since_unix = datetime.fromisoformat(
                args.since.replace("Z", "+00:00")
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError as e:
            print(f"--since parse error: {e}")
            return 1

    conn = init_db(args.db)
    run(conn, since_unix=since_unix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
