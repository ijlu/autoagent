#!/usr/bin/env python3
"""
Audit the directional pivot's viability using weather_mm_shadow as a
signal+price+outcome ground-truth dataset.

Why this instead of alpha_backtest:
  alpha_backtest is empty (the scanner is currently blind to non-parlay
  markets — unrelated blocking bug). But weather_mm_shadow has 27,720
  settled rows where each row captures:
    · fair_value_cents        ← our ensemble's P(YES) at that instant
    · market_yes_bid / ask    ← what Kalshi was quoting
    · ticker_settled_yes      ← actual outcome (back-filled today)
  That's everything we need to simulate a directional trade.

Model:
  For each ticker (grouped), walk rows oldest→newest. At each row compute
  edge = our P(our_side) - market P(our_side).  If edge >= MIN_EDGE, take
  ONE directional trade at the market ask (our side) with N contracts,
  and hold to settlement. (No intra-trade exits — matches 'entry is
  most of the alpha' thesis from the Apr 17 markout data.)
  Then skip to the next ticker: one trade per ticker, first qualifying
  opportunity. This approximates a 60s scanner that passes on tickers
  where it has an open position.

Fees:
  Taker fee on entry (we cross the spread), no exit fee (hold to
  settlement). Uses kalshi_taker_fee.

Blocklist:
  Honors DIRECTIONAL_BLOCKLIST from env — reports with and without to
  give the clean comparison.

Output: per-family and per-(family×side×MIN_EDGE) P&L.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Optional

sys.path.insert(0, ".")

from bot.core.money import kalshi_taker_fee
from bot.db import init_db

# Try multiple MIN_EDGE thresholds so we can see the curve. Current live
# MIN_EDGE is 0.105 per the daemon log; 0.05 is a generous floor; 0.08/0.10
# bracket the existing config.
MIN_EDGE_CANDIDATES = [0.05, 0.08, 0.10, 0.12]

# Fixed size for the simulation — matches current VPS MM_ORDER_SIZE.
# Directional would use kelly_contracts at real time; we use fixed size
# here for apples-to-apples comparison across candidates.
SIM_CONTRACTS = 3

# Directional blocklist (from .env).
BLOCKLIST = frozenset(
    s.strip().upper() for s in
    os.environ.get("DIRECTIONAL_BLOCKLIST", "KXBTC,KXETH,KXHIGHDEN").split(",")
    if s.strip()
)


def directional_pnl_cents(
    *, side: str, fill_price_cents: int, contracts: int, won_yes: bool,
) -> int:
    """Net P&L on a directional entry taken at market ask, held to settle.

    side='yes': won if won_yes; side='no': won if not won_yes.
    Subtracts taker fee at entry; no exit fee (settlement).
    """
    won = won_yes if side == "yes" else (not won_yes)
    settle = 100 if won else 0
    gross = (settle - fill_price_cents) * contracts
    fee = kalshi_taker_fee(contracts, fill_price_cents)
    return gross - fee


def main() -> int:
    conn = init_db()

    # Pull everything in one pass ordered by ticker+ts so we can do a
    # per-ticker first-qualifying-opportunity walk in Python.
    rows = conn.execute("""
        SELECT ticker, series, ts_unix, fair_value_cents,
               market_yes_bid, market_yes_ask, ticker_settled_yes
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND fair_value_cents IS NOT NULL
          AND market_yes_bid IS NOT NULL
          AND market_yes_ask IS NOT NULL
          AND ticker_settled_yes IS NOT NULL
        ORDER BY ticker, ts_unix ASC
    """).fetchall()

    print(f"Annotated rows with usable market snapshots: {len(rows)}")

    # Group by ticker
    per_ticker: dict[str, list] = defaultdict(list)
    for r in rows:
        per_ticker[r[0]].append(r)

    print(f"Distinct tickers: {len(per_ticker)}")
    print(f"BLOCKLIST = {sorted(BLOCKLIST)}")
    print()

    for min_edge in MIN_EDGE_CANDIDATES:
        print("=" * 80)
        print(f" MIN_EDGE = {min_edge:.3f}")
        print("=" * 80)

        # family → [n_trades, n_wins, sum_pnl, yes_trades, no_trades]
        stats = defaultdict(lambda: {
            "n": 0, "wins": 0, "pnl": 0, "yes": 0, "no": 0, "gross": 0, "fees": 0
        })
        blocked_stats = defaultdict(lambda: dict(stats["placeholder"]))
        skipped_blocklist = 0

        for ticker, ticker_rows in per_ticker.items():
            # Derive family from the ticker prefix: KXHIGHNY-26APR21-T75 → KXHIGHNY
            family = ticker.split("-")[0].upper()

            # Settlement is the same on every row for this ticker.
            won_yes = bool(ticker_rows[0][6])

            # Walk chronologically; take first row where edge ≥ threshold.
            taken = False
            for (_tk, _series, _ts, fv, m_bid, m_ask, _wy) in ticker_rows:
                if taken:
                    break
                # Our P(YES), market mid, market ask for YES, etc.
                our_p_yes = fv / 100.0
                mid = (m_bid + m_ask) / 2.0 / 100.0

                # Candidate sides:
                # YES side: we cross ask — pay m_ask. Edge = our_p_yes - mid.
                # NO side: we cross bid (sell YES = buy NO at 100-m_bid).
                #           Our P(NO) = 1 - our_p_yes.
                #           Market P(NO) = 1 - mid.
                #           Edge_no = (1 - our_p_yes) - (1 - mid) = mid - our_p_yes.
                edge_yes = our_p_yes - mid
                edge_no = mid - our_p_yes

                if edge_yes >= min_edge:
                    side = "yes"
                    fill_price = int(m_ask)
                elif edge_no >= min_edge:
                    side = "no"
                    fill_price = 100 - int(m_bid)
                else:
                    continue  # this row doesn't qualify; walk to next

                # Guard against degenerate fills at 1¢ or 99¢ (no edge to
                # capture there — would be infinite implied odds).
                if fill_price < 1 or fill_price > 99:
                    continue

                pnl = directional_pnl_cents(
                    side=side, fill_price_cents=fill_price,
                    contracts=SIM_CONTRACTS, won_yes=won_yes,
                )
                fee = kalshi_taker_fee(SIM_CONTRACTS, fill_price)
                gross = pnl + fee

                bucket = blocked_stats if family in BLOCKLIST else stats
                d = bucket[family]
                # lazy init: blocked_stats lambda returned a copy; ensure keys
                if "n" not in d:
                    d.update({"n": 0, "wins": 0, "pnl": 0,
                              "yes": 0, "no": 0, "gross": 0, "fees": 0})
                d["n"] += 1
                if pnl > 0:
                    d["wins"] += 1
                d["pnl"] += pnl
                d["gross"] += gross
                d["fees"] += fee
                d[side] += 1
                taken = True

        def _print_block(title, bucket):
            print(f"\n{title}")
            print(f"  {'family':<12} {'n':>5} {'wins':>5} {'wr':>6} "
                  f"{'gross':>8} {'fees':>6} {'pnl':>8} {'pnl/tr':>8} "
                  f"{'yes':>4} {'no':>4}")
            grand = {"n": 0, "wins": 0, "pnl": 0, "gross": 0,
                     "fees": 0, "yes": 0, "no": 0}
            for fam in sorted(bucket.keys()):
                d = bucket[fam]
                n = d["n"]
                if n == 0:
                    continue
                wr = d["wins"] / n
                ppt = d["pnl"] / n
                print(f"  {fam:<12} {n:>5d} {d['wins']:>5d} {wr:>6.1%} "
                      f"{d['gross']:>8d} {d['fees']:>6d} "
                      f"{d['pnl']:>8d} {ppt:>8.1f} "
                      f"{d['yes']:>4d} {d['no']:>4d}")
                for k in grand:
                    grand[k] += d[k]
            if grand["n"]:
                ppt_g = grand["pnl"] / grand["n"]
                print(f"  {'TOTAL':<12} {grand['n']:>5d} {grand['wins']:>5d} "
                      f"{grand['wins']/grand['n']:>6.1%} "
                      f"{grand['gross']:>8d} {grand['fees']:>6d} "
                      f"{grand['pnl']:>8d} {ppt_g:>8.1f} "
                      f"{grand['yes']:>4d} {grand['no']:>4d}")

        _print_block("[allowed families]", stats)
        _print_block("[blocked families]  (DIRECTIONAL_BLOCKLIST, reference only)",
                     blocked_stats)

    print()
    print("Notes:")
    print("  · One trade per ticker, first row where edge ≥ MIN_EDGE.")
    print(f"  · Size = {SIM_CONTRACTS} contracts, matches VPS MM_ORDER_SIZE.")
    print("  · Fee = kalshi_taker_fee on entry; hold to settlement (no exit fee).")
    print("  · 'pnl' is NET of fees; 'gross' excludes fees.")
    print("  · Blocked families are reported for reference — excluded from live go.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
