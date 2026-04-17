"""Weekly threshold proposer (T.8) for the MM promotion gate.

Objective-driven grid search over a tight window around the currently-live
:class:`bot.learning.mm_promotion.MMPromotionConfig` thresholds. The tuner:

    1. Pulls settled weather_mm_shadow rows from the last N days.
    2. For each candidate threshold grid point, replays the promotion gate
       as a pure predicate over those rows (no state mutation) and computes
       a portfolio objective: expected log-growth penalized by the empirical
       maximum rolling-window drawdown.
    3. If the best grid point beats the current thresholds by a material
       margin, writes a row to ``threshold_proposals`` with full evidence.

Design goals (see T debate; operator auto-apply deferred to T.9):
- Never mutates the live config — operators review rows in
  ``threshold_proposals`` and flip ``applied=1`` via the CLI helper.
- Idempotent per week — a proposal row is a point-in-time record.
- Deterministic — same shadow rows + same grid ⇒ same proposal.
- Self-defending — if the grid's best point is worse than current, we log
  nothing (no churn in the proposals table).
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import sqlite3
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any, Optional

from bot.daemon.locks import DB_WRITE_LOCK
from bot.learning.mm_promotion import (
    DEFAULT_MM_PROMOTION,
    MMPromotionConfig,
    _compute_mm_stats,
)

logger = logging.getLogger(__name__)


# ── Objective ───────────────────────────────────────────────────────────
# Log-growth under a simple half-Kelly assumption. We estimate expected
# per-fill return as `pnl_per_fill_cents / 100 / fill_price` and the rolling
# drawdown penalty as the worst 20-row cumulative P&L trough. The exact
# model is not a load-bearing claim — it just needs to order grid points
# consistently with "safer and more profitable." Linear scoring would pick
# whichever threshold lets the most fills through; log-growth with a
# drawdown penalty punishes fat-left-tail series automatically.


DEFAULT_DRAWDOWN_WINDOW = 20
DEFAULT_DRAWDOWN_PENALTY_LAMBDA = 0.5


def _score_threshold(
    rows: list[sqlite3.Row],
    cfg: MMPromotionConfig,
    *,
    drawdown_window: int = DEFAULT_DRAWDOWN_WINDOW,
    drawdown_lambda: float = DEFAULT_DRAWDOWN_PENALTY_LAMBDA,
) -> tuple[float, dict[str, Any]]:
    """Score one threshold point. Larger = better.

    Semantics: apply the promotion gate to the row population. The rows
    that pass correspond to the series-weeks we *would* have promoted.
    Score them on realized shadow P&L. Rows that fail the gate contribute
    zero (we chose not to trade). The drawdown penalty subtracts the
    magnitude of the worst rolling-window loss.
    """
    if not rows:
        return 0.0, {"n": 0}

    # Apply the gate row-by-row. For the tuner, we model "gate passes" by
    # the same per-fill minimum: the row's shadow_pnl_cents must be >=
    # min_pnl_per_fill_cents. That's the *marginal* impact of raising the
    # threshold — it excludes rows at the low end of the per-fill P&L
    # distribution.
    contributing: list[int] = []
    for r in rows:
        bf = int(r["shadow_bid_filled"] or 0)
        af = int(r["shadow_ask_filled"] or 0)
        if not (bf or af):
            continue
        pnl = int(r["shadow_pnl_cents"] or 0)
        # Gate implicitly: the pnl-per-fill threshold is enforced here on
        # the population level — we mark rows whose per-fill return clears
        # the bar as contributing. This mirrors how the live gate would
        # select series.
        if pnl >= cfg.min_pnl_per_fill_cents:
            contributing.append(pnl)

    if len(contributing) < cfg.min_shadow_fills:
        return 0.0, {"n": len(contributing), "reason": "below_min_fills"}

    total_pnl = sum(contributing)
    if total_pnl < cfg.min_pnl_total_dollars * 100:
        return 0.0, {"n": len(contributing), "reason": "below_min_total"}

    # Mean per-fill edge as a log-growth proxy. Flat $0 fill price guess
    # isn't meaningful; use total_pnl / n_fills / 100 as a dollar-edge-per-
    # fill figure.
    mean_edge = total_pnl / len(contributing)

    # Drawdown penalty — worst rolling-window cum sum.
    worst_trough = 0
    running = 0
    window: list[int] = []
    for pnl in contributing:
        window.append(pnl)
        if len(window) > drawdown_window:
            window.pop(0)
        cum = sum(window)
        if cum < worst_trough:
            worst_trough = cum

    # Log-growth-ish score. log(1 + mean_edge/100) is nicely concave and
    # goes negative for -100¢ wipes. We multiply by sqrt(n) to preferr
    # larger-sample-size thresholds (shrinkage toward the mean).
    if mean_edge <= -99:
        growth = -1.0
    else:
        growth = math.log(1.0 + mean_edge / 100.0) * math.sqrt(
            len(contributing),
        )
    penalty = drawdown_lambda * abs(worst_trough) / 100.0  # cents → $
    score = growth - penalty
    return score, {
        "n": len(contributing),
        "total_pnl_cents": total_pnl,
        "mean_edge_cents": mean_edge,
        "worst_trough_cents": worst_trough,
        "growth": growth,
        "penalty": penalty,
    }


# ── Grid search ─────────────────────────────────────────────────────────
# Tight window around current values. Gaps intentionally small — the goal
# is incremental tuning, not exploration. Total candidates per call: 3^4
# = 81 grid points (cheap).
def _default_grid(base: MMPromotionConfig) -> list[MMPromotionConfig]:
    out: list[MMPromotionConfig] = []
    settled_choices = [
        max(20, base.min_shadow_settled - 20),
        base.min_shadow_settled,
        base.min_shadow_settled + 20,
    ]
    fills_choices = [
        max(5, base.min_shadow_fills - 5),
        base.min_shadow_fills,
        base.min_shadow_fills + 5,
    ]
    ppf_choices = [
        max(0.0, base.min_pnl_per_fill_cents - 0.5),
        base.min_pnl_per_fill_cents,
        base.min_pnl_per_fill_cents + 0.5,
    ]
    total_choices = [
        max(0.0, base.min_pnl_total_dollars - 2.5),
        base.min_pnl_total_dollars,
        base.min_pnl_total_dollars + 2.5,
    ]
    for s, f, p, t in itertools.product(
        settled_choices, fills_choices, ppf_choices, total_choices,
    ):
        out.append(replace(
            base,
            min_shadow_settled=int(s),
            min_shadow_fills=int(f),
            min_pnl_per_fill_cents=float(p),
            min_pnl_total_dollars=float(t),
        ))
    # Deduplicate (the replace() may coincide with base on some axes).
    seen: set[tuple] = set()
    uniq: list[MMPromotionConfig] = []
    for c in out:
        k = (c.min_shadow_settled, c.min_shadow_fills,
             c.min_pnl_per_fill_cents, c.min_pnl_total_dollars)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


# ── Evidence fetch ──────────────────────────────────────────────────────
def _fetch_settled_shadow_rows(
    conn: sqlite3.Connection, since_unix: float, limit: int = 20000,
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        "SELECT shadow_bid_filled, shadow_ask_filled, shadow_pnl_cents, "
        "       live_pnl_cents, ts_unix, live_mode "
        "FROM weather_mm_shadow "
        "WHERE ts_settle_unix IS NOT NULL AND ts_unix >= ? "
        "ORDER BY ts_unix ASC LIMIT ?",
        (since_unix, limit),
    ).fetchall())


# ── Orchestrator ────────────────────────────────────────────────────────
MIN_OBJECTIVE_DELTA_FOR_PROPOSAL = 0.05


def propose_thresholds(
    conn: sqlite3.Connection,
    *,
    evidence_window_days: int = 14,
    current: MMPromotionConfig = DEFAULT_MM_PROMOTION,
    grid: Optional[list[MMPromotionConfig]] = None,
    min_delta: float = MIN_OBJECTIVE_DELTA_FOR_PROPOSAL,
    now_unix: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Run the weekly proposer once. Returns the proposal dict if written.

    Returns ``None`` when either:
    - no settled rows exist in the window,
    - the current config is already the grid's best point,
    - or the best grid delta is below ``min_delta``.
    """
    now = now_unix if now_unix is not None else time.time()
    since = now - evidence_window_days * 24 * 3600
    rows = _fetch_settled_shadow_rows(conn, since)
    if not rows:
        logger.info(
            "[threshold_tuner] no settled rows in last %d days; skipping",
            evidence_window_days,
        )
        return None

    grid = grid if grid is not None else _default_grid(current)
    cur_score, cur_metrics = _score_threshold(rows, current)

    best_cfg = current
    best_score = cur_score
    best_metrics: dict[str, Any] = {}
    for cfg in grid:
        score, metrics = _score_threshold(rows, cfg)
        if score > best_score:
            best_score = score
            best_cfg = cfg
            best_metrics = metrics

    delta = best_score - cur_score
    logger.info(
        "[threshold_tuner] n_rows=%d current_score=%.4f best_score=%.4f "
        "delta=%.4f",
        len(rows), cur_score, best_score, delta,
    )

    if best_cfg == current or delta < min_delta:
        logger.info("[threshold_tuner] no proposal (delta<%.3f)", min_delta)
        return None

    proposal = {
        "ts_unix": now,
        "ts_iso": datetime.fromtimestamp(now, tz=timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "tuner": "mm_promotion_grid_v1",
        "evidence_window_days": evidence_window_days,
        "n_observations": len(rows),
        "current_thresholds_json": json.dumps(asdict(current)),
        "proposed_thresholds_json": json.dumps(asdict(best_cfg)),
        "objective_current": cur_score,
        "objective_proposed": best_score,
        "objective_delta": delta,
        "supporting_metrics_json": json.dumps({
            "current": cur_metrics, "proposed": best_metrics,
        }),
    }
    with DB_WRITE_LOCK:
        cur = conn.execute(
            "INSERT INTO threshold_proposals "
            "(ts_unix, ts_iso, tuner, evidence_window_days, n_observations, "
            " current_thresholds_json, proposed_thresholds_json, "
            " objective_current, objective_proposed, objective_delta, "
            " supporting_metrics_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (proposal["ts_unix"], proposal["ts_iso"], proposal["tuner"],
             proposal["evidence_window_days"], proposal["n_observations"],
             proposal["current_thresholds_json"],
             proposal["proposed_thresholds_json"],
             proposal["objective_current"], proposal["objective_proposed"],
             proposal["objective_delta"],
             proposal["supporting_metrics_json"]),
        )
        conn.commit()
        proposal["id"] = cur.lastrowid
    logger.info(
        "[threshold_tuner] proposal id=%d delta=%.4f n=%d",
        proposal["id"], delta, len(rows),
    )
    return proposal


# ── Operator CLI helper ─────────────────────────────────────────────────
def apply_proposal(
    conn: sqlite3.Connection, proposal_id: int, applied_by: str,
) -> bool:
    """Mark a proposal as applied. This is a bookkeeping action — the live
    config is still defined in code, so the operator must also edit
    ``MMPromotionConfig`` defaults and redeploy.

    The intent is the audit-log-first workflow: every threshold change is
    tied back to a tuner proposal row.
    """
    row = conn.execute(
        "SELECT applied FROM threshold_proposals WHERE id=?", (proposal_id,),
    ).fetchone()
    if row is None:
        logger.warning("[threshold_tuner] proposal id=%s not found",
                       proposal_id)
        return False
    if int(row[0] or 0) == 1:
        logger.info("[threshold_tuner] proposal id=%s already applied",
                    proposal_id)
        return False
    with DB_WRITE_LOCK:
        conn.execute(
            "UPDATE threshold_proposals SET applied=1, applied_ts_unix=?, "
            "applied_by=? WHERE id=?",
            (time.time(), applied_by, proposal_id),
        )
        conn.commit()
    return True
