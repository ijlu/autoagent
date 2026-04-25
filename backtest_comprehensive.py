#!/usr/bin/env python3
"""Comprehensive historical backtest — answers 'do the signals have alpha?'

Analyzes every decision the bot has ever made against actual outcomes.
Computes calibration curves, source accuracy, edge-vs-win-rate, family
performance, MM profitability, and statistical significance tests.

Run on VPS (has DB + API access):
    python3 backtest_comprehensive.py

Run locally (needs kalshi_trades.db or kalshi_trades_live.db):
    python3 backtest_comprehensive.py --db kalshi_trades_live.db

Outputs:
    - Structured text report to stdout
    - JSON summary to backtest_results.json (for programmatic use)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from math import ceil, sqrt
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Fee formulas (must match bot/core/money.py exactly)
# ─────────────────────────────────────────────────────────────────────────────

def kalshi_maker_fee(contracts: int, price_cents: int) -> int:
    """Exact Kalshi maker fee: ceil(0.0175 * C * P * (1-P)) in cents."""
    p = price_cents / 100
    raw = 0.0175 * contracts * p * (1 - p) * 100
    return ceil(raw) if raw > 0 else 0


def kalshi_taker_fee(contracts: int, price_cents: int) -> int:
    """Exact Kalshi taker fee: ceil(0.07 * C * P * (1-P)) in cents."""
    p = price_cents / 100
    raw = 0.07 * contracts * p * (1 - p) * 100
    return ceil(raw) if raw > 0 else 0


# ─────────────────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────────────────

def family_of(ticker: str) -> str:
    """Extract market family prefix from ticker."""
    if "-" in ticker:
        return ticker.split("-")[0]
    return ticker[:6].rstrip("-")


def prob_bucket(p: float, n_buckets: int = 10) -> str:
    """Map probability to calibration bucket string."""
    b = min(n_buckets - 1, int(p * n_buckets))
    lo = b / n_buckets
    hi = (b + 1) / n_buckets
    return f"{lo:.1f}-{hi:.1f}"


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p_hat = wins / n
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    spread = z * sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n) / denom
    return (max(0, centre - spread), min(1, centre + spread))


def brier_score(predictions: list[tuple[float, int]]) -> float:
    """Brier score: mean squared error of predicted probabilities vs outcomes.
    Lower is better. Random = 0.25 for balanced outcomes."""
    if not predictions:
        return 1.0
    return sum((p - o) ** 2 for p, o in predictions) / len(predictions)


def bar(value: float, width: int = 30) -> str:
    """ASCII bar chart helper."""
    filled = int(value * width)
    return "█" * filled + "░" * (width - filled)


def fmt_pnl(cents: int) -> str:
    """Format P&L in dollars with sign."""
    return f"${cents / 100:+.2f}"


def pct(n: int, d: int) -> str:
    """Format percentage with safety."""
    return f"{100 * n / d:.1f}%" if d > 0 else "N/A"


# ═════════════════════════════════════════════════════════════════════════════
# Section 1: Ensemble Calibration Analysis
# ═════════════════════════════════════════════════════════════════════════════

def analyze_calibration(conn) -> dict:
    """Do our ensemble probabilities match actual outcomes?

    A well-calibrated model should have:
    - Events predicted at 70% actually happening 70% of the time
    - Brier score < 0.25 (better than random)
    """
    print(f"\n{'═' * 72}")
    print(f" 1. ENSEMBLE CALIBRATION ANALYSIS")
    print(f"{'═' * 72}")

    results = {"brier_score": None, "buckets": {}, "n_samples": 0}

    # Method 1: Use calibration table (directional trades with est_prob + outcome)
    cal_rows = conn.execute("""
        SELECT estimated_prob, actual_outcome FROM calibration
    """).fetchall()

    # Method 2: Use opportunity_log joined with settlements
    # CRITICAL #1: won=1 means "we profited", NOT "YES happened".
    # Must use settlement side to convert: side=yes,won=1→YES; side=no,won=1→NO
    # CRITICAL #2: opportunity_log.ensemble_prob is stored as P(our chosen side),
    # NOT P(YES). For side='no' rows it's actually (1 - P(YES)). Normalize here.
    opp_rows = conn.execute("""
        SELECT CASE WHEN o.side='no' THEN 1.0 - o.ensemble_prob ELSE o.ensemble_prob END AS yes_prob,
               s.won, o.ticker, o.strategy, o.sources_json, s.side
        FROM opportunity_log o
        JOIN settlements s ON o.ticker = s.ticker
        WHERE o.ensemble_prob IS NOT NULL
          AND o.action IN ('trade', 'candidate', 'buy', 'sell')
          AND o.skip_reason IS NULL
    """).fetchall()

    # Method 3: Use mm_orders fair_value_cents vs settlements
    # CRITICAL: fair_value_cents is stored per ORDER SIDE (YES orders store YES-prob,
    # NO orders store NO-prob = 100 - YES-prob). Naively averaging across sides
    # inverts probability on tickers where we quoted both sides. Normalize to
    # YES-probability BEFORE averaging: yes_fv = fv; no_fv → (100 - fv).
    mm_rows = conn.execute("""
        SELECT m.ticker,
               AVG(CASE WHEN m.side='yes' THEN m.fair_value_cents
                        ELSE 100 - m.fair_value_cents END) AS avg_yes_fv,
               s.side, s.won
        FROM mm_orders m
        JOIN settlements s ON m.ticker = s.ticker
        WHERE m.fair_value_cents > 0 AND m.fill_qty > 0
          AND m.side IN ('yes', 'no')
        GROUP BY m.ticker
    """).fetchall()

    # Combine all sources of calibration data
    predictions = []  # (predicted_prob_of_yes, actual_yes)

    # Helper: convert (settlement_side, won) → did YES actually happen?
    def won_to_actual_yes(settle_side: str, won: int) -> int:
        """Convert 'did we profit' + 'our side' → 'did YES happen'.

        side=yes, won=1 → YES happened (we held YES, profited)
        side=yes, won=0 → NO happened (we held YES, lost)
        side=no,  won=1 → NO happened (we held NO, profited)
        side=no,  won=0 → YES happened (we held NO, lost)
        """
        if settle_side == "yes":
            return int(won)
        else:
            return 1 - int(won)

    # From calibration table
    for est_prob, outcome in cal_rows:
        if est_prob is not None and outcome is not None:
            predictions.append((est_prob, int(outcome)))

    # From opportunity_log — deduplicate by ticker (take latest)
    opp_by_ticker = {}
    for ep, won, ticker, strat, sources, settle_side in opp_rows:
        if ep is not None and won is not None and settle_side:
            actual_yes = won_to_actual_yes(settle_side, int(won))
            opp_by_ticker[ticker] = (ep, actual_yes)
    for ticker, (ep, actual_yes) in opp_by_ticker.items():
        predictions.append((ep, actual_yes))

    # From MM orders (use avg fair_value_cents / 100 as P(YES))
    mm_by_ticker = {}
    for ticker, avg_fv, settle_side, won in mm_rows:
        if avg_fv and avg_fv > 0 and settle_side:
            fv_prob = avg_fv / 100.0
            actual_yes = won_to_actual_yes(settle_side, int(won))
            mm_by_ticker[ticker] = (fv_prob, actual_yes)

    for ticker, (fv_prob, actual_yes) in mm_by_ticker.items():
        predictions.append((fv_prob, actual_yes))

    if not predictions:
        print("\n  ⚠ No calibration data available. Need trades with known outcomes.")
        print("  The bot needs more settlements to build calibration data.")
        return results

    results["n_samples"] = len(predictions)

    # Compute Brier score
    bs = brier_score(predictions)
    results["brier_score"] = round(bs, 4)

    print(f"\n  Calibration data points: {len(predictions)}")
    print(f"  Brier score: {bs:.4f} (random=0.250, perfect=0.000)")
    if bs < 0.20:
        print(f"  ✓ Better than random — signals have informational content")
    elif bs < 0.25:
        print(f"  ~ Marginal — near-random performance")
    else:
        print(f"  ✗ Worse than random — signals are anti-predictive")

    # Bucket analysis
    buckets = defaultdict(lambda: {"total": 0, "yes": 0, "sum_pred": 0.0})
    for pred, actual in predictions:
        b = prob_bucket(pred)
        buckets[b]["total"] += 1
        buckets[b]["yes"] += actual
        buckets[b]["sum_pred"] += pred

    print(f"\n  {'Bucket':>10s}  {'N':>5s}  {'Predicted':>10s}  {'Actual':>8s}  {'Diff':>7s}  {'Bar'}")
    print(f"  {'-' * 10}  {'-' * 5}  {'-' * 10}  {'-' * 8}  {'-' * 7}  {'-' * 30}")

    for b_key in sorted(buckets.keys()):
        d = buckets[b_key]
        if d["total"] < 1:
            continue
        avg_pred = d["sum_pred"] / d["total"]
        actual_rate = d["yes"] / d["total"]
        diff = actual_rate - avg_pred
        ci_lo, ci_hi = wilson_ci(d["yes"], d["total"])
        calibrated = "✓" if ci_lo <= avg_pred <= ci_hi else "✗"
        print(f"  {b_key:>10s}  {d['total']:>5d}  {avg_pred:>10.1%}  {actual_rate:>7.1%}  "
              f"{diff:>+6.1%}  {bar(actual_rate)}  {calibrated}")
        results["buckets"][b_key] = {
            "n": d["total"], "predicted": round(avg_pred, 3),
            "actual": round(actual_rate, 3), "diff": round(diff, 3)
        }

    # Calibration error (mean absolute difference between predicted and actual per bucket)
    weighted_errors = []
    for b_key, d in buckets.items():
        if d["total"] >= 2:
            avg_pred = d["sum_pred"] / d["total"]
            actual_rate = d["yes"] / d["total"]
            weighted_errors.append((abs(actual_rate - avg_pred), d["total"]))

    if weighted_errors:
        ece = sum(e * n for e, n in weighted_errors) / sum(n for _, n in weighted_errors)
        results["ece"] = round(ece, 4)
        print(f"\n  Expected Calibration Error (ECE): {ece:.4f}")
        print(f"  (0 = perfectly calibrated, higher = more miscalibrated)")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 2: Edge vs Win Rate
# ═════════════════════════════════════════════════════════════════════════════

def analyze_edge_vs_winrate(conn) -> dict:
    """Does higher edge actually predict higher win rate?

    This is the single most important question for directional trading.
    If edge doesn't predict outcomes, the ensemble has no alpha.
    """
    print(f"\n{'═' * 72}")
    print(f" 2. EDGE VS WIN RATE")
    print(f"{'═' * 72}")

    results = {"edge_buckets": {}, "correlation": None}

    # Get trades with edge and outcome (deduplicate per ticker)
    rows = conn.execute("""
        SELECT o.edge, s.won, o.strategy, o.ticker
        FROM opportunity_log o
        JOIN settlements s ON o.ticker = s.ticker
        WHERE o.edge IS NOT NULL AND o.action IN ('trade', 'candidate', 'buy', 'sell')
          AND o.skip_reason IS NULL
        GROUP BY o.ticker
    """).fetchall()

    # Also from MM: avg edge per ticker (not per fill — avoids counting same settlement N times)
    # fv-mixed-side-ok: storage is P(YES) on both rows; AVG returns P(YES)
    # which is what the per-ticker edge computation here expects
    # (CLAUDE.md Known Bug Pattern #13).
    mm_rows = conn.execute("""
        SELECT AVG(m.fair_value_cents), AVG(m.price_cents), s.won, m.ticker
        FROM mm_orders m
        JOIN settlements s ON m.ticker = s.ticker
        WHERE m.fair_value_cents > 0 AND m.fill_qty > 0
        GROUP BY m.ticker
    """).fetchall()

    # Compute edge data (one point per ticker)
    edge_data = []  # (edge, won)
    seen_tickers = set()
    for edge, won, strat, ticker in rows:
        if edge is not None and won is not None:
            edge_data.append((abs(float(edge)), int(won)))
            seen_tickers.add(ticker)

    for avg_fv, avg_price, won, ticker in mm_rows:
        if ticker in seen_tickers:
            continue  # already counted from opportunity_log
        if avg_fv and avg_price and avg_fv > 0 and avg_price > 0:
            # Edge = |fair_value - fill_price| / 100
            e = abs(avg_fv - avg_price) / 100
            edge_data.append((e, int(won)))

    if not edge_data:
        print("\n  ⚠ No edge data available. Need trades with edge values and outcomes.")
        return results

    # Bucket by edge magnitude
    edge_buckets = defaultdict(lambda: {"total": 0, "wins": 0})
    for edge, won in edge_data:
        if edge < 0.03:
            b = "0-3%"
        elif edge < 0.05:
            b = "3-5%"
        elif edge < 0.07:
            b = "5-7%"
        elif edge < 0.10:
            b = "7-10%"
        elif edge < 0.15:
            b = "10-15%"
        elif edge < 0.20:
            b = "15-20%"
        else:
            b = "20%+"
        edge_buckets[b]["total"] += 1
        edge_buckets[b]["wins"] += won

    print(f"\n  Total data points: {len(edge_data)}")
    overall_wr = sum(w for _, w in edge_data) / len(edge_data)
    print(f"  Overall win rate: {overall_wr:.1%}")

    print(f"\n  {'Edge':>8s}  {'N':>5s}  {'Wins':>5s}  {'WR':>6s}  {'CI 95%':>14s}  {'Bar'}")
    print(f"  {'-' * 8}  {'-' * 5}  {'-' * 5}  {'-' * 6}  {'-' * 14}  {'-' * 30}")

    bucket_order = ["0-3%", "3-5%", "5-7%", "7-10%", "10-15%", "15-20%", "20%+"]
    win_rates = []
    for b in bucket_order:
        d = edge_buckets.get(b)
        if not d or d["total"] == 0:
            continue
        wr = d["wins"] / d["total"]
        ci_lo, ci_hi = wilson_ci(d["wins"], d["total"])
        win_rates.append(wr)
        print(f"  {b:>8s}  {d['total']:>5d}  {d['wins']:>5d}  {wr:>5.1%}  "
              f"[{ci_lo:.1%}-{ci_hi:.1%}]  {bar(wr)}")
        results["edge_buckets"][b] = {
            "n": d["total"], "wins": d["wins"],
            "wr": round(wr, 3), "ci": [round(ci_lo, 3), round(ci_hi, 3)]
        }

    # Check monotonicity — does higher edge → higher win rate?
    if len(win_rates) >= 3:
        increases = sum(1 for i in range(1, len(win_rates)) if win_rates[i] > win_rates[i - 1])
        monotonic = increases / (len(win_rates) - 1)
        results["monotonicity"] = round(monotonic, 2)
        if monotonic >= 0.6:
            print(f"\n  ✓ Higher edge → higher win rate (monotonicity: {monotonic:.0%})")
            print(f"    This means the ensemble estimates have genuine predictive power.")
        else:
            print(f"\n  ✗ Edge does NOT predict win rate (monotonicity: {monotonic:.0%})")
            print(f"    The 'edge' signal may not represent real alpha.")

    # Breakeven analysis — what minimum edge is needed to be profitable?
    for b in bucket_order:
        d = edge_buckets.get(b)
        if d and d["total"] >= 5:
            wr = d["wins"] / d["total"]
            if wr > 0.5:
                print(f"\n  Breakeven edge: ~{b} (first bucket above 50% WR)")
                results["breakeven_edge"] = b
                break
    else:
        print(f"\n  ⚠ No edge bucket achieved >50% win rate")
        results["breakeven_edge"] = None

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 3: Source Accuracy by Category
# ═════════════════════════════════════════════════════════════════════════════

def analyze_source_accuracy(conn) -> dict:
    """Which data sources are actually predictive?

    Parses sources_json from opportunity_log to identify which sources
    contributed to winning vs losing predictions.
    """
    print(f"\n{'═' * 72}")
    print(f" 3. SOURCE ACCURACY ANALYSIS")
    print(f"{'═' * 72}")

    results = {"sources": {}, "combos": {}}

    rows = conn.execute("""
        SELECT o.sources_json, s.won, o.ensemble_prob, o.market_prob, o.ticker
        FROM opportunity_log o
        JOIN settlements s ON o.ticker = s.ticker
        WHERE o.sources_json IS NOT NULL
          AND o.action IN ('trade', 'candidate', 'buy', 'sell')
          AND o.skip_reason IS NULL
    """).fetchall()

    if not rows:
        print("\n  ⚠ No source data available.")
        return results

    # Parse source contributions (one entry per ticker from opportunity_log)
    source_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_cents": 0})
    combo_stats = defaultdict(lambda: {"total": 0, "wins": 0})

    for src_json, won, ep, mp, ticker in rows:
        if not src_json:
            continue
        # Parse "ensemble(poly+weather+metar+...)" format
        sources = set()
        if "ensemble(" in src_json:
            inner = src_json.split("ensemble(")[1].rstrip(")")
            for s in inner.split("+"):
                s = s.strip().lower()
                if s:
                    sources.add(s)
        elif ":" in src_json:
            sources.add(src_json.split(":")[0].strip().lower())
        else:
            sources.add(src_json.strip().lower())

        combo_key = "+".join(sorted(sources))
        combo_stats[combo_key]["total"] += 1
        if won:
            combo_stats[combo_key]["wins"] += 1

        for s in sources:
            source_stats[s]["total"] += 1
            if won:
                source_stats[s]["wins"] += 1

    # Print per-source stats
    if source_stats:
        print(f"\n  Individual source accuracy (when present in ensemble):")
        print(f"  {'Source':>15s}  {'N':>5s}  {'Wins':>5s}  {'WR':>6s}  {'CI 95%':>14s}")
        print(f"  {'-' * 15}  {'-' * 5}  {'-' * 5}  {'-' * 6}  {'-' * 14}")

        for src, d in sorted(source_stats.items(), key=lambda x: -x[1]["total"]):
            if d["total"] < 2:
                continue
            wr = d["wins"] / d["total"]
            ci_lo, ci_hi = wilson_ci(d["wins"], d["total"])
            star = " ✓" if wr > 0.5 and ci_lo > 0.35 else (" ✗" if wr < 0.3 else "")
            print(f"  {src:>15s}  {d['total']:>5d}  {d['wins']:>5d}  {wr:>5.1%}  "
                  f"[{ci_lo:.1%}-{ci_hi:.1%}]{star}")
            results["sources"][src] = {
                "n": d["total"], "wins": d["wins"],
                "wr": round(wr, 3), "ci": [round(ci_lo, 3), round(ci_hi, 3)]
            }

    # Print source combination stats
    if combo_stats:
        print(f"\n  Source combination accuracy (top 15):")
        print(f"  {'Combination':>35s}  {'N':>5s}  {'WR':>6s}")
        print(f"  {'-' * 35}  {'-' * 5}  {'-' * 6}")

        for combo, d in sorted(combo_stats.items(), key=lambda x: -x[1]["total"])[:15]:
            if d["total"] < 2:
                continue
            wr = d["wins"] / d["total"]
            print(f"  {combo:>35s}  {d['total']:>5d}  {wr:>5.1%}")
            results["combos"][combo] = {"n": d["total"], "wr": round(wr, 3)}

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 4: Per-Family Performance
# ═════════════════════════════════════════════════════════════════════════════

def analyze_family_performance(conn) -> dict:
    """Which market families make/lose money?"""
    print(f"\n{'═' * 72}")
    print(f" 4. PERFORMANCE BY MARKET FAMILY")
    print(f"{'═' * 72}")

    results = {"families": {}}

    settlements = conn.execute("""
        SELECT ticker, side, price_cents, contracts, profit_cents, won, strategy
        FROM settlements ORDER BY profit_cents
    """).fetchall()

    if not settlements:
        print("\n  ⚠ No settlement data.")
        return results

    family_data = defaultdict(lambda: {
        "total": 0, "wins": 0, "pnl_cents": 0,
        "contracts": 0, "max_loss": 0, "max_win": 0,
        "strategies": defaultdict(int)
    })

    for ticker, side, price, contracts, profit, won, strat in settlements:
        fam = family_of(ticker)
        fd = family_data[fam]
        fd["total"] += 1
        fd["wins"] += int(won)
        fd["pnl_cents"] += profit
        fd["contracts"] += contracts
        fd["max_loss"] = min(fd["max_loss"], profit)
        fd["max_win"] = max(fd["max_win"], profit)
        fd["strategies"][strat or "unknown"] += 1

    total_pnl = sum(d["pnl_cents"] for d in family_data.values())
    total_wins = sum(d["wins"] for d in family_data.values())
    total_n = sum(d["total"] for d in family_data.values())

    print(f"\n  Total settlements: {total_n}")
    print(f"  Overall win rate: {pct(total_wins, total_n)}")
    print(f"  Overall P&L: {fmt_pnl(total_pnl)}")

    print(f"\n  {'Family':>12s}  {'N':>4s}  {'Wins':>4s}  {'WR':>6s}  {'P&L':>10s}  "
          f"{'MaxLoss':>9s}  {'MaxWin':>9s}  {'Strat'}")
    print(f"  {'-' * 12}  {'-' * 4}  {'-' * 4}  {'-' * 6}  {'-' * 10}  "
          f"{'-' * 9}  {'-' * 9}  {'-' * 20}")

    for fam, d in sorted(family_data.items(), key=lambda x: x[1]["pnl_cents"]):
        wr = d["wins"] / d["total"] if d["total"] > 0 else 0
        main_strat = max(d["strategies"].items(), key=lambda x: x[1])[0] if d["strategies"] else "?"
        print(f"  {fam:>12s}  {d['total']:>4d}  {d['wins']:>4d}  {wr:>5.0%}  "
              f"{fmt_pnl(d['pnl_cents']):>10s}  {fmt_pnl(d['max_loss']):>9s}  "
              f"{fmt_pnl(d['max_win']):>9s}  {main_strat}")
        results["families"][fam] = {
            "n": d["total"], "wins": d["wins"], "wr": round(wr, 3),
            "pnl_cents": d["pnl_cents"],
            "pnl_dollars": round(d["pnl_cents"] / 100, 2)
        }

    # Which families should we avoid?
    print(f"\n  Recommendation:")
    for fam, d in sorted(family_data.items(), key=lambda x: x[1]["pnl_cents"]):
        if d["total"] >= 5 and d["wins"] / d["total"] < 0.15 and d["pnl_cents"] < -100:
            print(f"  ✗ AVOID {fam}: {pct(d['wins'], d['total'])} WR, {fmt_pnl(d['pnl_cents'])} P&L")
        elif d["total"] >= 5 and d["wins"] / d["total"] > 0.40:
            print(f"  ✓ FOCUS {fam}: {pct(d['wins'], d['total'])} WR, {fmt_pnl(d['pnl_cents'])} P&L")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 5: MM-Specific Analysis
# ═════════════════════════════════════════════════════════════════════════════

def analyze_mm_performance(conn) -> dict:
    """Detailed market-making profitability analysis.

    Key questions:
    - Are our spreads wide enough to cover adverse selection?
    - What's our realized P&L per contract after fees?
    - Which families are profitable for MM?
    """
    print(f"\n{'═' * 72}")
    print(f" 5. MARKET MAKING DEEP DIVE")
    print(f"{'═' * 72}")

    results = {
        "total_fills": 0, "total_contracts": 0,
        "adverse_selection": {}, "spread_analysis": {},
        "family_mm": {}
    }

    # ── Fill statistics ──
    fills = conn.execute("""
        SELECT timestamp, ticker, side, price_cents, contracts, fill_qty,
               fair_value_cents, tag
        FROM mm_orders WHERE fill_qty > 0
        ORDER BY timestamp
    """).fetchall()

    if not fills:
        print("\n  ⚠ No MM fills found.")
        return results

    results["total_fills"] = len(fills)
    total_contracts = sum(f[5] for f in fills)
    results["total_contracts"] = total_contracts

    print(f"\n  Total fills: {len(fills)}")
    print(f"  Total contracts: {total_contracts}")

    # ── Adverse selection analysis (markout) ──
    print(f"\n  ─── Adverse Selection (Fill Price vs Fair Value) ───")

    markouts = []  # (markout_cents, ticker, side)
    for ts, ticker, side, price, contracts, fill_qty, fv, tag in fills:
        if not fv or fv <= 0:
            continue
        if side == "yes":
            markout = fv - price  # positive = bought below fair value (good)
        else:
            markout = (100 - fv) - price  # NO price = 100 - YES_fv
        markouts.append((markout, ticker, side))

    if markouts:
        avg_markout = sum(m[0] for m in markouts) / len(markouts)
        positive = sum(1 for m in markouts if m[0] > 0)
        negative = sum(1 for m in markouts if m[0] < 0)

        print(f"  Fills with fair value data: {len(markouts)}")
        print(f"  Average markout: {avg_markout:+.2f}¢ ({'good' if avg_markout > 0 else 'BAD'})")
        print(f"  Favorable fills: {positive}/{len(markouts)} ({pct(positive, len(markouts))})")
        print(f"  Adverse fills: {negative}/{len(markouts)} ({pct(negative, len(markouts))})")

        results["adverse_selection"]["avg_markout"] = round(avg_markout, 2)
        results["adverse_selection"]["favorable_pct"] = round(positive / len(markouts), 3)

        # Per-family markout
        fam_markout = defaultdict(lambda: {"total": 0, "sum": 0, "adverse": 0})
        for markout, ticker, side in markouts:
            fam = family_of(ticker)
            fam_markout[fam]["total"] += 1
            fam_markout[fam]["sum"] += markout
            if markout < 0:
                fam_markout[fam]["adverse"] += 1

        print(f"\n  Per-family markout:")
        print(f"  {'Family':>12s}  {'N':>5s}  {'AvgMk':>7s}  {'Adverse':>8s}  {'Rate':>6s}")
        print(f"  {'-' * 12}  {'-' * 5}  {'-' * 7}  {'-' * 8}  {'-' * 6}")

        for fam, d in sorted(fam_markout.items(), key=lambda x: x[1]["sum"] / max(1, x[1]["total"])):
            if d["total"] < 3:
                continue
            avg_m = d["sum"] / d["total"]
            rate = d["adverse"] / d["total"]
            flag = " ⚠" if rate > 0.5 else ""
            print(f"  {fam:>12s}  {d['total']:>5d}  {avg_m:>+6.1f}¢  {d['adverse']:>8d}  "
                  f"{rate:>5.0%}{flag}")

    # ── Fill rate analysis ──
    print(f"\n  ─── Fill Rate Analysis ───")

    all_orders = conn.execute("""
        SELECT ticker, fill_qty, timestamp
        FROM mm_orders
        WHERE timestamp > datetime('now', '-7 days')
    """).fetchall()

    fam_fillrate = defaultdict(lambda: {"total": 0, "filled": 0})
    for ticker, fill_qty, ts in all_orders:
        fam = family_of(ticker)
        fam_fillrate[fam]["total"] += 1
        if fill_qty and fill_qty > 0:
            fam_fillrate[fam]["filled"] += 1

    print(f"  {'Family':>12s}  {'Orders':>7s}  {'Filled':>7s}  {'Rate':>7s}  {'Signal'}")
    print(f"  {'-' * 12}  {'-' * 7}  {'-' * 7}  {'-' * 7}  {'-' * 20}")

    for fam, d in sorted(fam_fillrate.items(), key=lambda x: -x[1]["filled"] / max(1, x[1]["total"])):
        if d["total"] < 4:
            continue
        rate = d["filled"] / d["total"]
        signal = ""
        if rate > 0.35:
            signal = "⚠ BLOCK (>35%)"
        elif rate > 0.20:
            signal = "⚠ WIDEN (>20%)"
        elif rate > 0.10:
            signal = "~ moderate"
        else:
            signal = "✓ healthy"
        print(f"  {fam:>12s}  {d['total']:>7d}  {d['filled']:>7d}  {rate:>6.0%}  {signal}")

    # ── Spread adequacy ──
    print(f"\n  ─── Spread Adequacy ───")

    # For each family, compute: avg spread (from fill price pairs) vs avg adverse markout
    # If spread < adverse markout, we're losing money on every round trip
    for fam, d in sorted(fam_markout.items(), key=lambda x: x[1]["sum"] / max(1, x[1]["total"])):
        if d["total"] < 5:
            continue
        avg_adverse = abs(d["sum"] / d["total"]) if d["sum"] < 0 else 0
        # Minimum profitable half-spread = adverse markout + maker fee (~0.44¢ at 50¢)
        min_hs = avg_adverse + 0.5  # rough maker fee
        results["spread_analysis"][fam] = {
            "avg_markout": round(d["sum"] / d["total"], 2),
            "min_profitable_hs": round(min_hs, 1)
        }
        if avg_adverse > 0:
            print(f"  {fam}: avg adverse markout = {avg_adverse:.1f}¢ → "
                  f"min profitable half-spread = {min_hs:.1f}¢")

    # ── MM settlement P&L ──
    print(f"\n  ─── MM Settlement P&L ───")

    mm_settlements = conn.execute("""
        SELECT ticker, profit_cents, won, contracts FROM settlements
        WHERE strategy LIKE 'mm:%'
    """).fetchall()

    if mm_settlements:
        mm_pnl = sum(p for _, p, _, _ in mm_settlements)
        mm_wins = sum(1 for _, _, w, _ in mm_settlements if w)
        mm_contracts = sum(c for _, _, _, c in mm_settlements)

        print(f"  MM settlements: {len(mm_settlements)}")
        print(f"  MM win rate: {pct(mm_wins, len(mm_settlements))}")
        print(f"  MM P&L: {fmt_pnl(mm_pnl)}")
        print(f"  MM P&L per contract: {mm_pnl / max(1, mm_contracts):.2f}¢")
        print(f"  MM P&L per settlement: {fmt_pnl(mm_pnl // max(1, len(mm_settlements)))}")

        results["mm_pnl_cents"] = mm_pnl
        results["mm_pnl_per_contract"] = round(mm_pnl / max(1, mm_contracts), 2)

        # Per-family MM P&L
        fam_mm_pnl = defaultdict(lambda: {"pnl": 0, "n": 0, "wins": 0})
        for ticker, profit, won, contracts in mm_settlements:
            fam = family_of(ticker)
            fam_mm_pnl[fam]["pnl"] += profit
            fam_mm_pnl[fam]["n"] += 1
            if won:
                fam_mm_pnl[fam]["wins"] += 1

        print(f"\n  {'Family':>12s}  {'N':>4s}  {'WR':>6s}  {'P&L':>10s}  {'$/settle':>10s}")
        print(f"  {'-' * 12}  {'-' * 4}  {'-' * 6}  {'-' * 10}  {'-' * 10}")

        for fam, d in sorted(fam_mm_pnl.items(), key=lambda x: x[1]["pnl"]):
            wr = d["wins"] / d["n"] if d["n"] > 0 else 0
            per = d["pnl"] / d["n"] if d["n"] > 0 else 0
            print(f"  {fam:>12s}  {d['n']:>4d}  {wr:>5.0%}  {fmt_pnl(d['pnl']):>10s}  "
                  f"{fmt_pnl(int(per)):>10s}")
            results["family_mm"][fam] = {
                "n": d["n"], "wr": round(wr, 3),
                "pnl_cents": d["pnl"], "pnl_per_settle": round(per, 0)
            }

    # ── Fee impact ──
    print(f"\n  ─── Fee Impact ───")

    try:
        # T3.3: read from the canonical fills ledger. Price-per-side comes
        # from yes_price_cents when side='yes' else no_price_cents — same
        # CASE pattern used in regime.detect_regime.
        fee_rows = conn.execute("""
            SELECT CASE WHEN side='yes' THEN yes_price_cents
                         ELSE no_price_cents END,
                   contracts, fee_cents
            FROM fills_ledger
        """).fetchall()
        total_fees = sum(int(f[2] or 0) for f in fee_rows)
        total_notional = sum(int(f[0] or 0) * int(f[1] or 0) for f in fee_rows)
    except Exception:
        # Fallback: legacy mm_processed_fills (pre-T3 data only; no
        # live writer since the MM path was removed). Kept as a
        # read-through so historical DBs still report a fee total.
        try:
            fee_rows = conn.execute(
                "SELECT price_cents, contracts, fee_cents FROM mm_processed_fills"
            ).fetchall()
            total_fees = sum(int(f[2] or 0) for f in fee_rows)
            total_notional = sum(int(f[0] or 0) * int(f[1] or 0) for f in fee_rows)
        except Exception:
            try:
                fee_rows = conn.execute(
                    "SELECT fee_cents FROM mm_processed_fills"
                ).fetchall()
                total_fees = sum(int(f[0] or 0) for f in fee_rows)
                total_notional = 0
            except Exception:
                fee_rows = []
                total_fees = 0
                total_notional = 0

    if fee_rows:
        print(f"  Total fees paid: {fmt_pnl(total_fees)}")
        if total_notional > 0:
            print(f"  Fee rate: {total_fees / total_notional:.2%} of notional")
        if mm_settlements:
            gross_revenue = mm_pnl + total_fees
            print(f"  Gross revenue (P&L + fees): {fmt_pnl(gross_revenue)}")
            if gross_revenue != 0:
                print(f"  Fee drag: {fmt_pnl(-total_fees)} ({abs(total_fees) / max(1, abs(gross_revenue)):.0%} of gross)")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 6: Loss Classification Analysis
# ═════════════════════════════════════════════════════════════════════════════

def analyze_loss_postmortems(conn) -> dict:
    """What types of losses are we experiencing? Are they fixable?"""
    print(f"\n{'═' * 72}")
    print(f" 6. LOSS CLASSIFICATION (POST-MORTEM ANALYSIS)")
    print(f"{'═' * 72}")

    results = {"loss_types": {}, "total_losses": 0}

    rows = conn.execute("""
        SELECT loss_type, ticker, source_combo, estimated_prob, market_prob,
               edge_at_entry, detail
        FROM loss_postmortems
        ORDER BY loss_type
    """).fetchall()

    if not rows:
        print("\n  ⚠ No loss postmortem data. Run after more settlements.")
        return results

    results["total_losses"] = len(rows)

    # Classification breakdown
    by_type = defaultdict(lambda: {"count": 0, "tickers": set()})
    for lt, ticker, src, ep, mp, edge, detail in rows:
        by_type[lt]["count"] += 1
        by_type[lt]["tickers"].add(family_of(ticker))

    print(f"\n  Total classified losses: {len(rows)}")
    print(f"\n  {'Loss Type':>25s}  {'Count':>6s}  {'%':>6s}  {'Families'}")
    print(f"  {'-' * 25}  {'-' * 6}  {'-' * 6}  {'-' * 30}")

    for lt, d in sorted(by_type.items(), key=lambda x: -x[1]["count"]):
        rate = d["count"] / len(rows)
        families = ", ".join(sorted(d["tickers"])[:5])
        actionable = ""
        if lt == "mm_adverse_selection":
            actionable = " → WIDEN SPREADS / BLOCK"
        elif lt == "mm_fee_erosion":
            actionable = " → WIDEN SPREADS"
        elif lt == "mm_inventory_decay":
            actionable = " → FASTER EXITS"
        elif lt == "mm_directional_loss":
            actionable = " → IMPROVE SIGNALS"
        elif lt == "bad_source":
            actionable = " → DISABLE SOURCE"
        elif lt == "fee_erosion":
            actionable = " → INCREASE MIN EDGE"
        print(f"  {lt:>25s}  {d['count']:>6d}  {rate:>5.0%}  {families}{actionable}")
        results["loss_types"][lt] = {"count": d["count"], "pct": round(rate, 3)}

    # Fixable vs structural losses
    fixable = sum(d["count"] for lt, d in by_type.items()
                  if lt in ("mm_fee_erosion", "fee_erosion", "mm_inventory_decay"))
    structural = sum(d["count"] for lt, d in by_type.items()
                     if lt in ("mm_adverse_selection", "adverse_selection", "bad_source"))
    normal = sum(d["count"] for lt, d in by_type.items()
                 if lt in ("mm_directional_loss", "efficient_market", "timing"))

    print(f"\n  Classification:")
    print(f"    Fixable (fee/inventory/timing): {fixable} ({pct(fixable, len(rows))})")
    print(f"    Structural (adverse selection/bad source): {structural} ({pct(structural, len(rows))})")
    print(f"    Normal MM losses (directional/market): {normal} ({pct(normal, len(rows))})")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 7: Opportunity Cost — What We Missed
# ═════════════════════════════════════════════════════════════════════════════

def analyze_opportunity_cost(conn) -> dict:
    """What markets did we skip that we should have traded?"""
    print(f"\n{'═' * 72}")
    print(f" 7. OPPORTUNITY COST ANALYSIS")
    print(f"{'═' * 72}")

    results = {"skipped_total": 0, "skip_reasons": {}}

    # Skipped opportunities from opportunity_log
    skipped = conn.execute("""
        SELECT ticker, skip_reason, ensemble_prob, market_prob, edge, strategy
        FROM opportunity_log
        WHERE skip_reason IS NOT NULL AND skip_reason != ''
    """).fetchall()

    # Traded markets
    traded = conn.execute("""
        SELECT DISTINCT ticker FROM opportunity_log
        WHERE action IN ('trade', 'candidate') AND skip_reason IS NULL
    """).fetchall()
    traded_tickers = {r[0] for r in traded}

    # Settled markets we were involved with
    settled = conn.execute("""
        SELECT DISTINCT ticker FROM settlements
    """).fetchall()
    settled_tickers = {r[0] for r in settled}

    if not skipped:
        print("\n  ⚠ No skip data in opportunity_log.")
        return results

    results["skipped_total"] = len(skipped)

    # Analyze skip reasons
    skip_reasons = defaultdict(int)
    skip_edges = []
    for ticker, reason, ep, mp, edge, strat in skipped:
        skip_reasons[reason or "unknown"] += 1
        if edge:
            skip_edges.append(float(edge))

    print(f"\n  Total skipped opportunities: {len(skipped)}")
    print(f"  Markets we traded: {len(traded_tickers)}")
    print(f"  Markets settled: {len(settled_tickers)}")

    print(f"\n  Skip reasons:")
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1])[:15]:
        r = reason[:50]
        print(f"    {r:>50s}  {count:>5d}")
        results["skip_reasons"][r] = count

    if skip_edges:
        print(f"\n  Edge distribution of skipped trades:")
        print(f"    Median edge: {sorted(skip_edges)[len(skip_edges) // 2]:.1%}")
        print(f"    Mean edge: {sum(skip_edges) / len(skip_edges):.1%}")
        high_edge_skips = sum(1 for e in skip_edges if e > 0.10)
        print(f"    Skipped with >10% edge: {high_edge_skips} ({pct(high_edge_skips, len(skip_edges))})")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 8: Timing Analysis
# ═════════════════════════════════════════════════════════════════════════════

def analyze_timing(conn) -> dict:
    """When do we perform best/worst?"""
    print(f"\n{'═' * 72}")
    print(f" 8. TIMING ANALYSIS")
    print(f"{'═' * 72}")

    results = {"by_hour": {}, "by_dow": {}}

    # From timing_patterns table
    rows = conn.execute("""
        SELECT hour_utc, day_of_week, won, profit_cents
        FROM timing_patterns
    """).fetchall()

    # Also from mm_orders with timestamp
    mm_rows = conn.execute("""
        SELECT m.timestamp, s.won, s.profit_cents
        FROM mm_orders m
        JOIN settlements s ON m.ticker = s.ticker
        WHERE m.fill_qty > 0 AND m.timestamp IS NOT NULL
    """).fetchall()

    hour_data = defaultdict(lambda: {"total": 0, "wins": 0, "pnl": 0})
    dow_data = defaultdict(lambda: {"total": 0, "wins": 0, "pnl": 0})

    for hour, dow, won, pnl in rows:
        if hour is not None:
            hour_data[hour]["total"] += 1
            hour_data[hour]["wins"] += int(won or 0)
            hour_data[hour]["pnl"] += int(pnl or 0)
        if dow is not None:
            dow_data[dow]["total"] += 1
            dow_data[dow]["wins"] += int(won or 0)
            dow_data[dow]["pnl"] += int(pnl or 0)

    for ts, won, pnl in mm_rows:
        if not ts:
            continue
        try:
            hour = int(ts[11:13])
            hour_data[hour]["total"] += 1
            hour_data[hour]["wins"] += int(won or 0)
            hour_data[hour]["pnl"] += int(pnl or 0)
        except (ValueError, IndexError):
            pass

    if hour_data:
        print(f"\n  Performance by hour (UTC):")
        print(f"  {'Hour':>6s}  {'N':>5s}  {'WR':>6s}  {'P&L':>10s}")
        print(f"  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 10}")

        for h in sorted(hour_data.keys()):
            d = hour_data[h]
            if d["total"] < 2:
                continue
            wr = d["wins"] / d["total"]
            print(f"  {h:>4d}:00  {d['total']:>5d}  {wr:>5.0%}  {fmt_pnl(d['pnl']):>10s}")
            results["by_hour"][str(h)] = {"n": d["total"], "wr": round(wr, 3), "pnl": d["pnl"]}

    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    if dow_data:
        print(f"\n  Performance by day of week:")
        for d in sorted(dow_data.keys()):
            dd = dow_data[d]
            if dd["total"] < 2:
                continue
            wr = dd["wins"] / dd["total"]
            print(f"  {dow_names.get(d, str(d)):>5s}  N={dd['total']:>4d}  "
                  f"WR={wr:.0%}  P&L={fmt_pnl(dd['pnl'])}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 9: Statistical Significance
# ═════════════════════════════════════════════════════════════════════════════

def analyze_statistical_significance(conn) -> dict:
    """Are the results statistically significant or just noise?"""
    print(f"\n{'═' * 72}")
    print(f" 9. STATISTICAL SIGNIFICANCE")
    print(f"{'═' * 72}")

    results = {}

    # Get overall win rate
    settlements = conn.execute("SELECT won FROM settlements").fetchall()
    if not settlements:
        print("\n  ⚠ No data for significance testing.")
        return results

    n = len(settlements)
    wins = sum(1 for (w,) in settlements if w)
    wr = wins / n
    ci_lo, ci_hi = wilson_ci(wins, n)

    print(f"\n  Overall: {wins}/{n} = {wr:.1%} win rate")
    print(f"  95% confidence interval: [{ci_lo:.1%}, {ci_hi:.1%}]")

    results["overall_wr"] = round(wr, 3)
    results["overall_ci"] = [round(ci_lo, 3), round(ci_hi, 3)]
    results["n_settlements"] = n

    # Test: is win rate significantly different from 50%?
    # Using normal approximation to binomial
    if n >= 10:
        z = (wr - 0.5) / sqrt(0.25 / n)
        # Two-tailed p-value approximation
        p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / sqrt(2))))
        results["vs_50pct_z"] = round(z, 2)
        results["vs_50pct_p"] = round(p_value, 4)

        print(f"\n  Test: WR ≠ 50% (random)")
        print(f"    z-statistic: {z:.2f}")
        print(f"    p-value: {p_value:.4f}")
        if p_value < 0.05:
            direction = "ABOVE" if wr > 0.5 else "BELOW"
            print(f"    ✓ Statistically significant — WR is {direction} random at p<0.05")
        else:
            print(f"    ✗ NOT statistically significant (p={p_value:.2f})")
            print(f"    Need ~{int((1.96 / (wr - 0.5)) ** 2 * 0.25) if wr != 0.5 else '∞'} samples "
                  f"for significance at current WR")

    # Test: is there a trend in P&L?
    pnl_rows = conn.execute("""
        SELECT profit_cents FROM settlements ORDER BY recorded_at
    """).fetchall()

    if len(pnl_rows) >= 10:
        cumulative = []
        running = 0
        for (pnl,) in pnl_rows:
            running += pnl
            cumulative.append(running)

        # Simple trend: compare first half vs second half average P&L
        mid = len(pnl_rows) // 2
        first_half = sum(p for (p,) in pnl_rows[:mid]) / mid
        second_half = sum(p for (p,) in pnl_rows[mid:]) / (len(pnl_rows) - mid)

        print(f"\n  P&L trend:")
        print(f"    First half avg P&L/trade: {first_half / 100:.2f}¢")
        print(f"    Second half avg P&L/trade: {second_half / 100:.2f}¢")
        improving = second_half > first_half
        print(f"    Trend: {'IMPROVING ✓' if improving else 'DETERIORATING ✗'}")
        results["trend_improving"] = improving

    # Sample size adequacy
    print(f"\n  Sample size adequacy:")
    print(f"    Current: {n} settlements")
    print(f"    For 95% CI width ±5%: need ~{int(1.96**2 * 0.25 / 0.05**2)} samples")
    print(f"    For 95% CI width ±3%: need ~{int(1.96**2 * 0.25 / 0.03**2)} samples")
    if n < 100:
        print(f"    ⚠ Current sample size is small. Results may not be stable.")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 10: Inventory & Position Analysis
# ═════════════════════════════════════════════════════════════════════════════

def analyze_inventory(conn) -> dict:
    """Current inventory risk assessment."""
    print(f"\n{'═' * 72}")
    print(f" 10. CURRENT INVENTORY RISK")
    print(f"{'═' * 72}")

    results = {"positions": 0, "exposure_cents": 0, "families": {}}

    inv = conn.execute("""
        SELECT ticker, net_position, avg_entry_cents, realized_pnl_cents
        FROM mm_inventory WHERE net_position != 0
        ORDER BY ABS(net_position) * avg_entry_cents DESC
    """).fetchall()

    if not inv:
        print("\n  No active inventory.")
        return results

    results["positions"] = len(inv)

    total_exposure = 0
    total_realized = 0
    fam_exposure = defaultdict(lambda: {"contracts": 0, "exposure": 0, "positions": 0, "rpnl": 0})

    for ticker, net, avg_e, rpnl in inv:
        exposure = abs(net) * int(avg_e or 50)
        total_exposure += exposure
        total_realized += (rpnl or 0)
        fam = family_of(ticker)
        fam_exposure[fam]["contracts"] += abs(net)
        fam_exposure[fam]["exposure"] += exposure
        fam_exposure[fam]["positions"] += 1
        fam_exposure[fam]["rpnl"] += (rpnl or 0)

    results["exposure_cents"] = total_exposure

    print(f"\n  Active positions: {len(inv)}")
    print(f"  Total exposure: ${total_exposure / 100:.2f}")
    print(f"  Total realized P&L: {fmt_pnl(total_realized)}")

    print(f"\n  {'Family':>12s}  {'Pos':>4s}  {'Contracts':>10s}  {'Exposure':>10s}  {'Realized':>10s}")
    print(f"  {'-' * 12}  {'-' * 4}  {'-' * 10}  {'-' * 10}  {'-' * 10}")

    for fam, d in sorted(fam_exposure.items(), key=lambda x: -x[1]["exposure"]):
        concentration = d["exposure"] / max(1, total_exposure)
        flag = " ⚠" if concentration > 0.30 else ""
        print(f"  {fam:>12s}  {d['positions']:>4d}  {d['contracts']:>10d}  "
              f"${d['exposure'] / 100:>9.2f}  {fmt_pnl(d['rpnl']):>10s}  "
              f"({concentration:.0%}){flag}")
        results["families"][fam] = {
            "positions": d["positions"], "contracts": d["contracts"],
            "exposure_cents": d["exposure"], "concentration": round(concentration, 3)
        }

    # Concentration risk
    max_fam = max(fam_exposure.items(), key=lambda x: x[1]["exposure"])
    max_conc = max_fam[1]["exposure"] / max(1, total_exposure)
    if max_conc > 0.50:
        print(f"\n  ⚠ HIGH CONCENTRATION: {max_fam[0]} = {max_conc:.0%} of total exposure")
        print(f"    Single-event risk: if {max_fam[0]} resolves unfavorably, "
              f"max loss = ${max_fam[1]['exposure'] / 100:.2f}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 11: Learning System Status
# ═════════════════════════════════════════════════════════════════════════════

def analyze_learning_system(conn) -> dict:
    """Is the learning system actually learning?"""
    print(f"\n{'═' * 72}")
    print(f" 11. LEARNING SYSTEM STATUS")
    print(f"{'═' * 72}")

    results = {"tables": {}}

    # Check each learning table
    tables = [
        ("calibration", "Calibration records (est_prob vs outcome)"),
        ("loss_postmortems", "Loss classifications"),
        ("timing_patterns", "Timing pattern records"),
        ("edge_convergence", "Edge convergence tracking"),
        ("hyperparam_shadow", "Hyperparameter shadow testing"),
        ("pipeline_health", "Pipeline health records"),
        ("strategy_journal", "Strategy journal entries"),
        ("position_health_log", "Position health scores"),
    ]

    print(f"\n  {'Table':>25s}  {'Rows':>7s}  {'Status'}")
    print(f"  {'-' * 25}  {'-' * 7}  {'-' * 30}")

    for table, desc in tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            # Check if data is recent
            recent = 0
            try:
                recent = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE "
                    f"COALESCE(recorded_at, timestamp) > datetime('now', '-7 days')"
                ).fetchone()[0]
            except Exception:
                pass

            status = "✓ active" if recent > 0 else ("~ stale" if count > 0 else "✗ empty")
            min_needed = {"calibration": 50, "timing_patterns": 100,
                          "edge_convergence": 20, "loss_postmortems": 10}.get(table, 5)
            if count < min_needed:
                status += f" (need {min_needed}+)"

            print(f"  {table:>25s}  {count:>7d}  {status}")
            results["tables"][table] = {"count": count, "recent": recent}
        except Exception as e:
            print(f"  {table:>25s}  {'N/A':>7s}  ✗ error: {e}")

    # Adaptive weights status
    try:
        learned = conn.execute("""
            SELECT param_name, value, updated_at FROM learned_config
            WHERE param_name LIKE 'weight_%'
        """).fetchall()
        if learned:
            print(f"\n  Learned weights ({len(learned)} sources):")
            for name, val, updated in learned:
                print(f"    {name}: {val} (updated {updated})")
        else:
            print(f"\n  No learned weight adjustments yet (using defaults)")
    except Exception:
        print(f"\n  learned_config table not available")

    # kv_cache status
    try:
        kv_count = conn.execute("SELECT COUNT(*) FROM kv_cache WHERE expires_at > ?",
                                (time.time(),)).fetchone()[0]
        print(f"\n  Active kv_cache entries: {kv_count}")
    except Exception:
        pass

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Section 12: API-Based Opportunity Analysis (requires VPS)
# ═════════════════════════════════════════════════════════════════════════════

def analyze_api_opportunities(conn) -> dict:
    """Fetch settled markets from Kalshi API and find missed opportunities.

    Only runs on VPS where API access is available.
    """
    results = {"available": False}

    try:
        from trade import api_get
    except ImportError:
        return results

    print(f"\n{'═' * 72}")
    print(f" 12. MARKET OPPORTUNITY SCAN (API)")
    print(f"{'═' * 72}")

    results["available"] = True

    # Fetch recently settled markets
    print(f"\n  Fetching settled markets from Kalshi API...")
    all_markets = []
    cursor = None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    for page in range(20):
        params = {"limit": "200", "status": "settled"}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = api_get("/markets", params)
        except Exception as e:
            print(f"  API error: {e}")
            break

        markets = resp.get("markets", [])
        if not markets:
            break

        for m in markets:
            close_time = m.get("close_time") or m.get("expiration_time") or ""
            if close_time >= cutoff:
                all_markets.append(m)

        cursor = resp.get("cursor")
        if not cursor:
            break
        time.sleep(0.3)

    print(f"  Fetched {len(all_markets)} settled markets (last 14 days)")

    # Compare with what we traded
    our_tickers = set()
    for row in conn.execute("SELECT DISTINCT ticker FROM mm_orders WHERE fill_qty > 0"):
        our_tickers.add(row[0])
    for row in conn.execute("SELECT DISTINCT ticker FROM trades"):
        our_tickers.add(row[0])

    available = {m["ticker"] for m in all_markets if m.get("ticker")}
    overlap = our_tickers & available
    missed = available - our_tickers

    print(f"\n  Available settled markets: {len(available)}")
    print(f"  Markets we participated in: {len(overlap)}")
    print(f"  Markets we missed: {len(missed)}")
    print(f"  Coverage: {pct(len(overlap), len(available))}")

    results["total_available"] = len(available)
    results["participated"] = len(overlap)
    results["missed"] = len(missed)

    # Categorize missed markets
    missed_cats = defaultdict(int)
    easy_wins = 0
    for m in all_markets:
        tk = m.get("ticker", "")
        if tk not in missed:
            continue
        fam = family_of(tk)
        missed_cats[fam] += 1

        # Easy wins: extreme prices that resolved correctly
        yes_price = m.get("last_price") or 0
        if isinstance(yes_price, str):
            yes_price = float(yes_price)
        if yes_price > 1:
            yes_price /= 100
        result = (m.get("result") or "").lower()
        if (yes_price < 0.15 and result == "no") or (yes_price > 0.85 and result == "yes"):
            easy_wins += 1

    print(f"\n  Missed by family (top 10):")
    for fam, count in sorted(missed_cats.items(), key=lambda x: -x[1])[:10]:
        print(f"    {fam:>12s}  {count:>4d}")

    print(f"\n  Easy wins missed (extreme prices, correct resolution): {easy_wins}")
    results["easy_wins_missed"] = easy_wins

    # Safe Compounder opportunity
    sc_candidates = 0
    sc_wins = 0
    sc_losses = 0
    sc_pnl = 0
    for m in all_markets:
        result = (m.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        yes_price = m.get("last_price") or 0
        if isinstance(yes_price, str):
            yes_price = float(yes_price)
        if yes_price > 1:
            yes_price /= 100
        if yes_price <= 0.15:  # Strong NO candidate
            sc_candidates += 1
            no_price_cents = int((1 - yes_price) * 100)
            if result == "no":
                sc_wins += 1
                sc_pnl += (100 - no_price_cents) * 5  # 5 contracts
            else:
                sc_losses += 1
                sc_pnl -= no_price_cents * 5

    if sc_candidates:
        print(f"\n  Safe Compounder opportunity (YES ≤ 15¢, buy NO):")
        print(f"    Candidates: {sc_candidates}")
        print(f"    Win rate: {pct(sc_wins, sc_candidates)} ({sc_wins}/{sc_candidates})")
        print(f"    Hypothetical P&L (5 contracts): {fmt_pnl(sc_pnl)}")
        results["safe_compounder"] = {
            "candidates": sc_candidates, "wr": round(sc_wins / max(1, sc_candidates), 3),
            "pnl_cents": sc_pnl
        }

    return results


# ═════════════════════════════════════════════════════════════════════════════
# EXECUTIVE SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

def print_executive_summary(all_results: dict):
    """Print the final verdict: do the signals have alpha?"""
    print(f"\n{'═' * 72}")
    print(f" EXECUTIVE SUMMARY: DO THE SIGNALS HAVE ALPHA?")
    print(f"{'═' * 72}")

    cal = all_results.get("calibration", {})
    edge = all_results.get("edge_vs_winrate", {})
    fam = all_results.get("family_performance", {})
    mm = all_results.get("mm_performance", {})
    sig = all_results.get("significance", {})
    loss = all_results.get("loss_postmortems", {})

    # Scorecard
    scores = []

    # 1. Calibration
    brier = cal.get("brier_score")
    if brier is not None:
        if brier < 0.20:
            scores.append(("Calibration (Brier)", "✓ PASS", f"{brier:.4f} < 0.20"))
        elif brier < 0.25:
            scores.append(("Calibration (Brier)", "~ MARGINAL", f"{brier:.4f} ≈ random"))
        else:
            scores.append(("Calibration (Brier)", "✗ FAIL", f"{brier:.4f} > 0.25"))
    else:
        scores.append(("Calibration (Brier)", "? NO DATA", "Insufficient samples"))

    # 2. Edge monotonicity
    mono = edge.get("monotonicity")
    if mono is not None:
        if mono >= 0.6:
            scores.append(("Edge → Win Rate", "✓ PASS", f"Monotonicity {mono:.0%}"))
        else:
            scores.append(("Edge → Win Rate", "✗ FAIL", f"Monotonicity {mono:.0%}"))
    else:
        scores.append(("Edge → Win Rate", "? NO DATA", ""))

    # 3. Statistical significance
    p = sig.get("vs_50pct_p")
    if p is not None:
        if p < 0.05:
            scores.append(("Statistical significance", "✓ PASS", f"p={p:.4f}"))
        else:
            scores.append(("Statistical significance", "✗ FAIL", f"p={p:.4f}"))
    else:
        scores.append(("Statistical significance", "? NO DATA", ""))

    # 4. Adverse selection
    as_data = mm.get("adverse_selection", {})
    fav_pct = as_data.get("favorable_pct")
    if fav_pct is not None:
        if fav_pct > 0.5:
            scores.append(("MM Adverse Selection", "✓ PASS", f"{fav_pct:.0%} favorable"))
        else:
            scores.append(("MM Adverse Selection", "✗ FAIL", f"{fav_pct:.0%} favorable"))
    else:
        scores.append(("MM Adverse Selection", "? NO DATA", ""))

    # 5. P&L trend
    trend = sig.get("trend_improving")
    if trend is not None:
        if trend:
            scores.append(("P&L trend", "✓ IMPROVING", "Second half > first half"))
        else:
            scores.append(("P&L trend", "✗ DETERIORATING", "Getting worse"))
    else:
        scores.append(("P&L trend", "? NO DATA", ""))

    # Print scorecard
    print(f"\n  {'Criterion':>25s}  {'Result':>15s}  {'Detail'}")
    print(f"  {'-' * 25}  {'-' * 15}  {'-' * 30}")

    passes = 0
    fails = 0
    nodata = 0
    for criterion, result, detail in scores:
        print(f"  {criterion:>25s}  {result:>15s}  {detail}")
        if "PASS" in result or "IMPROVING" in result:
            passes += 1
        elif "FAIL" in result or "DETERIORATING" in result:
            fails += 1
        else:
            nodata += 1

    # Final verdict
    print(f"\n  ─── VERDICT ───")
    if nodata >= 3:
        print(f"\n  ⚠ INSUFFICIENT DATA ({nodata}/5 criteria have no data)")
        print(f"    Need more settlements and trades to draw conclusions.")
        print(f"    Target: 100+ settlements with ensemble_prob and outcomes.")
        verdict = "INSUFFICIENT_DATA"
    elif passes >= 3:
        print(f"\n  ✓ SIGNALS HAVE ALPHA ({passes}/5 criteria pass)")
        print(f"    The ensemble has genuine predictive power.")
        print(f"    Focus: expand coverage, optimize spreads, re-enable directional.")
        verdict = "ALPHA_DETECTED"
    elif passes >= 2:
        print(f"\n  ~ MARGINAL ALPHA ({passes}/5 criteria pass, {fails} fail)")
        print(f"    Some signal exists but not consistently profitable.")
        print(f"    Focus: improve worst-performing sources, widen spreads on losers.")
        verdict = "MARGINAL"
    else:
        print(f"\n  ✗ NO ALPHA DETECTED ({fails}/5 criteria fail)")
        print(f"    The signals do not reliably predict outcomes.")
        print(f"    Action: fundamental strategy rethink needed.")
        verdict = "NO_ALPHA"

    all_results["verdict"] = {
        "result": verdict, "passes": passes, "fails": fails, "no_data": nodata
    }

    # Concrete next steps
    print(f"\n  ─── CONCRETE NEXT STEPS ───")

    loss_types = loss.get("loss_types", {})
    adverse_pct = loss_types.get("mm_adverse_selection", {}).get("pct", 0)
    fee_pct = loss_types.get("mm_fee_erosion", {}).get("pct", 0)

    steps = []
    if adverse_pct > 0.3:
        steps.append(f"1. Adverse selection is {adverse_pct:.0%} of losses → "
                     f"widen spreads + block toxic families (DEPLOYED)")
    if fee_pct > 0.1:
        steps.append(f"2. Fee erosion is {fee_pct:.0%} of losses → "
                     f"increase min spread above fee breakeven")

    fam_data = fam.get("families", {})
    for f, d in fam_data.items():
        if d.get("n", 0) >= 5 and d.get("wr", 0) < 0.15 and d.get("pnl_cents", 0) < -500:
            steps.append(f"3. Block {f}: {d['wr']:.0%} WR, {fmt_pnl(d['pnl_cents'])} P&L")

    if sig.get("n_settlements", 0) < 100:
        steps.append(f"4. Continue trading to accumulate data — "
                     f"need 100+ settlements (have {sig.get('n_settlements', 0)})")

    be = edge.get("breakeven_edge")
    if be:
        steps.append(f"5. Set MIN_EDGE ≥ {be} (first profitable edge bucket)")

    if not steps:
        steps.append("1. Accumulate more data (need 100+ settlements)")
        steps.append("2. Monitor adverse selection defenses")
        steps.append("3. Run this backtest again in 1 week")

    for s in steps:
        print(f"  {s}")


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Comprehensive backtest analysis")
    parser.add_argument("--db", default=None,
                        help="Path to SQLite database (default: auto-detect)")
    parser.add_argument("--json", default="backtest_results.json",
                        help="Output JSON path (default: backtest_results.json)")
    parser.add_argument("--no-api", action="store_true",
                        help="Skip API-based analysis even if available")
    args = parser.parse_args()

    # Find database
    db_path = args.db
    if not db_path:
        for candidate in ["kalshi_trades.db", "kalshi_trades_live.db"]:
            if os.path.exists(candidate):
                db_path = candidate
                break
    if not db_path:
        print("ERROR: No database found. Specify with --db path/to/db")
        sys.exit(1)

    print(f"{'═' * 72}")
    print(f" COMPREHENSIVE BACKTEST — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f" Database: {db_path}")
    print(f"{'═' * 72}")

    conn = sqlite3.connect(db_path)

    # Quick stats
    try:
        n_mm = conn.execute("SELECT COUNT(*) FROM mm_orders WHERE fill_qty > 0").fetchone()[0]
        n_settle = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
        n_opp = conn.execute("SELECT COUNT(*) FROM opportunity_log").fetchone()[0]
        n_postmortem = conn.execute("SELECT COUNT(*) FROM loss_postmortems").fetchone()[0]
        print(f"\n  Data available: {n_mm} MM fills, {n_settle} settlements, "
              f"{n_opp} opportunity logs, {n_postmortem} postmortems")
    except Exception as e:
        print(f"\n  Error reading DB stats: {e}")

    # Run all analyses
    all_results = {"timestamp": datetime.now(timezone.utc).isoformat(), "db_path": db_path}

    all_results["calibration"] = analyze_calibration(conn)
    all_results["edge_vs_winrate"] = analyze_edge_vs_winrate(conn)
    all_results["source_accuracy"] = analyze_source_accuracy(conn)
    all_results["family_performance"] = analyze_family_performance(conn)
    all_results["mm_performance"] = analyze_mm_performance(conn)
    all_results["loss_postmortems"] = analyze_loss_postmortems(conn)
    all_results["opportunity_cost"] = analyze_opportunity_cost(conn)
    all_results["timing"] = analyze_timing(conn)
    all_results["significance"] = analyze_statistical_significance(conn)
    all_results["inventory"] = analyze_inventory(conn)
    all_results["learning"] = analyze_learning_system(conn)

    if not args.no_api:
        all_results["api_opportunities"] = analyze_api_opportunities(conn)

    # Executive summary
    print_executive_summary(all_results)

    # Save JSON
    json_path = args.json
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Results saved to: {json_path}")

    conn.close()


if __name__ == "__main__":
    main()
