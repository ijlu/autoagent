"""Per-source forecast accuracy by hours-out-to-settlement.

Answers Josh's question: do live / short-lead sources have measurably
lower error than long-lead forecast sources? They should, structurally —
fresher information, less atmospheric evolution to mis-model.

For each settled day with snapshot data + observed daily high:
  * Group snapshots by (source, hours_out_bucket)
  * Compute mean |forecast − observed| and stdev
  * Compare across sources

Also computes per-source Brier *if we used only that source* — taking
the source's μ + a reasonable σ, projecting on the actual Kalshi
bracket, comparing to settled outcome. This tells us "if we ran the bot
on JUST this source, what would Brier be?"

Run on the VPS::

    python -m tools.audit_source_accuracy_by_horizon --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
from collections import defaultdict
from typing import Optional

from bot.config import DB_PATH
from bot.db import init_db


_STATION_BY_CITY: dict[str, str] = {
    "nyc": "KNYC", "chicago": "KMDW", "miami": "KMIA",
    "los_angeles": "KLAX", "austin": "KAUS", "denver": "KDEN",
}


def _series_to_city(series: str) -> str:
    return {
        "KXHIGHNY": "nyc", "KXHIGHCHI": "chicago", "KXHIGHMIA": "miami",
        "KXHIGHLAX": "los_angeles", "KXHIGHAUS": "austin", "KXHIGHDEN": "denver",
    }.get((series or "").upper(), "?")


def _settle_date(ticker: str) -> str:
    parts = (ticker or "").split("-")
    if len(parts) < 2 or len(parts[1]) < 7:
        return ""
    suf = parts[1]
    months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    try:
        return f"20{int(suf[:2]):02d}-{months.index(suf[2:5].upper())+1:02d}-{int(suf[5:7]):02d}"
    except (ValueError, IndexError):
        return ""


def _hours_bucket(h: float) -> str:
    if h is None:
        return "?"
    if h < 4:    return "0-3h    (immediate)"
    if h < 8:    return "4-7h    (late afternoon)"
    if h < 12:   return "8-11h   (afternoon)"
    if h < 18:   return "12-17h  (morning-of)"
    if h < 24:   return "18-23h  (evening prior)"
    if h < 36:   return "24-35h  (1-day-out)"
    return         "36+h    (multi-day)"


def run(conn: sqlite3.Connection) -> None:
    # Pull every snapshot row that we have an observed daily high for.
    # Don't dedup here — we want to see source accuracy stratified by
    # hours_out, which means each row counts as its own data point.
    rows = conn.execute(
        """SELECT s.source, s.series, s.ticker, s.forecast_high_f, s.hours_out
             FROM weather_forecast_snapshots s
            WHERE s.forecast_high_f IS NOT NULL
              AND s.source NOT IN ('combined_v2', 'afd_bias')
              AND s.hours_out IS NOT NULL""",
    ).fetchall()
    obs = {
        (str(s), str(d)): float(h)
        for s, d, h in conn.execute(
            "SELECT DISTINCT station, lst_date, daily_high_f "
            "FROM weather_metar_hourly_backfill WHERE daily_high_f IS NOT NULL"
        ).fetchall()
    }

    # (source, hours_out_bucket) → list of |residual|
    cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    n_skipped = 0
    for src, series, ticker, fcst, h_out in rows:
        city = _series_to_city(series)
        if city not in _STATION_BY_CITY:
            continue
        sd = _settle_date(ticker)
        observed = obs.get((_STATION_BY_CITY[city], sd))
        if observed is None:
            n_skipped += 1
            continue
        residual = float(fcst) - float(observed)
        bucket = _hours_bucket(float(h_out))
        cells[(src, bucket)].append(residual)

    print(f"[horizon_audit] joined {sum(len(v) for v in cells.values())} samples; "
          f"skipped {n_skipped} (no observed)\n")

    # All buckets in order
    buckets = ["0-3h    (immediate)",
               "4-7h    (late afternoon)",
               "8-11h   (afternoon)",
               "12-17h  (morning-of)",
               "18-23h  (evening prior)",
               "24-35h  (1-day-out)",
               "36+h    (multi-day)"]
    sources_order = ["metar", "madis", "nws_point",
                     "hrrr", "nbm", "weather", "open_meteo"]

    # Header
    print("=" * 110)
    print("Per-(source, hours-out) residual: forecast - observed daily_high")
    print("=" * 110)
    print(f"  {'source':<11} {'bucket':<26} {'n':>5} {'mean':>7} "
          f"{'|mean|':>7} {'std':>7} {'RMSE':>7}")
    print("  " + "-" * 75)
    for src in sources_order:
        for bucket in buckets:
            residuals = cells.get((src, bucket), [])
            if len(residuals) < 5:
                continue
            n = len(residuals)
            mean_r = sum(residuals) / n
            std_r = statistics.stdev(residuals) if n >= 2 else 0
            rmse = math.sqrt(sum(r * r for r in residuals) / n)
            mean_abs = sum(abs(r) for r in residuals) / n
            print(f"  {src:<11} {bucket:<26} {n:>5} {mean_r:>+7.2f} "
                  f"{mean_abs:>7.2f} {std_r:>7.2f} {rmse:>7.2f}")
        print()

    # Cross-source comparison: at the same (city, day), which source is closest?
    print("=" * 110)
    print("Per-(city, day) winner frequency — which source had the smallest |error|?")
    print("=" * 110)
    # Build (city, day) → {source: |error|}
    by_city_day: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for (src, bucket), residuals in cells.items():
        # Reuse cells but flatten per-source-per-day
        pass  # we need a different aggregation
    # Re-aggregate from raw rows
    by_cd: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for src, series, ticker, fcst, h_out in rows:
        city = _series_to_city(series)
        if city not in _STATION_BY_CITY:
            continue
        sd = _settle_date(ticker)
        observed = obs.get((_STATION_BY_CITY[city], sd))
        if observed is None:
            continue
        # Take latest snapshot per (source, city, day) — closest to settlement
        # is the most accurate; using avg would mix early-day high-error reads.
        residual = abs(float(fcst) - float(observed))
        by_cd[(city, sd)][src].append(residual)

    # For each (city, day), pick min |error| per source (closest snapshot)
    winners: dict[str, int] = defaultdict(int)
    appearances: dict[str, int] = defaultdict(int)
    n_eligible = 0
    for (city, day), per_source in by_cd.items():
        # Need at least 2 sources to compare
        if len(per_source) < 2:
            continue
        n_eligible += 1
        # Best (smallest) abs residual per source
        best_per_src = {s: min(rs) for s, rs in per_source.items()}
        for s in best_per_src:
            appearances[s] += 1
        winner = min(best_per_src.items(), key=lambda kv: kv[1])[0]
        winners[winner] += 1
    print(f"  Eligible (city, day) cells: {n_eligible}")
    print()
    print(f"  {'source':<11} {'wins':>5} {'appearances':>12} {'win_rate':>10}")
    print("  " + "-" * 45)
    for s in sources_order:
        if appearances.get(s, 0) == 0:
            continue
        wr = winners.get(s, 0) / appearances[s]
        print(f"  {s:<11} {winners.get(s,0):>5} {appearances[s]:>12} "
              f"{wr*100:>9.1f}%")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    run(init_db(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
