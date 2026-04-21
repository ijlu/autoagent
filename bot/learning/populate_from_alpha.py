"""Settlement-driven population of learning tables from alpha_backtest rows.

Phase 1 bridges the gap between our decision journal (alpha_backtest) and the
existing learning pipeline (calibration, timing_patterns, edge_convergence,
loss_postmortems). The legacy writers in bot/learning/{calibration,
timing_patterns, postmortems, edge_convergence}.py read from the `trades` and
`mm_orders` tables — but directional is DRY_RUN (no `trades` rows) and MM was
deleted (no fresh `mm_orders` rows), so those writers are starved.

This module adds alpha_backtest-driven writers that run alongside the legacy
ones. Every downstream table now accepts rows from either source:

  - Legacy writers populate rows with alpha_id IS NULL
  - This module populates rows with alpha_id pointing at alpha_backtest.id

Idempotency is enforced via a partial unique index on alpha_id (NOT NULL).
Calls are safe to repeat each cycle — already-populated rows are filtered out
by a NOT EXISTS subquery before insertion.

Design choices
--------------

1. **One function per target table.** Keeps SQL focused, errors localised,
   and each function independently testable. `populate_all(conn)` is the
   orchestrator called from the cycle.

2. **Strict "settled alpha_backtest rows only" filter.** Every SELECT includes
   `ts_settle_unix IS NOT NULL`. We never populate learning from pending rows
   — the outcome column is the whole point of the join.

3. **Provenance tag.** Every inserted row includes `alpha_id=<id>`, and the
   `source` / `source_desc` / `source_combo` fields are prefixed with `alpha:`
   so post-hoc queries can separate shadow-learned rows from legacy.

4. **Never raises.** Logging path — if a single writer fails, the others still
   run, the cycle continues, and we log a warning. Matches the philosophy of
   alpha_log.py: instrumentation is a side effect, not a dependency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from bot.core.categorization import categorize_market
from bot.daemon.locks import DB_WRITE_LOCK
from bot.db import db_write_ctx
from bot.learning.calibration import _prob_bucket_label as _prob_bucket

logger = logging.getLogger(__name__)


# ── Loss classification thresholds ────────────────────────────────────────
# Same shape as bot/learning/postmortems.py — duplicated here because the
# legacy function ingests `trades`-joined rows with a different schema.
# Keeping thresholds in one place would require a shared helper; for now the
# duplication is localised and the values match.
_LOSS_BAD_SOURCE_GAP = 0.30        # |est - market| > 30% → bad source
_LOSS_EFFICIENT_EDGE = 0.07        # |edge| < 7% → efficient market
_LOSS_CONFIDENCE_CUTOFF = 0.55     # est > 55% + lost → adverse selection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_decision_ts(unix_ts: float) -> tuple[int, int]:
    """Return (hour_utc, day_of_week) for a unix timestamp."""
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    return dt.hour, dt.weekday()


# ══════════════════════════════════════════════════════════════════════════
# Calibration
# ══════════════════════════════════════════════════════════════════════════
def populate_calibration(conn) -> int:
    """Insert one calibration row per newly-settled alpha_backtest row.

    The calibration curve is fed P(our-side-wins) vs actual outcome. For a
    YES-side row we feed ensemble_p_yes directly; for a NO-side row we feed
    (1 - ensemble_p_yes) so the bucket is "what we thought the probability
    of winning was."

    won_yes is already normalised per-side by fill_settlement_for_ticker,
    so actual_outcome = won_yes directly.
    """
    try:
        now = _now_iso()
        rows = conn.execute(
            """SELECT ab.id, ab.ticker, ab.side, ab.ensemble_p_yes,
                      ab.source_count, ab.won_yes
               FROM alpha_backtest ab
               WHERE ab.ts_settle_unix IS NOT NULL
                 AND ab.won_yes IS NOT NULL
                 AND ab.side IS NOT NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM calibration c WHERE c.alpha_id = ab.id
                 )"""
        ).fetchall()

        if not rows:
            return 0

        inserted = 0
        with db_write_ctx(conn):
            for alpha_id, ticker, side, p_yes, src_count, won_yes in rows:
                if p_yes is None:
                    continue
                # p(our side wins) for calibration bucket
                our_prob = p_yes if side == "yes" else (1.0 - p_yes)
                bucket = _prob_bucket(our_prob)
                conn.execute(
                    """INSERT INTO calibration
                       (recorded_at, ticker, estimated_prob, actual_outcome,
                        source_desc, n_sources, bucket, alpha_id)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (now, ticker, our_prob, int(won_yes),
                     "alpha:shadow", src_count, bucket, alpha_id),
                )
                inserted += 1
        return inserted
    except Exception as e:
        logger.warning("[populate_from_alpha] calibration failed: %s", e)
        return 0


