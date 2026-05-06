"""Historical cross-bracket portfolio backtest.

Reconstructs what cross_bracket_shadow would have decided on every
settled weather settlement, using:

  * σ-fixed predicted (μ, σ) from ``replay_postfix_results`` (already
    applies the σ floor + state machine + sanity gate).
  * Per-bracket market quotes from ``weather_mm_shadow`` (every cycle's
    YES bid/ask plus the actual ticker outcome via ``ticker_settled_yes``).
  * The production scorer ``score_market_portfolio()`` — same code that
    runs in production, no parallel implementation drift.

For each settled (settle_key) we pick a single decision time T = settle - 4h
(mimicking the cycle's typical TTE window for weather). At T, we snapshot
each bracket's bid/ask (closest weather_mm_shadow row within ±30min),
plug in the σ-fixed Gaussian, run the scorer, and compute PnL using the
real outcome.

Output: per-portfolio PnL (gross + net of maker fee), per-family
aggregates, win rate, and side-by-side comparison vs single-side
directional shadow PnL on the same settlements.

Usage:
    python3 tools/backtest_cross_bracket_historical.py [--db PATH]
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Optional

# Run from repo root.
sys.path.insert(0, ".")
from bot.scoring.bracket_portfolio import (
    BracketDecision, score_market_portfolio,
)
from bot.signals.weather_ensemble_v2 import _COMBINED_SIGMA_FLOOR_F


# Hours-before-settle to take the snapshot. Mirrors typical cross_bracket
# fire timing (TTE 4-12h, settling daily). Picking too close to settle
# = trivially-perfect; too far = sigma still wide.
DECISION_HOURS_BEFORE_SETTLE = 4

# Maximum time delta when matching a weather_mm_shadow row to the
# decision time. Snapshots are recorded ~every cycle so 1800s ≫ cadence.
QUOTE_TOLERANCE_S = 1800

# Same tolerance for matching replay_postfix_results.
PREDICTION_TOLERANCE_S = 3600


def _parse_settle_key(ticker: str) -> Optional[str]:
    for sep in ("-B", "-T"):
        idx = ticker.rfind(sep)
        if idx > 0:
            return ticker[:idx]
    return None


def _build_market_dict(row: sqlite3.Row) -> Optional[dict]:
    """Construct a Kalshi-shaped market_data dict from a weather_mm_shadow
    row so it can flow through ``score_market_portfolio``. Mirrors the
    shape used by tests/test_bracket_portfolio.py:_market.
    """
    ticker = row["ticker"]
    bracket_lo: Optional[float] = None
    bracket_hi: Optional[float] = None
    threshold: Optional[float] = None

    if "-B" in ticker:
        b_value = float(ticker.rsplit("-B", 1)[1])
        bracket_lo = b_value - 0.5
        bracket_hi = b_value + 1.5
    elif "-T" in ticker:
        threshold = float(ticker.rsplit("-T", 1)[1])
    else:
        return None

    yes_bid = row["market_yes_bid"]
    yes_ask = row["market_yes_ask"]
    # Allow one-sided quotes — score_market_portfolio handles None on
    # either side (one of edge_yes / edge_no will be None and that side
    # won't fire). Reject only if BOTH are missing.
    if yes_bid is None and yes_ask is None:
        return None

    base = {
        "ticker": ticker,
        "title": f"high temp {ticker}",
        "subtitle": "high temp",
        "yes_sub_title": f"{int(threshold or bracket_lo or 75)} or above",
        "close_time": "2030-04-30T23:59:59Z",
    }
    if yes_bid is not None:
        base["yes_bid_dollars"] = f"0.{yes_bid:02d}"
    if yes_ask is not None:
        base["yes_ask_dollars"] = f"0.{yes_ask:02d}"
    if bracket_lo is not None:
        base["floor_strike"] = bracket_lo
        base["cap_strike"] = bracket_hi
    return base


def _kalshi_maker_fee(price_cents: int) -> int:
    """Inline copy of bot.core.money.kalshi_maker_fee for 1 contract."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    numerator = 175 * 1 * price_cents * (100 - price_cents)
    return (numerator + 999999) // 1000000


