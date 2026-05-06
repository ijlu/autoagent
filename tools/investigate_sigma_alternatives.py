"""Investigate σ formulation alternatives for the v2 weather ensemble.

Loads per-source snapshots from `weather_forecast_snapshots`, re-derives the
precision-weighted combined μ from those rows, then projects onto each
ticker's bracket using FIVE different σ formulations:

  A. baseline           — current production: precision-combine σ floored at 1.0°F
  B. median_per_source  — replace combined σ with median of contributing-source σs
  C. lst_hour_floor     — floor depends on local-standard-time hour of day:
                          ≥19 LST → 0.3 (high locked); 17-19 → 0.7;
                          14-17 → 1.5 (peak window); <14 → 2.5
  D. no_floor           — use the raw precision-combine σ unchanged
  E. aleatoric_add      — σ_final = √(σ_precision² + 1.2²) (adds irreducible
                          forecast spread)

For each variant, computes Brier per ticker (one prediction per settled
ticker, the latest pre-settle snapshot) and stratifies by family + LST-hour
bucket + TTE band. Outputs a side-by-side comparison table.

Usage:
  python3 tools/investigate_sigma_alternatives.py --db /tmp/kalshi_trades.db
"""
from __future__ import annotations

import argparse
import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from typing import Optional

# Sources contributing to the production combine (per
# bot.signals.weather_sources.GAUSSIAN_COMBINE_SOURCES).
_COMBINE_SOURCES: frozenset[str] = frozenset({
    "hrrr", "metar", "nws_point", "weather", "icon", "ukmo",
})

# LST offsets per family (matches bot/daemon/stations.py — fixed,
# DST-agnostic per the registry comment).
_LST_OFFSET: dict[str, int] = {
    "KXHIGHNY":  -5,
    "KXHIGHMIA": -5,
    "KXHIGHCHI": -6,
    "KXHIGHAUS": -6,
    "KXHIGHDEN": -7,
    "KXHIGHLAX": -8,
}

_SQRT_2 = math.sqrt(2.0)


def _ncdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1 + math.erf((x - mu) / (sigma * _SQRT_2)))


def parse_ticker(ticker: str) -> Optional[tuple[str, bool, Optional[float],
                                                Optional[float], Optional[float],
                                                bool]]:
    """Return (family, is_bracket, threshold, lo, hi, is_above).

    Bracket centers are encoded as the .5 number; bracket spans ±1°F.
    Threshold tickers encode the threshold directly; we treat them as
    "above" since Kalshi's KXHIGH-T markets are always "≥X".
    """
    m = re.match(r"^(KXHIGH[A-Z]+)-\d{2}[A-Z]+\d{2}-([BT])(\d+(?:\.\d+)?)$", ticker)
    if not m:
        return None
    family, kind, num = m.group(1), m.group(2), float(m.group(3))
    if kind == "B":
        return family, True, None, num - 1.0, num + 1.0, True
    else:
        return family, False, num, None, None, True


def project(mu: float, sigma: float, is_bracket: bool,
            threshold: Optional[float], lo: Optional[float],
            hi: Optional[float], is_above: bool) -> float:
    if sigma <= 0:
        sigma = 0.01
    if is_bracket:
        p = _ncdf(hi, mu, sigma) - _ncdf(lo, mu, sigma)
    else:
        p = 1.0 - _ncdf(threshold, mu, sigma) if is_above else _ncdf(threshold, mu, sigma)
    return max(0.005, min(0.995, p))


