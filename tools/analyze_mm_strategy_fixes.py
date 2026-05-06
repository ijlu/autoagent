#!/usr/bin/env python3
"""
Evaluate two proposed fixes against real shadow data:

  (a) Mid-probability bucket filter — restrict quoting to FV ∈ [lo, hi]
      at quote time. Recompute gate (n_fills, pnl/fill) at several
      candidate bands and report which bands pass the SHADOW→CANARY gate.

  (b) Requote-lag analysis — for each adverse fill, measure time-to-next
      shadow-write on the same ticker, and whether that next write
      changed the quoted prices. Catches "stale quote, not requoted"
      vs "requote chain was ≤X seconds" failure modes.

Read-only: does not write to DB. Designed to run against the backfilled
weather_mm_shadow (27,720 rows annotated 2026-04-22).
"""
from __future__ import annotations

import sys
from collections import defaultdict, Counter
from statistics import mean, median

sys.path.insert(0, ".")

from bot.config import MM_ORDER_SIZE
from bot.db import init_db

GATE_MIN_N = 5              # MM_SIZING_MIN_N
GATE_MIN_PNL_PER_FILL = 1.0  # MM_CANARY_MIN_PNL_PER_FILL_CENTS

# Mid-bucket filter candidates (fv_lo, fv_hi) in cents, where fv_mid is
# inferred from (proposed_bid + proposed_ask) / 2.
FILTER_BANDS = [
    (0, 100),    # no filter (baseline)
    (20, 80),
    (25, 75),
    (30, 70),
    (35, 65),
    (40, 60),
]


# ═══════════════════════════════════════════════════════════════════════
# Part A — Mid-bucket filter
# ═══════════════════════════════════════════════════════════════════════

def part_a_mid_bucket_filter(conn) -> None:
    print("=" * 80)
    print(" PART A — Mid-probability bucket filter")
    print("=" * 80)
    print(f"Gate: n_fills ≥ {GATE_MIN_N}  AND  pnl/fill ≥ +{GATE_MIN_PNL_PER_FILL}c")
    print(f"fv_mid = (proposed_bid_cents + proposed_ask_cents) / 2")
    print()

    rows = conn.execute("""
        SELECT series, proposed_bid_cents, proposed_ask_cents,
               shadow_bid_filled, shadow_ask_filled, shadow_pnl_cents,
               ticker_settled_yes
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
    """).fetchall()
    print(f"Annotated rows under analysis: {len(rows)}\n")

    # Per-series, per-band aggregates
    by_band: dict[tuple[int, int], dict[str, dict]] = {}
    for band in FILTER_BANDS:
        by_band[band] = defaultdict(
            lambda: {"n_total": 0, "n_fills": 0, "pnl_cents": 0,
                     "bid_only_wins": 0, "ask_only_wins": 0,
                     "bid_only_loss": 0, "ask_only_loss": 0}
        )

    for (series, bid_c, ask_c, bf, af, pnl, wy) in rows:
        if bid_c is None or ask_c is None:
            continue
        fv_mid = (bid_c + ask_c) / 2.0
        filled = (bf or 0) + (af or 0) > 0
        for (lo, hi) in FILTER_BANDS:
            if not (lo <= fv_mid < hi):
                continue
            d = by_band[(lo, hi)][series]
            d["n_total"] += 1
            if filled:
                d["n_fills"] += 1
                d["pnl_cents"] += int(pnl or 0)
                # Classify by outcome × side
                is_win = (pnl or 0) > 0
                if bf and not af:
                    (d["bid_only_wins"] if is_win else d["bid_only_loss"]).__iadd__ \
                        if False else None
                    if is_win: d["bid_only_wins"] += 1
                    else:      d["bid_only_loss"] += 1
                elif af and not bf:
                    if is_win: d["ask_only_wins"] += 1
                    else:      d["ask_only_loss"] += 1

    for band in FILTER_BANDS:
        lo, hi = band
        tag = "[baseline]" if band == (0, 100) else ""
        print(f"─── FV band [{lo}, {hi}]  {tag} "
              f"{'─' * (65 - len(tag))}")
        d_all = by_band[band]
        series_list = sorted(d_all.keys())
        grand_fills = 0
        grand_pnl = 0
        print(f"  {'series':<12} {'n_quotes':>8} {'n_fills':>7} "
              f"{'pnl':>7} {'pnl/fill':>9} {'gate':>5}")
        for s in series_list:
            d = d_all[s]
            nf = d["n_fills"]
            ppf = d["pnl_cents"] / nf if nf else 0.0
            passes = (nf >= GATE_MIN_N and ppf >= GATE_MIN_PNL_PER_FILL)
            grand_fills += nf
            grand_pnl += d["pnl_cents"]
            print(f"  {s:<12} {d['n_total']:>8d} {nf:>7d} "
                  f"{d['pnl_cents']:>7d} {ppf:>9.1f} "
                  f"{'PASS' if passes else 'FAIL':>5}")
        grand_ppf = grand_pnl / grand_fills if grand_fills else 0.0
        print(f"  {'TOTAL':<12} {'':>8} {grand_fills:>7d} "
              f"{grand_pnl:>7d} {grand_ppf:>9.1f}")
        print()


# ═══════════════════════════════════════════════════════════════════════
# Part B — Requote-lag analysis
# ═══════════════════════════════════════════════════════════════════════

