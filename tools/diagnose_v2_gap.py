"""Stratified diagnostics for the v2 vs market Brier gap.

The hypothesis sweep showed five of six knobs are flat or wrong-signed,
leaving the −0.04 pooled gap structurally unexplained. This tool slices
the same dataset along three axes to localize where the gap lives:

    --report horizon   bin by hours-to-settlement at decision time
    --report bracket   bin by |combined.μ − bracket boundary|
                       (close-call vs clear-call cases)
    --report miami     per-ticker drill on the worst family
                       (KXHIGHMIA at baseline Brier 0.18)
    --report all       run all three

Mechanics:

  * One replay pass; rich per-record capture (μ, σ, bracket bounds,
    horizon, live/mkt/replay Brier).
  * Combined μ + σ from predict_v2 are recovered by redirecting
    ``_write_snapshots`` to an in-memory dict instead of the DB.
  * Settlement time is parsed from the ticker's date suffix —
    KXHIGH<CITY>-26APR23-... → 2026-04-23 → midnight UTC. Approximate
    (±12h depending on local close), good enough for 6-hour bucketing.
  * Bracket-distance is computed against the bracket center for -B
    tickers and against the threshold for -T tickers. Negative
    distance means our μ sits outside the bracket / on the wrong side
    of the threshold.

Run on the VPS:

    python -m tools.diagnose_v2_gap --report all \\
        --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from bot.config import DB_PATH
from bot.db import init_db
from bot.signals import weather_ensemble_v2 as v2

from tools.backtest_v2_replay import _fetch_market, _replay_predict_v2


# ── Settlement time parsing ───────────────────────────────────────────

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _settle_unix_from_ticker(ticker: str) -> Optional[float]:
    """Parse settlement timestamp from KXHIGH<CITY>-YYMMMDD-... .

    Settlement happens at end-of-local-day on the named date — Kalshi
    weather markets resolve once NWS publishes the daily high, which is
    midnight-to-midnight local. Approximate as 04:00 UTC the day *after*
    the named date (covers ET=midnight ET / PT=8pm PT, plenty close for
    6-hour bucketing). Off by hours in either direction is fine; off by
    a full day (which the named-date-midnight-UTC parsing produced) is
    not — every ts_unix lands negative under that interpretation.
    """
    parts = ticker.split("-")
    if len(parts) < 2 or len(parts[1]) < 7:
        return None
    suf = parts[1]
    try:
        yy = int(suf[:2])
        mon_idx = _MONTHS.index(suf[2:5].upper()) + 1
        dd = int(suf[5:7])
        dt = datetime(2000 + yy, mon_idx, dd, 4, 0, tzinfo=timezone.utc)
        return dt.timestamp() + 86400.0  # +1 day → 04:00 UTC next morning
    except (ValueError, IndexError):
        return None


# ── Record collection ─────────────────────────────────────────────────

def _collect(
    conn: sqlite3.Connection, limit: Optional[int] = None,
) -> list[dict]:
    """One replay per settled ticker, capturing μ, σ, bounds, horizon.

    Settlement time is taken from ``alpha_backtest.ts_settle_unix`` when
    a row exists for the ticker (authoritative), falling back to the
    ticker-date approximation otherwise. The earlier midnight-UTC parse
    was too crude — produced negative hours_out on ~21% of tickers,
    which silently dropped them from the horizon buckets.
    """
    rows = conn.execute(
        """SELECT s.ticker, s.series,
                  MAX(s.ts_unix) AS ts,
                  MAX(s.fair_value_cents) AS live_fair,
                  MAX(s.market_mid) AS market_mid,
                  MAX(s.ticker_settled_yes) AS settled,
                  MAX(ab.ts_settle_unix) AS ab_settle_unix
             FROM weather_mm_shadow s
             LEFT JOIN alpha_backtest ab ON ab.ticker = s.ticker
            WHERE s.ticker_settled_yes IS NOT NULL
              AND s.fair_value_cents IS NOT NULL
              AND s.market_mid IS NOT NULL
         GROUP BY s.ticker
         ORDER BY ts ASC""",
    ).fetchall()
    if limit:
        rows = rows[:limit]
    print(f"[diagnose] {len(rows)} settled tickers loaded")

    # Capture combined_v2 mean/sigma by redirecting snapshot writes into
    # a per-ticker dict. We'd otherwise need to either modify predict_v2
    # to return more or duplicate the combine pipeline.
    captured: dict[str, dict] = {}

    def _capture(snapshot_rows):
        for r in snapshot_rows:
            # tuple shape: (recorded_at, series, ticker, source, prob,
            #               mean_f, sigma_f, hours_out)
            if r[3] == "combined_v2":
                captured[r[2]] = {"mean_f": r[5], "sigma_f": r[6]}

    saved_writer = v2._write_snapshots
    v2._write_snapshots = _capture

    records: list[dict] = []
    skipped = {"no_market": 0, "no_replay": 0, "no_settle": 0,
               "no_proj": 0, "no_capture": 0}
    n = 0
    t0 = time.time()
    last_print = t0

    try:
        for ticker, series, ts_unix, live_fair, market_mid, settled, ab_settle in rows:
            # Prefer alpha_backtest's authoritative ts_settle_unix; fall
            # back to the ticker-date approximation when no alpha_backtest
            # row exists for this ticker.
            if ab_settle is not None:
                settle_unix = float(ab_settle)
            else:
                settle_unix = _settle_unix_from_ticker(ticker)
            if settle_unix is None:
                skipped["no_settle"] += 1
                continue

            market = _fetch_market(ticker)
            if not market:
                skipped["no_market"] += 1
                continue

            try:
                replay = _replay_predict_v2(ticker, market, conn)
            except Exception:
                replay = None
            if replay is None or replay[0] is None:
                skipped["no_replay"] += 1
                continue

            cap = captured.get(ticker)
            if cap is None:
                skipped["no_capture"] += 1
                continue

            proj = v2._parse_market_for_projection(ticker, market)
            if proj is None:
                skipped["no_proj"] += 1
                continue
            is_bracket, threshold_f, _is_above, lo_f, hi_f = proj

            won_yes = float(settled)
            replay_prob = float(replay[0])
            # weather_mm_shadow stores market_mid + fair_value_cents in cents
            # (0-100). predict_v2 returns probability in 0-1. Bring all three
            # to 0-1 before computing Brier so the comparison is apples-to-
            # apples.
            mkt_prob = float(market_mid) / 100.0
            live_prob = float(live_fair) / 100.0
            hours_out = (settle_unix - float(ts_unix)) / 3600.0

            records.append({
                "ticker": ticker,
                "series": series,
                "won_yes": won_yes,
                "live_prob": live_prob,
                "mkt_prob": mkt_prob,
                "replay_prob": replay_prob,
                "live_b": (live_prob - won_yes) ** 2,
                "mkt_b": (mkt_prob - won_yes) ** 2,
                "replay_b": (replay_prob - won_yes) ** 2,
                "mu": float(cap["mean_f"]),
                "sigma": float(cap["sigma_f"]),
                "is_bracket": bool(is_bracket),
                "threshold_f": threshold_f,
                "bracket_lo": lo_f,
                "bracket_hi": hi_f,
                "hours_out": hours_out,
            })
            n += 1
            if time.time() - last_print > 10:
                rate = n / max(time.time() - t0, 1e-6)
                print(f"  ... {n}/{len(rows)} ({rate:.1f}/s)")
                last_print = time.time()
    finally:
        v2._write_snapshots = saved_writer

    print(f"[diagnose] collected {len(records)} records, "
          f"skipped={skipped} ({time.time()-t0:.0f}s)")
    return records


# ── Report 1: horizon stratification ──────────────────────────────────

_HORIZON_BUCKETS = [
    ("0-6h",   0.0,    6.0),
    ("6-12h",  6.0,   12.0),
    ("12-24h", 12.0,  24.0),
    ("24-48h", 24.0,  48.0),
    ("48h+",   48.0, 1e9),
]


def report_horizon(records: list[dict]) -> None:
    print()
    print("=" * 92)
    print("Brier by hours-to-settlement at decision time (= shadow row ts_unix)")
    print("=" * 92)
    print(f"  {'bucket':<10} {'n':>5} {'v1_live':>9} {'v2_replay':>10} "
          f"{'mkt_mid':>9} {'gap_v2-mkt':>12}")
    print("  " + "-" * 70)

    for label, lo, hi in _HORIZON_BUCKETS:
        bucket = [r for r in records if lo <= r["hours_out"] < hi]
        if not bucket:
            continue
        live = statistics.mean(r["live_b"] for r in bucket)
        mkt = statistics.mean(r["mkt_b"] for r in bucket)
        replay = statistics.mean(r["replay_b"] for r in bucket)
        gap = replay - mkt
        print(f"  {label:<10} {len(bucket):>5} {live:>9.4f} {replay:>10.4f} "
              f"{mkt:>9.4f} {gap:>+12.4f}")

    print()
    print("  Reading: gap_v2-mkt > 0 means we trail the market in that horizon bucket.")
    print("           If the gap is uniform across horizons → structural methodology issue.")
    print("           If the gap closes near settlement → freshness / nowcast deficit.")
    print("           If the gap closes far out → late-day overconfidence.")


# ── Report 2: bracket-distance stratification ─────────────────────────

def _edge_distance(r: dict) -> float:
    """Signed distance from combined.μ to the relevant bracket boundary.

    Bracket: distance to the *nearest* boundary, positive when μ inside
    [lo, hi], negative when μ outside.
    Threshold: signed distance to the threshold; positive when μ above
    for is_above=True (or below for is_above=False — but we always
    record from "above" perspective in our snapshots).
    """
    mu = r["mu"]
    if r["is_bracket"]:
        lo = r["bracket_lo"]; hi = r["bracket_hi"]
        if mu < lo:
            return mu - lo  # negative (below bracket)
        if mu > hi:
            return hi - mu  # negative (above bracket)
        return min(mu - lo, hi - mu)  # positive (inside)
    # Threshold marker: distance to the threshold itself.
    return mu - r["threshold_f"]


_DIST_BUCKETS = [
    ("deep_out  (<-2°F)",    -1e9,  -2.0),
    ("out       (-2 to -0.5)",-2.0, -0.5),
    ("edge      (-0.5 to 0.5)",-0.5, 0.5),
    ("in        (0.5 to 2)",   0.5,  2.0),
    ("deep_in   (>2°F)",       2.0,  1e9),
]


def report_bracket_distance(records: list[dict]) -> None:
    print()
    print("=" * 92)
    print("Brier by combined.μ distance from bracket boundary (close-call vs clear)")
    print("=" * 92)
    print("  Brackets: distance is to the nearest bracket edge (negative = outside).")
    print("  Thresholds: distance is signed (μ - threshold).")
    print()
    print(f"  {'bucket':<26} {'n':>5} {'v1_live':>9} {'v2_replay':>10} "
          f"{'mkt_mid':>9} {'gap_v2-mkt':>12}")
    print("  " + "-" * 80)

    for label, lo, hi in _DIST_BUCKETS:
        bucket = [r for r in records if lo <= _edge_distance(r) < hi]
        if not bucket:
            continue
        live = statistics.mean(r["live_b"] for r in bucket)
        mkt = statistics.mean(r["mkt_b"] for r in bucket)
        replay = statistics.mean(r["replay_b"] for r in bucket)
        gap = replay - mkt
        print(f"  {label:<26} {len(bucket):>5} {live:>9.4f} {replay:>10.4f} "
              f"{mkt:>9.4f} {gap:>+12.4f}")

    print()
    print("  Reading: a large gap in 'edge' = we're losing close calls (σ-shape problem).")
    print("           A large gap in 'deep_in/out' = we're wrong on clear cases (μ bias).")


# ── Report 3: Miami drill ─────────────────────────────────────────────

def report_miami(records: list[dict]) -> None:
    miami = [r for r in records if r["series"] == "KXHIGHMIA"]
    if not miami:
        print("\n[miami] no KXHIGHMIA records found")
        return

    pooled_replay = statistics.mean(r["replay_b"] for r in miami)
    pooled_mkt = statistics.mean(r["mkt_b"] for r in miami)
    pooled_live = statistics.mean(r["live_b"] for r in miami)
    print()
    print("=" * 96)
    print(f"KXHIGHMIA drill — n={len(miami)}, replay_B={pooled_replay:.4f}, "
          f"mkt_B={pooled_mkt:.4f}, live_B={pooled_live:.4f}")
    print("=" * 96)

    # Sort worst-replay first so the cases that drive the family Brier
    # surface at the top.
    miami_sorted = sorted(miami, key=lambda r: -r["replay_b"])

    # Show worst 20 — enough to spot patterns.
    n_show = min(20, len(miami_sorted))
    print(f"  Worst {n_show} by replay_B (where we lost most):")
    print(f"  {'ticker':<28} {'won':>4} {'live_p':>7} {'v2_p':>6} "
          f"{'mkt_p':>6} {'μ':>6} {'σ':>5} {'lo-hi':<14} {'h_out':>6} "
          f"{'rB':>6} {'mB':>6}")
    print("  " + "-" * 110)
    for r in miami_sorted[:n_show]:
        bracket = (
            f"{r['bracket_lo']:.0f}-{r['bracket_hi']:.0f}"
            if r["is_bracket"] else f"T{r['threshold_f']:.0f}"
        )
        print(f"  {r['ticker']:<28} {int(r['won_yes']):>4} "
              f"{r['live_prob']:>7.3f} {r['replay_prob']:>6.3f} "
              f"{r['mkt_prob']:>6.3f} {r['mu']:>6.1f} {r['sigma']:>5.2f} "
              f"{bracket:<14} {r['hours_out']:>6.1f} "
              f"{r['replay_b']:>6.3f} {r['mkt_b']:>6.3f}")

    # Quick pattern checks
    print()
    confident_wrong = [
        r for r in miami if r["replay_b"] > 0.5 and r["mkt_b"] < 0.1
    ]
    print(f"  Confident-wrong cases (replay_B > 0.5 AND mkt_B < 0.1): "
          f"{len(confident_wrong)}/{len(miami)}")
    if confident_wrong:
        avg_mu_offset = statistics.mean(
            abs(_edge_distance(r)) for r in confident_wrong
        )
        avg_sigma = statistics.mean(r["sigma"] for r in confident_wrong)
        print(f"    avg |edge_distance| on these = {avg_mu_offset:.2f}°F, "
              f"avg σ = {avg_sigma:.2f}°F")
        print(f"    → if avg|distance|/σ ≫ 1, μ is genuinely off; if ~0, σ is too tight.")


# ── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--report", default="all",
                   choices=["horizon", "bracket", "miami", "all"])
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    conn = init_db(args.db)
    records = _collect(conn, limit=args.limit)
    if not records:
        print("[diagnose] no records collected — abort")
        return 1

    if args.report in ("horizon", "all"):
        report_horizon(records)
    if args.report in ("bracket", "all"):
        report_bracket_distance(records)
    if args.report in ("miami", "all"):
        report_miami(records)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