def precision_combine(per_source: list[tuple[str, float, float]]
                      ) -> Optional[tuple[float, float, list[float]]]:
    """Precision-weighted combine of (source_name, mean, sigma) tuples.

    Returns (combined_mu, combined_sigma, list_of_contributing_sigmas).
    Group discount: model = {hrrr, nws_point, weather}, obs = {metar},
    other = {icon, ukmo} — matches production weather_ensemble_v2 layout
    EXCEPT we put icon/ukmo in their own bucket since that's how prod
    actually behaves (one of the bugs we're investigating).
    """
    model_group = {"hrrr", "nws_point", "weather"}
    obs_group = {"metar"}
    other_group = {"icon", "ukmo"}

    counts = {"model": 0, "obs": 0, "other": 0}
    for name, _, _ in per_source:
        if name in model_group:
            counts["model"] += 1
        elif name in obs_group:
            counts["obs"] += 1
        elif name in other_group:
            counts["other"] += 1

    total_precision = 0.0
    weighted_mean_num = 0.0
    sigmas: list[float] = []
    for name, mu, sigma in per_source:
        if sigma <= 0 or not math.isfinite(sigma):
            continue
        if name in model_group:
            n = max(1, counts["model"])
        elif name in obs_group:
            n = max(1, counts["obs"])
        elif name in other_group:
            n = max(1, counts["other"])
        else:
            n = 1
        weight = 1.0 / n
        precision = weight * (1.0 / (sigma * sigma))
        total_precision += precision
        weighted_mean_num += precision * mu
        sigmas.append(sigma)

    if total_precision <= 0 or not sigmas:
        return None
    return (weighted_mean_num / total_precision,
            1.0 / math.sqrt(total_precision), sigmas)


def lst_hour(ts_unix: int, family: str) -> int:
    offset = _LST_OFFSET.get(family, -5)
    lst_unix = ts_unix + offset * 3600
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(lst_unix).hour