# ══════════════════════════════════════════════════════════════════════════
# Timing patterns
# ══════════════════════════════════════════════════════════════════════════
def populate_timing_patterns(conn) -> int:
    """Insert one timing_patterns row per newly-settled alpha_backtest row.

    hour_utc / day_of_week come from ts_decision; category from family lookup;
    source is stamped as 'alpha:<decision_type>' so analysis can slice by
    shadow type.
    """
    try:
        now = _now_iso()
        rows = conn.execute(
            """SELECT ab.id, ab.ticker, ab.ts_decision_unix, ab.decision_type,
                      ab.ensemble_p_yes, ab.market_prob_yes, ab.side,
                      ab.won_yes, ab.realized_pnl_cents
               FROM alpha_backtest ab
               WHERE ab.ts_settle_unix IS NOT NULL
                 AND ab.won_yes IS NOT NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM timing_patterns tp WHERE tp.alpha_id = ab.id
                 )"""
        ).fetchall()

        if not rows:
            return 0

        inserted = 0
        with db_write_ctx(conn):
            for (alpha_id, ticker, ts_dec, dtype, p_yes, mkt_p, side,
                 won_yes, pnl) in rows:
                try:
                    hour_utc, dow = _parse_decision_ts(ts_dec)
                except Exception:
                    continue
                cat = categorize_market(ticker, "")
                # Edge is p(our side) - p(market on our side). Mirrors the
                # directional scorer's convention: positive edge = we thought
                # our side was cheap.
                edge = None
                if p_yes is not None and mkt_p is not None and side:
                    our_p = p_yes if side == "yes" else (1.0 - p_yes)
                    our_mkt = mkt_p if side == "yes" else (1.0 - mkt_p)
                    edge = our_p - our_mkt
                source_tag = f"alpha:{dtype}"
                order_key = f"alpha_{alpha_id}"  # unique per row
                conn.execute(
                    """INSERT INTO timing_patterns
                       (recorded_at, order_id, hour_utc, day_of_week,
                        category, source, edge, won, profit_cents, alpha_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (now, order_key, hour_utc, dow, cat, source_tag,
                     edge, int(won_yes), pnl, alpha_id),
                )
                inserted += 1
        return inserted
    except Exception as e:
        logger.warning("[populate_from_alpha] timing_patterns failed: %s", e)
        return 0


# ══════════════════════════════════════════════════════════════════════════
# Edge convergence
# ══════════════════════════════════════════════════════════════════════════
def populate_edge_convergence(conn) -> int:
    """Insert one edge_convergence row per newly-settled alpha_backtest row.

    Unlike the legacy writer (which polls 6-48h post-trade from the Kalshi
    API), this writer is trivially correct: the final settlement price is the
    ground truth the market converges toward (1.0 if result=yes, else 0.0).

    "Converged" means |our_estimate - settlement_price| < |market_at_entry
    - settlement_price| — i.e. our estimate was closer to reality than the
    market's entry price. convergence_pct is how much of the gap we closed.
    """
    try:
        now = _now_iso()
        rows = conn.execute(
            """SELECT ab.id, ab.ticker, ab.side, ab.ensemble_p_yes,
                      ab.market_prob_yes, ab.settlement_result
               FROM alpha_backtest ab
               WHERE ab.ts_settle_unix IS NOT NULL
                 AND ab.settlement_result IS NOT NULL
                 AND ab.ensemble_p_yes IS NOT NULL
                 AND ab.market_prob_yes IS NOT NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM edge_convergence ec WHERE ec.alpha_id = ab.id
                 )"""
        ).fetchall()

        if not rows:
            return 0

        inserted = 0
        with db_write_ctx(conn):
            for alpha_id, ticker, side, p_yes, mkt_p, result in rows:
                settlement_yes = 1.0 if result == "yes" else 0.0
                our_gap = abs(p_yes - settlement_yes)
                mkt_gap = abs(mkt_p - settlement_yes)
                if mkt_gap < 1e-9:
                    # Market was already at the settled truth at entry —
                    # our estimate can't "converge" anywhere meaningful.
                    continue
                conv_pct = (mkt_gap - our_gap) / mkt_gap
                converged = 1 if conv_pct > 0.10 else 0
                conn.execute(
                    """INSERT INTO edge_convergence
                       (recorded_at, ticker, side, our_estimate,
                        market_price_at_entry, market_price_after_24h,
                        converged, convergence_pct, alpha_id)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (now, ticker, side, p_yes, mkt_p, settlement_yes,
                     converged, conv_pct, alpha_id),
                )
                inserted += 1
        return inserted
    except Exception as e:
        logger.warning("[populate_from_alpha] edge_convergence failed: %s", e)
        return 0


