"""Phase A.2 — does residual peak σ stratify by weather regime?

Reads the per-station CSVs from ``tools.regime_features_pull`` and asks
the central Phase A question: for each (station, lst_hour) cell, can we
explain a meaningful fraction of the pooled σ by conditioning on one of
- wind direction (proxy for sea breeze / onshore vs offshore)
- sky cover (radiative regime: clear day vs overcast)
- dewpoint depression (humidity / boundary-layer depth)

The current production model fits one σ per (station, lst_hour). If the
pooled σ ≈ within-regime σ, the regime hypothesis is wrong: the current
model is already as good as it can get without new features. If the
weighted within-regime σ is meaningfully smaller, regime conditioning
reduces uncertainty on the high-confidence days (and surfaces it on the
volatile ones) — directly attacking the close-edge bracket gap.

Output: a summary table per station showing σ-reduction for each
candidate regime feature, plus per-bucket detail for the most-promising
hours.

Usage::

    python -m tools.regime_stratify_residuals \\
        --csv-dir reports/regime_features \\
        --hours 10-18

Decision criterion for the report:
- σ-reduction > 20% on the close-edge-relevant hours (peak-heating window)
  with adequate sample size → ship Phase A as designed.
- σ-reduction < 10% across the board → kill Phase A; pivot.
- 10-20% → marginal; consider hierarchical pooling and revisit after
  more data accumulates.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Optional

# 4-direction wind buckets covering 360° with N centered on 0 (a wrap).
_WIND_BUCKET_EDGES_DEG: tuple[tuple[str, float, float], ...] = (
    ("N",  315.0, 45.0),    # wraps through 0
    ("E",  45.0, 135.0),
    ("S",  135.0, 225.0),
    ("W",  225.0, 315.0),
)

_SKY_BUCKET: dict[str, str] = {
    "":     "unknown",
    "CLR":  "clear",
    "FEW":  "clear",
    "SCT":  "partly",
    "BKN":  "partly",
    "OVC":  "overcast",
    "VV":   "overcast",   # vertical visibility = obscured = effectively OVC
}


def _wind_bucket(deg: Optional[float]) -> str:
    if deg is None:
        return "unknown"
    deg = deg % 360.0
    for name, lo, hi in _WIND_BUCKET_EDGES_DEG:
        if lo <= hi:
            if lo <= deg < hi:
                return name
        else:
            # Wraps through 0 (N case)
            if deg >= lo or deg < hi:
                return name
    return "unknown"


def _dewpoint_bucket(ddep: Optional[float]) -> str:
    if ddep is None:
        return "unknown"
    if ddep <= 5.0:
        return "humid"
    if ddep <= 15.0:
        return "moderate"
    return "dry"


def _sky_bucket(skyc1: str) -> str:
    return _SKY_BUCKET.get((skyc1 or "").strip().upper(), "unknown")


def _pop_sigma(values: list[float]) -> float:
    if len(values) < 2:
        return float("nan")
    return pstdev(values)


def _load_csv(path: Path) -> list[dict]:
    """Parse a single station CSV into typed rows.

    Rows without CF6 (= today's day; CF6 publishes the following morning)
    have empty ``daily_high_cf6_f`` and ``residual_peak_f`` — these are
    skipped for fitting (need ground truth), but kept available for
    predict-time regime lookup via the index built upstream.
    """
    rows: list[dict] = []
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                row = {
                    "station": r["station"],
                    "lst_date": r["lst_date"],
                    "lst_hour": int(r["lst_hour"]),
                    "tmpf": float(r["tmpf"]),
                    "dwpf": float(r["dwpf"]) if r["dwpf"] else None,
                    "drct": float(r["drct"]) if r["drct"] else None,
                    "sknt": float(r["sknt"]) if r["sknt"] else None,
                    "skyc1": r["skyc1"],
                    "ddep": (
                        float(r["dewpoint_depression_f"])
                        if r["dewpoint_depression_f"]
                        else None
                    ),
                    "daily_high_cf6_f": (
                        float(r["daily_high_cf6_f"])
                        if r["daily_high_cf6_f"] else None
                    ),
                    "running_max_f": float(r["running_max_tmpf_at_hour_f"]),
                    "residual_peak_f": (
                        float(r["residual_peak_f"])
                        if r["residual_peak_f"] else None
                    ),
                }
            except (KeyError, ValueError):
                continue
            rows.append(row)
    return rows


def _hours_filter(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _by_hour(
    rows: list[dict], hours: set[int],
) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if hours and r["lst_hour"] not in hours:
            continue
        out[r["lst_hour"]].append(r)
    return out


def _stratify(
    rows: list[dict], bucket_fn,
) -> dict[str, list[float]]:
    """Group residual_peak values by bucket_fn(row)."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        buckets[bucket_fn(r)].append(r["residual_peak_f"])
    return buckets


def _weighted_within_sigma(
    buckets: dict[str, list[float]],
    *, min_n: int = 3,
) -> tuple[float, int]:
    """Within-bucket σ pooled across buckets.

    Each bucket contributes (n, σ²); pooled variance = Σ n_i σ²_i / Σ n_i.
    Buckets with fewer than ``min_n`` samples are dropped (too noisy to
    trust the σ estimate). Returns (sqrt-pooled-variance, total_n_used).
    Returns (nan, 0) when no bucket meets ``min_n``.
    """
    num = 0.0
    den = 0
    for name, vals in buckets.items():
        if name == "unknown" or len(vals) < min_n:
            continue
        s = pstdev(vals)
        num += len(vals) * (s ** 2)
        den += len(vals)
    if den == 0:
        return float("nan"), 0
    return math.sqrt(num / den), den


def _format_pct(reduce_pct: float) -> str:
    if math.isnan(reduce_pct):
        return "  n/a"
    if reduce_pct > 0:
        return f"+{reduce_pct:5.1f}%"
    return f"{reduce_pct:6.1f}%"


def analyze_station(
    rows: list[dict], station: str, hours: set[int],
    *, top_n: int = 5,
) -> None:
    print(f"\n{'='*88}")
    print(f"  {station} — n_rows={len(rows)}, hours_filter={sorted(hours) or 'all'}")
    print(f"{'='*88}")

    by_hour = _by_hour(rows, hours)
    if not by_hour:
        print("  (no rows after hour filter)")
        return

    # Per-hour summary table
    print(
        f"  {'hr':>3} {'n':>4} {'pooled σ':>9} {'wind σ':>8} {'sky σ':>8} "
        f"{'ddep σ':>8} {'wind+sky σ':>11} {'best Δ':>8}"
    )
    print(f"  {'---':>3} {'---':>4} {'---':>9} {'---':>8} {'---':>8} "
          f"{'---':>8} {'---':>11} {'---':>8}")

    # Track per-feature aggregate: weighted average of pooled vs within
    feat_total = defaultdict(lambda: {"pool_sse": 0.0, "with_sse": 0.0, "n": 0})

    for hr in sorted(by_hour):
        cell = by_hour[hr]
        if len(cell) < 5:
            continue
        residuals = [r["residual_peak_f"] for r in cell]
        pooled = pstdev(residuals)

        wind_b = _stratify(cell, lambda r: _wind_bucket(r["drct"]))
        sky_b = _stratify(cell, lambda r: _sky_bucket(r["skyc1"]))
        ddep_b = _stratify(cell, lambda r: _dewpoint_bucket(r["ddep"]))
        ws_b = _stratify(
            cell,
            lambda r: f"{_wind_bucket(r['drct'])}+{_sky_bucket(r['skyc1'])}",
        )

        wind_sig, _ = _weighted_within_sigma(wind_b)
        sky_sig, _ = _weighted_within_sigma(sky_b)
        ddep_sig, _ = _weighted_within_sigma(ddep_b)
        ws_sig, _ = _weighted_within_sigma(ws_b)

        # Best reduction across the 4 features
        candidates = [wind_sig, sky_sig, ddep_sig, ws_sig]
        valid = [c for c in candidates if not math.isnan(c)]
        if valid:
            best = min(valid)
            delta = (best - pooled) / pooled * 100.0
        else:
            delta = float("nan")

        def _fmt(s):
            return f"{s:8.2f}" if not math.isnan(s) else "    n/a"

        print(
            f"  {hr:3d} {len(cell):4d} {pooled:9.2f} "
            f"{_fmt(wind_sig)} {_fmt(sky_sig)} {_fmt(ddep_sig)} "
            f"{_fmt(ws_sig):>11s} {_format_pct(delta)}"
        )

        # Aggregate for the per-feature roll-up
        for label, within in (
            ("wind", wind_sig), ("sky", sky_sig),
            ("ddep", ddep_sig), ("wind+sky", ws_sig),
        ):
            if not math.isnan(within):
                feat_total[label]["pool_sse"] += len(cell) * (pooled ** 2)
                feat_total[label]["with_sse"] += len(cell) * (within ** 2)
                feat_total[label]["n"] += len(cell)

    # Per-feature aggregate σ-reduction across the analyzed hours
    print()
    print(
        f"  Aggregate σ-reduction (across all hours, weighted by n):"
    )
    for label in ("wind", "sky", "ddep", "wind+sky"):
        t = feat_total[label]
        if t["n"] == 0:
            print(f"    {label:9s}  n=0   no data")
            continue
        pool_sig = math.sqrt(t["pool_sse"] / t["n"])
        with_sig = math.sqrt(t["with_sse"] / t["n"])
        delta = (with_sig - pool_sig) / pool_sig * 100.0
        print(
            f"    {label:9s}  n={t['n']:4d}   pooled={pool_sig:.2f}°F   "
            f"within={with_sig:.2f}°F   Δ={_format_pct(delta)}"
        )

    # Per-hour worst-bucket detail for the top-N hours by sample count
    # so the report has concrete examples (not just summary numbers).
    print()
    print(f"  Per-bucket detail at the {top_n} highest-n hours:")
    hour_by_n = sorted(by_hour, key=lambda h: -len(by_hour[h]))[:top_n]
    for hr in sorted(hour_by_n):
        cell = by_hour[hr]
        if len(cell) < 5:
            continue
        residuals = [r["residual_peak_f"] for r in cell]
        pooled = pstdev(residuals)
        print(f"    hour={hr:02d} pooled σ={pooled:.2f}°F (n={len(cell)})")

        for feat_label, bucket_fn in (
            ("wind", lambda r: _wind_bucket(r["drct"])),
            ("sky",  lambda r: _sky_bucket(r["skyc1"])),
            ("ddep", lambda r: _dewpoint_bucket(r["ddep"])),
        ):
            buckets = _stratify(cell, bucket_fn)
            line = f"      {feat_label:5s}:"
            for b_name in sorted(buckets):
                vals = buckets[b_name]
                if len(vals) < 3 or b_name == "unknown":
                    continue
                m = mean(vals)
                s = pstdev(vals)
                line += f"  {b_name}(n={len(vals)} μ={m:+.1f} σ={s:.2f})"
            print(line)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv-dir", default="reports/regime_features",
        help="Directory of per-station CSVs from regime_features_pull.",
    )
    ap.add_argument(
        "--hours", default="10-18",
        help=(
            "LST hour filter — analyze residuals during peak-heating "
            "window. '10-18' = 10am to 6pm. Pass empty string for all 24."
        ),
    )
    ap.add_argument(
        "--stations", default="",
        help="Comma-separated ICAO list (default: every CSV in --csv-dir)",
    )
    args = ap.parse_args(argv)

    csv_dir = Path(args.csv_dir)
    if not csv_dir.exists():
        print(f"[error] {csv_dir} does not exist", file=sys.stderr)
        return 1

    hours = _hours_filter(args.hours) if args.hours.strip() else set()

    if args.stations:
        wanted = {s.strip().upper() for s in args.stations.split(",") if s.strip()}
        files = [csv_dir / f"{s}.csv" for s in wanted]
    else:
        files = sorted(csv_dir.glob("*.csv"))

    for f in files:
        if not f.exists():
            print(f"[skip] {f} missing")
            continue
        rows = _load_csv(f)
        analyze_station(rows, f.stem, hours)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