def brier_each(per_source: list[tuple[str, float, float]],
               ticker: str, ts_unix: int, settle_unix: int,
               y: int) -> dict[str, float]:
    """Compute (p_yes, brier) under each σ formulation for one snapshot."""
    parsed = parse_ticker(ticker)
    if parsed is None:
        return {}
    family, is_bracket, threshold, lo, hi, is_above = parsed
    combined = precision_combine(per_source)
    if combined is None:
        return {}
    mu, sigma_prec, sigmas = combined
    h = lst_hour(ts_unix, family)

    # Variant A: current production (floor 1.0)
    sigma_a = max(sigma_prec, 1.0)

    # Variant B: median of per-source σ
    sigma_b = statistics.median(sigmas) if sigmas else sigma_prec

    # Variant C: LST-hour conditional floor
    if h >= 19:
        floor_c = 0.3
    elif h >= 17:
        floor_c = 0.7
    elif h >= 14:
        floor_c = 1.5
    else:
        floor_c = 2.5
    sigma_c = max(sigma_prec, floor_c)

    # Variant D: no floor
    sigma_d = sigma_prec

    # Variant E: aleatoric add (1.2°F irreducible spread)
    sigma_e = math.sqrt(sigma_prec * sigma_prec + 1.2 * 1.2)

    out: dict[str, float] = {}
    for label, s in [("A_baseline_floor1", sigma_a),
                     ("B_median_persource", sigma_b),
                     ("C_lst_hour_floor", sigma_c),
                     ("D_no_floor", sigma_d),
                     ("E_aleatoric_add12", sigma_e)]:
        p = project(mu, s, is_bracket, threshold, lo, hi, is_above)
        out[label + "_p"] = p
        out[label + "_b"] = (y - p) ** 2
    out["mu"] = mu
    out["sigma_prec"] = sigma_prec
    out["lst_hour"] = h
    out["family"] = family
    out["ticker"] = ticker
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--since", default="2026-04-22")
    ap.add_argument("--tte-target-h", type=float, default=4.0,
                    help="Pick the snapshot closest to this TTE per ticker (default 4h).")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # 1) Settled tickers in window — get ground truth + settle time.
    truth_rows = conn.execute("""
        SELECT ticker, MAX(ticker_settled_yes) AS y, MAX(ts_settle_unix) AS settle_unix
        FROM weather_mm_shadow
        WHERE ticker_settled_yes IS NOT NULL
          AND ts_settle_unix >= strftime('%s', ?)
        GROUP BY ticker
    """, (f"{args.since}T00:00:00Z",)).fetchall()
    truths = {r["ticker"]: (int(r["y"]), int(r["settle_unix"])) for r in truth_rows}
    print(f"[setup] {len(truths)} settled tickers since {args.since}")

    # 2) For each ticker, find the snapshot recorded_at closest to
    # (settle - tte_target_h) and pull every per-source row at that
    # recorded_at.
    selected_rows: list[dict] = []
    for ticker, (y, settle_unix) in truths.items():
        target_unix = settle_unix - int(args.tte_target_h * 3600)
        ra_row = conn.execute("""
            SELECT recorded_at,
                   ABS(CAST(strftime('%s', recorded_at) AS INTEGER) - ?) AS dt_s
            FROM weather_forecast_snapshots
            WHERE ticker = ? AND source != 'combined_v2' AND source != 'afd_bias'
            ORDER BY dt_s ASC LIMIT 1
        """, (target_unix, ticker)).fetchone()
        if ra_row is None:
            continue
        recorded_at = ra_row["recorded_at"]
        per_src = conn.execute("""
            SELECT source, forecast_high_f, sigma_f
            FROM weather_forecast_snapshots
            WHERE ticker = ? AND recorded_at = ?
              AND source IN ('hrrr','metar','nws_point','weather','icon','ukmo')
              AND forecast_high_f IS NOT NULL AND sigma_f IS NOT NULL
        """, (ticker, recorded_at)).fetchall()
        if len(per_src) < 2:
            continue
        per_source = [(r["source"], float(r["forecast_high_f"]),
                       float(r["sigma_f"])) for r in per_src]
        ts_unix = int(__import__("datetime").datetime.fromisoformat(
            recorded_at.replace("Z", "+00:00")).timestamp())
        scores = brier_each(per_source, ticker, ts_unix, settle_unix, y)
        if scores:
            scores["y"] = y
            scores["ts_unix"] = ts_unix
            scores["settle_unix"] = settle_unix
            scores["n_sources"] = len(per_source)
            selected_rows.append(scores)

    if not selected_rows:
        print("[error] no rows produced — aborting")
        return 1
    print(f"[setup] {len(selected_rows)} ticker × snapshot rows scored")

    # 3) Aggregate: by family, by LST-hour bucket, overall.
    variants = ["A_baseline_floor1", "B_median_persource",
                "C_lst_hour_floor", "D_no_floor", "E_aleatoric_add12"]

    def fmt(rows: list[dict]) -> str:
        n = len(rows)
        if n == 0:
            return f"n={n:>3}  (empty)"
        out = [f"n={n:>3}"]
        for v in variants:
            mean_b = sum(r[v + "_b"] for r in rows) / n
            out.append(f"{v}={mean_b:.3f}")
        return "  ".join(out)

    print()
    print("=" * 100)
    print(f"OVERALL ({len(selected_rows)} ticker-snapshots, target TTE = {args.tte_target_h}h)")
    print("=" * 100)
    print(fmt(selected_rows))

    print()
    print("BY FAMILY")
    print("-" * 100)
    by_family: dict[str, list[dict]] = defaultdict(list)
    for r in selected_rows:
        by_family[r["family"]].append(r)
    for fam in sorted(by_family):
        print(f"  {fam:12s}  {fmt(by_family[fam])}")

    print()
    print("BY LST HOUR BUCKET (UTC + family LST offset)")
    print("-" * 100)
    by_lst: dict[str, list[dict]] = defaultdict(list)
    for r in selected_rows:
        h = r["lst_hour"]
        if h >= 19:
            bkt = "C: ≥19 LST (high locked)"
        elif h >= 17:
            bkt = "B: 17-19 LST (post-peak)"
        elif h >= 14:
            bkt = "A: 14-17 LST (peak window)"
        else:
            bkt = "0: <14 LST (pre-peak)"
        by_lst[bkt].append(r)
    for bkt in sorted(by_lst):
        print(f"  {bkt:32s}  {fmt(by_lst[bkt])}")

    # 4) Sanity print: a few raw rows so we can spot-check the math.
    print()
    print("SAMPLE ROWS (first 5)")
    print("-" * 100)
    for r in selected_rows[:5]:
        print(f"  {r['ticker']:30s} y={r['y']} μ={r['mu']:.2f} σ_prec={r['sigma_prec']:.2f} "
              f"lst_h={r['lst_hour']:>2d}  "
              f"A={r['A_baseline_floor1_p']:.2f} "
              f"B={r['B_median_persource_p']:.2f} "
              f"C={r['C_lst_hour_floor_p']:.2f} "
              f"D={r['D_no_floor_p']:.2f} "
              f"E={r['E_aleatoric_add12_p']:.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
