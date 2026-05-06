"""Validate candidate new forecast sources against observed daily highs.

For each candidate model (ECMWF HRES, GraphCast, AIFS, GEM, etc.):
  a) Better than current?  Compare MAE / RMSE / bias vs observed CF6 highs
  b) Bug-free?            Reject pathological values + check non-constant
  c) Independent?         Pairwise residual correlation vs current sources
  d) Improves ensemble?   Synthetic precision-weighted combine W vs W/O it

Pulls last N days from Open-Meteo's historical-forecast archive, joins to
``weather_metar_hourly_backfill.daily_high_f`` (CF6-corrected), reports.

Open-Meteo historical forecast archive serves the model output that was
actually issued for past dates — so it's an honest backtest, not "we
trained on it then asked it to predict the same dates."

Usage:
  python3 tools/investigate_new_forecast_sources.py \\
      --db /tmp/kalshi_trades.db --days 30 --start 2026-04-01

NOTE: hits Open-Meteo from the local IP. Free non-commercial limits
(10K/day) apply but our footprint is tiny: 6 cities × N models × 1 call.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Cities + their CF6 settlement station ─────────────────────────────
CITIES: dict[str, dict] = {
    "miami":       {"lat": 25.79, "lon": -80.29, "station": "KMIA"},
    "nyc":         {"lat": 40.78, "lon": -73.97, "station": "KNYC"},
    "chicago":     {"lat": 41.79, "lon": -87.75, "station": "KMDW"},
    "los_angeles": {"lat": 33.94, "lon": -118.41, "station": "KLAX"},
    "austin":      {"lat": 30.19, "lon": -97.67, "station": "KAUS"},
    "denver":      {"lat": 39.85, "lon": -104.66, "station": "KDEN"},
}


# ── Candidate models to validate ──────────────────────────────────────
CANDIDATE_MODELS: dict[str, str] = {
    # Currently in our combine (baseline)
    "hrrr_baseline":      "gfs_hrrr",
    "ukmo_baseline":      "ukmo_seamless",
    "icon_baseline":      "icon_seamless",
    # New candidates
    "ecmwf_hres":         "ecmwf_ifs025",
    "ecmwf_aifs":         "ecmwf_aifs025_single",
    "graphcast":          "gfs_graphcast025",
    "gem":                "gem_seamless",
    "metno":              "metno_seamless",
    "meteofrance":        "meteofrance_seamless",
}


_HIST_BASE = "https://historical-forecast-api.open-meteo.com/v1/forecast"


def fetch_historical_daily_max(
    lat: float, lon: float, model: str, start_date: str, end_date: str,
) -> dict[str, float]:
    """Return {YYYY-MM-DD: daily_high_f} for ``model`` at (lat, lon).

    Empty dict on any fetch failure or empty response.
    """
    import requests
    url = (
        f"{_HIST_BASE}?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&timezone=America/New_York&models={model}"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"  [{model}] HTTP {r.status_code}: {r.text[:120]}")
            return {}
        body = r.json()
    except Exception as e:
        print(f"  [{model}] {type(e).__name__}: {e}")
        return {}
    daily = body.get("daily") or {}
    times = daily.get("time") or []
    temps = daily.get("temperature_2m_max") or []
    out: dict[str, float] = {}
    for d, t in zip(times, temps):
        if t is not None and isinstance(t, (int, float)) and math.isfinite(t):
            out[d] = float(t)
    return out


def load_observed_highs(
    conn: sqlite3.Connection, station: str, start_date: str, end_date: str,
) -> dict[str, float]:
    """Return {YYYY-MM-DD: observed_high_f} for one station."""
    rows = conn.execute(
        """SELECT DISTINCT lst_date, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE station = ?
              AND lst_date >= ? AND lst_date <= ?
              AND daily_high_f IS NOT NULL""",
        (station, start_date, end_date),
    ).fetchall()
    return {str(d): float(h) for d, h in rows}


def fmt_metric(label: str, value: Optional[float], unit: str = "°F") -> str:
    if value is None:
        return f"{label}=n/a"
    return f"{label}={value:+.2f}{unit}" if "bias" in label else f"{label}={value:.2f}{unit}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/tmp/kalshi_trades.db")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--start", type=str,
                    default=(datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d"))
    args = ap.parse_args()

    end_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = args.start
    print(f"[setup] window: {start_date} → {end_date}")
    print(f"[setup] cities: {list(CITIES.keys())}")
    print(f"[setup] models: {list(CANDIDATE_MODELS.keys())}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Build per-city, per-model time series of (forecast, observed) pairs.
    # Outer dict: model_label → city → {date: (forecast, observed)}
    pairs: dict[str, dict[str, dict[str, tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(dict)
    )

    for city, cfg in CITIES.items():
        observed = load_observed_highs(conn, cfg["station"], start_date, end_date)
        if not observed:
            print(f"[{city}] no observed highs in window — skipping")
            continue
        print(f"[{city}] {len(observed)} observed days")
        for model_label, model_id in CANDIDATE_MODELS.items():
            forecasts = fetch_historical_daily_max(
                cfg["lat"], cfg["lon"], model_id, start_date, end_date,
            )
            time.sleep(0.4)  # be polite to the rate limit
            paired: dict[str, tuple[float, float]] = {}
            for date_iso, fcst in forecasts.items():
                obs = observed.get(date_iso)
                if obs is not None:
                    paired[date_iso] = (fcst, obs)
            pairs[model_label][city] = paired
            print(f"  {model_label:20s}  paired {len(paired):2d}/{len(observed)}")

    # ── (a) MAE / RMSE / bias per (model, city) ────────────────────
    print()
    print("=" * 100)
    print("(A) ACCURACY: MAE / RMSE / bias  (forecast minus observed; positive = forecast warm)")
    print("=" * 100)
    print(f"{'model':22s} {'city':14s} {'n':>4s}  {'MAE':>7s} {'RMSE':>7s} {'bias':>7s}")
    print("-" * 100)
    per_model_mae: dict[str, list[float]] = defaultdict(list)
    per_model_n: dict[str, int] = defaultdict(int)
    per_model_residuals: dict[str, list[float]] = defaultdict(list)  # for (c) below
    for model_label in CANDIDATE_MODELS:
        for city in CITIES:
            paired = pairs[model_label].get(city, {})
            n = len(paired)
            if n == 0:
                continue
            errs = [(f - o) for f, o in paired.values()]
            mae = sum(abs(e) for e in errs) / n
            rmse = math.sqrt(sum(e * e for e in errs) / n)
            bias = sum(errs) / n
            print(f"{model_label:22s} {city:14s} {n:>4d}  {mae:>6.2f}°F {rmse:>6.2f}°F {bias:>+6.2f}°F")
            per_model_mae[model_label].append(mae)
            per_model_n[model_label] += n
            for date_iso, (f, o) in paired.items():
                per_model_residuals[model_label].append(f - o)

    # ── pooled aggregate ───────────────────────────────────────────
    print()
    print("Pooled across cities:")
    print(f"{'model':22s} {'n':>5s}  {'mean MAE':>10s} {'mean bias':>11s}")
    print("-" * 60)
    for label in CANDIDATE_MODELS:
        if not per_model_mae[label]:
            continue
        mean_mae = sum(per_model_mae[label]) / len(per_model_mae[label])
        mean_bias = sum(per_model_residuals[label]) / max(1, len(per_model_residuals[label]))
        print(f"{label:22s} {per_model_n[label]:>5d}  {mean_mae:>9.2f}°F {mean_bias:>+10.2f}°F")

    # ── (b) sanity / bug-free check ────────────────────────────────
    print()
    print("=" * 100)
    print("(B) SANITY: any constant predictions, NaN, or extreme outliers?")
    print("=" * 100)
    for model_label in CANDIDATE_MODELS:
        all_fcsts = [
            f for city in CITIES
            for f, _ in pairs[model_label].get(city, {}).values()
        ]
        if not all_fcsts:
            print(f"{model_label:22s}  no forecasts (model id may be invalid)")
            continue
        n = len(all_fcsts)
        unique = len(set(round(x, 1) for x in all_fcsts))
        std = statistics.stdev(all_fcsts) if n > 1 else 0.0
        mn, mx = min(all_fcsts), max(all_fcsts)
        flag = ""
        if unique < 5:
            flag = "⚠ low diversity"
        elif std < 1.0:
            flag = "⚠ low spread"
        elif mn < -50 or mx > 150:
            flag = "⚠ extreme values"
        elif n < 5:
            flag = "⚠ thin sample"
        print(f"{model_label:22s}  n={n:3d} unique={unique:3d} std={std:5.2f}°F "
              f"range=[{mn:.1f}, {mx:.1f}]  {flag}")

    # ── (c) Independence: pairwise residual correlation ────────────
    print()
    print("=" * 100)
    print("(C) INDEPENDENCE: pairwise correlation of residuals (forecast - observed)")
    print("    Lower abs correlation = more independent signal. Same-day residuals only.")
    print("=" * 100)
    # Build (city, date) → {model_label: residual}
    by_cell: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for model_label in CANDIDATE_MODELS:
        for city in CITIES:
            for date_iso, (f, o) in pairs[model_label].get(city, {}).items():
                by_cell[(city, date_iso)][model_label] = f - o
    # For each pair of models, gather co-occurring residuals.
    model_list = list(CANDIDATE_MODELS.keys())
    print(f"{'pair':50s} {'n':>4s} {'rho':>7s}")
    print("-" * 100)
    for i in range(len(model_list)):
        for j in range(i + 1, len(model_list)):
            a, b = model_list[i], model_list[j]
            xs, ys = [], []
            for residuals in by_cell.values():
                if a in residuals and b in residuals:
                    xs.append(residuals[a])
                    ys.append(residuals[b])
            if len(xs) < 5:
                continue
            mean_x = sum(xs) / len(xs)
            mean_y = sum(ys) / len(ys)
            num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
            denom = math.sqrt(
                sum((x - mean_x) ** 2 for x in xs) *
                sum((y - mean_y) ** 2 for y in ys)
            )
            rho = num / denom if denom > 0 else 0.0
            print(f"{a:>22s} ↔ {b:25s} {len(xs):>4d} {rho:>+7.3f}")

    # ── (d) Synthetic ensemble Brier-equivalent: MAE of equal-weight
    #     mean of (baseline + new) vs baseline alone. Lower is better.
    # Baseline = mean of (hrrr_baseline, icon_baseline, ukmo_baseline)
    # We use MAE rather than Brier because we don't have brackets here;
    # MAE on daily-high directly tracks μ accuracy which is what we
    # care about most.
    print()
    print("=" * 100)
    print("(D) ENSEMBLE IMPACT: MAE of equal-weighted ensemble (baseline ± candidate)")
    print("    Compares baseline = mean(hrrr+icon+ukmo) vs baseline + new candidate.")
    print("=" * 100)
    print(f"{'candidate':22s} {'n':>4s}  {'baseline MAE':>14s}  {'with cand MAE':>14s}  {'Δ':>9s}")
    print("-" * 100)
    baseline_models = ["hrrr_baseline", "icon_baseline", "ukmo_baseline"]
    candidates = ["ecmwf_hres", "ecmwf_aifs", "graphcast", "gem",
                  "metno", "meteofrance"]
    for cand in candidates:
        rows = []
        for (city, date_iso), residuals in by_cell.items():
            base_avail = [residuals.get(b) for b in baseline_models]
            if not all(r is not None for r in base_avail):
                continue
            cand_resid = residuals.get(cand)
            if cand_resid is None:
                continue
            baseline_resid = sum(base_avail) / len(base_avail)
            with_cand_resid = (sum(base_avail) + cand_resid) / (len(base_avail) + 1)
            rows.append((baseline_resid, with_cand_resid))
        if not rows:
            continue
        n = len(rows)
        base_mae = sum(abs(r[0]) for r in rows) / n
        cand_mae = sum(abs(r[1]) for r in rows) / n
        delta = cand_mae - base_mae
        improvement = "✓" if delta < -0.05 else ("≈" if abs(delta) <= 0.05 else "✗")
        print(f"{cand:22s} {n:>4d}  {base_mae:>13.3f}°F  {cand_mae:>13.3f}°F  "
              f"{delta:>+8.3f} {improvement}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
