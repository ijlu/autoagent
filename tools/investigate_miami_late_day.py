"""Investigate the late-day Miami Brier blowups.

Two questions, one per case:

(a) METAR freshness: how stale was our latest METAR snapshot relative
    to settlement, and how much did the actual running max climb in
    that gap? If snapshot is at 3pm and the running max kept rising
    until 5pm, the replay's combined.μ never saw the late peak.

(b) Quoter activity: did the daemon write shadow rows in the final 2h
    before settlement? Sparse shadow rows in the final hours = the
    event-driven requote path was silent (METAR poller dead, or
    WeatherQuoter not being called on temp changes, or production was
    in a degraded mode for that window).

Cases investigated are the 4 worst by replay_b in the diagnose_v2_gap
Miami drill — every one has won_yes=1, market mid ≈ 0.99, our v2 ≈ 0.01.

Run on the VPS:

    python3 -m tools.investigate_miami_late_day \\
        --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

from bot.config import DB_PATH
from bot.db import init_db


CASES = [
    "KXHIGHMIA-26APR22-B81.5",
    "KXHIGHMIA-26APR24-B84.5",
    "KXHIGHMIA-26APR23-B79.5",
    "KXHIGHMIA-26APR25-B85.5",
]

_MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN",
           "JUL","AUG","SEP","OCT","NOV","DEC"]


def _lst_date_from_ticker(ticker: str) -> str:
    parts = ticker.split("-")
    suf = parts[1]
    yy = int(suf[:2])
    mon = _MONTHS.index(suf[2:5].upper()) + 1
    dd = int(suf[5:7])
    return f"20{yy:02d}-{mon:02d}-{dd:02d}"


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def investigate(conn: sqlite3.Connection, ticker: str) -> None:
    print()
    print("=" * 88)
    print(f"  {ticker}")
    print("=" * 88)

    # Settlement timestamp (authoritative — alpha_backtest)
    r = conn.execute(
        "SELECT MAX(ts_settle_unix) FROM alpha_backtest WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    settle_unix = float(r[0]) if r and r[0] else None
    if settle_unix is None:
        print("  no alpha_backtest row — abort case")
        return
    print(f"  settle_unix: {settle_unix:.0f}  ({_fmt_ts(settle_unix)})")

    # Latest 5 METAR snapshots — when, what μ
    print()
    print("  Latest 5 METAR snapshots logged for this ticker:")
    print(f"    {'recorded_at':<28} {'μ_°F':>7} {'hrs_out':>8} {'staleness_h':>13}")
    rows = conn.execute(
        """SELECT recorded_at, forecast_high_f, hours_out
             FROM weather_forecast_snapshots
            WHERE ticker = ? AND source = 'metar'
              AND forecast_high_f IS NOT NULL
            ORDER BY id DESC LIMIT 5""",
        (ticker,),
    ).fetchall()
    if not rows:
        print("    (no METAR snapshots ever logged for this ticker)")
    else:
        for rec_at, mu, h_out in rows:
            try:
                rec_dt = datetime.fromisoformat(rec_at.replace("Z", "+00:00"))
                staleness_h = (settle_unix - rec_dt.timestamp()) / 3600.0
            except Exception:
                staleness_h = float("nan")
            print(f"    {rec_at:<28} {mu:>7.1f} {h_out:>8} "
                  f"{staleness_h:>13.2f}")

    # Combined_v2 latest snapshot — what was our final prediction made of?
    print()
    print("  Latest combined_v2 snapshot:")
    r = conn.execute(
        """SELECT recorded_at, forecast_high_f, sigma_f, forecast_prob, hours_out
             FROM weather_forecast_snapshots
            WHERE ticker = ? AND source = 'combined_v2'
            ORDER BY id DESC LIMIT 1""",
        (ticker,),
    ).fetchone()
    if r:
        rec_at, mu, sigma, prob, h_out = r
        print(f"    {rec_at}  μ={mu:.2f}  σ={sigma:.2f}  prob={prob:.3f}  h_out={h_out}")
    else:
        print("    (none)")

    # Shadow row activity: count over time
    print()
    print("  weather_mm_shadow row counts by horizon-bucket:")
    rows = conn.execute(
        "SELECT ts_unix FROM weather_mm_shadow WHERE ticker = ? ORDER BY ts_unix",
        (ticker,),
    ).fetchall()
    n_total = len(rows)
    if not rows:
        print("    no shadow rows for this ticker")
    else:
        ts_first = float(rows[0][0])
        ts_last = float(rows[-1][0])
        bucket_counts = {"<2h": 0, "2-6h": 0, "6-12h": 0, "12-24h": 0, ">=24h": 0}
        for (ts,) in rows:
            h = (settle_unix - float(ts)) / 3600.0
            if h < 2:
                bucket_counts["<2h"] += 1
            elif h < 6:
                bucket_counts["2-6h"] += 1
            elif h < 12:
                bucket_counts["6-12h"] += 1
            elif h < 24:
                bucket_counts["12-24h"] += 1
            else:
                bucket_counts[">=24h"] += 1
        print(f"    total: {n_total}  first: {_fmt_ts(ts_first)}  "
              f"last: {_fmt_ts(ts_last)}  "
              f"last-row-staleness: {(settle_unix - ts_last)/3600.0:.2f}h before settle")
        print(f"    by bucket: {bucket_counts}")

    # Actual hourly METAR trajectory — what was the truth doing?
    print()
    print("  Actual hourly METAR (KMIA) for the settlement day:")
    lst_date = _lst_date_from_ticker(ticker)
    rows = conn.execute(
        """SELECT lst_hour, temp_f, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE station = 'KMIA' AND lst_date = ?
            ORDER BY lst_hour""",
        (lst_date,),
    ).fetchall()
    if not rows:
        print(f"    (no hourly METAR rows for KMIA on {lst_date})")
    else:
        running_max = float("-inf")
        print(f"    {'lst_hour':>8} {'temp_°F':>8} {'running_max':>11} "
              f"{'daily_high':>11}")
        for hr, temp, dh in rows:
            if temp is not None:
                running_max = max(running_max, float(temp))
            rm_str = f"{running_max:>11.1f}" if running_max != float("-inf") else "    --"
            dh_str = f"{dh:>11.1f}" if dh is not None else "         --"
            t_str = f"{float(temp):>8.1f}" if temp is not None else "      --"
            print(f"    {hr:>8} {t_str} {rm_str} {dh_str}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    conn = init_db(args.db)
    for t in CASES:
        investigate(conn, t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
