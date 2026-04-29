"""Cross-bracket portfolio shadow logger — Phase B.3 rollout.

Runs every cycle: pulls open weather markets, groups by settlement
event, scores via ``score_market_portfolio``, logs each per-bracket
decision to ``alpha_backtest`` with a shared ``market_id`` (settlement
key) so we can reconstruct portfolios later.

This is shadow-only — no orders placed, no live behavior change.
After accumulating ~1 week of decisions, compare cross-bracket
realized PnL to the existing single-side directional shadow.
Promote to live trading only if cross-bracket Brier / PnL beats
the single-side flow.

Why a separate runner instead of patching trade.py:
  * Cleanest separation. Existing trade flow keeps producing one
    decision per market visit; cross-bracket scoring runs in parallel.
  * Easier to roll back — disable one scheduler task vs refactor.
  * Shadow data is logged to the same alpha_backtest table, joinable
    by ticker for retro analysis.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from bot.signals.weather_ensemble_v2 import (
    _city_for_ticker,
    _collect_gaussians,
    _weighted_inputs_with_group_discount,
    _COMBINED_SIGMA_FLOOR_F,
)
from bot.signals.weather_forecast import combine_gaussian


logger = logging.getLogger(__name__)


def run_cross_bracket_shadow(conn) -> dict:
    """Score all currently-open weather markets via cross-bracket
    portfolio. Log each per-bracket decision to alpha_backtest with
    decision_type='cross_bracket_shadow'.

    Returns stats dict for telemetry.
    """
    stats = {
        "settlements_scored": 0,
        "total_brackets": 0,
        "decisions_buy_yes": 0,
        "decisions_buy_no": 0,
        "decisions_skip": 0,
        "errors": 0,
    }

    try:
        markets = _fetch_open_weather_markets()
    except Exception as exc:
        logger.warning("[cross_bracket_shadow] fetch failed: %s", exc)
        stats["errors"] += 1
        return stats

    if not markets:
        return stats

    from bot.scoring.bracket_portfolio import (
        group_markets_by_settlement, score_market_portfolio,
    )

    grouped = group_markets_by_settlement(markets)
    stats["settlements_scored"] = len(grouped)

    for settlement_key, group in grouped.items():
        try:
            decisions = _score_one_settlement(group)
        except Exception as exc:
            logger.warning(
                "[cross_bracket_shadow] %s scoring failed: %s",
                settlement_key, exc,
            )
            stats["errors"] += 1
            continue

        if not decisions:
            continue

        stats["total_brackets"] += len(decisions)
        for d in decisions:
            if d.action == "buy_yes":
                stats["decisions_buy_yes"] += 1
            elif d.action == "buy_no":
                stats["decisions_buy_no"] += 1
            else:
                stats["decisions_skip"] += 1

        # Log to alpha_backtest. Each leg gets its own row, all sharing
        # the settlement_key as a market_id (stored in `notes` for now;
        # a dedicated column would be cleaner but adds schema migration).
        _log_decisions_to_alpha_backtest(conn, settlement_key, group, decisions)

    return stats


def _fetch_open_weather_markets() -> list[dict]:
    """Pull open KXHIGH* markets from Kalshi.

    Returns a flat list of market_data dicts (Kalshi's response
    format). Used by the cycle to score all weather brackets at once.
    """
    from bot.api import api_get

    out: list[dict] = []
    for series in ("KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA",
                   "KXHIGHAUS", "KXHIGHLAX", "KXHIGHDEN"):
        try:
            # api_get already prepends /trade-api/v2 — pass only the path tail.
            data = api_get(
                f"/markets?status=open&series_ticker={series}&limit=200"
            )
            if data:
                out.extend(data.get("markets", []))
        except Exception as exc:
            logger.warning("[cross_bracket_shadow] %s fetch failed: %s",
                          series, exc)
    return out


def _score_one_settlement(group: list[dict]) -> list:
    """Compute combined μ/σ from the first market in the group, then
    score every bracket against it.

    All markets in ``group`` share the same settlement event so they
    have the same predicted distribution.
    """
    from bot.scoring.bracket_portfolio import score_market_portfolio

    if not group:
        return []

    sample = group[0]
    gaussians = _collect_gaussians(sample.get("ticker", ""), sample)
    if not gaussians:
        return []

    weighted = _weighted_inputs_with_group_discount(gaussians)
    combined = combine_gaussian(weighted, combined_name="combined_v2")
    if combined is None:
        return []

    # Enforce σ floor (matches predict_v2 step 4d).
    sigma = max(combined.sigma_f, _COMBINED_SIGMA_FLOOR_F)

    return score_market_portfolio(
        group,
        combined_mu=combined.mean_f,
        combined_sigma=sigma,
        sigma_floor=_COMBINED_SIGMA_FLOOR_F,
    )


def _log_decisions_to_alpha_backtest(
    conn, settlement_key: str, group: list[dict], decisions: list,
) -> None:
    """One alpha_backtest row per non-skip decision, tagged with the
    settlement key in ``notes`` so portfolios are reconstructable."""
    from bot.learning.alpha_log import (
        DecisionOutcome, DecisionType, EnsembleSnapshot, MarketSnapshot,
        log_decision, market_snapshot_from_dict,
    )

    # Build a lookup so we can resolve each decision's market_data
    by_ticker = {m.get("ticker"): m for m in group}

    for leg_idx, d in enumerate(decisions):
        if d.action == "skip":
            # Optionally log skips too — but for shadow analysis we mostly
            # care about would-be trades. Keep skip rate stat in scheduler
            # log line; don't bloat alpha_backtest with skip rows.
            continue

        market = by_ticker.get(d.ticker)
        if market is None:
            continue

        # Notes carries the per-leg edge; market_id and leg_count live in
        # their own columns (see bot/db.py alpha_backtest schema).
        edge_yes_str = (
            f";edge_yes={d.edge_yes:+.3f}" if d.edge_yes is not None else ""
        )
        edge_no_str = (
            f";edge_no={d.edge_no:+.3f}" if d.edge_no is not None else ""
        )
        notes = f"cross_bracket;leg={leg_idx};p_yes={d.p_yes:.3f}{edge_yes_str}{edge_no_str}"

        try:
            log_decision(
                conn,
                ticker=d.ticker,
                decision_type=DecisionType.DIRECTIONAL_SHADOW,  # reuse type for now
                decision_outcome=DecisionOutcome.SHADOW_ONLY,
                ensemble=EnsembleSnapshot(p_yes=float(d.p_yes)),
                market=market_snapshot_from_dict(market),
                side=d.side,
                price_cents=d.price_cents,
                contracts=1,  # placeholder — Kelly happens at live promotion
                notes=notes,
                market_id=settlement_key,
                portfolio_leg_count=len(decisions),
            )
        except Exception as exc:
            logger.warning("[cross_bracket_shadow] log_decision failed for %s: %r", d.ticker, exc)
