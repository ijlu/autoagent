"""Cross-bracket vs single-side directional retro-replay validator.

The Phase B.3 go/no-go question: does scoring a full bracket portfolio
beat scoring one bracket per market visit?

Reads settled rows from ``alpha_backtest``, splits into cohorts, prints a
per-family comparison: n, mean PnL/leg with 95% CI (gross + net),
mean PnL/portfolio (cross-bracket only), Brier, and win rate.

Cohorts
-------
``cross_bracket`` — rows tagged with ``market_id IS NOT NULL`` and
``notes LIKE 'cross_bracket%'``. One row = one leg of a portfolio.

``single_side`` — every other directional_shadow row. One row = one
trade.py cycle decision.

Conventions
-----------
``alpha_backtest.won_yes`` stores **"did our trade win"** — that equals
"YES outcome happened" for YES-side rows but is *flipped* for NO-side
rows. See ``bot/learning/alpha_log.py:fill_settlement``. The Brier
formula compares prediction (P(YES outcome)) to the actual YES outcome,
so for NO-side rows we flip the column: ``(1 - won_yes)``.

PnL math
--------
``alpha_backtest.realized_pnl_cents`` was not back-filled historically;
we recompute from (price_cents, won_yes). Since ``won_yes`` already
encodes "our trade won," the formula is the same regardless of side:

    gross = (won_yes ? 100 - price_cents : -price_cents)
    net   = gross - kalshi_maker_fee(1, price_cents)   # 1 contract

Cross-bracket would post limit orders (maker fee), so maker fee is the
realistic execution model. Single-side directional currently uses
crossing orders (taker), but for cohort comparison we use maker
uniformly so the only delta between cohorts is the strategy itself, not
the assumed execution path.

Caveats
-------
* Cross-bracket selection bias: scores 6 brackets/settlement; single-side
  scores 1 per cycle visit after pre-filter (volume, dedup, exposure).
  So comparison is per-leg quality, not per-cycle yield.
* Settlement back-fill required: ts_settle_unix IS NOT NULL.
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from typing import Optional


# Gross PnL: won_yes already means "our trade won" — no side check needed.
_GROSS_PNL_EXPR = "(100.0 * won_yes - price_cents)"

# Kalshi maker fee for 1 contract at price P:
#   ceil(0.0175 * P * (100-P) / 100) = ceil(175 * P * (100-P) / 1000000)
# Integer ceil via (n + d - 1) // d. Returns 0 if price out of [1, 99].
_MAKER_FEE_EXPR = """
    CASE
        WHEN price_cents > 0 AND price_cents < 100
        THEN (175 * price_cents * (100 - price_cents) + 999999) / 1000000
        ELSE 0
    END
