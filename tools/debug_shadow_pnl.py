#!/usr/bin/env python3
"""
Forensic debug of the weather_mm_shadow P&L catastrophe.

Pulls distributions + worst-case rows so we can see *why* pnl/fill ≈ -100c.
Hypotheses under investigation:

  H1: MM_ORDER_SIZE misattribution (rows represent N contracts, not 1)
  H2: match_shadow_fills producing spurious fills (both sides filled on
      bracket resolutions that never had real counterparties)
  H3: Adversely-selected one-sided fills (we got crossed only when FV
      moved against us)
  H4: Quote width too narrow vs realized settlement tails
  H5: Bracket-bucket mispricing (we quote "5c YES" on <1%-probability
      tails that sometimes resolve YES @ +95c reverse-adverse — or the
      mirror: we quote "95c NO" on >99%-probability buckets with no edge)
  H6: Duplicate rows per quote event (same METAR tick recorded twice)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, ".")

from bot.config import MM_ORDER_SIZE
from bot.db import init_db


def pct(num: int | float, denom: int | float) -> str:
    if not denom:
        return "—"
    return f"{100.0 * num / denom:.1f}%"


def main() -> int:
    conn = init_db()
    print(f"MM_ORDER_SIZE (env) = {MM_ORDER_SIZE}")
    print("=" * 80)

    # 1 ─ Per-series headline + fill-shape breakdown
    print("\n[1] Per-series fill-shape + P&L (annotated rows only)")
    print("-" * 80)
    rows = conn.execute("""
        SELECT series,
               COUNT(*) AS n_total,
               SUM(CASE WHEN shadow_bid_filled=1 AND shadow_ask_filled=1
                        THEN 1 ELSE 0 END) AS both,
               SUM(CASE WHEN shadow_bid_filled=1 AND shadow_ask_filled=0
                        THEN 1 ELSE 0 END) AS bid_only,
               SUM(CASE WHEN shadow_bid_filled=0 AND shadow_ask_filled=1
                        THEN 1 ELSE 0 END) AS ask_only,
               SUM(CASE WHEN shadow_bid_filled=0 AND shadow_ask_filled=0
                        THEN 1 ELSE 0 END) AS neither,
               SUM(shadow_pnl_cents) AS pnl_sum,
               MIN(shadow_pnl_cents) AS worst_loss,
               MAX(shadow_pnl_cents) AS best_win
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
        GROUP BY series ORDER BY pnl_sum ASC
    """).fetchall()
    print(f"{'series':<12} {'n':>5} {'both':>5} {'bid':>5} {'ask':>5} "
          f"{'neith':>6} {'pnl':>9} {'worst':>8} {'best':>8}")
    for r in rows:
        (series, n_total, both, bid_only, ask_only, neither,
         pnl_sum, worst, best) = r
        print(f"{series:<12} {n_total:>5d} {both:>5d} {bid_only:>5d} "
              f"{ask_only:>5d} {neither:>6d} "
              f"{(pnl_sum or 0):>9d} {(worst or 0):>8d} {(best or 0):>8d}")

    # 2 ─ P&L distribution among FILLED rows, bucketed
    print("\n[2] shadow_pnl_cents distribution, filled-rows only")
    print("-" * 80)
    buckets = [-1000, -500, -300, -200, -100, -50, -10, 0, 10, 50, 100, 200, 500, 1000]
    for lo, hi in zip(buckets[:-1], buckets[1:]):
        cnt = conn.execute("""
            SELECT COUNT(*) FROM weather_mm_shadow
            WHERE ts_settle_unix IS NOT NULL
              AND (shadow_bid_filled=1 OR shadow_ask_filled=1)
              AND shadow_pnl_cents >= ? AND shadow_pnl_cents < ?
        """, (lo, hi)).fetchone()[0]
        print(f"   [{lo:>6d}, {hi:>6d}) :  {cnt}")
    # Tails
    lt = conn.execute("""SELECT COUNT(*) FROM weather_mm_shadow
         WHERE ts_settle_unix IS NOT NULL
           AND (shadow_bid_filled=1 OR shadow_ask_filled=1)
           AND shadow_pnl_cents < -1000""").fetchone()[0]
    gt = conn.execute("""SELECT COUNT(*) FROM weather_mm_shadow
         WHERE ts_settle_unix IS NOT NULL
           AND (shadow_bid_filled=1 OR shadow_ask_filled=1)
           AND shadow_pnl_cents >= 1000""").fetchone()[0]
    print(f"   (< -1000) : {lt}")
    print(f"   (>= 1000) : {gt}")

    # 3 ─ Sanity of the fill prices themselves
    print("\n[3] Filled-row proposed prices (what we quoted + what 'filled')")
    print("-" * 80)
    r = conn.execute("""
        SELECT
            AVG(proposed_bid_cents) AS avg_bid,
            AVG(proposed_ask_cents) AS avg_ask,
            AVG(proposed_ask_cents - proposed_bid_cents) AS avg_spread,
            MIN(proposed_bid_cents) AS min_bid, MAX(proposed_bid_cents) AS max_bid,
            MIN(proposed_ask_cents) AS min_ask, MAX(proposed_ask_cents) AS max_ask
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND (shadow_bid_filled=1 OR shadow_ask_filled=1)
    """).fetchone()
    print(f"   avg bid quoted = {r[0]:.1f}c   avg ask = {r[1]:.1f}c   "
          f"avg spread = {r[2]:.2f}c")
    print(f"   bid range: [{r[3]}, {r[4]}]   ask range: [{r[5]}, {r[6]}]")

    # 4 ─ Worst 20 filled rows
    print("\n[4] Worst 20 rows (biggest single-row loss)")
    print("-" * 80)
    print(f"{'ticker':<35} {'side_fills':<12} {'bid':>4} {'ask':>4} "
          f"{'wonYES':>7} {'pnl':>8}")
    rows = conn.execute("""
        SELECT ticker,
               shadow_bid_filled, shadow_ask_filled,
               proposed_bid_cents, proposed_ask_cents,
               ticker_settled_yes, shadow_pnl_cents
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND (shadow_bid_filled=1 OR shadow_ask_filled=1)
        ORDER BY shadow_pnl_cents ASC LIMIT 20
    """).fetchall()
    for r in rows:
        (ticker, bf, af, bid, ask, wy, pnl) = r
        sides = ("B" if bf else "-") + ("A" if af else "-")
        print(f"{ticker:<35} {sides:<12} {bid!s:>4} {ask!s:>4} "
              f"{wy!s:>7} {pnl!s:>8}")

    # 5 ─ Paired bid+ask rows: should capture spread (net positive)
    print("\n[5] Paired-fill rows only (both sides filled): should net ≈ +spread")
    print("-" * 80)
    r = conn.execute("""
        SELECT COUNT(*) AS n,
               SUM(shadow_pnl_cents) AS pnl_sum,
               AVG(shadow_pnl_cents) AS avg_pnl,
               AVG(proposed_ask_cents - proposed_bid_cents) AS avg_spread
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND shadow_bid_filled=1 AND shadow_ask_filled=1
    """).fetchone()
    if r[0]:
        print(f"   n={r[0]}  sum_pnl={r[1]}c  avg_pnl={r[2]:.1f}c/row  "
              f"avg_spread={r[3]:.2f}c")
        print(f"   Theoretical: avg_pnl ≈ (avg_spread * MM_ORDER_SIZE) - 2*fee")
        # Fee is ~0.035c/contract at low px; two-sided → ~0.07c/row
        print(f"   Expected for spread={r[3]:.2f}, size={MM_ORDER_SIZE}: "
              f"≈ {r[3] * MM_ORDER_SIZE:.1f}c - 2×fee")
    else:
        print("   no paired-fill rows")

    # 6 ─ Bid-only vs ask-only conditional on outcome
    print("\n[6] One-sided fills — loss conditional on 'wrong-side' settlement")
    print("-" * 80)
    for label, sql in [
        ("bid-only, wonYES=0 (bought YES, NO won)",
         "shadow_bid_filled=1 AND shadow_ask_filled=0 AND ticker_settled_yes=0"),
        ("bid-only, wonYES=1 (bought YES, YES won)",
         "shadow_bid_filled=1 AND shadow_ask_filled=0 AND ticker_settled_yes=1"),
        ("ask-only, wonYES=1 (bought NO, YES won)",
         "shadow_bid_filled=0 AND shadow_ask_filled=1 AND ticker_settled_yes=1"),
        ("ask-only, wonYES=0 (bought NO, NO won)",
         "shadow_bid_filled=0 AND shadow_ask_filled=1 AND ticker_settled_yes=0"),
    ]:
        r = conn.execute(f"""
            SELECT COUNT(*), SUM(shadow_pnl_cents), AVG(shadow_pnl_cents),
                   AVG(proposed_bid_cents), AVG(proposed_ask_cents)
            FROM weather_mm_shadow
            WHERE ts_settle_unix IS NOT NULL AND {sql}
        """).fetchone()
        n, pnl, avg, abid, aask = r
        if n:
            print(f"   {label:<48} n={n:>4d} "
                  f"sum={pnl or 0:>7d}c avg={avg or 0:>7.1f}c  "
                  f"avg_bid={abid or 0:.1f} avg_ask={aask or 0:.1f}")
        else:
            print(f"   {label:<48} n=0")

    # 7 ─ Duplicate check: same (ticker, ts_unix, side prices)
    print("\n[7] Duplicate row detection")
    print("-" * 80)
    r = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT ticker, ts_unix, proposed_bid_cents, proposed_ask_cents,
                   COUNT(*) AS c
            FROM weather_mm_shadow
            WHERE ts_settle_unix IS NOT NULL
            GROUP BY ticker, ts_unix, proposed_bid_cents, proposed_ask_cents
            HAVING c > 1
        )
    """).fetchone()[0]
    print(f"   rows with identical (ticker,ts_unix,bid,ask) tuple: {r}")

    # 8 ─ Do we have paired-row coverage at all, per series?
    print("\n[8] Paired-fill rate per series (both bid AND ask filled)")
    print("-" * 80)
    rows = conn.execute("""
        SELECT series,
               SUM(CASE WHEN shadow_bid_filled=1 AND shadow_ask_filled=1 THEN 1 ELSE 0 END) AS paired,
               SUM(CASE WHEN shadow_bid_filled=1 OR  shadow_ask_filled=1 THEN 1 ELSE 0 END) AS any_fill
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
        GROUP BY series ORDER BY series
    """).fetchall()
    for r in rows:
        series, paired, any_fill = r
        print(f"   {series:<12} paired={paired:>4d}  "
              f"any_fill={any_fill:>4d}  paired_rate={pct(paired, any_fill)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
