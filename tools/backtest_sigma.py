#!/usr/bin/env python3
"""
Backtest sigma schedules against existing weather_mm_shadow settled rows.

For each row we:
  1. Infer `is_above` from the stored fair_value_cents + current sigma formula.
  2. Recompute fair_value_cents with each candidate sigma schedule.
  3. Bucket by recomputed fv vs actual settlement and report calibration.

Goal: find the smallest sigma multiplier (or custom schedule) that brings
every bucket with n>=50 to |avg_est - yes_rate| <= 0.05 across T and B markets.

Usage:
    python3 tools/backtest_sigma.py [db_path]
"""
from __future__ import annotations

import math
import re
import sys
from collections import defaultdict
from typing import Optional

sys.path.insert(0, ".")
from bot.db import init_db


# ── Sigma schedules ───────────────────────────────────────────────────────

def _sigma_v1_original(h: float) -> float:
    """Original (pre-2026-04-24) schedule — σ=0.3-2°F. Kept as reference."""
    if h <= 0:      return 0.1
    elif h < 1:     return 0.3
    elif h < 2:     return 0.5 + (h - 1.0) * 0.3
    elif h < 6:     return 0.8 + (h - 2.0) * 0.175
    elif h < 12:    return 1.5 + (h - 6.0) * 0.083
    else:           return 2.0


def _sigma_current(h: float) -> float:
    """Current deployed schedule (v2, 2026-04-24). σ=1-8°F."""
    if h <= 0:      return 0.5
    elif h < 1:     return 1.0
    elif h < 2:     return 2.0
    elif h < 4:     return 3.5
    elif h < 6:     return 5.0
    elif h < 12:    return 6.5
    else:           return 8.0


def _make_scaled(factor: float):
    def f(h: float) -> float:
        return min(15.0, _sigma_current(h) * factor)
    return f


def _sigma_v2(h: float) -> float:
    """More aggressive — σ=1.5-9°F."""
    if h <= 0:      return 0.5
    elif h < 1:     return 1.5
    elif h < 2:     return 3.0
    elif h < 4:     return 4.5
    elif h < 6:     return 6.0
    elif h < 12:    return 7.5
    else:           return 9.0


def _sigma_v3(h: float) -> float:
    """Conservative — σ=0.5-6°F."""
    if h <= 0:      return 0.3
    elif h < 1:     return 0.5
    elif h < 2:     return 1.0
    elif h < 4:     return 2.0
    elif h < 6:     return 3.0
    elif h < 12:    return 4.5
    else:           return 6.0


SCHEDULES = [
    ("v1 original",   _sigma_v1_original),
    ("current (v2)",  _sigma_current),
    ("0.5x scale",    _make_scaled(0.5)),
    ("1.5x scale",    _make_scaled(1.5)),
    ("2x scale",      _make_scaled(2)),
    ("3x scale",      _make_scaled(3)),
    ("6x scale",      _make_scaled(6)),
    ("8x scale",      _make_scaled(8)),
    ("10x scale",     _make_scaled(10)),
    ("v2 empirical",  _sigma_v2),
    ("v3 moderate",   _sigma_v3),
]


# ── Math helpers ──────────────────────────────────────────────────────────

def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _blended_mu(rh: float, fh: float, hours_left: float) -> float:
    total = 24.0
    frac = max(0.0, min(1.0, 1.0 - hours_left / total))
    if hours_left > 0:
        fw = max(0.1, 1.0 - frac)
        ow = 1.0 - fw
        return fw * max(fh, rh) + ow * rh
    return rh


# ── Ticker parsing ────────────────────────────────────────────────────────

def _parse_ticker(ticker: str):
    """Returns (is_bracket, threshold, bracket_floor, bracket_cap) or None."""
    tu = ticker.upper()
    is_bracket = "-B" in tu
    if is_bracket:
        m = re.search(r'-[Bb](-?\d+\.?\d*)', ticker)
        if not m:
            return None
        floor_val = float(m.group(1))
        # Try to get cap from the next part of the ticker or default to +2
        cap_val = floor_val + 2.0
        return (True, None, floor_val, cap_val)
    else:
        m = re.search(r'-[Tt](-?\d+\.?\d*)', ticker)
        if not m:
            return None
        threshold = float(m.group(1))
        return (False, threshold, None, None)


