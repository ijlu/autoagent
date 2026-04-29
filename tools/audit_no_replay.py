"""Phase C — characterize the no_replay rows in diagnose_v2_gap.

Question: are the 33 missing-snapshot tickers a random slice, or systematically
biased (e.g., late-day settlements where predict_v2 didn't run)? Bias would
mean every gap number we've quoted (0-6h +0.31, close-edge +0.15) is computed
on the easy 80% and the hard 20% is silently dropped.

Output: distribution tables + per-ticker drill so we can either rule out bias
or characterize it precisely.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

# Reuse the diagnostic's settle-time parser so apples-to-apples
sys.path.insert(0, "/home/kalshi/autoagent")
from tools.diagnose_v2_gap import _settle_unix_from_ticker  # noqa: E402


def _family_of(ticker: str) -> str:
    parts = ticker.split("-")
    return parts[0] if parts else "?"


def _settle_date_of(ticker: str) -> str:
    parts = ticker.split("-")
    if len(parts) < 2 or len(parts[1]) < 7:
        return "?"
    return parts[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/home/kalshi/autoagent/kalshi_trades.db")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    # Same candidate set as diagnose_v2_gap
    rows = conn.execute(
        """SELECT s.ticker, s.series,
                  MAX(s.ts_unix) AS ts,
                  MAX(s.fair_value_cents) AS live_fair,
                  MAX(s.market_mid) AS market_mid,
                  MAX(s.ticker_settled_yes) AS settled,
                  MAX(ab.ts_settle_unix) AS ab_settle_unix
             FROM weather_mm_shadow s
             LEFT JOIN alpha_backtest ab ON ab.ticker = s.ticker
            WHERE s.ticker_settled_yes IS NOT NULL
              AND s.fair_value_cents IS NOT NULL
              AND s.market_mid IS NOT NULL
         GROUP BY s.ticker
         ORDER BY ts ASC""",
    ).fetchall()

    print(f"[audit] {len(rows)} settled tickers (same as diagnose_v2_gap candidate set)\n")

    # For each, classify whether replay would succeed
    have_snapshots: list[tuple] = []
    no_snapshots: list[tuple] = []

    for tup in rows:
        ticker = tup[0]
        cnt = conn.execute(
            """SELECT COUNT(*)
                 FROM weather_forecast_snapshots
                WHERE ticker = ?
                  AND source NOT IN ('combined_v2', 'afd_bias')
                  AND forecast_high_f IS NOT NULL
                  AND hours_out IS NOT NULL""",
            (ticker,),
        ).fetchone()[0]
        if cnt == 0:
            no_snapshots.append(tup)
        else:
            have_snapshots.append(tup)

    print(f"have_snapshots = {len(have_snapshots)}")
    print(f"no_snapshots   = {len(no_snapshots)}\n")

    # Also count rows where there ARE snapshots but they're filtered out
    # (forecast_high_f NULL or hours_out NULL). If those drive most no_replay,
    # the issue isn't "predict_v2 didn't run" — it's "predict_v2 ran but
    # didn't write usable rows".
    print("=" * 70)
    print("For no_snapshots tickers: do they have ANY weather_forecast_snapshots")
    print("rows at all (even with NULL fields)?")
    print("=" * 70)
    has_any_snap = 0
    has_any_combined_v2 = 0
    for tup in no_snapshots:
        ticker = tup[0]
        any_cnt = conn.execute(
            """SELECT COUNT(*) FROM weather_forecast_snapshots WHERE ticker = ?""",
            (ticker,),
        ).fetchone()[0]
        cv2_cnt = conn.execute(
            """SELECT COUNT(*) FROM weather_forecast_snapshots
                WHERE ticker = ? AND source = 'combined_v2'""",
            (ticker,),
        ).fetchone()[0]
        if any_cnt > 0:
            has_any_snap += 1
        if cv2_cnt > 0:
            has_any_combined_v2 += 1
    print(f"  with any rows at all:       {has_any_snap}/{len(no_snapshots)}")
    print(f"  with combined_v2 rows:      {has_any_combined_v2}/{len(no_snapshots)}")
    print(f"  → if has_any=0: predict_v2 never ran on this ticker")
    print(f"  → if has_any>0 and cv2=0: source rows missing required fields")
    print(f"  → if cv2>0 but per-source=0: only combined_v2 was written, sources lost\n")

    # Distribution by family
    print("=" * 70)
    print("By family (no_snapshots / total)")
    print("=" * 70)
    fam_total: Counter = Counter()
    fam_no: Counter = Counter()
    for tup in rows:
        fam_total[_family_of(tup[0])] += 1
    for tup in no_snapshots:
        fam_no[_family_of(tup[0])] += 1
    for fam in sorted(fam_total):
        no_n = fam_no[fam]
        tot_n = fam_total[fam]
        rate = no_n / tot_n if tot_n else 0.0
        print(f"  {fam:25s}  {no_n:3d} / {tot_n:3d}  ({rate*100:5.1f}%)")
    print()

    # Distribution by settle date
    print("=" * 70)
    print("By settle date (no_snapshots / total)")
    print("=" * 70)
    date_total: Counter = Counter()
    date_no: Counter = Counter()
    for tup in rows:
        date_total[_settle_date_of(tup[0])] += 1
    for tup in no_snapshots:
        date_no[_settle_date_of(tup[0])] += 1
    for d in sorted(date_total):
        no_n = date_no[d]
        tot_n = date_total[d]
        marker = " ←" if no_n > 0 else ""
        print(f"  {d:15s}  {no_n:3d} / {tot_n:3d}{marker}")
    print()

    # Hours-to-settle for no_replay rows specifically
    print("=" * 70)
    print("Hours-to-settle distribution at the SHADOW row's ts_unix")
    print("=" * 70)
    print("(if no_replay tickers concentrate at h_out < 6h, the diagnostic")
    print(" is biased: it drops the hard close-to-settle cases)\n")

    def _h_out_of(tup):
        ticker, _series, ts, _lf, _mm, _settled, ab_set = tup
        if ab_set is not None:
            settle_unix = float(ab_set)
        else:
            settle_unix = _settle_unix_from_ticker(ticker)
        if settle_unix is None or ts is None:
            return None
        return (settle_unix - float(ts)) / 3600.0

    def _bucket(h):
        if h is None:
            return "no_settle"
        if h < 0:
            return "post_settle"
        if h < 6:
            return "0-6h"
        if h < 12:
            return "6-12h"
        if h < 24:
            return "12-24h"
        if h < 48:
            return "24-48h"
        return "48h+"

    bk_total: Counter = Counter()
    bk_no: Counter = Counter()
    for tup in rows:
        bk_total[_bucket(_h_out_of(tup))] += 1
    for tup in no_snapshots:
        bk_no[_bucket(_h_out_of(tup))] += 1

    order = ["post_settle", "0-6h", "6-12h", "12-24h", "24-48h", "48h+", "no_settle"]
    print(f"  {'bucket':15s}  no_snap / total   rate")
    print(f"  {'-'*15}  {'-'*15}   {'-'*5}")
    for bk in order:
        if bk in bk_total:
            no_n = bk_no.get(bk, 0)
            tot_n = bk_total[bk]
            rate = no_n / tot_n if tot_n else 0.0
            marker = " ← bias" if rate > 0.30 and tot_n >= 10 else ""
            print(f"  {bk:15s}  {no_n:3d} / {tot_n:3d}     ({rate*100:5.1f}%){marker}")
    print()

    # Per-ticker drill
    print("=" * 70)
    print(f"All {len(no_snapshots)} no_snapshots tickers (sorted by settle date)")
    print("=" * 70)
    no_snapshots_sorted = sorted(no_snapshots, key=lambda t: t[0])
    for tup in no_snapshots_sorted:
        ticker, series, ts, live_fair, market_mid, settled, ab_set = tup
        h_out = _h_out_of(tup)
        h_str = f"{h_out:5.1f}h" if h_out is not None else "  N/A"
        ts_str = (
            datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if ts else "?"
        )
        print(f"  {ticker:35s}  shadow_ts={ts_str}  h_out={h_str}  "
              f"settled={int(settled)}  market_mid={market_mid:5.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
