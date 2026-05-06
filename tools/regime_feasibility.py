"""Phase A.3 — sample-size + late-day feasibility per (city, hour, regime).

Two questions for the report:
  1. **Is the σ-reduction concentrated in the bracket-edge-relevant hours?**
     A weighted-aggregate that blends 10am (wide pooled σ) with 5pm
     (narrow pooled σ) hides the lift that matters most. Bracket calls
     get tight in the final 4-6 hours before settle. Stratify by hour
     and look at the late-day window separately.
  2. **Do we have enough samples per cell to fit per-(city, hour, regime),
     or do we need hierarchical fallback?**
     For each candidate taxonomy, count cells with n ≥ {3, 5, 10}.

Output: per-city, late-day σ-reduction + cell density.

Usage::

    python -m tools.regime_feasibility \\
        --csv-dir reports/regime_features \\
        --late-window 14-18

The choice of 14-18 LST = 2pm-6pm covers the period where the day's
peak typically occurs and where the residual peak σ still matters
(after 6pm temp is usually trending down and σ collapses).
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import pstdev
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.regime_stratify_residuals import (  # noqa: E402
    _load_csv,
    _wind_bucket,
    _sky_bucket,
    _dewpoint_bucket,
    _stratify,
    _weighted_within_sigma,
    _hours_filter,
    _by_hour,
)


_TAXONOMIES = (
    ("wind",        lambda r: _wind_bucket(r["drct"])),
    ("sky",         lambda r: _sky_bucket(r["skyc1"])),
    ("ddep",        lambda r: _dewpoint_bucket(r["ddep"])),
    ("wind+sky",    lambda r: f"{_wind_bucket(r['drct'])}|{_sky_bucket(r['skyc1'])}"),
    ("wind+ddep",   lambda r: f"{_wind_bucket(r['drct'])}|{_dewpoint_bucket(r['ddep'])}"),
)


def _aggregate_within(
    by_hour: dict[int, list[dict]], bucket_fn,
) -> tuple[float, float, int]:
    """Pooled σ vs within-bucket σ across all hours in the window.

    Returns (pooled_sigma, within_sigma, total_n_used). Uses the same
    weighting rule as the stratify tool: drop unknown bucket, drop cells
    with n < 3, then sum n_i × σ²_i across the surviving buckets.
    """
    pool_sse = 0.0
    with_sse = 0.0
    total_n = 0
    for cell in by_hour.values():
        if len(cell) < 5:
            continue
        residuals = [r["residual_peak_f"] for r in cell]
        pooled = pstdev(residuals)
        buckets = _stratify(cell, bucket_fn)
        within, n_used = _weighted_within_sigma(buckets, min_n=3)
        if math.isnan(within) or n_used == 0:
            continue
        pool_sse += len(cell) * (pooled ** 2)
        with_sse += n_used * (within ** 2)
        total_n += n_used
    if total_n == 0:
        return float("nan"), float("nan"), 0
    return (
        math.sqrt(pool_sse / total_n),
        math.sqrt(with_sse / total_n),
        total_n,
    )


def _cell_counts(
    rows: list[dict], bucket_fn, hours: set[int],
) -> dict[tuple[int, str], int]:
    """For each (lst_hour, bucket), count samples — for fitting feasibility."""
    out: dict[tuple[int, str], int] = defaultdict(int)
    for r in rows:
        if hours and r["lst_hour"] not in hours:
            continue
        b = bucket_fn(r)
        if b == "unknown":
            continue
        out[(r["lst_hour"], b)] += 1
    return out


def _fmt_pct(p: float) -> str:
    if math.isnan(p):
        return "  n/a"
    if p >= 0:
        return f"+{p:5.1f}%"
    return f"{p:6.1f}%"


def analyze_city(rows: list[dict], station: str, late_hours: set[int]) -> None:
    print(f"\n{'='*94}")
    print(f"  {station}  (n_rows={len(rows)})")
    print(f"{'='*94}")

    by_hour_late = _by_hour(rows, late_hours)
    n_late = sum(len(v) for v in by_hour_late.values())
    if n_late == 0:
        print(f"  no rows in late-day window {sorted(late_hours)}")
        return

    print(f"\n  Late-day σ-reduction (hours {sorted(late_hours)}, n={n_late}):")
    print(f"  {'taxonomy':12s}  {'pooled':>7s}  {'within':>7s}  {'Δ':>7s}  "
          f"{'n_used':>7s}")
    print(f"  {'-'*12}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}")

    rankings = []
    for label, fn in _TAXONOMIES:
        pool, within, n = _aggregate_within(by_hour_late, fn)
        if math.isnan(pool):
            print(f"  {label:12s}  {'n/a':>7s}  {'n/a':>7s}  {'n/a':>7s}  "
                  f"{n:7d}")
            continue
        delta = (within - pool) / pool * 100.0
        print(f"  {label:12s}  {pool:7.2f}  {within:7.2f}  "
              f"{_fmt_pct(delta):>7s}  {n:7d}")
        rankings.append((delta, label, fn))

    if not rankings:
        print("  (no taxonomy produced usable σ — too few samples)")
        return

    rankings.sort(key=lambda t: t[0])
    best_delta, best_label, best_fn = rankings[0]

    # Cell-density audit for the best taxonomy
    print(f"\n  Cell density for best taxonomy '{best_label}' (Δ={_fmt_pct(best_delta)}):")
    counts = _cell_counts(rows, best_fn, late_hours)
    if not counts:
        print("    no cells")
        return

    by_n_band: dict[str, int] = defaultdict(int)
    for n in counts.values():
        if n >= 10:
            by_n_band[">=10"] += 1
        elif n >= 5:
            by_n_band["5-9"] += 1
        elif n >= 3:
            by_n_band["3-4"] += 1
        else:
            by_n_band["<3"] += 1
    total_cells = sum(by_n_band.values())
    print(f"    cells with n ≥ 10:  {by_n_band['>=10']:3d} / {total_cells}")
    print(f"    cells with n 5-9:   {by_n_band['5-9']:3d} / {total_cells}")
    print(f"    cells with n 3-4:   {by_n_band['3-4']:3d} / {total_cells}")
    print(f"    cells with n < 3:   {by_n_band['<3']:3d} / {total_cells}  "
          f"(need fallback)")

    # Per-cell breakdown so the report has concrete numbers
    print(f"\n    Per-cell sample counts (hour × bucket):")
    hours_seen = sorted({h for (h, _) in counts})
    buckets_seen = sorted({b for (_, b) in counts})
    header = "      hr   " + "  ".join(f"{b:>10s}" for b in buckets_seen)
    print(header)
    for hr in hours_seen:
        row_parts = [f"      {hr:2d}   "]
        for b in buckets_seen:
            n = counts.get((hr, b), 0)
            mark = " " if n >= 5 else "*" if n >= 3 else "·"
            row_parts.append(f"  {n:>3d}{mark}      ")
        print("".join(row_parts))
    print("        (* = thin cell n=3-4; · = below threshold n<3)")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="reports/regime_features")
    ap.add_argument("--late-window", default="14-18",
                    help="LST hours where bracket-edge calls live")
    ap.add_argument("--stations", default="")
    args = ap.parse_args(argv)

    csv_dir = Path(args.csv_dir)
    late_hours = _hours_filter(args.late_window)

    if args.stations:
        wanted = {s.strip().upper() for s in args.stations.split(",") if s.strip()}
        files = [csv_dir / f"{s}.csv" for s in wanted]
    else:
        files = sorted(csv_dir.glob("*.csv"))

    for f in files:
        if not f.exists():
            print(f"[skip] {f}")
            continue
        rows = _load_csv(f)
        analyze_city(rows, f.stem, late_hours)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
