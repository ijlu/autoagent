"""Per-source forecast accuracy audit.

For each (source, city, hours_out_bucket), join snapshots in
``weather_forecast_snapshots`` to observed daily highs in
``weather_metar_hourly_backfill`` and compute:

  * mean residual (forecast − observed) — the bias we should be
    correcting via MOS bias
  * std residual — the σ we should be using as skill σ
  * RMSE — the value the skill-σ fitter persists

Compares each fit to what's currently in ``kv_cache``. Surfaces gaps:
  * Cells where actual data disagrees with the persisted fit
  * Cells with no fit at all (so we know which sources/cities still
    use a wide pooled fallback)
  * Cells where data is too thin to fit

Run on the VPS::

    python -m tools.audit_source_residuals --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict

from bot.config import DB_PATH
from bot.db import init_db


_STATION_BY_CITY: dict[str, str] = {
    "nyc": "KNYC", "chicago": "KMDW", "miami": "KMIA",
    "los_angeles": "KLAX", "austin": "KAUS", "denver": "KDEN",
}
_BUCKET_EDGES = (0, 6, 24, 48, 168)


def _bucket_for(hours_out: float) -> str:
    if hours_out is None or hours_out < 0:
        return "?"
    for lo, hi in zip(_BUCKET_EDGES[:-1], _BUCKET_EDGES[1:]):
        if lo <= hours_out < hi:
            return f"{lo}_{hi}"
    return "out_of_range"


def _series_to_city(series: str) -> str:
    s = (series or "").upper()
    return {
        "KXHIGHNY": "nyc", "KXHIGHCHI": "chicago", "KXHIGHMIA": "miami",
        "KXHIGHLAX": "los_angeles", "KXHIGHAUS": "austin", "KXHIGHDEN": "denver",
    }.get(s, "?")


def _settle_date_from_ticker(ticker: str) -> str:
    parts = (ticker or "").split("-")
    if len(parts) < 2:
        return ""
    suf = parts[1]
    if len(suf) < 7:
        return ""
    months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    try:
        return f"20{int(suf[:2]):02d}-{months.index(suf[2:5].upper())+1:02d}-{int(suf[5:7]):02d}"
    except (ValueError, IndexError):
        return ""


def run(conn: sqlite3.Connection) -> None:
    # Latest snapshot per (source, ticker) — dedup so each settled day
    # contributes one (forecast, observed) pair, not 100 quote-time
    # repeats of the same forecast.
    rows = conn.execute(
        """SELECT s.source, s.series, s.ticker, s.forecast_high_f, s.hours_out
             FROM weather_forecast_snapshots s
             JOIN (SELECT source, ticker, MAX(id) AS mid
                     FROM weather_forecast_snapshots
                    WHERE forecast_high_f IS NOT NULL
                      AND source NOT IN ('combined_v2', 'afd_bias')
                      AND hours_out IS NOT NULL
                    GROUP BY source, ticker) latest ON latest.mid = s.id""",
    ).fetchall()

    obs_rows = conn.execute(
        """SELECT DISTINCT station, lst_date, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE daily_high_f IS NOT NULL""",
    ).fetchall()
    obs_lookup: dict[tuple[str, str], float] = {
        (str(s), str(d)): float(h) for s, d, h in obs_rows
    }

    cells: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    no_obs = 0
    for src, series, ticker, fcst, h_out in rows:
        city = _series_to_city(series)
        if city not in _STATION_BY_CITY:
            continue
        station = _STATION_BY_CITY[city]
        sd = _settle_date_from_ticker(ticker)
        observed = obs_lookup.get((station, sd))
        if observed is None:
            no_obs += 1
            continue
        residual = float(fcst) - float(observed)
        bucket = _bucket_for(float(h_out))
        cells[(src, city, bucket)].append(residual)

    print(f"[residual_audit] joined {sum(len(v) for v in cells.values())} samples; "
          f"skipped {no_obs} for missing observed daily high")
    print()

    # Pull current kv state for comparison
    def _kv_skill(src: str, city: str, bucket: str) -> tuple:
        for key in (f"weather_skill_{src}_{city}_{bucket}",
                    f"weather_skill_{src}_{bucket}"):
            row = conn.execute(
                "SELECT value FROM kv_cache WHERE key=?", (key,)
            ).fetchone()
            if row:
                try:
                    payload = json.loads(row[0])
                    return (key, float(payload.get("sigma", 0)),
                            int(payload.get("n", 0)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        return (None, None, None)

    def _kv_bias(src: str, city: str) -> tuple:
        row = conn.execute(
            "SELECT value FROM kv_cache WHERE key=?",
            (f"weather_mos_bias_{src}_{city}",),
        ).fetchone()
        if not row:
            return (None, None)
        try:
            payload = json.loads(row[0])
            return (float(payload.get("bias", 0)), int(payload.get("n", 0)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return (None, None)

    print("=" * 110)
    print("Per-(source, city, bucket) residuals vs current kv fit")
    print("=" * 110)
    print(f"  {'source':<11} {'city':<11} {'bucket':<6} {'n':>4} "
          f"{'data_bias':>10} {'data_σ':>8} {'data_RMSE':>10} | "
          f"{'kv_σ':>7} {'kv_key':<46}")
    print("  " + "-" * 105)
    for (src, city, bucket), residuals in sorted(cells.items()):
        if len(residuals) < 5:
            continue
        n = len(residuals)
        # Winsorize 2% tails before computing.
        sr = sorted(residuals)
        lo = sr[int(n * 0.02)]
        hi = sr[n - 1 - int(n * 0.02)]
        clipped = [max(lo, min(hi, r)) for r in residuals]
        mean_r = sum(clipped) / n
        std_r = statistics.stdev(clipped) if n >= 2 else 0.0
        rmse = math.sqrt(sum((r) ** 2 for r in clipped) / n)
        kv_key, kv_sigma, kv_n = _kv_skill(src, city, bucket)
        kv_str = f"{kv_sigma:>7.2f}" if kv_sigma is not None else "    --"
        kv_key_str = (kv_key or "(none)").replace("weather_skill_", "")
        print(f"  {src:<11} {city:<11} {bucket:<6} {n:>4} "
              f"{mean_r:>+10.2f} {std_r:>8.2f} {rmse:>10.2f} | "
              f"{kv_str} {kv_key_str:<46}")

    print()
    print("=" * 110)
    print("Per-(source, city) MOS bias residuals vs current kv fit")
    print("=" * 110)
    bias_cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (src, city, _bucket), residuals in cells.items():
        bias_cells[(src, city)].extend(residuals)
    print(f"  {'source':<11} {'city':<12} {'n':>4} {'data_bias':>10} "
          f"{'kv_bias':>9} {'gap':>8} {'note':<28}")
    print("  " + "-" * 90)
    for (src, city), residuals in sorted(bias_cells.items()):
        if len(residuals) < 5:
            continue
        n = len(residuals)
        sr = sorted(residuals)
        lo = sr[int(n * 0.02)]
        hi = sr[n - 1 - int(n * 0.02)]
        clipped = [max(lo, min(hi, r)) for r in residuals]
        mean_r = sum(clipped) / n
        kv_bias_val, kv_n = _kv_bias(src, city)
        if kv_bias_val is None:
            note = "NO_KV_FIT"
            kv_bias_str = "  --"
            gap_str = "  --"
        else:
            note = "ok" if abs(mean_r - kv_bias_val) < 0.5 else "DRIFT"
            kv_bias_str = f"{kv_bias_val:>+9.2f}"
            gap_str = f"{(mean_r - kv_bias_val):>+7.2f}"
        print(f"  {src:<11} {city:<12} {n:>4} {mean_r:>+10.2f} "
              f"{kv_bias_str} {gap_str}  {note}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    run(init_db(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