# ── Core recompute logic ──────────────────────────────────────────────────

def _compute_prob_above(rh: float, fh: float, hours_left: float,
                        threshold: float, sigma_fn) -> float:
    """Compute P(daily high > threshold) using given sigma schedule."""
    if rh >= threshold:
        margin = rh - threshold
        if margin >= 3.0:   return 0.98
        elif margin >= 1.0: return 0.96
        else:               return 0.95
    mu = _blended_mu(rh, fh, hours_left)
    sigma = sigma_fn(hours_left)
    return max(0.02, min(0.98, 1.0 - _logistic_cdf(threshold, mu, sigma)))


def _infer_is_above(rh: float, fh: float, hours_left: float,
                    threshold: float, stored_fv: int) -> bool:
    """
    Infer is_above by checking which direction (above vs below threshold)
    produces a fair value closer to the stored value under the current sigma.
    For rh >= threshold: stored_fv >= 90 → is_above=True, else False.
    """
    if rh >= threshold:
        return stored_fv >= 50

    prob_above = _compute_prob_above(rh, fh, hours_left, threshold, _sigma_current)
    fv_if_above = max(2, min(98, int(round(prob_above * 100))))
    fv_if_below = max(2, min(98, int(round((1.0 - prob_above) * 100))))
    return abs(stored_fv - fv_if_above) <= abs(stored_fv - fv_if_below)


def _recompute_fv(rh: float, fh: float, hours_left: float,
                  ticker: str, stored_fv: int, sigma_fn) -> Optional[int]:
    parsed = _parse_ticker(ticker)
    if parsed is None:
        return None
    is_bracket, threshold, bracket_floor, bracket_cap = parsed

    if is_bracket:
        mu = _blended_mu(rh, fh, hours_left)
        sigma = sigma_fn(hours_left)
        if rh >= bracket_cap:
            prob = 0.02
        elif rh >= bracket_floor:
            prob = max(0.02, min(0.98, _logistic_cdf(bracket_cap, mu, sigma)))
        else:
            cu = _logistic_cdf(bracket_cap, mu, sigma)
            cl = _logistic_cdf(bracket_floor, mu, sigma)
            prob = max(0.02, min(0.98, cu - cl))
        return max(2, min(98, int(round(prob * 100))))
    else:
        is_above = _infer_is_above(rh, fh, hours_left, threshold, stored_fv)
        prob_above = _compute_prob_above(rh, fh, hours_left, threshold, sigma_fn)
        if is_above:
            prob = prob_above
        else:
            prob = max(0.02, min(0.98, 1.0 - prob_above))
        return max(2, min(98, int(round(prob * 100))))


# ── Calibration reporting ─────────────────────────────────────────────────

def _suffix(ticker: str) -> str:
    parts = ticker.split("-")
    if not parts:
        return "?"
    s = parts[-1]
    if s.upper().startswith("T"): return "T"
    if s.upper().startswith("B"): return "B"
    return "?"


