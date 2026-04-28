"""Stratified analysis of AFD backtest results.

The pooled AFD signal hurts (mean error 1.88 with shift vs 0.98 without).
This tool slices the data to find any subset where AFD adds value:

  * Per-city: maybe NY's forecasters have signal even if Miami's don't
  * Per-confidence-bin (LLM)
  * By model_agreement category ("good"/"spread"/"outliers")
  * Shift magnitude bands: maybe small shifts are calibrated
  * Sign-only test: ignore magnitude, does direction help?
  * Days where NBM was wrong by ≥3°F: did AFD catch any of these?
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


_CITY_STATION = {
    "nyc": "KNYC", "chicago": "KMDW", "miami": "KMIA",
    "los_angeles": "KLAX", "austin": "KAUS", "denver": "KDEN",
}


def _build_dataset(conn: sqlite3.Connection) -> list[dict]:
    """Pull AFD backtest rows + observed highs + NBM baseline."""
    obs = {
        (s, d): float(h)
        for s, d, h in conn.execute(
            "SELECT station, lst_date, daily_high_f "
            "FROM weather_metar_hourly_backfill "
            "WHERE daily_high_f IS NOT NULL"
        ).fetchall()
    }
    # NBM baselines per (city, settle_date) from snapshots.
    nbm = {}
    for series, ticker, fcst in conn.execute(
        """SELECT s.series, s.ticker, AVG(s.forecast_high_f)
             FROM weather_forecast_snapshots s
            WHERE s.source='nbm' AND s.forecast_high_f IS NOT NULL
            GROUP BY s.ticker"""
    ).fetchall():
        city_map = {"KXHIGHNY":"nyc","KXHIGHCHI":"chicago","KXHIGHMIA":"miami",
                    "KXHIGHLAX":"los_angeles","KXHIGHAUS":"austin","KXHIGHDEN":"denver"}
        c = city_map.get(series)
        if not c or len(ticker.split('-')) < 2:
            continue
        suf = ticker.split('-')[1]
        if len(suf) < 7:
            continue
        months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
        try:
            sd = f"20{int(suf[:2]):02d}-{months.index(suf[2:5].upper())+1:02d}-{int(suf[5:7]):02d}"
        except (ValueError, IndexError):
            continue
        nbm.setdefault((c, sd), float(fcst))

    rows = conn.execute(
        """SELECT city, settle_date, old_bias_f, new_expected_high_f,
                  new_shift_vs_model_f, new_confidence, new_model_agreement,
                  new_key_driver
             FROM afd_backtest""",
    ).fetchall()
    out = []
    for (city, sd, old_bias, new_high, new_shift, new_conf,
         new_agree, new_driver) in rows:
        station = _CITY_STATION.get(city)
        if not station:
            continue
        observed = obs.get((station, sd))
        baseline = nbm.get((city, sd))
        if observed is None:
            continue
        out.append({
            "city": city, "date": sd,
            "observed": observed, "baseline": baseline,
            "old_bias": old_bias, "new_high": new_high,
            "new_shift": new_shift, "new_conf": new_conf,
            "new_agree": new_agree, "new_driver": new_driver,
            "residual": (observed - baseline) if baseline is not None else None,
        })
    return out


def _err_stats(samples, *, key_with_shift, key_without):
    """Compute mean |error| with vs without AFD shift."""
    err_with = []
    err_without = []
    for s in samples:
        if s["baseline"] is None:
            continue
        if s.get(key_with_shift) is None:
            continue
        applied = s[key_with_shift]
        err_with.append(abs(s["baseline"] + applied - s["observed"]))
        err_without.append(abs(s["baseline"] - s["observed"]))
    return err_with, err_without


def report(samples: list[dict]) -> None:
    n_total = len(samples)
    n_with_baseline = sum(1 for s in samples if s["baseline"] is not None)
    print(f"[stratified] {n_total} samples; {n_with_baseline} with NBM baseline")
    print()

    # ── 1. Per-city: does AFD help anywhere? ────────────────────────────
    print("=" * 96)
    print("Per-city: AFD shift effect on |error|")
    print("=" * 96)
    print(f"  {'city':<13} {'n':>4} {'|err|w/AFD':>12} {'|err|w/oAFD':>13} "
          f"{'gap':>7} {'verdict':<10}")
    print("  " + "-" * 70)
    for city in sorted(set(s["city"] for s in samples)):
        cs = [s for s in samples if s["city"] == city
              and s["baseline"] is not None and s["new_shift"] is not None]
        if not cs:
            continue
        applied_shift = [max(-5.0, min(5.0, s["new_shift"])) * (s["new_conf"] or 0)
                         for s in cs]
        err_w = [abs(s["baseline"] + a - s["observed"])
                 for s, a in zip(cs, applied_shift)]
        err_wo = [abs(s["baseline"] - s["observed"]) for s in cs]
        mw, mwo = statistics.mean(err_w), statistics.mean(err_wo)
        verdict = "HELPS" if mw < mwo - 0.05 else "HURTS" if mw > mwo + 0.05 else "neutral"
        print(f"  {city:<13} {len(cs):>4} {mw:>11.2f}°F {mwo:>12.2f}°F "
              f"{mw-mwo:>+6.2f} {verdict:<10}")

    # ── 2. Confidence bins: any band where AFD is calibrated? ───────────
    print()
    print("=" * 96)
    print("Confidence bins: does AFD help when LLM says 'high confidence'?")
    print("=" * 96)
    print(f"  {'conf bin':<14} {'n':>4} {'|err|w/AFD':>12} {'|err|w/oAFD':>13} "
          f"{'gap':>7} {'verdict':<10}")
    print("  " + "-" * 70)
    for lo, hi, label in [(0, 0.001, "0   (none)"),
                           (0.001, 0.4, "0.001-0.4"),
                           (0.4, 0.7, "0.4-0.7"),
                           (0.7, 1.01, "0.7-1.0  (high)")]:
        cs = [s for s in samples if s["baseline"] is not None
              and s["new_shift"] is not None and s["new_conf"] is not None
              and lo <= s["new_conf"] < hi]
        if not cs:
            continue
        applied = [max(-5.0, min(5.0, s["new_shift"])) * s["new_conf"] for s in cs]
        err_w = [abs(s["baseline"] + a - s["observed"]) for s, a in zip(cs, applied)]
        err_wo = [abs(s["baseline"] - s["observed"]) for s in cs]
        mw, mwo = statistics.mean(err_w), statistics.mean(err_wo)
        verdict = "HELPS" if mw < mwo - 0.05 else "HURTS" if mw > mwo + 0.05 else "neutral"
        print(f"  {label:<14} {len(cs):>4} {mw:>11.2f}°F {mwo:>12.2f}°F "
              f"{mw-mwo:>+6.2f} {verdict:<10}")

    # ── 3. By model_agreement: "good" vs "spread" vs "outliers" ─────────
    print()
    print("=" * 96)
    print("By model_agreement: when forecaster says spread/outliers, does AFD add info?")
    print("=" * 96)
    print(f"  {'agreement':<14} {'n':>4} {'|err|w/AFD':>12} {'|err|w/oAFD':>13} "
          f"{'gap':>7} {'verdict':<10}")
    print("  " + "-" * 70)
    for agree_label in ["good", "spread", "outliers", "unstated", None]:
        cs = [s for s in samples if s["baseline"] is not None
              and s["new_shift"] is not None
              and (s["new_agree"] == agree_label or
                   (agree_label is None and not s["new_agree"]))]
        if len(cs) < 5:
            continue
        applied = [max(-5.0, min(5.0, s["new_shift"])) * (s["new_conf"] or 0)
                   for s in cs]
        err_w = [abs(s["baseline"] + a - s["observed"]) for s, a in zip(cs, applied)]
        err_wo = [abs(s["baseline"] - s["observed"]) for s in cs]
        mw, mwo = statistics.mean(err_w), statistics.mean(err_wo)
        verdict = "HELPS" if mw < mwo - 0.05 else "HURTS" if mw > mwo + 0.05 else "neutral"
        label = agree_label if agree_label else "(blank)"
        print(f"  {label:<14} {len(cs):>4} {mw:>11.2f}°F {mwo:>12.2f}°F "
              f"{mw-mwo:>+6.2f} {verdict:<10}")

    # ── 4. Shift magnitude bands: maybe SMALL shifts are calibrated ─────
    print()
    print("=" * 96)
    print("Shift magnitude bands: are smaller shifts more reliable?")
    print("=" * 96)
    print(f"  {'|shift|':<14} {'n':>4} {'|err|w/AFD':>12} {'|err|w/oAFD':>13} "
          f"{'gap':>7} {'verdict':<10}")
    print("  " + "-" * 70)
    for lo, hi, label in [(0, 0.5, "0.0-0.5"), (0.5, 1.5, "0.5-1.5"),
                           (1.5, 3.0, "1.5-3.0"), (3.0, 5.01, "3.0-5.0"),
                           (5.01, 100, ">5  (cap viol)")]:
        cs = [s for s in samples if s["baseline"] is not None
              and s["new_shift"] is not None
              and lo <= abs(s["new_shift"]) < hi]
        if len(cs) < 5:
            continue
        applied = [max(-5.0, min(5.0, s["new_shift"])) * (s["new_conf"] or 0)
                   for s in cs]
        err_w = [abs(s["baseline"] + a - s["observed"]) for s, a in zip(cs, applied)]
        err_wo = [abs(s["baseline"] - s["observed"]) for s in cs]
        mw, mwo = statistics.mean(err_w), statistics.mean(err_wo)
        verdict = "HELPS" if mw < mwo - 0.05 else "HURTS" if mw > mwo + 0.05 else "neutral"
        print(f"  {label:<14} {len(cs):>4} {mw:>11.2f}°F {mwo:>12.2f}°F "
              f"{mw-mwo:>+6.2f} {verdict:<10}")

    # ── 5. Direction-only test: ignore magnitude, does sign help? ───────
    print()
    print("=" * 96)
    print("Direction-only: apply ±0.5°F by sign of AFD shift. Does direction add info?")
    print("=" * 96)
    cs = [s for s in samples if s["baseline"] is not None
          and s["new_shift"] is not None and abs(s["new_shift"]) > 0.001]
    err_w = [abs(s["baseline"] + (0.5 if s["new_shift"] > 0 else -0.5) - s["observed"])
             for s in cs]
    err_wo = [abs(s["baseline"] - s["observed"]) for s in cs]
    if cs:
        mw, mwo = statistics.mean(err_w), statistics.mean(err_wo)
        verdict = "HELPS" if mw < mwo - 0.05 else "HURTS" if mw > mwo + 0.05 else "neutral"
        print(f"  n={len(cs)}  |err| w/sign-shift={mw:.2f}°F  "
              f"w/o={mwo:.2f}°F  gap={mw-mwo:+.2f}  {verdict}")

    # ── 6. AFD on hard days: did it catch big residuals? ────────────────
    print()
    print("=" * 96)
    print("Hard days: when |NBM residual| ≥ 3°F, did AFD catch the direction?")
    print("=" * 96)
    hard = [s for s in samples if s["baseline"] is not None
            and s["residual"] is not None and abs(s["residual"]) >= 3.0
            and s["new_shift"] is not None]
    if hard:
        n_dir_right = sum(1 for s in hard
                          if (s["new_shift"] > 0) == (s["residual"] > 0))
        n_total = sum(1 for s in hard if abs(s["new_shift"]) > 0.001)
        applied = [max(-5.0, min(5.0, s["new_shift"])) * (s["new_conf"] or 0)
                   for s in hard]
        err_w = [abs(s["baseline"] + a - s["observed"])
                 for s, a in zip(hard, applied)]
        err_wo = [abs(s["baseline"] - s["observed"]) for s in hard]
        mw, mwo = statistics.mean(err_w), statistics.mean(err_wo)
        print(f"  n={len(hard)} hard days  ({n_total} non-zero AFD shifts)")
        if n_total:
            print(f"  Sign alignment: {n_dir_right}/{n_total} = "
                  f"{100*n_dir_right/n_total:.1f}%  (50% = chance)")
        print(f"  Mean |err| w/AFD: {mw:.2f}°F  w/o: {mwo:.2f}°F  gap: {mw-mwo:+.2f}")

    # ── 7. AFD-helped subset: when WAS AFD right? ───────────────────────
    print()
    print("=" * 96)
    print("Find the wins: rows where AFD shift HELPED reduce |error| by ≥1°F")
    print("=" * 96)
    wins = []
    for s in samples:
        if s["baseline"] is None or s["new_shift"] is None:
            continue
        applied = max(-5.0, min(5.0, s["new_shift"])) * (s["new_conf"] or 0)
        err_w = abs(s["baseline"] + applied - s["observed"])
        err_wo = abs(s["baseline"] - s["observed"])
        if err_wo - err_w >= 1.0:
            wins.append((err_wo - err_w, s))
    wins.sort(reverse=True)
    print(f"  Total wins (≥1°F improvement): {len(wins)} of "
          f"{sum(1 for s in samples if s['baseline'] is not None and s['new_shift'] is not None)}")
    if wins:
        print(f"  Top 8 wins:")
        for improvement, s in wins[:8]:
            print(f"    {s['city']:<12} {s['date']}  shift={s['new_shift']:>+5.1f}°F "
                  f"conf={s['new_conf'] or 0:.2f}  "
                  f"err_w={abs(s['baseline'] + max(-5,min(5,s['new_shift'])) * (s['new_conf'] or 0) - s['observed']):.2f}°F  "
                  f"err_wo={abs(s['baseline']-s['observed']):.2f}°F  "
                  f"saved={improvement:.2f}°F  driver='{s['new_driver'] or ''}'")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    samples = _build_dataset(init_db(args.db))
    report(samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
