"""Active feedback loop — synthesizes all learning mechanisms into behavioral adjustments.

This is where the learning loops close. Reads from pipeline_health, edge_convergence,
loss_postmortems, timing_patterns, and strategy bandit tables. Produces a single
feedback dict that modifies trading behavior (edge requirements, source disabling,
hour skipping, strategy gating).

Extracted from trade.py compute_active_feedback() (lines ~4364-4524).
"""

from __future__ import annotations

from bot.learning.bandit import compute_strategy_bandit


def compute_active_feedback(conn):
    """Read ALL learning loop outputs and produce a single feedback dict that
    modifies trading behavior. Called once per run, results passed to score_market().
    Returns dict with keys:
      - disabled_sources: set of source names to skip entirely
      - disabled_strategies: set of strategy names to skip this run
      - edge_multiplier: float multiplier on MIN_EDGE (>1 = more conservative)
      - skip_hours: set of hour_utc values to avoid trading in
      - loss_type_adjustments: dict of {loss_type: count} for pattern detection
      - convergence_rate: float, % of edges that converge (None if insufficient data)
      - strategy_stats: dict of per-strategy performance stats
      - strategy_bandit: dict of Thompson Sampling results per strategy
    """
    feedback = {
        "disabled_sources": set(),
        "disabled_strategies": set(),
        "edge_multiplier": 1.0,
        "skip_hours": set(),
        "loss_type_adjustments": {},
        "convergence_rate": None,
        "strategy_stats": {},
    }

    # -- 1. Pipeline health -> disable broken sources (with recovery) --------
    # If a source was broken or degraded for the last 18/20 consecutive runs, disable it.
    # But every 3 runs, re-enable disabled sources to check if they've recovered.
    # Important: "idle" status does NOT count as broken -- it means the source was
    # correctly not applicable for the markets it was tested against.
    try:
        sources = conn.execute(
            "SELECT DISTINCT source FROM pipeline_health"
        ).fetchall()
        total_runs = conn.execute(
            "SELECT COUNT(DISTINCT recorded_at) FROM pipeline_health"
        ).fetchone()[0]
        for (source,) in sources:
            recent = conn.execute(
                "SELECT status FROM pipeline_health "
                "WHERE source = ? ORDER BY id DESC LIMIT 20",
                (source,)
            ).fetchall()
            # Only count actual failures (broken/degraded), not idle
            failure_count = sum(1 for r in recent if r[0] in ("broken", "degraded"))
            if len(recent) >= 20 and failure_count >= 18:
                # Recovery window: every 3 runs, give disabled sources another chance
                if total_runs % 3 == 0:
                    print(f"[feedback] RECOVERY CHECK: re-enabling source '{source}' "
                          f"(was failing for {failure_count}/{len(recent)} runs, periodic retry)")
                else:
                    feedback["disabled_sources"].add(source)
                    print(f"[feedback] DISABLING source '{source}' -- "
                          f"failing {failure_count}/{len(recent)} recent runs")
    except Exception:
        pass

    # -- 2. Edge convergence -> tighten edge requirements if edges don't converge
    try:
        conv_rows = conn.execute(
            "SELECT converged, convergence_pct FROM edge_convergence"
        ).fetchall()
        if len(conv_rows) >= 15:
            conv_rate = sum(r[0] for r in conv_rows) / len(conv_rows)
            feedback["convergence_rate"] = conv_rate

            if conv_rate < 0.25:
                # Very few edges converge -- we're probably trading noise
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.5)
                print(f"[feedback] Edge convergence VERY LOW ({conv_rate:.0%}) -- "
                      f"requiring 1.5x edge")
            elif conv_rate < 0.40:
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.25)
                print(f"[feedback] Edge convergence LOW ({conv_rate:.0%}) -- "
                      f"requiring 1.25x edge")
            elif conv_rate > 0.60:
                # Strong convergence -- edges are real, can be slightly less conservative
                feedback["edge_multiplier"] = min(feedback["edge_multiplier"], 0.90)
                print(f"[feedback] Edge convergence STRONG ({conv_rate:.0%}) -- "
                      f"relaxing edge to 0.9x")
    except Exception:
        pass

    # -- 3. Loss post-mortems -> detect systematic failure patterns -----------
    try:
        loss_types = conn.execute(
            "SELECT loss_type, COUNT(*) FROM loss_postmortems GROUP BY loss_type"
        ).fetchall()
        total_losses = sum(c for _, c in loss_types)
        for lt, count in loss_types:
            feedback["loss_type_adjustments"][lt] = count
            pct = count / total_losses if total_losses > 0 else 0

            if lt == "fee_erosion" and pct > 0.30 and total_losses >= 10:
                # >30% of losses are fee erosion -- need higher edge
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.3)
                print(f"[feedback] {pct:.0%} of losses are fee_erosion -- "
                      f"requiring 1.3x edge")

            if lt == "bad_source" and pct > 0.40 and total_losses >= 10:
                # >40% of losses are bad source estimates -- be more conservative
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.2)
                print(f"[feedback] {pct:.0%} of losses are bad_source -- "
                      f"requiring 1.2x edge")
    except Exception:
        pass

    # -- 4. Timing patterns -> identify hours to avoid -----------------------
    try:
        hours = conn.execute("""
            SELECT hour_utc, COUNT(*) as n, AVG(won) as wr, SUM(profit_cents) as profit
            FROM timing_patterns
            GROUP BY hour_utc
            HAVING n >= 5
        """).fetchall()
        for hour, n, wr, profit in hours:
            if wr < 0.35 and n >= 8:
                # Consistently losing at this hour -- skip it
                feedback["skip_hours"].add(hour)
                print(f"[feedback] SKIP hour {hour}:00 UTC -- "
                      f"win rate {wr:.0%} over {n} trades")
    except Exception:
        pass

    # -- 5. Per-strategy Thompson Sampling bandit ----------------------------
    # Instead of hard disabling, use Thompson Sampling to weight strategies.
    # Strategies with bad track records get low samples (rarely picked),
    # but are NEVER fully killed -- they always have a chance of being re-tested.
    try:
        bandit = compute_strategy_bandit(conn)
        feedback["strategy_bandit"] = bandit

        for strat, stats in bandit.items():
            sample = stats["sample"]
            n = stats["n"]
            wins = stats["wins"]
            losses = stats["losses"]
            wr = wins / max(1, wins + losses)

            if n >= 20 and wr < 0.30 and sample < 0.20:
                # Very consistently bad AND drew a low sample this run -> skip this run
                # But only for THIS run -- next run gets a fresh sample
                feedback["disabled_strategies"].add(strat)
                print(f"[bandit] Strategy '{strat}' COLD this run "
                      f"(wr={wr:.0%}, n={n}, sample={sample:.2f}) -- skipping")
            elif stats["explore"]:
                print(f"[bandit] Strategy '{strat}' in EXPLORE mode "
                      f"(wr={wr:.0%}, n={n}, sample={sample:.2f})")
            elif n >= 10:
                status = "STRONG" if wr > 0.55 else "OK" if wr > 0.45 else "WEAK"
                print(f"[bandit] Strategy '{strat}' {status} "
                      f"(wr={wr:.0%}, n={n}, sample={sample:.2f})")

        # Log overall strategy ranking by Thompson sample
        ranked = sorted(bandit.items(), key=lambda x: x[1]["sample"], reverse=True)
        rank_str = " > ".join(f"{s}({v['sample']:.2f})" for s, v in ranked)
        print(f"[bandit] Strategy ranking this run: {rank_str}")

        # Also compute simple stats for the report
        feedback["strategy_stats"] = {
            strat: {"n": s["n"], "win_rate": s["wins"] / max(1, s["wins"] + s["losses"]),
                    "wins": s["wins"], "losses": s["losses"], "sample": s["sample"]}
            for strat, s in bandit.items()
        }
    except Exception as e:
        print(f"[bandit] Error computing strategy bandit: {e}")

    return feedback
