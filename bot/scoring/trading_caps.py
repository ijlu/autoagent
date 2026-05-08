"""Trading-time risk caps for model-emitted probabilities.

This module exists to enforce a hard humility ceiling/floor on probabilities
*at the moment they become trading decisions* — sizing, edge gates, conviction
gates, fair-value posting. The model itself remains free to express extreme
probabilities (and should, for honest calibration); the cap is a downstream
risk control, applied at the trading layer only.

DO NOT use these helpers in:
  - Logging paths (alpha_backtest, opportunity_log, decision_log,
    strategy_journal). Logs must record raw model output.
  - Calibration paths (Platt fit, isotonic fit, populate_from_alpha,
    edge_convergence). Calibration must see model truth.
  - Diagnostics (cross_bracket_diagnostic, dashboards, post-mortems).

DO use these helpers at:
  - bot/scoring/bracket_portfolio.py:_decide_leg (cross-bracket)
  - bot/scoring/market_scorer.py:score_market (directional/MM scoring)
  - bot/scoring/four_factor.py:_score_edge inputs
  - trade.py:kelly_contracts (sizing chokepoint)
  - trade.py sub-strategies (clevfed/BLS/consensus/ensemble edge math)
  - bot/strategies/safe_compounder.py
  - bot/daemon/weather_quoter.py:_compute_fair_value

Background: see memory/project_layer_separation_model_vs_trading.md.
Decision date: 2026-05-08, Phase 2 item 4.
"""

from __future__ import annotations

import threading
from collections import Counter

# Cap bounds. Tightening these is a global behavior change; widening them
# is also a behavior change (e.g. removing humility). Edit deliberately.
TRADING_PROB_CAP_HI: float = 0.99
TRADING_PROB_CAP_LO: float = 0.01

# Per-source counters of how often the cap actually fires. Useful as a
# Phase-1-regression detector: if hits suddenly spike for a source, it
# means the model is regularly bumping into the ceiling, which would
# indicate σ-inflation has degraded. Read by the daemon's health-log task.
_HIT_COUNTER: Counter = Counter()
_HIT_COUNTER_LOCK = threading.Lock()


def cap_trading_prob(p: float, *, source: str) -> float:
    """Apply the trading-time humility cap to a probability.

    Args:
        p: Raw model probability in [0, 1] (callers may pass slightly out
           of range due to floating-point; we clamp into [LO, HI]).
        source: Short identifier for the callsite (e.g. "decide_leg",
           "kelly", "four_factor"). Used for hit counting only.

    Returns:
        Capped probability in [TRADING_PROB_CAP_LO, TRADING_PROB_CAP_HI].
    """
    if p > TRADING_PROB_CAP_HI:
        with _HIT_COUNTER_LOCK:
            _HIT_COUNTER[f"{source}.hi"] += 1
        return TRADING_PROB_CAP_HI
    if p < TRADING_PROB_CAP_LO:
        with _HIT_COUNTER_LOCK:
            _HIT_COUNTER[f"{source}.lo"] += 1
        return TRADING_PROB_CAP_LO
    return p


def get_hit_counts() -> dict[str, int]:
    """Snapshot the per-source cap-hit counters. Thread-safe."""
    with _HIT_COUNTER_LOCK:
        return dict(_HIT_COUNTER)


def reset_hit_counts() -> None:
    """Zero the per-source counters. Called by the daemon's health-log task
    after persisting the snapshot, so the next interval starts clean."""
    with _HIT_COUNTER_LOCK:
        _HIT_COUNTER.clear()
