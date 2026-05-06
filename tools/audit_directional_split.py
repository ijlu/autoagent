#!/usr/bin/env python3
"""Split the directional viability audit by market suffix.

Hypothesis: brackets (-B) vs thresholds (-T) have fundamentally
different probability structures. Thresholds only need a CDF at one
point; brackets need a PDF over a narrow interval. Our ensemble may be
calibrated on one and broken on the other.
"""
from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, ".")

from bot.db import init_db


def classify(ticker: str) -> str:
    # KXHIGHNY-26APR21-T75  → threshold
    # KXHIGHNY-26APR21-B74.5 → bracket
    # Default → other
    parts = ticker.split("-")
    if not parts:
        return "other"
    suffix = parts[-1]
    if suffix.startswith("T"):
        return "threshold"
    if suffix.startswith("B"):
        return "bracket"
    return "other"


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

    # Split by suffix type × family
    split: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        split[(classify(r[0]), r[1])].append(r)

    print(f"Total rows: {len(rows)}\n")
    print("=" * 80)
    print(" Brier / WR / implied EV by (suffix, family)")
    print("=" * 80)
    print(f"  {'suffix':<10} {'family':<12} {'n':>5} "
          f"{'brier_o':>8} {'brier_m':>8} {'Δbrier':>8} "
          f"{'wr_sig':>7} {'yes_rt':>7}")

    overall: dict[str, dict] = {
        s: {"n": 0, "bo": 0.0, "bm": 0.0, "correct_side": 0, "yes": 0}
        for s in ("threshold", "bracket", "other")
    }

    for (suffix, family) in sorted(split):
        rs = split[(suffix, family)]
        bo = bm = 0.0
        correct_side = 0
        yes = 0
        for (_tk, _s, fv, mb, ma, wy) in rs:
            p_ours = fv / 100.0
            p_mkt = (mb + ma) / 2.0 / 100.0
            y = int(wy)
            bo += (p_ours - y) ** 2
            bm += (p_mkt - y) ** 2
            # "correct side" = our prediction relative to 50% agrees with outcome
            our_pred_yes = p_ours > p_mkt
            if (our_pred_yes and y == 1) or (not our_pred_yes and y == 0):
                correct_side += 1
            yes += y
        n = len(rs)
        wr = correct_side / n
        bo_avg = bo / n
        bm_avg = bm / n
        print(f"  {suffix:<10} {family:<12} {n:>5d} "
              f"{bo_avg:>8.4f} {bm_avg:>8.4f} {bm_avg - bo_avg:>+8.4f} "
              f"{wr:>7.1%} {yes / n:>7.1%}")
        overall[suffix]["n"] += n
        overall[suffix]["bo"] += bo
        overall[suffix]["bm"] += bm
        overall[suffix]["correct_side"] += correct_side
        overall[suffix]["yes"] += yes

    print()
    print(f"  {'OVERALL BY SUFFIX':<22} {'':>12} {'n':>5} "
          f"{'brier_o':>8} {'brier_m':>8} {'Δbrier':>8} "
          f"{'wr_sig':>7} {'yes_rt':>7}")
    for suffix in ("threshold", "bracket", "other"):
        d = overall[suffix]
        n = d["n"]
        if not n:
            continue
        bo = d["bo"] / n
        bm = d["bm"] / n
        wr = d["correct_side"] / n
        print(f"  {suffix:<22} {'':>12} {n:>5d} "
              f"{bo:>8.4f} {bm:>8.4f} {bm - bo:>+8.4f} "
              f"{wr:>7.1%} {d['yes'] / n:>7.1%}")

    # And one more angle: on thresholds only, simulate directional entries
    # and compute net P&L. This is the clean test for the pivot.
    print()
    print("=" * 80)
    print(" Threshold-only directional simulation (MIN_EDGE sweep)")
    print("=" * 80)

    from bot.core.money import kalshi_taker_fee
    per_ticker: dict[str, list] = defaultdict(list)
    for r in rows:
        if classify(r[0]) != "threshold":
            continue
        per_ticker[r[0]].append(r)
    print(f"Threshold tickers: {len(per_ticker)}")

    for thresh in [0.03, 0.05, 0.08, 0.10, 0.12]:
        print(f"\n  |edge| ≥ {thresh:.2f}")
        print(f"  {'size':>5} {'n':>4} {'wr':>6} {'gross':>7} "
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
                print(f"  {size:>5d} {n_tr:>4d} {n_wins/n_tr:>6.1%} "
                      f"{gross:>7d} {fees:>6d} {net:>7d} {net/n_tr:>8.2f}")

    # Same for brackets — stress confirm failure mode
    print()
    print("=" * 80)
    print(" Bracket-only directional simulation (MIN_EDGE sweep)")
    print("=" * 80)
    per_ticker = defaultdict(list)
    for r in rows:
        if classify(r[0]) != "bracket":
            continue
        per_ticker[r[0]].append(r)
    print(f"Bracket tickers: {len(per_ticker)}")
    for thresh in [0.03, 0.05, 0.10]:
        print(f"\n  |edge| ≥ {thresh:.2f}")
        print(f"  {'size':>5} {'n':>4} {'wr':>6} {'gross':>7} "
              f"{'fees':>6} {'net':>7} {'net/tr':>8}")
        for size in [1, 3]:
            n_tr = n_wins = gross = fees = 0
            for tk, rs in per_ticker.items():
                taken = False
                for (_t, _s, fv, mb, ma, wy) in sorted(rs, key=lambda x: x[0]):
                    if taken:
                        break
                    our_p = fv / 100.0
                    mkt_p = (mb + ma) / 2.0 / 100.0
                    if abs(our_p - mkt_p) < thresh:
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
                print(f"  {size:>5d} {n_tr:>4d} {n_wins/n_tr:>6.1%} "
                      f"{gross:>7d} {fees:>6d} {net:>7d} {net/n_tr:>8.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