def _bucket(fv: int) -> str:
    p = fv / 100.0
    lo = int(p * 10) / 10
    if lo >= 1.0: lo = 0.9
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def calibration_report(rows, sigma_name: str, sigma_fn) -> dict:
    """
    Returns dict of {(suffix, bucket): (n, avg_est, yes_rate, bias)}.
    Also prints a summary table.
    """
    by_sb: dict = defaultdict(list)
    skipped = 0
    for (ticker, rh, fh, hours_left, stored_fv, settled) in rows:
        new_fv = _recompute_fv(rh, fh, hours_left, ticker, stored_fv, sigma_fn)
        if new_fv is None:
            skipped += 1
            continue
        sfx = _suffix(ticker)
        b = _bucket(new_fv)
        by_sb[(sfx, b)].append((new_fv / 100.0, settled))

    fails = 0
    passes = 0
    results = {}
    rows_out = []
    for (sfx, b), samples in sorted(by_sb.items()):
        n = len(samples)
        if n < 50:
            continue
        avg_est = sum(x[0] for x in samples) / n
        yes_rate = sum(x[1] for x in samples) / n
        bias = avg_est - yes_rate
        results[(sfx, b)] = (n, avg_est, yes_rate, bias)
        rows_out.append((sfx, b, n, avg_est, yes_rate, bias))
        if abs(bias) <= 0.05:
            passes += 1
        else:
            fails += 1

    print(f"\n{'='*72}")
    print(f"  Sigma: {sigma_name}  |  buckets n>=50: {passes+fails}  "
          f"PASS={passes}  FAIL={fails}  (skipped={skipped})")
    print(f"{'='*72}")
    print(f"  {'sfx':<4} {'bucket':<12} {'n':>6} {'avg_est':>8} "
          f"{'yes_rate':>9} {'bias':>8}  {'ok?':>4}")
    for (sfx, b, n, avg_est, yes_rate, bias) in rows_out:
        ok = "PASS" if abs(bias) <= 0.05 else "FAIL"
        print(f"  {sfx:<4} {b:<12} {n:>6} {avg_est:>8.3f} "
              f"{yes_rate:>9.3f} {bias:>+8.3f}  {ok:>4}")

    return results


def main(db: str = "kalshi_trades.db") -> None:
    conn = init_db(db)

    rows = conn.execute("""
        SELECT
            ticker,
            running_high_f,
            forecast_high_f,
            hours_left,
            fair_value_cents,
            ticker_settled_yes
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND ticker_settled_yes IS NOT NULL
          AND fair_value_cents IS NOT NULL
          AND running_high_f IS NOT NULL
          AND forecast_high_f IS NOT NULL
          AND hours_left IS NOT NULL
    """).fetchall()

    print(f"\nTotal rows for backtest: {len(rows):,}")

    # Quick distribution of hours_left to understand the dataset
    buckets_hrs = defaultdict(int)
    for r in rows:
        h = r[3]
        if h < 2: buckets_hrs["<2h"] += 1
        elif h < 6: buckets_hrs["2-6h"] += 1
        elif h < 12: buckets_hrs["6-12h"] += 1
        else: buckets_hrs[">12h"] += 1
    print(f"hours_left distribution: " +
          ", ".join(f"{k}={v}" for k, v in sorted(buckets_hrs.items())))

    all_results = {}
    for name, fn in SCHEDULES:
        all_results[name] = calibration_report(rows, name, fn)

    # Summary: for each schedule, count passing buckets
    print(f"\n{'='*72}")
    print("  SUMMARY — passes (|bias|<=0.05) across all buckets with n>=50")
    print(f"{'='*72}")
    for name, results in all_results.items():
        total = len(results)
        passing = sum(1 for (n, ae, yr, bias) in results.values() if abs(bias) <= 0.05)
        worst_bias = max((abs(bias) for (n, ae, yr, bias) in results.values()), default=0)
        print(f"  {name:<20}  {passing}/{total} buckets pass  "
              f"worst_bias={worst_bias:.3f}")

    # Print sigma values at key hours for the schedules
    print(f"\n{'='*72}")
    print("  SIGMA VALUES at key hours_left")
    print(f"{'='*72}")
    hours_checkpoints = [0.5, 1, 2, 3, 4, 6, 8, 12, 18, 24]
    header = f"  {'schedule':<20}" + "".join(f"  {h:>4}h" for h in hours_checkpoints)
    print(header)
    for name, fn in SCHEDULES:
        row = f"  {name:<20}" + "".join(f"  {fn(h):>5.2f}" for h in hours_checkpoints)
        print(row)


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "kalshi_trades.db"
    main(db)