# ══════════════════════════════════════════════════════════════════════════
# Loss postmortems
# ══════════════════════════════════════════════════════════════════════════
def populate_postmortems(conn) -> int:
    """Classify losses in newly-settled alpha_backtest rows."""
    try:
        now = _now_iso()
        rows = conn.execute(
            """SELECT ab.id, ab.ticker, ab.side, ab.ensemble_p_yes,
                      ab.market_prob_yes, ab.price_cents, ab.contracts,
                      ab.decision_type, ab.sources_json, ab.won_yes,
                      ab.realized_pnl_cents
               FROM alpha_backtest ab
               WHERE ab.ts_settle_unix IS NOT NULL
                 AND ab.won_yes = 0
                 AND ab.ensemble_p_yes IS NOT NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM loss_postmortems lp WHERE lp.alpha_id = ab.id
                 )"""
        ).fetchall()

        if not rows:
            return 0

        inserted = 0
        with db_write_ctx(conn):
            for (alpha_id, ticker, side, p_yes, mkt_p, price_c, contracts,
                 dtype, sources_json, won_yes, pnl) in rows:
                cat = categorize_market(ticker, "")
                our_prob = (p_yes if side == "yes" else (1.0 - p_yes)
                            if p_yes is not None and side else None)
                our_mkt = (mkt_p if side == "yes" else (1.0 - mkt_p)
                           if mkt_p is not None and side else None)
                edge = (our_prob - our_mkt
                        if our_prob is not None and our_mkt is not None
                        else None)

                loss_type = "unknown"
                detail = ""
                if our_prob is not None and our_mkt is not None and edge is not None:
                    err = our_prob - our_mkt
                    if abs(err) > _LOSS_BAD_SOURCE_GAP:
                        loss_type = "bad_source"
                        detail = (f"est {our_prob:.0%} vs mkt {our_mkt:.0%} "
                                  f"gap {err:+.0%} → lost")
                    elif abs(edge) < _LOSS_EFFICIENT_EDGE:
                        loss_type = "efficient_market"
                        detail = (f"edge {edge:+.1%}; market approx correct "
                                  f"(est {our_prob:.2f} vs {our_mkt:.2f})")
                    elif our_prob > _LOSS_CONFIDENCE_CUTOFF:
                        loss_type = "adverse_selection"
                        detail = (f"confident {our_prob:.0%} but lost — "
                                  f"possible informed traders")
                    else:
                        loss_type = "bad_source"
                        detail = (f"est {our_prob:.2f} edge {edge:.1%} lost "
                                  f"(dtype={dtype})")
                else:
                    loss_type = "unknown"
                    detail = "missing market_prob_yes or side"

                source_combo = f"alpha:{dtype}"
                conn.execute(
                    """INSERT INTO loss_postmortems
                       (recorded_at, order_id, ticker, category, loss_type,
                        source_combo, estimated_prob, market_prob,
                        edge_at_entry, price_at_settlement, detail, alpha_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now, f"alpha_{alpha_id}", ticker, cat, loss_type,
                     source_combo, our_prob, our_mkt, edge,
                     price_c, detail, alpha_id),
                )
                inserted += 1
        return inserted
    except Exception as e:
        logger.warning("[populate_from_alpha] postmortems failed: %s", e)
        return 0


# ══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ══════════════════════════════════════════════════════════════════════════
def populate_all(conn) -> dict:
    """Run every populator. Returns a count dict so the cycle can log it.

    Order is deliberate: calibration first (most important for downstream
    apply_calibration_correction), then the analytical tables. A failure in
    one does not block the others — each function internally catches and
    logs.
    """
    results = {
        "calibration": populate_calibration(conn),
        "timing_patterns": populate_timing_patterns(conn),
        "edge_convergence": populate_edge_convergence(conn),
        "postmortems": populate_postmortems(conn),
    }
    total = sum(results.values())
    if total > 0:
        logger.info(
            "[populate_from_alpha] inserted "
            "cal=%d timing=%d conv=%d postmortem=%d",
            results["calibration"], results["timing_patterns"],
            results["edge_convergence"], results["postmortems"],
        )
    return results