def _leg_pnl(side: str, price_cents: int, ticker_yes_outcome: int) -> tuple[int, int]:
    """Returns (gross, net) PnL in cents for a single leg."""
    won = (side == "yes" and ticker_yes_outcome == 1) or (
        side == "no" and ticker_yes_outcome == 0
    )
    gross = (100 - price_cents) if won else (-price_cents)
    fee = _kalshi_maker_fee(price_cents)
    return gross, gross - fee


def _fetch_settlement_groups(conn: sqlite3.Connection) -> dict:
    """Group every settled weather_mm_shadow row by settle_key. Returns
    {settle_key: {"ts_settle_unix": float, "rows": list[Row], "outcomes": dict[ticker, int]}}.
    """
    rows = conn.execute(
        """SELECT ticker, ts_unix, ts_settle_unix, ticker_settled_yes,
                  market_yes_bid, market_yes_ask
           FROM weather_mm_shadow
           WHERE ticker_settled_yes IS NOT NULL
             AND ts_settle_unix IS NOT NULL"""
    ).fetchall()
    groups: dict = defaultdict(lambda: {"ts_settle_unix": None, "rows": [], "outcomes": {}})
    for r in rows:
        key = _parse_settle_key(r["ticker"])
        if key is None:
            continue
        g = groups[key]
        g["ts_settle_unix"] = float(r["ts_settle_unix"])
        g["rows"].append(r)
        g["outcomes"][r["ticker"]] = int(r["ticker_settled_yes"])
    return dict(groups)


def _pick_quote_at(rows: list, ticker: str, target_t: float) -> Optional[dict]:
    """For the given ticker, pick the weather_mm_shadow row closest to
    target_t with both yes_bid + yes_ask populated."""
    best: Optional[sqlite3.Row] = None
    best_dt = float("inf")
    for r in rows:
        if r["ticker"] != ticker:
            continue
        # Need at least one side of the quote.
        if r["market_yes_bid"] is None and r["market_yes_ask"] is None:
            continue
        dt = abs(float(r["ts_unix"]) - target_t)
        if dt < best_dt and dt <= QUOTE_TOLERANCE_S:
            best = r
            best_dt = dt
    return _build_market_dict(best) if best is not None else None


def _pick_prediction_at(
    conn: sqlite3.Connection, settle_key: str, target_t: float,
) -> Optional[tuple[float, float]]:
    """Get σ-fixed (μ, σ) at target_t from replay_postfix_results.
    All brackets in a settle_key share the same Gaussian, so any one's
    value at the right time suffices."""
    rows = conn.execute(
        """SELECT postfix_mu_f, postfix_sigma_f, recorded_at
           FROM replay_postfix_results
           WHERE ticker LIKE ? || '%'
             AND postfix_mu_f IS NOT NULL
             AND postfix_sigma_f IS NOT NULL""",
        (settle_key,),
    ).fetchall()
    best: Optional[sqlite3.Row] = None
    best_dt = float("inf")
    for r in rows:
        try:
            r_t = float(_parse_iso_to_unix(r["recorded_at"]))
        except Exception:
            continue
        dt = abs(r_t - target_t)
        if dt < best_dt and dt <= PREDICTION_TOLERANCE_S:
            best = r
            best_dt = dt
    if best is None:
        return None
    return (float(best["postfix_mu_f"]), float(best["postfix_sigma_f"]))


