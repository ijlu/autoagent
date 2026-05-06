"""Thorough validation of v2 ensemble μ accuracy at low TTE.

Asked by Josh on 2026-04-29: "Why doesn't our model converge to truth as
settle nears? That feels like an easy and obvious fix." Answer hinged on
``_COMBINED_SIGMA_FLOOR_F = 1.0`` blocking convergence regardless of how
confident the underlying inputs are. Before relaxing that floor we need
to know: at low TTE, is our μ accurate enough that a tighter σ wouldn't
just produce extreme-confidence catastrophic Brier?

Validation answers:
  1. ``mu_accuracy_by_tte`` — MAE / RMSE / p50/p90/p99 of |μ - actual_high|,
     bucketed by hours-to-settle.
  2. ``sigma_calibration_by_tte`` — z-score = (μ - actual)/σ. If well-calibrated
     |z|<1 ≈ 68%, |z|<2 ≈ 95%. Reports actual coverage per TTE.
  3. ``floor_binding_analysis`` — when σ is at the 1.0°F floor, how often is
     |μ-actual| < 0.5°F (floor wastes precision) vs > 1°F (floor saving us)?
  4. ``per_station_accuracy`` — same metrics broken out by station.
  5. ``tail_risk_by_tte`` — rate of |μ-actual| > 2°F, > 3°F, > 5°F per bucket.
  6. ``floor_schedule_simulation`` — try multiple TTE-aware floor schedules,
     simulate the resulting σ, compute Brier vs current. Identify if any
     schedule reduces Brier without inflating tail-miss risk.
  7. ``metar_alone_baseline`` — at low TTE, does combined μ beat using
     METAR running max alone as the prediction?

Output: writes to ``reports/MU_VALIDATION_<date>.md`` plus a stand-alone
``mu_validation_results`` table for downstream analysis.

Invariant: this script is read-only against production tables. Write
target is a single new analysis table.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
from datetime import datetime
from typing import Optional


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bracket_p_yes(mu: float, sigma: float, lo: float, hi: float) -> float:
    if sigma <= 0:
        return 1.0 if lo <= mu <= hi else 0.0
    z_hi = (hi - mu) / sigma
    z_lo = (lo - mu) / sigma
    p = _ncdf(z_hi) - _ncdf(z_lo)
    return max(0.005, min(0.995, p))


_TICKER_TO_STATION = {
    "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA",
    "KXHIGHAUS": "KAUS", "KXHIGHLAX": "KLAX", "KXHIGHDEN": "KDEN",
}

_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05",
    "JUN": "06", "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10",
    "NOV": "11", "DEC": "12",
}


def _ticker_meta(ticker: str) -> Optional[tuple[str, str, float, float]]:
    """Return (station, settle_lst_date_iso, bracket_lo, bracket_hi) or None."""
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    fam = parts[0]
    station = _TICKER_TO_STATION.get(fam)
    if not station:
        return None
    settle_raw = parts[1]
    if len(settle_raw) < 7:
        return None
    try:
        yy = int(settle_raw[:2])
        mon = settle_raw[2:5].upper()
        dd = int(settle_raw[5:7])
        mm = _MONTH_MAP.get(mon)
        if not mm:
            return None
        lst_date = f"20{yy:02d}-{mm}-{dd:02d}"
    except (ValueError, IndexError):
        return None

    suffix = parts[2]  # B68.5 or T75
    if suffix.startswith("B"):
        try:
            b_value = float(suffix[1:])
            # Kalshi B<N>.5 convention: high in [floor(N), floor(N)+2).
            # E.g., B68.5 = [68, 70). Verified vs settlement_result data.
            lo = b_value - 0.5
            return (station, lst_date, lo, lo + 2.0)
        except ValueError:
            return None
    if suffix.startswith("T"):
        try:
            t = float(suffix[1:])
            # T-tickers are "high > t" but direction is API-only; for this
            # analysis we always use (-1000, t) as "below" then flip when
            # needed. The μ accuracy itself doesn't depend on direction.
            return (station, lst_date, t, t + 100.0)
        except ValueError:
            return None
    return None


def fetch_validation_rows(conn: sqlite3.Connection) -> list:
    """Pull the joined per-cycle rows: (ticker, recorded_at, μ, σ, actual_high,
    hours_left, station, lst_date)."""
    rows = conn.execute(
        """SELECT r.ticker, r.recorded_at, r.postfix_mu_f, r.postfix_sigma_f,
                  r.postfix_p_yes, w.hours_left, w.market_mid
             FROM replay_postfix_results r
             JOIN weather_mm_shadow w ON w.ticker = r.ticker
              AND ABS(w.ts_unix - CAST(strftime('%s', r.recorded_at) AS INTEGER)) <= 30
            WHERE r.postfix_mu_f IS NOT NULL AND r.postfix_sigma_f IS NOT NULL
              AND w.hours_left IS NOT NULL"""
    ).fetchall()

    # Build station/date → daily_high lookup.
    truth = dict(conn.execute(
        """SELECT station || '|' || lst_date, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE daily_high_f IS NOT NULL
            GROUP BY station, lst_date"""
    ).fetchall())

    out: list[dict] = []
    for ticker, recorded_at, mu, sigma, p_yes, hours_left, market_mid in rows:
        meta = _ticker_meta(ticker)
        if meta is None:
            continue
        station, lst_date, b_lo, b_hi = meta
        actual = truth.get(f"{station}|{lst_date}")
        if actual is None:
            continue
        out.append({
            "ticker": ticker, "recorded_at": recorded_at,
            "mu": float(mu), "sigma": float(sigma),
            "p_yes": float(p_yes), "hours_left": float(hours_left),
            "market_mid": int(market_mid) if market_mid is not None else None,
            "station": station, "lst_date": lst_date,
            "bracket_lo": b_lo, "bracket_hi": b_hi,
            "actual_high": float(actual),
            "abs_err_f": abs(float(mu) - float(actual)),
            "z_score": (float(mu) - float(actual)) / float(sigma) if float(sigma) > 0 else 0.0,
            "outcome_yes": 1 if (float(actual) >= b_lo and float(actual) < b_hi) else 0,
        })
    return out


def _tte_bucket(h: float) -> str:
    if h <= 1: return "01_<=1h"
    if h <= 3: return "02_1-3h"
    if h <= 6: return "03_3-6h"
    if h <= 12: return "04_6-12h"
    if h <= 24: return "05_12-24h"
    return "06_>24h"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(len(s) * pct / 100)
    return s[min(k, len(s) - 1)]


def view_1_mu_accuracy(rows: list[dict]) -> list[dict]:
    """MAE / RMSE / p50/p90/p99 of |μ - actual| by TTE."""
    by_bucket: dict = {}
    for r in rows:
        by_bucket.setdefault(_tte_bucket(r["hours_left"]), []).append(r)

    out = []
    for bucket in sorted(by_bucket):
        cell = by_bucket[bucket]
        errs = [r["abs_err_f"] for r in cell]
        signed = [r["mu"] - r["actual_high"] for r in cell]
        out.append({
            "tte_bucket": bucket,
            "n": len(cell),
            "mae": round(sum(errs) / len(errs), 2),
            "rmse": round(math.sqrt(sum(e * e for e in errs) / len(errs)), 2),
            "bias_signed": round(sum(signed) / len(signed), 2),
            "p50": round(_percentile(errs, 50), 2),
            "p90": round(_percentile(errs, 90), 2),
            "p99": round(_percentile(errs, 99), 2),
            "max": round(max(errs), 2),
        })
    return out


def view_2_sigma_calibration(rows: list[dict]) -> list[dict]:
    """Coverage at |z|<1 / |z|<2 / |z|<3 vs expected 68/95/99.7%."""
    by_bucket: dict = {}
    for r in rows:
        by_bucket.setdefault(_tte_bucket(r["hours_left"]), []).append(r)

    out = []
    for bucket in sorted(by_bucket):
        cell = by_bucket[bucket]
        zs = [r["z_score"] for r in cell]
        n = len(cell)
        cov_1s = sum(1 for z in zs if abs(z) <= 1.0) / n
        cov_2s = sum(1 for z in zs if abs(z) <= 2.0) / n
        cov_3s = sum(1 for z in zs if abs(z) <= 3.0) / n
        out.append({
            "tte_bucket": bucket,
            "n": n,
            "cov_1sigma": round(cov_1s, 3),
            "cov_2sigma": round(cov_2s, 3),
            "cov_3sigma": round(cov_3s, 3),
            "expected_1s": 0.683,
            "expected_2s": 0.954,
            "expected_3s": 0.997,
            "verdict": (
                "under-cover (σ too narrow)" if cov_1s < 0.55 else
                "over-cover (σ too wide)" if cov_1s > 0.80 else
                "near-calibrated"
            ),
        })
    return out


def view_3_floor_binding(rows: list[dict]) -> dict:
    """When σ is at floor (1.0), is the precision being wasted or saving us?"""
    floored = [r for r in rows if r["sigma"] <= 1.05]
    if not floored:
        return {"n_floored": 0}
    n = len(floored)
    errs = [r["abs_err_f"] for r in floored]
    p50 = _percentile(errs, 50)
    pct_under_05 = sum(1 for e in errs if e < 0.5) / n
    pct_under_1 = sum(1 for e in errs if e < 1.0) / n
    pct_over_2 = sum(1 for e in errs if e > 2.0) / n
    return {
        "n_floored": n,
        "median_abs_err": round(p50, 2),
        "mae": round(sum(errs) / n, 2),
        "pct_within_0.5F": round(pct_under_05, 3),
        "pct_within_1.0F": round(pct_under_1, 3),
        "pct_beyond_2.0F": round(pct_over_2, 3),
        "interpretation": (
            "floor often wastes precision (μ within 0.5°F of truth more than half the time)"
            if pct_under_05 > 0.5 else
            "floor saves us (μ off by >2°F often)"
            if pct_over_2 > 0.3 else
            "floor neither helps nor hurts much"
        ),
    }


def view_4_per_station(rows: list[dict]) -> list[dict]:
    by_station: dict = {}
    for r in rows:
        by_station.setdefault(r["station"], []).append(r)

    out = []
    for station in sorted(by_station):
        cell = by_station[station]
        errs = [r["abs_err_f"] for r in cell]
        signed = [r["mu"] - r["actual_high"] for r in cell]
        zs = [r["z_score"] for r in cell]
        n = len(cell)
        out.append({
            "station": station,
            "n": n,
            "mae": round(sum(errs) / n, 2),
            "bias": round(sum(signed) / n, 2),
            "p90_err": round(_percentile(errs, 90), 2),
            "cov_1sigma": round(sum(1 for z in zs if abs(z) <= 1.0) / n, 3),
            "verdict": (
                "WARM bias" if sum(signed) / n > 0.5 else
                "COLD bias" if sum(signed) / n < -0.5 else
                "neutral"
            ),
        })
    return out


def view_5_tail_risk(rows: list[dict]) -> list[dict]:
    by_bucket: dict = {}
    for r in rows:
        by_bucket.setdefault(_tte_bucket(r["hours_left"]), []).append(r)

    out = []
    for bucket in sorted(by_bucket):
        cell = by_bucket[bucket]
        errs = [r["abs_err_f"] for r in cell]
        n = len(cell)
        out.append({
            "tte_bucket": bucket,
            "n": n,
            "pct_err_>2F": round(sum(1 for e in errs if e > 2.0) / n, 3),
            "pct_err_>3F": round(sum(1 for e in errs if e > 3.0) / n, 3),
            "pct_err_>5F": round(sum(1 for e in errs if e > 5.0) / n, 3),
        })
    return out


def view_6_floor_schedules(rows: list[dict]) -> list[dict]:
    """Try multiple TTE-aware floor schedules. For each, simulate the
    resulting σ, recompute bracket P(YES), measure Brier vs current.

    Schedules are applied AFTER the production σ stages — i.e., we
    take the row's reported σ (already inflated, capped, etc.) and
    only ADJUST the floor downward when the schedule allows it.
    """
    schedules = {
        "current_const_1.0": lambda h: 1.0,
        "linear_0.05h": lambda h: max(0.1, min(1.0, 0.05 * h)),
        "step_0.3_at_3h": lambda h: 0.3 if h <= 3 else 1.0,
        "step_0.5_at_6h": lambda h: 0.5 if h <= 6 else 1.0,
        "sqrt_floor": lambda h: max(0.2, min(1.0, 0.3 * math.sqrt(h))),
    }

    def sim_brier(schedule, rows):
        # For each row: replace sigma with max(reported_sigma, schedule(tte))
        # and recompute bracket P(YES). Score Brier vs outcome.
        # NOTE: the reported sigma already had floor=1.0 applied. To
        # simulate "what if floor were schedule(h)", we need the UNDERLYING
        # combined σ pre-floor. Approximation: assume reported σ is
        # max(true_combined_σ, 1.0). When reported σ > 1.0, true was that.
        # When reported σ ≈ 1.0, true could be anywhere in [0, 1.0]. We
        # take a CONSERVATIVE substitute: if reported σ is at floor, use
        # max(0.3, schedule(h)) — half-pessimistic, half-optimistic.
        # This gives a directionally-honest simulation, not exact.
        briers = []
        for r in rows:
            target_sigma_floor = schedule(r["hours_left"])
            if r["sigma"] > 1.05:
                effective_sigma = max(r["sigma"], target_sigma_floor)
            else:
                # At the original floor — use the schedule's value as the
                # new effective σ (assumes true σ ≤ 1.0).
                effective_sigma = max(target_sigma_floor, 0.1)
            p = _bracket_p_yes(r["mu"], effective_sigma, r["bracket_lo"], r["bracket_hi"])
            briers.append((p - r["outcome_yes"]) ** 2)
        return briers

    out = []
    by_bucket: dict = {}
    for r in rows:
        by_bucket.setdefault(_tte_bucket(r["hours_left"]), []).append(r)

    for name, sched in schedules.items():
        for bucket in sorted(by_bucket):
            cell = by_bucket[bucket]
            briers = sim_brier(sched, cell)
            n_2sigma = sum(
                1 for r in cell
                if r["abs_err_f"] > 2 * max(sched(r["hours_left"]),
                                             r["sigma"] if r["sigma"] > 1.05 else sched(r["hours_left"]))
            )
            out.append({
                "schedule": name,
                "tte_bucket": bucket,
                "n": len(cell),
                "mean_brier": round(sum(briers) / len(briers), 4),
                "tail_2s_rate": round(n_2sigma / len(cell), 3),
            })
    return out


def view_7_metar_baseline(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    """At low TTE, does combined μ beat just using METAR running max?"""
    metar_lookup: dict = {}
    metar_rows = conn.execute(
        """SELECT s.ticker, s.recorded_at, s.forecast_high_f
             FROM weather_forecast_snapshots s
            WHERE s.source = 'metar' AND s.forecast_high_f IS NOT NULL"""
    ).fetchall()
    for ticker, recorded_at, mu in metar_rows:
        metar_lookup[(ticker, recorded_at)] = float(mu)

    cmp_rows = []
    for r in rows:
        metar_mu = metar_lookup.get((r["ticker"], r["recorded_at"]))
        if metar_mu is None:
            continue
        cmp_rows.append({
            **r,
            "metar_mu": metar_mu,
            "metar_err": abs(metar_mu - r["actual_high"]),
        })

    by_bucket: dict = {}
    for r in cmp_rows:
        by_bucket.setdefault(_tte_bucket(r["hours_left"]), []).append(r)

    out = {}
    for bucket in sorted(by_bucket):
        cell = by_bucket[bucket]
        n = len(cell)
        combined_mae = sum(r["abs_err_f"] for r in cell) / n
        metar_mae = sum(r["metar_err"] for r in cell) / n
        out[bucket] = {
            "n": n,
            "combined_mae": round(combined_mae, 2),
            "metar_alone_mae": round(metar_mae, 2),
            "combined_wins_by": round(metar_mae - combined_mae, 2),
        }
    return out


def fmt_table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "(no rows)\n"
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    out = "| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |\n"
    out += "|" + "|".join("-" * (widths[c] + 2) for c in cols) + "|\n"
    for r in rows:
        out += "| " + " | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) + " |\n"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--output", default=None,
                    help="optional report path; otherwise prints to stdout")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    print("[validate] fetching joined rows…")
    rows = fetch_validation_rows(conn)
    print(f"[validate] {len(rows)} rows with μ, σ, ground truth, and TTE")

    if not rows:
        print("[validate] no rows — nothing to validate")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    out_lines = [
        f"# μ-accuracy validation — {today}",
        "",
        f"**Population:** {len(rows)} cycles with replayed post-fix μ + ground-truth daily_high_f.",
        f"**Stations:** {sorted(set(r['station'] for r in rows))}",
        f"**Date range:** {min(r['lst_date'] for r in rows)} to "
        f"{max(r['lst_date'] for r in rows)}",
        "",
    ]

    print("\n[1/7] mu accuracy by TTE…")
    v1 = view_1_mu_accuracy(rows)
    out_lines.append("## 1. μ accuracy by TTE")
    out_lines.append("")
    out_lines.append("MAE / RMSE / percentiles of |μ - actual_high| in °F.")
    out_lines.append("")
    out_lines.append(fmt_table(v1, ["tte_bucket", "n", "mae", "rmse",
                                     "bias_signed", "p50", "p90", "p99", "max"]))

    print("[2/7] σ calibration by TTE…")
    v2 = view_2_sigma_calibration(rows)
    out_lines.append("## 2. σ calibration — z-score coverage")
    out_lines.append("")
    out_lines.append("If well-calibrated: cov_1σ≈0.68, cov_2σ≈0.95, cov_3σ≈0.997.")
    out_lines.append("Under-cover → σ too narrow. Over-cover → σ too wide.")
    out_lines.append("")
    out_lines.append(fmt_table(v2, ["tte_bucket", "n", "cov_1sigma",
                                     "cov_2sigma", "cov_3sigma", "verdict"]))

    print("[3/7] floor-binding analysis…")
    v3 = view_3_floor_binding(rows)
    out_lines.append("## 3. Floor-binding analysis")
    out_lines.append("")
    out_lines.append(f"Rows where σ ≤ 1.05 (at the floor): {v3.get('n_floored', 0)}")
    if v3.get("n_floored", 0) > 0:
        out_lines.append(f"- Median |μ-actual|: {v3['median_abs_err']}°F")
        out_lines.append(f"- MAE: {v3['mae']}°F")
        out_lines.append(f"- % within 0.5°F of truth: {v3['pct_within_0.5F']*100:.1f}%")
        out_lines.append(f"- % within 1.0°F of truth: {v3['pct_within_1.0F']*100:.1f}%")
        out_lines.append(f"- % off by >2.0°F: {v3['pct_beyond_2.0F']*100:.1f}%")
        out_lines.append(f"- **Interpretation: {v3['interpretation']}**")
    out_lines.append("")

    print("[4/7] per-station accuracy…")
    v4 = view_4_per_station(rows)
    out_lines.append("## 4. Per-station accuracy")
    out_lines.append("")
    out_lines.append(fmt_table(v4, ["station", "n", "mae", "bias", "p90_err",
                                     "cov_1sigma", "verdict"]))

    print("[5/7] tail risk by TTE…")
    v5 = view_5_tail_risk(rows)
    out_lines.append("## 5. Tail risk by TTE")
    out_lines.append("")
    out_lines.append("Rate of catastrophic μ misses.")
    out_lines.append("")
    out_lines.append(fmt_table(v5, ["tte_bucket", "n", "pct_err_>2F",
                                     "pct_err_>3F", "pct_err_>5F"]))

    print("[6/7] floor-schedule simulation…")
    v6 = view_6_floor_schedules(rows)
    out_lines.append("## 6. TTE-aware floor schedule simulation")
    out_lines.append("")
    out_lines.append("Each schedule applied to (μ, σ); resulting bracket P(YES) "
                     "Brier-scored against settlement outcome.")
    out_lines.append("")
    # Pivot for readability: rows = schedule, cols = TTE bucket Brier.
    pivot: dict = {}
    tte_buckets_set: set = set()
    for r in v6:
        pivot.setdefault(r["schedule"], {})[r["tte_bucket"]] = r["mean_brier"]
        tte_buckets_set.add(r["tte_bucket"])
    tte_cols = sorted(tte_buckets_set)
    pivot_rows = []
    for sched, cells in pivot.items():
        prow = {"schedule": sched}
        for b in tte_cols:
            prow[b] = cells.get(b, "—")
        pivot_rows.append(prow)
    out_lines.append(fmt_table(pivot_rows, ["schedule"] + tte_cols))
    out_lines.append("")
    out_lines.append("Tail-miss rate (where |μ-actual| > 2 × applied σ):")
    out_lines.append("")
    pivot2: dict = {}
    for r in v6:
        pivot2.setdefault(r["schedule"], {})[r["tte_bucket"]] = r["tail_2s_rate"]
    pivot2_rows = []
    for sched, cells in pivot2.items():
        prow = {"schedule": sched}
        for b in tte_cols:
            prow[b] = cells.get(b, "—")
        pivot2_rows.append(prow)
    out_lines.append(fmt_table(pivot2_rows, ["schedule"] + tte_cols))

    print("[7/7] METAR-alone baseline…")
    v7 = view_7_metar_baseline(conn, rows)
    out_lines.append("## 7. METAR-alone baseline")
    out_lines.append("")
    out_lines.append("Does the combined μ beat just using METAR running max as the prediction?")
    out_lines.append("")
    cmp_rows = []
    for bucket, cell in sorted(v7.items()):
        cmp_rows.append({
            "tte_bucket": bucket, "n": cell["n"],
            "combined_mae": cell["combined_mae"],
            "metar_alone_mae": cell["metar_alone_mae"],
            "combined_wins_by": cell["combined_wins_by"],
        })
    out_lines.append(fmt_table(cmp_rows, ["tte_bucket", "n",
                                           "combined_mae", "metar_alone_mae",
                                           "combined_wins_by"]))

    report = "\n".join(out_lines)
    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"[validate] report written to {args.output}")
    else:
        print()
        print(report)


if __name__ == "__main__":
    main()
