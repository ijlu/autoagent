"""Adaptive source weight learning from settlement track records.

Uses Bayesian shrinkage: starts with prior (hardcoded weights from config) and
blends toward empirical accuracy as sample size grows.
"""

from __future__ import annotations

import re

from bot.config import SOURCE_WEIGHTS

_LEARNED_WEIGHTS = None  # cached per run


def _parse_sources_from_strategy(strategy_str):
    """Extract individual source names from strategy like 'ensemble(polymarket+weather+crypto)'."""
    if not strategy_str:
        return []
    m = re.search(r'ensemble\(([^)]+)\)', strategy_str)
    if not m:
        # Might be a single source like 'momentum_adj=+0.02'
        for src in SOURCE_WEIGHTS:
            if src in (strategy_str or "").lower():
                return [src]
        return []
    return [s.strip().lower() for s in m.group(1).split("+")]


def compute_adaptive_weights(conn):
    """Compute source weights adjusted by actual track record.
    Uses a Bayesian blend: start with prior (hardcoded weights) and blend toward
    empirical accuracy as sample size grows. Requires MIN_ADAPTIVE_SAMPLES to
    start adjusting. Returns dict of source -> adjusted weight."""
    global _LEARNED_WEIGHTS
    if _LEARNED_WEIGHTS is not None:
        return _LEARNED_WEIGHTS

    MIN_ADAPTIVE_SAMPLES = 10  # need at least this many per source to adjust

    # Get all settlements with their strategy (source combo) info
    rows = conn.execute(
        "SELECT strategy, won, profit_cents FROM settlements WHERE strategy IS NOT NULL"
    ).fetchall()

    if len(rows) < 20:
        # Not enough data — use defaults
        _LEARNED_WEIGHTS = dict(SOURCE_WEIGHTS)
        print(f"[adaptive] Too few settlements ({len(rows)}) — using default weights")
        return _LEARNED_WEIGHTS

    # Count wins/losses per source (a source gets credit when it was part of the ensemble)
    source_stats = {}  # {source: {"wins": n, "losses": n, "total_profit": x}}
    for strat, won, profit in rows:
        sources = _parse_sources_from_strategy(strat)
        for src in sources:
            if src not in source_stats:
                source_stats[src] = {"wins": 0, "losses": 0, "total_profit": 0}
            if won:
                source_stats[src]["wins"] += 1
            else:
                source_stats[src]["losses"] += 1
            source_stats[src]["total_profit"] += (profit or 0)

    # Compute adjusted weights using Bayesian shrinkage toward prior
    adjusted = {}
    for src, prior_weight in SOURCE_WEIGHTS.items():
        stats = source_stats.get(src)
        if not stats or (stats["wins"] + stats["losses"]) < MIN_ADAPTIVE_SAMPLES:
            # Not enough data — keep prior
            adjusted[src] = prior_weight
            continue

        n = stats["wins"] + stats["losses"]
        empirical_wr = stats["wins"] / n
        avg_profit = stats["total_profit"] / n

        # Blend factor: 0 = all prior, 1 = all empirical
        # Sigmoid that starts shifting at 20 samples and reaches ~0.7 at 100
        blend = min(0.7, (n - MIN_ADAPTIVE_SAMPLES) / 130)

        # Empirical quality score: win rate * profit direction
        # A source that wins 60% with positive avg profit is great
        # A source that wins 55% but loses money (fees?) is not
        if avg_profit > 0:
            empirical_quality = empirical_wr * 1.2  # bonus for profitable
        else:
            empirical_quality = empirical_wr * 0.8  # penalty for unprofitable despite wins

        # Scale to weight range [0.10, 0.95]
        empirical_weight = max(0.10, min(0.95, empirical_quality))

        adjusted[src] = prior_weight * (1 - blend) + empirical_weight * blend

        direction = "↑" if adjusted[src] > prior_weight else "↓" if adjusted[src] < prior_weight else "="
        print(f"[adaptive] {src}: prior={prior_weight:.2f} → adjusted={adjusted[src]:.2f} "
              f"{direction} (wr={empirical_wr:.0%}, n={n}, profit={avg_profit:+.0f}¢/trade)")

    _LEARNED_WEIGHTS = adjusted
    return adjusted
