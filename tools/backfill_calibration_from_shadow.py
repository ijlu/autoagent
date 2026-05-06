#!/usr/bin/env python3
"""One-off back-fill: weather_mm_shadow → calibration → Platt curve.

The steady-state daemon scheduler task re-runs this bridge every ~600s
(see ``bot/daemon/main.py``). This CLI exists for the *initial* run
against the production DB, where we have ~27K already-settled shadow
rows queued up that should populate the calibration table immediately
rather than drip in over the next 10-minute cadence.

Flow:

  1. ``bridge_shadow_to_calibration`` in a loop (chunks of ``--batch-size``)
     until no more pending rows.
  2. Print stats per batch so progress is visible on a long run.
  3. Optionally run ``fit_and_persist`` afterward to produce a Platt
     curve immediately and cache it under ``calibration_curve_v2``.
  4. Print curve summary: method, A, B, n_samples, per-family segments,
     brier_before vs brier_after.

Usage::

    # Dry-run preview (no writes, counts only)
    python3 tools/backfill_calibration_from_shadow.py --dry-run

    # Actual back-fill + immediate Platt fit (default)
    python3 tools/backfill_calibration_from_shadow.py

    # Back-fill only, skip fit (useful when something else will refit)
    python3 tools/backfill_calibration_from_shadow.py --no-fit

    # Custom DB path (defaults to kalshi_trades.db in cwd)
    python3 tools/backfill_calibration_from_shadow.py --db /path/to/kalshi_trades.db

    # Smaller batches for a big initial run (default 5000)
    python3 tools/backfill_calibration_from_shadow.py --batch-size 1000
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, ".")

from bot.db import init_db
from bot.learning.calibration import fit_and_persist
from bot.learning.shadow_calibration_bridge import (
    WATERMARK_KEY,
    bridge_shadow_to_calibration,
)


def _count_pending(conn, watermark: int) -> tuple[int, int]:
    """(pending_settled, total_shadow) — gives the user a sense of scale."""
    total = conn.execute(
        "SELECT COUNT(*) FROM weather_mm_shadow"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM weather_mm_shadow "
        "WHERE id > ? "
        "  AND ts_settle_unix IS NOT NULL "
        "  AND ticker_settled_yes IS NOT NULL "
        "  AND fair_value_cents IS NOT NULL",
        (watermark,),
    ).fetchone()[0]
    return pending, total


def _load_watermark(conn) -> int:
    row = conn.execute(
        "SELECT value FROM kv_cache WHERE key=?", (WATERMARK_KEY,)
    ).fetchone()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _print_curve(curve: dict) -> None:
    print()
    print("=" * 70)
    print("Platt curve summary")
    print("=" * 70)
    method = curve.get("method", "?")
    n = curve.get("n_samples", 0)
    A = curve.get("A", 1.0)
    B = curve.get("B", 0.0)
    bb = curve.get("brier_before")
    ba = curve.get("brier_after")
    print(f"  method       : {method}")
    print(f"  n_samples    : {n}")
    print(f"  A (slope)    : {A:.6f}")
    print(f"  B (bias)     : {B:.6f}")
    if bb is not None and ba is not None:
        print(f"  brier_before : {bb:.6f}")
        print(f"  brier_after  : {ba:.6f}  (Δ={bb - ba:+.6f})")
    if method == "identity":
        reason = curve.get("reason", "")
        if reason:
            print(f"  reason       : {reason}")

    families = curve.get("families", {})
    if families:
        print()
        print(f"  Per-family segments ({len(families)}):")
        print(f"    {'family':<14} {'n':>5} {'A':>10} {'B':>10}")
        for fam in sorted(families):
            seg = families[fam]
            print(
                f"    {fam:<14} {seg.get('n_samples', 0):>5} "
                f"{seg.get('A', 1.0):>10.4f} {seg.get('B', 0.0):>10.4f}"
            )
    else:
        print("  Per-family segments : (none — all families below MIN_FAMILY_SAMPLES=30)")

    buckets = curve.get("buckets_debug", {})
    if buckets:
        print()
        print(f"  Buckets (actual vs estimated):")
        print(f"    {'bucket':<12} {'n':>5} {'avg_est':>8} {'actual':>8} {'bias':>8}")
        for b in sorted(buckets):
            d = buckets[b]
            print(
                f"    {b:<12} {d['n']:>5} "
                f"{d['avg_est']:>8.4f} {d['actual_rate']:>8.4f} {d['bias']:>+8.4f}"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db", default="kalshi_trades.db",
        help="DB path (default: kalshi_trades.db in cwd)",
    )
    ap.add_argument(
        "--batch-size", type=int, default=5000,
        help="Rows per bridge call (default 5000)",
    )
    ap.add_argument(
        "--no-fit", action="store_true",
        help="Skip the Platt fit after bridging",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report counts only; don't insert anything",
    )
    ap.add_argument(
        "--max-batches", type=int, default=0,
        help="Cap total batches (0 = unlimited)",
    )
    args = ap.parse_args()

    conn = init_db(args.db)

    wm = _load_watermark(conn)
    pending, total = _count_pending(conn, wm)
    print(f"DB              : {args.db}")
    print(f"Watermark       : {wm}")
    print(f"Shadow rows     : {total:,} total")
    print(f"Pending bridge  : {pending:,} (settled + above watermark)")
    if args.dry_run:
        print("\n[dry-run] no writes performed")
        return 0
    if pending == 0:
        print("\nNothing to bridge. ", end="")
        if not args.no_fit:
            print("Running Platt fit on existing calibration rows anyway…")
            curve = fit_and_persist(conn)
            _print_curve(curve)
        else:
            print("Exiting.")
        return 0

    print(f"\nBridging in batches of {args.batch_size:,}…")
    total_bridged = 0
    total_skipped = 0
    tickers_seen: set[str] = set()
    t0 = time.time()
    batch_num = 0
    while True:
        batch_num += 1
        if args.max_batches and batch_num > args.max_batches:
            print(f"  [batch {batch_num - 1}] max-batches cap reached, stopping")
            break
        stats = bridge_shadow_to_calibration(conn, batch_limit=args.batch_size)
        if stats["rows_bridged"] == 0 and stats["skipped_invalid"] == 0:
            # nothing advanced — done
            break
        total_bridged += stats["rows_bridged"]
        total_skipped += stats["skipped_invalid"]
        elapsed = time.time() - t0
        print(
            f"  [batch {batch_num}] bridged={stats['rows_bridged']:>5} "
            f"skipped={stats['skipped_invalid']:>3} "
            f"watermark={stats['watermark_after']} "
            f"total_bridged={total_bridged:,} "
            f"elapsed={elapsed:.1f}s"
        )
        # If fewer rows than batch_size, we're out of pending work.
        if stats["rows_bridged"] + stats["skipped_invalid"] < args.batch_size:
            break

    elapsed = time.time() - t0
    print()
    print(f"Bridging complete in {elapsed:.1f}s")
    print(f"  rows_bridged    : {total_bridged:,}")
    print(f"  skipped_invalid : {total_skipped:,}")
    cal_total = conn.execute(
        "SELECT COUNT(*) FROM calibration WHERE source_desc='weather_mm_shadow'"
    ).fetchone()[0]
    cal_grand = conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0]
    print(f"  calibration (weather_mm_shadow) : {cal_total:,}")
    print(f"  calibration (all rows)          : {cal_grand:,}")

    if args.no_fit:
        print("\n[--no-fit] skipping Platt refit")
        return 0

    print("\nFitting Platt curve on combined calibration data…")
    curve = fit_and_persist(conn)
    _print_curve(curve)

    # Also dump the compact JSON so you can grep it later.
    print()
    print("Curve cached to kv_cache['calibration_curve_v2']")
    summary = {
        k: curve[k]
        for k in ("method", "A", "B", "n_samples",
                  "brier_before", "brier_after")
        if k in curve
    }
    summary["families"] = list(curve.get("families", {}).keys())
    print("Summary: " + json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