"""

_NET_PNL_EXPR = f"({_GROSS_PNL_EXPR} - {_MAKER_FEE_EXPR})"

# Did our trade win? (binary)
_WON_EXPR = "CAST(won_yes AS REAL)"

# Did the YES outcome happen? Equals won_yes for YES-side rows, flipped
# for NO-side rows.
_YES_OUTCOME_EXPR = "(CASE WHEN side='yes' THEN won_yes ELSE 1 - won_yes END)"

# Brier: (predicted P(YES) - actual YES outcome)^2
_BRIER_EXPR = (
    f"((ensemble_p_yes - {_YES_OUTCOME_EXPR}) "
    f" * (ensemble_p_yes - {_YES_OUTCOME_EXPR}))"
)

_COHORT_EXPR = (
    "CASE WHEN market_id IS NOT NULL AND notes LIKE 'cross_bracket%' "
    "THEN 'cross_bracket' ELSE 'single_side' END"
)


def _settled_filter() -> str:
    return (
        "ts_settle_unix IS NOT NULL "
        "AND won_yes IS NOT NULL "
        "AND price_cents IS NOT NULL "
        "AND ensemble_p_yes IS NOT NULL "
        "AND side IN ('yes','no') "
        "AND decision_type='directional_shadow'"
    )


def per_leg_summary(conn: sqlite3.Connection, family_filter: Optional[str] = None) -> list[dict]:
    """Per (cohort, family): n, mean PnL/leg with 95% CI, win rate, Brier.

    Returns rows sorted (cohort, family).
    """
    family_clause = ""
    params: tuple = ()
    if family_filter:
        family_clause = "AND family = ?"
        params = (family_filter,)

    rows = conn.execute(
        f"""
        SELECT
            {_COHORT_EXPR} AS cohort,
            family,
            COUNT(*) AS n,
            AVG({_GROSS_PNL_EXPR}) AS mean_gross_pnl,
            AVG({_NET_PNL_EXPR}) AS mean_net_pnl,
            -- Sample stddev via E[X^2] - E[X]^2 (numerically fine here)
            (AVG({_NET_PNL_EXPR} * {_NET_PNL_EXPR})
             - AVG({_NET_PNL_EXPR}) * AVG({_NET_PNL_EXPR})) AS var_net_pnl,
            AVG({_WON_EXPR}) AS win_rate,
            AVG({_BRIER_EXPR}) AS brier
        FROM alpha_backtest
        WHERE {_settled_filter()}
        {family_clause}
        GROUP BY cohort, family
        ORDER BY cohort, family
        """,
        params,
    ).fetchall()

    out: list[dict] = []
    for cohort, family, n, mean_gross, mean_net, var_net, win_rate, brier in rows:
        # Population variance from AVG diff can go slightly negative on
        # near-degenerate samples; clamp to 0.
        var_net = max(0.0, float(var_net or 0.0))
        sd = math.sqrt(var_net)
        ci95 = 1.96 * sd / math.sqrt(n) if n > 1 else float("nan")
        out.append({
            "cohort": cohort,
            "family": family,
            "n": n,
            "mean_gross_pnl_cents": mean_gross,
            "mean_net_pnl_cents": mean_net,
            "ci95_cents": ci95,
            "win_rate": win_rate,
            "brier": brier,
        })
    return out


def per_portfolio_summary(conn: sqlite3.Connection) -> list[dict]:
    """Cross-bracket only: aggregate per market_id.

    Sums realized PnL across all logged legs of each portfolio. Skipped
    legs don't write rows so the sum is over fired legs only — that's
    the correct "what would we have made" number.
    """
    rows = conn.execute(
        f"""
        WITH portfolio AS (
            SELECT
                market_id,
                family,
                portfolio_leg_count,
                COUNT(*) AS legs_fired,
                SUM({_GROSS_PNL_EXPR}) AS portfolio_gross,
                SUM({_NET_PNL_EXPR}) AS portfolio_net
            FROM alpha_backtest
            WHERE {_settled_filter()}
              AND market_id IS NOT NULL
              AND notes LIKE 'cross_bracket%'
            GROUP BY market_id, family, portfolio_leg_count
        )
        SELECT
            family,
            COUNT(*) AS n_portfolios,
            AVG(legs_fired) AS mean_legs_fired,
            AVG(portfolio_leg_count) AS mean_legs_total,
            AVG(portfolio_gross) AS mean_portfolio_gross,
            AVG(portfolio_net) AS mean_portfolio_net,
            (AVG(portfolio_net * portfolio_net)
             - AVG(portfolio_net) * AVG(portfolio_net)) AS var_portfolio_net
        FROM portfolio
        GROUP BY family
        ORDER BY family
        """
    ).fetchall()

    out: list[dict] = []
    for family, n, mean_fired, mean_total, mean_gross, mean_net, var_net in rows:
        var_net = max(0.0, float(var_net or 0.0))
        sd = math.sqrt(var_net)
        ci95 = 1.96 * sd / math.sqrt(n) if n > 1 else float("nan")
        out.append({
            "family": family,
            "n_portfolios": n,
            "mean_legs_fired": mean_fired,
            "mean_legs_total": mean_total,
            "mean_portfolio_gross_cents": mean_gross,
            "mean_portfolio_net_cents": mean_net,
            "ci95_cents": ci95,
        })
    return out


def _fmt_cents(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "  n/a "
    return f"{v:+6.1f}¢"


def _fmt_brier(v) -> str:
    if v is None:
        return "  n/a"
    return f"{v:.3f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "  n/a"
    return f"{v * 100:5.1f}%"


def _build_report_lines(conn: sqlite3.Connection) -> list[str]:
    """Single source of truth for the report. Returns lines for print or
    write-to-file."""
    out: list[str] = []
    out.append("═" * 96)
    out.append("Phase B.3 retro-replay: cross-bracket vs single-side directional shadow")
    out.append("═" * 96)

    out.append("")
    out.append("── Per-leg comparison ──────────────────────────────────────────────────")
    out.append(
        f"{'cohort':<14} {'family':<14} {'n':>5} "
        f"{'gross_pnl':>11} {'net_pnl':>11} {'±95%CI':>10} "
        f"{'win_rate':>9} {'Brier':>7}"
    )
    out.append("─" * 96)

    leg_rows = per_leg_summary(conn)
    if not leg_rows:
        out.append("  (no settled rows yet)")
    else:
        for r in leg_rows:
            out.append(
                f"{r['cohort']:<14} {r['family'] or '?':<14} "
                f"{r['n']:>5} "
                f"{_fmt_cents(r['mean_gross_pnl_cents']):>11} "
                f"{_fmt_cents(r['mean_net_pnl_cents']):>11} "
                f"{_fmt_cents(r['ci95_cents']):>10} "
                f"{_fmt_pct(r['win_rate']):>9} "
                f"{_fmt_brier(r['brier']):>7}"
            )

    out.append("")
    out.append("── Cross-bracket per-portfolio ─────────────────────────────────────────")
    out.append(
        f"{'family':<14} {'n_port':>6} {'legs/port':>10} "
        f"{'gross':>11} {'net':>11} {'±95%CI':>10}"
    )
    out.append("─" * 96)

    port_rows = per_portfolio_summary(conn)
    if not port_rows:
        out.append("  (no settled cross-bracket portfolios yet — first settles tomorrow)")
    else:
        for r in port_rows:
            legs_str = (
                f"{r['mean_legs_fired']:.1f}/{r['mean_legs_total']:.0f}"
                if r['mean_legs_total'] is not None else "?"
            )
            out.append(
                f"{r['family'] or '?':<14} {r['n_portfolios']:>6} "
                f"{legs_str:>10} "
                f"{_fmt_cents(r['mean_portfolio_gross_cents']):>11} "
                f"{_fmt_cents(r['mean_portfolio_net_cents']):>11} "
                f"{_fmt_cents(r['ci95_cents']):>10}"
            )

    out.append("")
    out.append("── Go/no-go heuristic ──────────────────────────────────────────────────")
    out.append("  PROMOTE if cross_bracket Brier < single_side Brier per family")
    out.append("  AND cross_bracket mean_net_pnl > single_side mean_net_pnl per family")
    out.append("  AND n ≥ 30 per cohort per family (CI tightness)")
    out.append("  Otherwise: keep shadowing.")
    out.append("")
    return out


def print_report(conn: sqlite3.Connection) -> None:
    for line in _build_report_lines(conn):
        print(line)


def write_report(conn: sqlite3.Connection, md_path: str, csv_path: str) -> None:
    """Persist the report.

    md_path  — overwritten each fire with the latest snapshot (one stable
               filename so 'tail -f' / browser-bookmark style reads work).
    csv_path — appended each fire (one row per (run, cohort, family) leg
               summary) so trends across days are diffable / plottable.
    """
    import csv
    from datetime import datetime, timezone
    import os.path

    os.makedirs(os.path.dirname(md_path) or ".", exist_ok=True)

    # Markdown: latest snapshot only, overwrite.
    with open(md_path, "w") as f:
        f.write("# Cross-bracket validation report\n\n")
        f.write(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_\n\n")
        f.write("```\n")
        for line in _build_report_lines(conn):
            f.write(line + "\n")
        f.write("```\n")

    # CSV: append one row per (run, cohort, family) leg-level result.
    leg_rows = per_leg_summary(conn)
    new_file = not os.path.exists(csv_path)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow([
                "run_ts", "cohort", "family", "n",
                "mean_gross_pnl_cents", "mean_net_pnl_cents", "ci95_cents",
                "win_rate", "brier",
            ])
        for r in leg_rows:
            w.writerow([
                ts, r["cohort"], r["family"] or "", r["n"],
                _csv_num(r["mean_gross_pnl_cents"]),
                _csv_num(r["mean_net_pnl_cents"]),
                _csv_num(r["ci95_cents"]),
                _csv_num(r["win_rate"]),
                _csv_num(r["brier"]),
            ])


def _csv_num(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=os.environ.get("DB_PATH", "kalshi_trades.db"))
    p.add_argument("--md-out", default=None,
                   help="If set, write markdown report to this path.")
    p.add_argument("--csv-out", default=None,
                   help="If set, append CSV row to this path.")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    print_report(conn)
    if args.md_out and args.csv_out:
        write_report(conn, args.md_out, args.csv_out)
    elif args.md_out or args.csv_out:
        print("(provide both --md-out and --csv-out to persist)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