def part_b_requote_lag(conn) -> None:
    print("=" * 80)
    print(" PART B — Requote lag on adverse fills")
    print("=" * 80)
    print(
        "For each row with an adverse fill (pnl < 0), find the NEXT shadow\n"
        "row on the same ticker after the fill_ts. Measure:\n"
        "  · time_to_requote (seconds)\n"
        "  · did the quote move? (bid/ask prices changed)\n"
        "Thesis: if time_to_requote is long OR prices didn't change, the\n"
        "quoter is leaving stale quotes on the book after adverse selection\n"
        "— which the data shows gets hit multiple times on the same bracket.\n"
    )

    # Pull all annotated-adverse rows with their fill timestamps.
    adverse = conn.execute("""
        SELECT id, ticker, ts_unix,
               proposed_bid_cents, proposed_ask_cents,
               shadow_bid_filled, shadow_ask_filled,
               shadow_bid_fill_ts_unix, shadow_ask_fill_ts_unix,
               shadow_pnl_cents
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND shadow_pnl_cents < 0
          AND (shadow_bid_filled = 1 OR shadow_ask_filled = 1)
        ORDER BY ticker, ts_unix
    """).fetchall()

    # Pull the full per-ticker shadow series (prices + timestamps). We
    # use this to find the next quote after each adverse fill.
    per_ticker: dict[str, list[tuple[float, int | None, int | None]]] = \
        defaultdict(list)
    all_rows = conn.execute("""
        SELECT ticker, ts_unix, proposed_bid_cents, proposed_ask_cents
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
        ORDER BY ticker, ts_unix
    """).fetchall()
    for t, ts, b, a in all_rows:
        per_ticker[t].append((float(ts), b, a))

    # Histograms
    time_buckets = [0, 5, 10, 15, 30, 60, 120, 300, 10**9]
    time_hist_moved = Counter()
    time_hist_stale = Counter()
    total_adverse = 0
    no_successor = 0
    successor_lags_s = []

    same_bracket_hits: dict[str, int] = Counter()
    same_bracket_adverse_pnl: dict[str, int] = defaultdict(int)

    for (rid, ticker, ts0, bid0, ask0, bf, af, bts, ats, pnl) in adverse:
        total_adverse += 1
        # Pick the relevant fill_ts (whichever side was hit; if both,
        # earliest). For adverse-bid fills only bid hits; analogous ask.
        fill_ts_candidates = [t for t in (bts, ats) if t is not None]
        if not fill_ts_candidates:
            # Shouldn't happen — fill flag is 1 but no ts. Skip.
            continue
        fill_ts = min(fill_ts_candidates)
        same_bracket_hits[ticker] += 1
        same_bracket_adverse_pnl[ticker] += int(pnl or 0)

        # Find next shadow row on same ticker after fill_ts
        series = per_ticker[ticker]
        successor = None
        for (ts_i, b_i, a_i) in series:
            if ts_i > fill_ts:
                successor = (ts_i, b_i, a_i)
                break
        if successor is None:
            no_successor += 1
            continue
        ts1, bid1, ask1 = successor
        lag = ts1 - fill_ts
        successor_lags_s.append(lag)
        moved = (bid1 != bid0) or (ask1 != ask0)
        # Bucketize
        bi = 0
        for i in range(len(time_buckets) - 1):
            if time_buckets[i] <= lag < time_buckets[i + 1]:
                bi = i; break
        label = f"[{time_buckets[bi]},{time_buckets[bi+1]})"
        if moved:
            time_hist_moved[label] += 1
        else:
            time_hist_stale[label] += 1

    print(f"Adverse filled rows: {total_adverse}")
    print(f"No successor row (quote was last on ticker): {no_successor}")
    if successor_lags_s:
        print(f"Successor-lag stats (seconds):")
        print(f"  min={min(successor_lags_s):.1f}  "
              f"median={median(successor_lags_s):.1f}  "
              f"mean={mean(successor_lags_s):.1f}  "
              f"max={max(successor_lags_s):.1f}")
    print()
    print("Histogram: time-from-fill to next shadow-write on same ticker")
    print(f"  {'bucket (s)':<18} {'quote_moved':>12} {'stale_quote':>12} {'total':>8}")
    for i in range(len(time_buckets) - 1):
        label = f"[{time_buckets[i]},{time_buckets[i+1]})"
        m = time_hist_moved[label]
        s = time_hist_stale[label]
        if m + s == 0:
            continue
        print(f"  {label:<18} {m:>12d} {s:>12d} {m+s:>8d}")
    print(f"  {'TOTAL':<18} {sum(time_hist_moved.values()):>12d} "
          f"{sum(time_hist_stale.values()):>12d}")

    # Repeated-adverse same bracket
    print()
    print("Per-ticker adverse-fill repetition (tickers hit 2+ times)")
    print(f"  {'ticker':<35} {'hits':>5} {'sum_pnl':>8}")
    tickers_repeat = sorted(
        [(t, n) for t, n in same_bracket_hits.items() if n >= 2],
        key=lambda x: -x[1],
    )[:25]
    for t, n in tickers_repeat:
        print(f"  {t:<35} {n:>5d} {same_bracket_adverse_pnl[t]:>8d}")
    total_repeat_hits = sum(n for _, n in same_bracket_hits.items() if n >= 2)
    total_repeat_pnl = sum(
        same_bracket_adverse_pnl[t] for t, n in same_bracket_hits.items()
        if n >= 2
    )
    print(f"\n  Rows from tickers hit 2+ times: {total_repeat_hits} of {total_adverse}")
    print(f"  Adverse P&L from repeat-hit tickers: {total_repeat_pnl}c")


def main():
    conn = init_db()
    print(f"MM_ORDER_SIZE = {MM_ORDER_SIZE}\n")
    part_a_mid_bucket_filter(conn)
    print("\n")
    part_b_requote_lag(conn)


if __name__ == "__main__":
    main()
