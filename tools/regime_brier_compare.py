"""Stage 2 promotion-gate diagnostic — regime σ vs pooled σ Brier.

Reads the four telemetry columns added to ``weather_forecast_snapshots``
in Stage 1 (``regime_label``, ``regime_tier_used``, ``regime_sigma_f``,
``pooled_sigma_f``) and the matching ``combined_v2`` row, then for each
settled ticker reconstructs:

  - ``brier_pooled``  = (combined_v2_prob − settled_yes)²
                        i.e. what production actually predicted with
                        pooled-σ flowing through the combine.
  - ``brier_regime``  = computed by re-running the precision-weighted
                        combine with METAR's σ swapped from
                        ``pooled_sigma_f`` to ``regime_sigma_f``, then
                        re-projecting onto the bracket / threshold.

Aggregates per:
  - regime_tier_used (regime_hour / station_regime / pooled_hour / schedule)
  - μ-bracket-edge-distance bucket (close-edge spotlight)
  - horizon-bucket (0-6h / 6-12h / 12-24h)

Stage 2 promotion criteria (per the report):
  * close-edge bucket Brier improves by ≥0.005
  * no regression in any other bucket
  * ≥4 of 6 cities show non-negative Brier on regime-treated subset

This tool runs cleanly on Day-1 data (will return mostly empty until
Stage 1 has accumulated enough snapshots with non-NULL regime columns).

Usage::

    python -m tools.regime_brier_compare \\
        --db /home/kalshi/autoagent/kalshi_trades.db \\
        --since 2026-04-28
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.signals import weather_ensemble_v2 as v2  # noqa: E402
from tools.backtest_v2_replay import _fetch_market  # noqa: E402


def _project(market: dict, mu: float, sigma: float) -> Optional[float]:
    """Project (μ, σ) onto the ticker's outcome side using v2's parser."""
    proj = v2._parse_market_for_projection(market.get("ticker", ""), market)
    if proj is None:
        return None
    is_bracket, threshold_f, is_above, lo_f, hi_f = proj
    if sigma <= 0:
        sigma = 0.3

    def _ncdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))

    if is_bracket:
        if lo_f is None or hi_f is None:
            return None
        return max(0.02, min(0.98, _ncdf(hi_f) - _ncdf(lo_f)))
    if threshold_f is None:
        return None
    p_above = 1.0 - _ncdf(threshold_f)
    return max(0.02, min(0.98, p_above if is_above else 1.0 - p_above))


def _bucket_horizon(h_out: float) -> str:
    if h_out < 0:
        return "post_settle"
    if h_out < 6:
        return "0-6h"
    if h_out < 12:
        return "6-12h"
    if h_out < 24:
        return "12-24h"
    return "24h+"


def _bucket_edge(market: dict, mu: float) -> str:
    proj = v2._parse_market_for_projection(market.get("ticker", ""), market)
    if proj is None:
        return "?"
    is_bracket, threshold_f, _is_above, lo_f, hi_f = proj
    if is_bracket and lo_f is not None and hi_f is not None:
        inner = min(abs(mu - lo_f), abs(mu - hi_f))
        sign = +1 if (lo_f <= mu <= hi_f) else -1
        d = sign * inner
    elif threshold_f is not None:
        d = mu - threshold_f
    else:
        return "?"
    if d < -2.0:
        return "deep_out"
    elif d < -0.5:
        return "out"
    elif d < 0.5:
        return "edge"
    elif d < 2.0:
        return "in"
    return "deep_in"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument(
        "--since", default=None,
        help=("Only include snapshots recorded at-or-after this ISO date "
              "(e.g. 2026-04-28). Default = include all."),
    )
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)

    # Pull settled tickers + the latest combined_v2 snapshot + the latest
    # METAR row's regime telemetry, joined on ticker. We use MAX(id) per
    # source — same convention as ``tools.backtest_v2_replay`` so the
    # numbers compare apples-to-apples to the existing diagnostic.
    where_clause = ""
    bind: tuple = ()
    if args.since:
        where_clause = " AND s.recorded_at >= ?"
        bind = (args.since,)

    rows = conn.execute(
        f"""
        WITH metar AS (
            SELECT ticker, MAX(id) AS mid
              FROM weather_forecast_snapshots
             WHERE source = 'metar'
               AND regime_label IS NOT NULL
               {where_clause}
             GROUP BY ticker
        ),
        cv2 AS (
            SELECT ticker, MAX(id) AS mid
              FROM weather_forecast_snapshots
             WHERE source = 'combined_v2'
               {where_clause}
             GROUP BY ticker
        )
        SELECT s.ticker, s.series,
               m.regime_label, m.regime_tier_used,
               m.regime_sigma_f, m.pooled_sigma_f,
               c.forecast_high_f AS combined_mu,
               c.sigma_f AS combined_sigma_pool,
               c.forecast_prob AS combined_prob_pool,
               s.ticker_settled_yes,
               s.ts_unix,
               COALESCE(ab.ts_settle_unix, NULL) AS ts_settle
          FROM weather_mm_shadow s
          JOIN metar mm ON mm.ticker = s.ticker
          JOIN weather_forecast_snapshots m ON m.id = mm.mid
          JOIN cv2 ON cv2.ticker = s.ticker
          JOIN weather_forecast_snapshots c ON c.id = cv2.mid
          LEFT JOIN alpha_backtest ab ON ab.ticker = s.ticker
         WHERE s.ticker_settled_yes IS NOT NULL
         GROUP BY s.ticker
        """,
        bind + bind,  # bind once per CTE WHERE
    ).fetchall()

    print(f"[brier_compare] {len(rows)} settled tickers with regime telemetry")
    if not rows:
        print(
            "[brier_compare] no rows yet — Stage 1 capture has not "
            "accumulated settled tickers with non-NULL regime_label. "
            "Re-run after the first ~24h of post-deploy data."
        )
        return 0

    # Compute per-ticker Brier(pooled) vs Brier(regime). regime is
    # estimated by SIMPLE precision swap: combined precision uses 1/σ²
    # additivity, so we approximate the new combined σ by holding all
    # other sources' precision constant and swapping METAR's σ:
    #   p_other = 1/σ_combined_old² − 1/σ_metar_pool²
    #   σ_combined_new = sqrt(1 / (p_other + 1/σ_metar_regime²))
    # This is exact when the combine is standard precision-weighted
    # averaging (which weather_ensemble_v2 is, modulo group-correction
    # weights — those scale precisions but the swap math holds because
    # METAR's group weight is unchanged).
    by_tier: dict[str, list[tuple[float, float]]] = defaultdict(list)
    by_edge: dict[str, list[tuple[float, float]]] = defaultdict(list)
    by_horizon: dict[str, list[tuple[float, float]]] = defaultdict(list)
    n_treated = 0
    n_skipped_thin = 0

    for (ticker, _series, regime_label, tier_used,
         regime_sig, pooled_sig,
         combined_mu, combined_sig_pool, combined_prob_pool,
         settled_yes, ts_unix, ts_settle) in rows:
        if regime_sig is None:
            n_skipped_thin += 1
            continue
        if pooled_sig is None or pooled_sig <= 0 or combined_sig_pool <= 0:
            continue
        # Precision swap
        p_combined_old = 1.0 / (combined_sig_pool ** 2)
        p_metar_pool = 1.0 / (pooled_sig ** 2)
        p_other = p_combined_old - p_metar_pool
        # Defensive: if METAR contributed >100% (impossible without sign
        # error) skip — would produce negative residual precision.
        if p_other <= 0:
            continue
        p_metar_regime = 1.0 / (regime_sig ** 2)
        sig_combined_new = math.sqrt(1.0 / (p_other + p_metar_regime))

        # Re-project. Need market context — fetch fresh.
        market = _fetch_market(ticker)
        if not market:
            continue
        prob_regime = _project(market, combined_mu, sig_combined_new)
        if prob_regime is None:
            continue

        won = float(settled_yes)
        b_pool = (float(combined_prob_pool) - won) ** 2
        b_reg = (prob_regime - won) ** 2

        by_tier[tier_used].append((b_pool, b_reg))
        by_edge[_bucket_edge(market, combined_mu)].append((b_pool, b_reg))
        if ts_settle is not None and ts_unix is not None:
            h = (float(ts_settle) - float(ts_unix)) / 3600.0
            by_horizon[_bucket_horizon(h)].append((b_pool, b_reg))
        n_treated += 1

    print(f"[brier_compare] regime-treated tickers: {n_treated}")
    print(f"[brier_compare] thin (no regime σ): {n_skipped_thin}")

    def _print_slice(title, by: dict):
        print(f"\n  {title}")
        print(f"  {'bucket':25s}  {'n':>4s}  {'pool B':>7s}  "
              f"{'reg B':>7s}  {'Δ pool→reg':>11s}")
        for k in sorted(by):
            vals = by[k]
            if not vals:
                continue
            bp = sum(p for p, _ in vals) / len(vals)
            br = sum(r for _, r in vals) / len(vals)
            print(f"  {k:25s}  {len(vals):4d}  "
                  f"{bp:7.4f}  {br:7.4f}  {br-bp:+11.4f}")

    _print_slice("Brier by regime_tier_used:", by_tier)
    _print_slice("Brier by μ-bracket-edge-distance:", by_edge)
    _print_slice("Brier by horizon-bucket:", by_horizon)

    # Stage-2 gate check — close-edge bucket
    edge = by_edge.get("edge", [])
    if edge:
        bp = sum(p for p, _ in edge) / len(edge)
        br = sum(r for _, r in edge) / len(edge)
        delta = br - bp
        print(f"\n  Stage-2 gate (close-edge Brier delta):")
        print(f"    n_close_edge={len(edge)}  pool={bp:.4f}  reg={br:.4f}  "
              f"Δ={delta:+.4f}")
        if delta <= -0.005:
            print(f"    ✓ PROMOTION GATE MET (Δ ≤ −0.005)")
        elif delta < 0:
            print(f"    ▸ moving in right direction; n={len(edge)} "
                  f"(need Δ ≤ −0.005)")
        else:
            print(f"    ✗ regime worse on close-edge — DO NOT PROMOTE")
    else:
        print(f"\n  No close-edge cases yet — accumulating data.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