def _parse_iso_to_unix(iso: str) -> float:
    from datetime import datetime, timezone
    # Handles both 'Z' suffix and '+00:00' offsets.
    s = iso.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def _single_side_pnl_for_settle(conn, settle_key: str) -> tuple[int, int]:
    """Sum directional_shadow gross + net PnL on this settle_key from
    alpha_backtest. Returns (gross_cents, net_cents). 0 if no rows."""
    rows = conn.execute(
        """SELECT side, price_cents, won_yes
           FROM alpha_backtest
           WHERE decision_type='directional_shadow'
             AND ts_settle_unix IS NOT NULL
             AND won_yes IS NOT NULL
             AND side IN ('yes','no')
             AND price_cents IS NOT NULL
             AND ticker LIKE ? || '%'""",
        (settle_key,),
    ).fetchall()
    gross = 0
    net = 0
    for r in rows:
        # alpha_backtest.won_yes already encodes "our trade won"
        won_yes = int(r["won_yes"])
        price = int(r["price_cents"])
        g = (100 - price) if won_yes else (-price)
        gross += g
        net += g - _kalshi_maker_fee(price)
    return gross, net, len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    global DECISION_HOURS_BEFORE_SETTLE
    p.add_argument("--db", default=os.environ.get("DB_PATH", "kalshi_trades.db"))
    p.add_argument("--min-edge", type=float, default=0.07)
    p.add_argument("--hours-before-settle", type=float,
                   default=DECISION_HOURS_BEFORE_SETTLE,
                   help="Decision time before settle in hours.")
    args = p.parse_args()
    DECISION_HOURS_BEFORE_SETTLE = args.hours_before_settle

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print(f"Loading settled weather settlements from {args.db}…")
    groups = _fetch_settlement_groups(conn)
    print(f"  {len(groups)} settled settle_keys")

    # Per-portfolio results
    portfolio_results: list[dict] = []

    for settle_key, g in sorted(groups.items()):
        ts_settle = g["ts_settle_unix"]
        decision_t = ts_settle - DECISION_HOURS_BEFORE_SETTLE * 3600

        # Get σ-fixed prediction at decision time
        pred = _pick_prediction_at(conn, settle_key, decision_t)
        if pred is None:
            continue
        mu, sigma = pred
        sigma = max(sigma, _COMBINED_SIGMA_FLOOR_F)

        # Build market dicts for each unique bracket in the settlement
        unique_tickers = sorted(set(r["ticker"] for r in g["rows"]))
        markets = []
        for t in unique_tickers:
            md = _pick_quote_at(g["rows"], t, decision_t)
            if md is not None:
                markets.append(md)

        if len(markets) < 3:
            continue  # too few brackets to be a meaningful portfolio

        decisions = score_market_portfolio(
            markets,
            combined_mu=mu,
            combined_sigma=sigma,
            min_edge=args.min_edge,
        )

        portfolio_gross = 0
        portfolio_net = 0
        legs_fired = 0
        legs_won = 0
        for d in decisions:
            if d.action == "skip" or d.side is None or d.price_cents is None:
                continue
            outcome = g["outcomes"].get(d.ticker)
            if outcome is None:
                continue
            gross, net = _leg_pnl(d.side, d.price_cents, outcome)
            portfolio_gross += gross
            portfolio_net += net
            legs_fired += 1
            if gross > 0:
                legs_won += 1

        ss_gross, ss_net, ss_n = _single_side_pnl_for_settle(conn, settle_key)

        portfolio_results.append({
            "settle_key": settle_key,
            "family": settle_key.split("-")[0],
            "n_brackets": len(markets),
            "legs_fired": legs_fired,
            "legs_won": legs_won,
            "cb_gross": portfolio_gross,
            "cb_net": portfolio_net,
            "ss_gross": ss_gross,
            "ss_net": ss_net,
            "ss_n_legs": ss_n,
            "mu": mu,
            "sigma": sigma,
        })

    # ── Per-portfolio summary ─────────────────────────────────────────
    print()
    print("=" * 96)
    print(f"Per-portfolio comparison (decision time T = settle - {DECISION_HOURS_BEFORE_SETTLE}h, "
          f"min_edge={args.min_edge})")
    print("=" * 96)
    print(f"{'settle_key':<22} {'brackets':>9} {'fired':>6} "
          f"{'cb_gross':>10} {'cb_net':>10} {'ss_gross':>10} {'ss_net':>10}")
    print("-" * 96)
    for p in sorted(portfolio_results, key=lambda x: x["settle_key"]):
        print(f"{p['settle_key']:<22} {p['n_brackets']:>9} {p['legs_fired']:>6} "
              f"{p['cb_gross']:+10d} {p['cb_net']:+10d} "
              f"{p['ss_gross']:+10d} {p['ss_net']:+10d}")

    # ── Per-family aggregates ─────────────────────────────────────────
    print()
    print("=" * 96)
    print("Per-family aggregates (sum across portfolios)")
    print("=" * 96)
    by_fam: dict = defaultdict(lambda: {
        "n_port": 0, "cb_legs": 0, "cb_won": 0, "cb_gross": 0, "cb_net": 0,
        "ss_legs": 0, "ss_gross": 0, "ss_net": 0,
    })
    for p in portfolio_results:
        f = by_fam[p["family"]]
        f["n_port"] += 1
        f["cb_legs"] += p["legs_fired"]
        f["cb_won"] += p.get("legs_won", 0)
        f["cb_gross"] += p["cb_gross"]
        f["cb_net"] += p["cb_net"]
        f["ss_legs"] += p["ss_n_legs"]
        f["ss_gross"] += p["ss_gross"]
        f["ss_net"] += p["ss_net"]

    print(f"{'family':<14} {'n_port':>6} {'cb_legs':>8} {'cb_gross':>11} "
          f"{'cb_net':>11} {'ss_legs':>8} {'ss_gross':>11} {'ss_net':>11}")
    print("-" * 96)
    total = {"n_port": 0, "cb_legs": 0, "cb_won": 0, "cb_gross": 0, "cb_net": 0,
             "ss_legs": 0, "ss_gross": 0, "ss_net": 0}
    for fam in sorted(by_fam):
        v = by_fam[fam]
        for k in total:
            total[k] += v[k]
        print(f"{fam:<14} {v['n_port']:>6} {v['cb_legs']:>8} {v['cb_gross']:>+10d}¢ "
              f"{v['cb_net']:>+10d}¢ {v['ss_legs']:>8} "
              f"{v['ss_gross']:>+10d}¢ {v['ss_net']:>+10d}¢")
    print("-" * 96)
    print(f"{'TOTAL':<14} {total['n_port']:>6} {total['cb_legs']:>8} "
          f"{total['cb_gross']:>+10d}¢ {total['cb_net']:>+10d}¢ "
          f"{total['ss_legs']:>8} {total['ss_gross']:>+10d}¢ {total['ss_net']:>+10d}¢")
    print()
    cb_per_leg = total["cb_net"] / max(1, total["cb_legs"])
    ss_per_leg = total["ss_net"] / max(1, total["ss_legs"])
    cb_per_port = total["cb_net"] / max(1, total["n_port"])
    cb_wr = total.get("cb_won", 0) * 100 / max(1, total["cb_legs"])
    print(f"Cross-bracket  net per-leg:      {cb_per_leg:+.1f}¢   "
          f"net per-portfolio: {cb_per_port:+.1f}¢   win_rate: {cb_wr:.0f}%")
    print(f"Single-side    net per-leg:      {ss_per_leg:+.1f}¢")
    print()
    if cb_per_leg > ss_per_leg:
        print(f"✓ Cross-bracket beats single-side by {cb_per_leg - ss_per_leg:+.1f}¢/leg net")
    else:
        print(f"✗ Cross-bracket UNDERPERFORMS single-side by {ss_per_leg - cb_per_leg:+.1f}¢/leg net")
    return 0


if __name__ == "__main__":
    sys.exit(main())
