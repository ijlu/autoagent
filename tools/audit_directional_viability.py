#!/usr/bin/env python3
"""
Directional pivot viability audit — second pass.

First pass (backtest_directional_from_shadow.py) took one trade per
ticker at first-qualifying-edge. N=21 trades, WR=47.6%, −$9/trade.
Sample too small to distinguish edge from noise.

This script asks the deeper question: **is our FV actually more
calibrated than the market's mid?** That's the necessary theoretical
condition for directional to have positive EV. It's also computable
over every annotated shadow row (N ≈ 2100), giving a much tighter
signal than per-ticker trade simulation.

Metrics:
  Brier_ours     = mean( (fv/100 − won_yes)² )
  Brier_market   = mean( (mid/100 − won_yes)² )
  Edge_realized  = (our_p - market_p) * (won_yes - market_p) — if
                    positive on average, we systematically move toward
                    the correct side relative to market.
  Log loss       = cross-entropy; heavier penalty on overconfidence.

Partitioned by family and by edge-magnitude bucket. The latter tells us
whether higher-confidence (|our_p - market_p| larger) bets are MORE
accurate or LESS — that's the key adverse-selection test for
directional, mirroring what we did for MM.
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from bot.db import init_db


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


def _log_loss(p: float, y: int) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def main() -> int:
    conn = init_db()
    rows = conn.execute("""
        SELECT ticker, series, fair_value_cents,
               market_yes_bid, market_yes_ask, ticker_settled_yes
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND fair_value_cents IS NOT NULL
          AND market_yes_bid IS NOT NULL
          AND market_yes_ask IS NOT NULL
          AND ticker_settled_yes IS NOT NULL
    """).fetchall()
    print(f"Rows under audit: {len(rows)}")

    # ─── A) per-family Brier + log-loss comparison ─────────────────────
    print("\n" + "=" * 80)
    print(" [A] Calibration: our FV vs market mid")
    print("=" * 80)
    print(f"  {'family':<12} {'n':>5} {'brier_ours':>11} {'brier_mkt':>11} "
          f"{'Δ_brier':>9} {'ll_ours':>9} {'ll_mkt':>9} {'Δ_ll':>9} "
          f"{'yes_rate':>9}")
    by_family = defaultdict(list)
    for r in rows:
        by_family[r[1]].append(r)
    grand = []
    for family in sorted(by_family):
        rs = by_family[family]
        bo = bm = lo = lm = 0.0
        yes = 0
        for (_tk, _s, fv, mb, ma, wy) in rs:
            p_ours = fv / 100.0
            p_mkt = (mb + ma) / 2.0 / 100.0
            y = int(wy)
            bo += _brier(p_ours, y); bm += _brier(p_mkt, y)
            lo += _log_loss(p_ours, y); lm += _log_loss(p_mkt, y)
            yes += y
        n = len(rs)
        bo /= n; bm /= n; lo /= n; lm /= n
        print(f"  {family:<12} {n:>5d} {bo:>11.4f} {bm:>11.4f} "
              f"{bm - bo:>+9.4f} {lo:>9.3f} {lm:>9.3f} {lm - lo:>+9.3f} "
              f"{yes / n:>9.1%}")
        grand.extend(rs)

    bo = bm = lo = lm = 0.0
    yes = 0
    for (_tk, _s, fv, mb, ma, wy) in grand:
        p_ours = fv / 100.0
        p_mkt = (mb + ma) / 2.0 / 100.0
        y = int(wy)
        bo += _brier(p_ours, y); bm += _brier(p_mkt, y)
        lo += _log_loss(p_ours, y); lm += _log_loss(p_mkt, y)
        yes += y
    n = len(grand)
    bo /= n; bm /= n; lo /= n; lm /= n
    print(f"  {'TOTAL':<12} {n:>5d} {bo:>11.4f} {bm:>11.4f} "
          f"{bm - bo:>+9.4f} {lo:>9.3f} {lm:>9.3f} {lm - lo:>+9.3f} "
          f"{yes / n:>9.1%}")
    print("\n  Δ_brier > 0 ⇒ our FV is better calibrated than market mid.")
    print("  Δ_ll    > 0 ⇒ same, log-loss (penalizes overconfidence).")

    # ─── B) Edge-magnitude bucket: is bigger 'edge' MORE or LESS accurate? ─
    print("\n" + "=" * 80)
    print(" [B] Accuracy by |edge| bucket  (our_p − mkt_p magnitude)")
    print("=" * 80)
    print(
        "  If high-edge rows are MORE accurate than low-edge rows, the signal\n"
        "  is genuine and directional entry at high-edge rows captures alpha.\n"
        "  If high-edge rows are LESS accurate, our confidence is a mirage.\n"
    )
    buckets = [(0, 0.03), (0.03, 0.05), (0.05, 0.08), (0.08, 0.12),
               (0.12, 0.20), (0.20, 0.50), (0.50, 1.01)]
    bucket_stats: dict[tuple[float, float], dict] = {
        b: {"n": 0, "wr_for": 0, "brier_ours": 0.0, "brier_mkt": 0.0,
            "pnl_net_yes_side": 0, "pnl_net_no_side": 0}
        for b in buckets
    }
    # For each row: we'd take YES if our_p > mkt_p, NO if <. Compute realized P&L
    # at taker price for 1 contract.
    for (_tk, _s, fv, mb, ma, wy) in rows:
        our_p = fv / 100.0
        mkt_p = (mb + ma) / 2.0 / 100.0
        edge_mag = abs(our_p - mkt_p)
        if our_p > mkt_p:
            side_yes = True
            entry = int(ma)   # buy YES at ask
        else:
            side_yes = False
            entry = 100 - int(mb)  # buy NO at 100-bid
        y = int(wy)
        won = (y == 1) if side_yes else (y == 0)
        pnl_1c = (100 if won else 0) - entry  # 1 contract, gross of fee
        fits = "won" if won else "lost"

        for (lo_b, hi_b) in buckets:
            if lo_b <= edge_mag < hi_b:
                d = bucket_stats[(lo_b, hi_b)]
                d["n"] += 1
                if won:
                    d["wr_for"] += 1
                # Brier of our_p using OUR predicted direction's outcome
                d["brier_ours"] += (our_p - y) ** 2
                d["brier_mkt"] += (mkt_p - y) ** 2
                if side_yes:
                    d["pnl_net_yes_side"] += pnl_1c
                else:
                    d["pnl_net_no_side"] += pnl_1c
                break

    print(f"  {'|edge|':<18} {'n':>5} {'wr_side':>8} {'brier_o':>8} "
          f"{'brier_m':>8} {'Δbrier':>8} {'pnl/1c':>8}")
    for (lo_b, hi_b) in buckets:
        d = bucket_stats[(lo_b, hi_b)]
        if d["n"] == 0:
            continue
        wr = d["wr_for"] / d["n"]
        bo = d["brier_ours"] / d["n"]
        bm = d["brier_mkt"] / d["n"]
        pnl_total = d["pnl_net_yes_side"] + d["pnl_net_no_side"]
        ppt = pnl_total / d["n"]
        label = f"[{lo_b:.2f},{hi_b:.2f})"
        print(f"  {label:<18} {d['n']:>5d} {wr:>8.1%} {bo:>8.4f} "
              f"{bm:>8.4f} {bm - bo:>+8.4f} {ppt:>8.2f}")

    # ─── C) FV distribution sanity — are we producing trivially-extreme FVs? ─
    print("\n" + "=" * 80)
    print(" [C] FV distribution (sanity check on ensemble output)")
    print("=" * 80)
    fv_buckets = [0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]
    fv_hist = defaultdict(int)
    for (_tk, _s, fv, _mb, _ma, _wy) in rows:
        for lo_b, hi_b in zip(fv_buckets[:-1], fv_buckets[1:]):
            if lo_b <= fv < hi_b:
                fv_hist[(lo_b, hi_b)] += 1
                break
    for lo_b, hi_b in zip(fv_buckets[:-1], fv_buckets[1:]):
        print(f"  FV ∈ [{lo_b:>2d}¢, {hi_b:>2d}¢)   {fv_hist[(lo_b, hi_b)]:>6d}")

    # ─── D) Per-ticker "trade-once-per-signal" net P&L over multiple sizes ─
    print("\n" + "=" * 80)
    print(" [D] Per-ticker directional sim — size sensitivity")
    print("=" * 80)
    print(
        "  One trade per (ticker, side-we-chose), entered at first row\n"
        "  where |edge| ≥ threshold. Repeat across contract sizes.\n"
    )
    from bot.core.money import kalshi_taker_fee
    from collections import defaultdict as dd
    per_ticker: dict[str, list] = dd(list)
    for r in rows:
        per_ticker[r[0]].append(r)

    for thresh in [0.05, 0.10]:
        print(f"\n  threshold |edge| ≥ {thresh:.2f}")
        print(f"  {'size':>5} {'n_tr':>5} {'wr':>6} {'gross':>7} "
              f"{'fees':>6} {'net':>7} {'net/tr':>8}")
        for size in [1, 3, 10]:
            n_tr = n_wins = gross = fees = 0
            for tk, rs in per_ticker.items():
                taken = False
                for (_t, _s, fv, mb, ma, wy) in sorted(rs, key=lambda x: x[0]):
                    if taken:
                        break
                    our_p = fv / 100.0
                    mkt_p = (mb + ma) / 2.0 / 100.0
                    edge_mag = abs(our_p - mkt_p)
                    if edge_mag < thresh:
                        continue
                    if our_p > mkt_p:
                        entry = int(ma); side_yes = True
                    else:
                        entry = 100 - int(mb); side_yes = False
                    if entry < 1 or entry > 99:
                        continue
                    y = int(wy)
                    won = (y == 1) if side_yes else (y == 0)
                    g = (100 if won else 0) * size - entry * size
                    f = kalshi_taker_fee(size, entry)
                    gross += g; fees += f
                    n_tr += 1
                    if won:
                        n_wins += 1
                    taken = True
            net = gross - fees
            if n_tr:
                print(f"  {size:>5d} {n_tr:>5d} {n_wins/n_tr:>6.1%} "
                      f"{gross:>7d} {fees:>6d} {net:>7d} {net/n_tr:>8.2f}")
            else:
                print(f"  {size:>5d} {n_tr:>5d}  —")

    return 0


if __name__ == "__main__":
    sys.exit(main())
