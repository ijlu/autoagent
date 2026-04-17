"""Orchestrator — the main 2-minute cycle coordinator.

This is the central loop that calls all sub-agents in order:
1. Housekeeping (prune orders, track fills, record settlements, manage positions)
2. Learning cycle (adaptive weights, calibration, postmortems, active feedback)
3. Risk checks (balance, circuit breakers, stress level)
4. Market scanning + scoring (directional strategies + four-factor gate)
5. Order execution (sized by Kelly, capped by exposure limits)
6. Market making (two-sided quotes in selected markets)
7. Post-trade learning (shadow evaluations, pipeline health)
8. Session logging + performance report

Mirrors the existing trade.py main() flow but uses modular bot.* imports.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

# ── Core infrastructure ──
from bot.config import (
    DRY_RUN, HOST, MAX_CONTRACTS, MAX_PER_CATEGORY, MAX_PORTFOLIO_PCT,
    MAX_POSITION_PCT, MIN_EDGE, MM_ENABLED, ORDER_MAX_AGE_HOURS,
    SINGLE_SOURCE_EDGE,
)
from bot.db import init_db, kv_cleanup
from bot.api import api_get, api_post, get_portfolio

# ── Learning ──
from bot.learning.adaptive_weights import compute_adaptive_weights
from bot.learning.calibration import compute_calibration_correction
from bot.learning.category_scoring import compute_category_edge_thresholds
from bot.learning.postmortems import run_loss_postmortems
from bot.learning.edge_convergence import check_edge_convergence
from bot.learning.timing_patterns import record_timing_data
from bot.learning.shadow_testing import analyze_shadow_performance, record_shadow_evaluations
from bot.learning.active_feedback import compute_active_feedback

# ── Scoring + Filters ──
from bot.scoring.filters import categorize_market, compute_avoid_filters
from bot.scoring.market_scorer import score_market, passes_filters
from bot.scoring.four_factor import score_four_factors, log_four_factor_decision

# ── Market Making ──
from bot.market_maker.core import mm_run

# ── Signals ──
from bot.signals.ensemble import record_pipeline_health

# ── Risk + Regime ──
from bot.signals.regime import detect_regime
from bot.risk.stress import compute_stress_level

# ── Learning: self-modification ──
from bot.learning.self_modifier import auto_promote_shadow_params, auto_revert_if_underperforming

# ── Types ──
from bot.types import RunContext


def main():
    """Single 2-minute cycle. Called by trade.py shim or directly."""
    # These are modified during the run based on active feedback
    min_edge = MIN_EDGE
    single_source_edge = SINGLE_SOURCE_EDGE

    conn = init_db()
    kv_cleanup(conn)
    now = datetime.now(timezone.utc).isoformat()

    # ── Phase system ──
    # Import these from trade.py since they reference global state we haven't
    # fully extracted yet. For now, the orchestrator coexists with trade.py.
    # TODO: Extract compute_current_phase, apply_phase_limits, check_limits,
    #       kelly_contracts, manage_positions, record_settlements, prune_stale_orders,
    #       track_fills, get_orderbook_depth, get_open_tickers, get_day_start_balance,
    #       generate_performance_report, generate_diagnostic_report into bot/ modules.
    try:
        from trade import (
            compute_current_phase, apply_phase_limits, check_limits,
            kelly_contracts, manage_positions, record_settlements,
            prune_stale_orders, track_fills, get_orderbook_depth,
            get_open_tickers, get_day_start_balance,
            generate_performance_report, generate_diagnostic_report,
        )
    except ImportError as e:
        print(f"[orchestrator] Cannot import from trade.py: {e}")
        print("[orchestrator] Running in standalone mode (limited functionality)")
        conn.close()
        return {"error": "trade.py imports not available"}

    phase_num, phase_cfg, phase_stats = compute_current_phase(conn)
    effective_limits = apply_phase_limits(phase_num, phase_cfg)
    print(f"[phase] Phase {phase_num}: {phase_cfg[7]}")
    print(f"[phase] Track record: {phase_stats['settled']} settled, "
          f"{phase_stats['win_rate']:.1%} win rate "
          f"(recent {phase_stats['recent_n']}: {phase_stats['recent_win_rate']:.1%})")

    result = {
        "markets_scanned": 0, "opportunities": [], "orders_placed": [],
        "positions_managed": 0, "orders_pruned": 0, "pnl": 0.0,
        "timestamp": now, "api_base": HOST, "dry_run": DRY_RUN,
        "halted": False, "halt_reason": "", "patterns_avoided": [],
        "settlements_recorded": 0,
        "phase": phase_num, "phase_desc": phase_cfg[7],
        "phase_stats": phase_stats, "effective_limits": effective_limits,
    }

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 1: Housekeeping
    # ═══════════════════════════════════════════════════════════════════════
    result["orders_pruned"] = prune_stale_orders()
    track_fills(conn)
    result["settlements_recorded"] = record_settlements(conn)
    result["positions_managed"] = manage_positions(conn)
    avoid_filters = compute_avoid_filters(conn)
    result["patterns_avoided"] = avoid_filters.get("summary", [])

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 1b: Learning cycle
    # ═══════════════════════════════════════════════════════════════════════
    adaptive_weights = compute_adaptive_weights(conn)
    calibration_corrections = compute_calibration_correction(conn)
    category_edges = compute_category_edge_thresholds(conn)
    result["adaptive_weights"] = adaptive_weights
    result["calibration_corrections"] = calibration_corrections
    result["category_edges"] = category_edges

    # Advanced learning loops (each wrapped in try/except for resilience)
    for name, fn, key in [
        ("postmortem", lambda: run_loss_postmortems(conn), "postmortems"),
        ("convergence", lambda: check_edge_convergence(conn), "convergence_checks"),
        ("timing", lambda: record_timing_data(conn), "timing_records"),
        ("shadow", lambda: analyze_shadow_performance(conn), None),
    ]:
        try:
            val = fn()
            if key:
                result[key] = val
        except Exception as e:
            print(f"[{name}] Error: {e}")
            if key:
                result[key] = 0

    # ── Regime detection ──
    regime = detect_regime(conn)
    result["regime"] = regime.value

    # ── Stress level ──
    stress_level = compute_stress_level(conn)
    result["stress_level"] = round(stress_level, 3)

    # ── Self-modification (stress-gated) ──
    try:
        promotions = auto_promote_shadow_params(conn, stress_level)
        if promotions:
            result["self_modifications"] = promotions
            print(f"[self-mod] Promoted {len(promotions)} params: "
                  f"{[p.get('param') for p in promotions]}")
        reverts = auto_revert_if_underperforming(conn, stress_level)
        if reverts:
            result["self_reverts"] = reverts
            print(f"[self-mod] Reverted {len(reverts)} params")
    except Exception as e:
        print(f"[self-mod] Error: {e}")

    # ── Active feedback: synthesize all learning into adjustments ──
    try:
        active_feedback = compute_active_feedback(conn)
        result["active_feedback"] = {
            "disabled_sources": list(active_feedback.get("disabled_sources", set())),
            "disabled_strategies": list(active_feedback.get("disabled_strategies", set())),
            "edge_multiplier": active_feedback.get("edge_multiplier", 1.0),
            "skip_hours": list(active_feedback.get("skip_hours", set())),
            "loss_type_breakdown": active_feedback.get("loss_type_adjustments", {}),
            "convergence_rate": active_feedback.get("convergence_rate"),
            "strategy_stats": active_feedback.get("strategy_stats", {}),
        }
        # Apply edge multiplier from convergence + loss analysis
        mult = active_feedback.get("edge_multiplier", 1.0)
        if mult != 1.0:
            min_edge *= mult
            single_source_edge *= mult
            print(f"[feedback] Adjusted MIN_EDGE to {min_edge:.3f}, "
                  f"SINGLE_SOURCE_EDGE to {single_source_edge:.3f} "
                  f"(multiplier={mult:.2f})")

        # Check if current hour should be skipped
        current_hour = datetime.now(timezone.utc).hour
        if current_hour in active_feedback.get("skip_hours", set()):
            print(f"[feedback] Hour {current_hour}:00 UTC is historically bad. "
                  f"Skipping new trades.")
            result["skip_new_trades"] = True
        else:
            result["skip_new_trades"] = False
    except Exception as e:
        print(f"[feedback] Error: {e}")
        active_feedback = {
            "disabled_sources": set(), "disabled_strategies": set(),
            "edge_multiplier": 1.0, "skip_hours": set(),
            "convergence_rate": None, "strategy_stats": {},
            "strategy_bandit": {},
        }
        result["skip_new_trades"] = False

    # Build RunContext for strategy protocol
    ctx = RunContext(
        conn=conn,
        balance_cents=0,  # filled below
        portfolio_cents=0,
        phase_num=phase_num,
        phase_config=phase_cfg,
        phase_stats=phase_stats,
        dry_run=DRY_RUN,
        mm_dry_run=DRY_RUN,  # TODO: separate MM_DRY_RUN
        stress_level=stress_level,
        regime=regime,
        active_feedback=active_feedback,
        calibration_corrections=calibration_corrections,
        adaptive_weights=adaptive_weights,
        category_scores=category_edges,
        avoid_filters=avoid_filters,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 2: Balance & limits
    # ═══════════════════════════════════════════════════════════════════════
    try:
        initial_balance, portfolio_value = get_portfolio()
        ctx.balance_cents = initial_balance
        ctx.portfolio_cents = portfolio_value
        print(f"[bot] Balance=${initial_balance/100:.2f}  "
              f"Portfolio=${portfolio_value/100:.2f}")
    except Exception as e:
        print(f"[bot] CRITICAL: Cannot fetch portfolio: {e}")
        conn.close()
        return {"error": f"portfolio_fetch_failed: {e}"}

    markets = []

    day_start = get_day_start_balance(conn)
    if day_start is None:
        day_start = initial_balance + portfolio_value
    ok, halt_reason = check_limits(day_start, initial_balance, portfolio_value)
    if not ok:
        print(f"[bot] HALTED: {halt_reason}")
        result.update(halted=True, halt_reason=halt_reason)
    elif result.get("skip_new_trades"):
        print("[bot] Skipping new trades (active feedback: bad hour)")
        result["trades_skipped_reason"] = "active_feedback_skip_hour"
    else:
        # ═══════════════════════════════════════════════════════════════════
        # PHASE 3: Scan & score
        # ═══════════════════════════════════════════════════════════════════
        print("[bot] Fetching markets...")
        cursor = None
        MAX_PAGES = 10
        try:
            for page in range(MAX_PAGES):
                url = "/markets?limit=500&status=open"
                if cursor:
                    url += f"&cursor={cursor}"
                resp = api_get(url)
                batch = resp.get("markets", [])
                markets.extend(batch)
                cursor = resp.get("cursor")
                if not cursor or len(batch) < 500:
                    break
        except Exception as e:
            print(f"[bot] ERROR fetching markets: {e}")
        result["markets_scanned"] = len(markets)

        candidates = []
        for m in markets:
            _t = m.get("ticker", "")
            if "KXMVE" in _t or "MULTIGAME" in _t or m.get("mve_collection_ticker"):
                continue
            score, side, strategy, detail, volume, sc, indep_prob, mkt_prob, edge = score_market(
                m, adaptive_weights=adaptive_weights,
                calibration_corrections=calibration_corrections,
                category_edges=category_edges,
                disabled_sources=active_feedback.get("disabled_sources"),
                disabled_strategies=active_feedback.get("disabled_strategies"),
                strategy_bandit=active_feedback.get("strategy_bandit"))
            if score <= 0:
                continue

            ticker = m.get("ticker", "")
            ok_t, skip_reason = passes_filters(ticker, strategy, volume, sc, avoid_filters)
            if not ok_t:
                print(f"  x {ticker}: {skip_reason}")
                continue

            # ── Four-factor gate (shadow mode: log but don't block yet) ──
            try:
                ff_score = score_four_factors(
                    market_data=m,
                    ensemble_prob=indep_prob,
                    market_prob=mkt_prob,
                    n_sources=detail.count("+") + 1 if "+" in str(detail) else 1,
                    source_desc=str(detail),
                    regime=regime,
                    active_feedback=active_feedback,
                    category_scores=category_edges,
                    conn=conn,
                )
                log_four_factor_decision(ticker, ff_score, "shadow_pass" if ff_score.passes else "shadow_fail", conn)
                if not ff_score.passes:
                    print(f"  [4F-shadow] {ticker} would FAIL: {ff_score.to_dict()}")
            except Exception as e:
                print(f"  [4F] Error scoring {ticker}: {e}")

            candidates.append((score, side, strategy, detail, volume, sc, indep_prob, mkt_prob, edge, m))

        # Dedup against open positions
        open_positions = get_open_tickers()
        candidates = [c for c in candidates if (c[9].get("ticker", ""), c[1]) not in open_positions]

        # Category correlation limits
        category_counts = {}
        try:
            resp = api_get("/portfolio/positions?limit=100")
            existing_pos = resp.get("market_positions", resp.get("positions", []))
            for pos in existing_pos:
                t = pos.get("ticker", "")
                _pos_raw = pos.get("position_fp") or pos.get("position", 0)
                if abs(round(float(_pos_raw))) > 0:
                    cat = categorize_market(t, "")
                    category_counts[cat] = category_counts.get(cat, 0) + 1
        except Exception as e:
            print(f"[correlation] Could not fetch positions: {e}")

        filtered_candidates = []
        for c in candidates:
            cticker = c[9].get("ticker", "")
            ctitle = c[9].get("title", "") or c[9].get("subtitle", "") or ""
            cat = categorize_market(cticker, ctitle)
            current = category_counts.get(cat, 0)
            if current >= MAX_PER_CATEGORY:
                continue
            category_counts[cat] = current + 1
            filtered_candidates.append(c)
        candidates = filtered_candidates
        candidates.sort(key=lambda x: x[0], reverse=True)

        # Explore/exploit balance
        from bot.learning.bandit import compute_exploration_targets
        n_explore = 0
        try:
            exploit_picks, explore_picks = compute_exploration_targets(conn, candidates, 5)
            top = exploit_picks + explore_picks
            n_explore = len(explore_picks)
        except Exception as e:
            print(f"[explore] Error: {e}")
            top = candidates[:5]
        print(f"[bot] {len(candidates)} candidates -> top {len(top)} "
              f"({len(top) - n_explore} exploit + {n_explore} explore)")

        # ═══════════════════════════════════════════════════════════════════
        # PHASE 4: Execute trades
        # ═══════════════════════════════════════════════════════════════════
        mm_inventory_cents = 0
        try:
            inv_rows = conn.execute(
                """SELECT SUM(
                    CASE WHEN net_position > 0 THEN net_position * avg_entry_cents
                         WHEN net_position < 0 THEN ABS(net_position) * (100 - avg_entry_cents)
                         ELSE 0 END
                ) FROM mm_inventory"""
            ).fetchone()
            mm_inventory_cents = int(inv_rows[0] or 0)
        except Exception as e:
            print(f"[orchestrator] mm_inventory exposure query failed: {e}")
        directional_exposure = max(0, portfolio_value - mm_inventory_cents)
        total_exposure_cents = directional_exposure
        max_exposure_cents = int(initial_balance * MAX_PORTFOLIO_PCT)

        current_balance = initial_balance
        for score, side, strategy, detail, volume, sc, indep_prob, mkt_prob, edge, m in top:
            try:
                current_balance, current_pv = get_portfolio()
            except Exception:
                current_pv = portfolio_value
            ok, halt_reason = check_limits(day_start, current_balance, current_pv)
            if not ok:
                result.update(halted=True, halt_reason=halt_reason)
                break

            ticker = m.get("ticker", "")

            def _pc(v):
                v = float(v or 99)
                return int(round(v * 100)) if v <= 1.0 else int(v)

            price_cents = max(1, min(99,
                _pc(m.get("yes_ask") or m.get("yes_ask_dollars")) if side == "yes"
                else _pc(m.get("no_ask") or m.get("no_ask_dollars"))))

            prob_for_kelly = indep_prob if indep_prob else (1 - price_cents / 100)
            contracts = kelly_contracts(prob_for_kelly, price_cents, current_balance)
            if contracts <= 0:
                continue

            book_depth = get_orderbook_depth(ticker, side, price_cents)
            if book_depth is not None:
                if book_depth < 3:
                    continue
                max_from_book = max(1, book_depth // 2)
                if contracts > max_from_book:
                    contracts = max_from_book

            order_cost_cents = contracts * price_cents
            if total_exposure_cents + order_cost_cents > max_exposure_cents:
                headroom = max_exposure_cents - total_exposure_cents
                if headroom <= 0:
                    continue
                contracts = max(1, min(MAX_CONTRACTS, int(headroom / price_cents)))
                order_cost_cents = contracts * price_cents

            opp = {
                "ticker": ticker, "side": side, "strategy": strategy,
                "score": round(score, 3), "detail": detail,
                "price_cents": price_cents, "contracts": contracts,
                "volume": volume, "spread_cents": sc,
                "independent_prob": round(indep_prob, 3) if indep_prob else None,
                "market_prob": round(mkt_prob, 3) if mkt_prob else None,
                "edge": round(edge, 3) if edge else None,
            }
            result["opportunities"].append(opp)
            print(f"  -> {ticker} {side} @ {price_cents}c x{contracts}  "
                  f"edge={edge:.1%}  [{strategy}]")

            order_id = error = None
            if not DRY_RUN and ticker:
                order_body = {
                    "ticker": ticker, "side": side, "type": "limit",
                    "count": contracts,
                    ("yes_price" if side == "yes" else "no_price"): price_cents,
                    "action": "buy",
                    "expiration_ts": int(time.time() + ORDER_MAX_AGE_HOURS * 3600),
                }
                try:
                    resp = api_post("/portfolio/orders", order_body)
                    order_id = resp.get("order", {}).get("order_id") or str(resp)
                    result["orders_placed"].append({"ticker": ticker, "contracts": contracts, "order_id": order_id})
                    total_exposure_cents += order_cost_cents
                except Exception as e:
                    error = str(e)
                    result["orders_placed"].append({"ticker": ticker, "error": error})
            else:
                result["orders_placed"].append({"ticker": ticker, "contracts": contracts, "dry_run": True})

            conn.execute(
                """INSERT INTO trades
                (timestamp,ticker,side,action,score,reason,strategy,price_cents,contracts,
                 volume,spread_cents,independent_prob,market_prob,edge,dry_run,order_id,error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now, ticker, side, "buy", score, detail, strategy, price_cents, contracts,
                 volume, sc, indep_prob, mkt_prob, edge, int(DRY_RUN), order_id, error))
            conn.commit()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 4a: Market Making
    # ═══════════════════════════════════════════════════════════════════════
    try:
        if markets and MM_ENABLED:
            mm_stats = mm_run(
                conn, markets, initial_balance, portfolio_value,
                adaptive_weights=adaptive_weights,
                calibration_corrections=calibration_corrections,
                disabled_sources=active_feedback.get("disabled_sources"))
            result["mm_stats"] = mm_stats
        else:
            result["mm_stats"] = {"mm_enabled": False}
    except Exception as e:
        print(f"[mm] Error: {e}")
        result["mm_stats"] = {"error": str(e)}

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 4b: Post-trade learning
    # ═══════════════════════════════════════════════════════════════════════
    try:
        record_shadow_evaluations(conn, result)
    except Exception as e:
        print(f"[shadow] Error recording: {e}")

    try:
        record_pipeline_health(conn)
    except Exception as e:
        print(f"[pipeline] Error recording: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 5: Session log
    # ═══════════════════════════════════════════════════════════════════════
    conn.execute(
        """INSERT INTO sessions
        (timestamp,balance_cents,portfolio_cents,markets_scanned,opportunities_found,
         orders_attempted,positions_managed,orders_pruned,dry_run,halted,halt_reason,patterns_avoided)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now, ctx.balance_cents, ctx.portfolio_cents, result["markets_scanned"],
         len(result["opportunities"]), len(result["orders_placed"]),
         result["positions_managed"], result["orders_pruned"],
         int(DRY_RUN), int(result["halted"]), result["halt_reason"],
         json.dumps(result["patterns_avoided"])))
    conn.commit()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 6: Performance report
    # ═══════════════════════════════════════════════════════════════════════
    try:
        generate_performance_report(conn, result)
    except Exception as e:
        print(f"[report] Error: {e}")

    conn.close()

    task_dir = "/task" if os.path.exists("/task") else "/tmp"
    with open(f"{task_dir}/trades.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"[bot] Done -> markets={result['markets_scanned']} "
          f"opps={len(result['opportunities'])} "
          f"orders={len(result['orders_placed'])} "
          f"positions_managed={result['positions_managed']} "
          f"pruned={result['orders_pruned']} "
          f"settlements={result['settlements_recorded']}")
    return result


if __name__ == "__main__":
    main()
