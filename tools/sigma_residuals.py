#!/usr/bin/env python3
"""Per-family residual analysis: actual high vs combined-Gaussian μ.

The 2026-05-12 audit Phase C.3 found that KXHIGHDEN's reported σ is
1.4–4.2 °F while actual day-to-day high RMSE is 11–12 °F (3+σ
misses on 40% of observed days). This script generalizes that finding
across every weather family by walking the historical record:

  predicted μ, σ   — from ``weather_forecast_snapshots`` (source=combined_v2)
  actual high      — from the ``alpha_backtest`` bracket that resolved YES

For each (family, settle_date), we take the LAST combined_v2 snapshot
that landed before settlement and the bracket whose strike range
contained the actual high. The residual is

    residual = actual_high_center - predicted_mu

Per family we report:
  n              — sample count
  mean_resid     — empirical bias (positive = model underpredicts)
  rms_resid      — empirical residual RMS (the σ that would have made
                   the model's bracket probabilities calibrated)
  reported_sigma — median predicted σ across the same sample
  inflation      — rms_resid / reported_sigma. >1 → σ floor needed.

Calibration use: a ``cross_bracket`` scorer that needs a per-family σ
floor for entry decisions should clamp the combined Gaussian σ to
``max(combined_sigma, rms_resid)``. This is the empirically-justified
floor; the constant ``_COMBINED_SIGMA_FLOOR_F`` (1.0 today) is the
hard physical floor.

Usage
-----
::

    python3 tools/sigma_residuals.py --db kalshi_trades.db --since 2026-04-24
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_BRACKET_RE = re.compile(r"-B(\d+(?:\.\d+)?)$")


def _bracket_center(ticker: str) -> float | None:
    """Extract the bracket center temperature from a bracket ticker.

    ``KXHIGHAUS-26MAY11-B81.5`` → 81.5
    ``KXHIGHAUS-26MAY11-B36``   → 36.0  (some series use integer labels)
    Returns None for threshold (T-prefix) or unparseable tickers.
    """
    m = _BRACKET_RE.search(ticker)
    if not m:
        return None
    return float(m.group(1))


def _family_date_key(ticker: str) -> tuple[str, str] | None:
    """``KXHIGHAUS-26MAY11-B81.5`` → ('KXHIGHAUS', '26MAY11')."""
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    return parts[0], parts[1]


def analyze(db_path: str, since: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Latest combined_v2 snapshot per (family, date), before settlement.
    # We approximate "before settlement" with "all snapshots in
    # weather_forecast_snapshots" — those are written through the
    # day, and we take the LATEST per ticker which is closest to peak.
    snapshots = conn.execute(
        """SELECT ticker, recorded_at, forecast_high_f, sigma_f
             FROM weather_forecast_snapshots
            WHERE source='combined_v2'
              AND ticker LIKE 'KXHIGH%-B%'
              AND recorded_at >= ?
              AND forecast_high_f IS NOT NULL
              AND sigma_f IS NOT NULL""",
        (since,),
    ).fetchall()

    # Aggregate: per (family, date), keep the LATEST snapshot row
    latest_per_event: dict[tuple[str, str], dict] = {}
    for r in snapshots:
        key = _family_date_key(r["ticker"])
        if key is None:
            continue
        prev = latest_per_event.get(key)
        if prev is None or r["recorded_at"] > prev["recorded_at"]:
            latest_per_event[key] = {
                "ticker": r["ticker"],
                "recorded_at": r["recorded_at"],
                "mu": float(r["forecast_high_f"]),
                "sigma": float(r["sigma_f"]),
            }

    # Resolve actual high per (family, date) from alpha_backtest
    # whose bracket settled YES.
    winners = conn.execute(
        """SELECT DISTINCT ticker, settlement_result
             FROM alpha_backtest
            WHERE settlement_result = 'yes'
              AND ticker LIKE 'KXHIGH%-B%'
              AND ts_settle >= ?""",
        (since,),
    ).fetchall()
    actual_per_event: dict[tuple[str, str], float] = {}
    for r in winners:
        key = _family_date_key(r["ticker"])
        if key is None:
            continue
        center = _bracket_center(r["ticker"])
        if center is None:
            continue
        # If multiple winners for the same key (shouldn't happen, but
        # could from data corruption), keep the one with the lowest
        # bracket label as a conservative baseline. The DISTINCT in
        # the SQL means we only see one row per ticker anyway.
        prev = actual_per_event.get(key)
        if prev is None or center < prev:
            actual_per_event[key] = center

    # Compute per-family stats.
    per_family: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for key, snap in latest_per_event.items():
        family, dt = key
        actual = actual_per_event.get(key)
        if actual is None:
            continue
        residual = actual - snap["mu"]
        per_family[family].append((residual, snap["sigma"], snap["mu"]))

    results = []
    for family, rows in sorted(per_family.items()):
        n = len(rows)
        if n == 0:
            continue
        resids = [r for r, _, _ in rows]
        sigmas = [s for _, s, _ in rows]
        mean_resid = sum(resids) / n
        rms_resid = math.sqrt(sum(r * r for r in resids) / n)
        # Median σ is more robust than mean when the bot's σ varies
        # across days (it does — early-morning snapshots have wider σ
        # than just-before-peak snapshots).
        med_sigma = sorted(sigmas)[n // 2]
        inflation = rms_resid / med_sigma if med_sigma > 0 else float("inf")
        results.append({
            "family": family,
            "n": n,
            "mean_resid_f": round(mean_resid, 2),
            "rms_resid_f": round(rms_resid, 2),
            "median_sigma_f": round(med_sigma, 2),
            "inflation_ratio": round(inflation, 2),
            "recommended_sigma_floor_f": round(rms_resid, 2),
        })

    conn.close()
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument(
        "--since", default="2026-04-24",
        help="ISO date — only use snapshots/settlements at or after",
    )
    args = p.parse_args()
    rows = analyze(args.db, args.since)

    if not rows:
        print("No data — check --since and that weather_forecast_snapshots "
              "and alpha_backtest are populated.")
        return 1

    cols = [
        ("family", 12), ("n", 5), ("mean_resid_f", 12), ("rms_resid_f", 11),
        ("median_sigma_f", 14), ("inflation_ratio", 15),
        ("recommended_sigma_floor_f", 24),
    ]
    header = "  ".join(f"{name:>{w}}" for name, w in cols)
    print(header)
    print("  ".join("-" * w for _, w in cols))
    for r in rows:
        print("  ".join(f"{str(r[name]):>{w}}" for name, w in cols))

    # Hint: which families are flagged
    print()
    flagged = [r for r in rows if r["inflation_ratio"] > 1.5]
    if flagged:
        print(
            "Families with σ-inflation > 1.5× — empirical RMSE much "
            "wider than reported σ. Consider σ floor or block:"
        )
        for r in flagged:
            print(
                f"  {r['family']}: rms_resid={r['rms_resid_f']}°F vs "
                f"σ={r['median_sigma_f']}°F ({r['inflation_ratio']}×)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
