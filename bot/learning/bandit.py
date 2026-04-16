"""Thompson Sampling bandit for strategies and exploration targets.

Proper multi-armed bandit using Thompson Sampling (Beta-Bernoulli model).
Each "arm" (strategy or category) has a Beta(alpha, beta) posterior where:
  alpha = 1 + wins (successes)
  beta = 1 + losses (failures)
Prior is Beta(1,1) = uniform. We sample from each arm's posterior and use
the samples to weight/rank arms. This naturally:
  - Explores arms with few samples (wide posterior -> random rank)
  - Exploits arms with proven track records (tight posterior -> consistent rank)
  - Never permanently kills an arm (always some probability of being sampled)
  - Handles non-stationarity via recency weighting
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from bot.market_maker.selection import categorize_market

EXPLORE_BUDGET_PCT = 0.10   # 10% of trades reserved for exploration (up from 5%)
EXPLORE_MIN_VOLUME = 100    # don't explore truly dead markets
STRATEGY_EXPLORE_PCT = 0.08 # 8% chance per run of re-testing a "cold" strategy

# Recency half-life: trades from >30 days ago count half as much
BANDIT_RECENCY_DAYS = 30


def _thompson_sample(wins, losses, n_samples=1):
    """Draw from Beta(1+wins, 1+losses) posterior. Returns single float [0,1]."""
    alpha = 1.0 + wins
    beta_param = 1.0 + losses
    try:
        return random.betavariate(alpha, beta_param)
    except ValueError:
        return 0.5  # fallback


def compute_strategy_bandit(conn):
    """Thompson Sampling over strategies.
    Returns dict: {strategy_name: {"sample": float, "wins": int, "losses": int,
                                     "n": int, "explore": bool}}
    The "sample" is a draw from the posterior -- higher = more likely to be selected.
    """
    all_strategies = ["info_edge", "event_driven", "cross_market", "near_resolution"]
    result = {}

    for strat in all_strategies:
        # Get recent performance with recency weighting
        # Trades from the last BANDIT_RECENCY_DAYS get full weight,
        # older trades get exponentially decayed weight
        rows = conn.execute("""
            SELECT won, recorded_at FROM settlements
            WHERE strategy = ?
            ORDER BY id DESC LIMIT 200
        """, (strat,)).fetchall()

        wins = 0.0
        losses = 0.0
        n = 0
        now = datetime.now(timezone.utc)

        for won, recorded_at in rows:
            # Compute recency weight
            try:
                if recorded_at is None:
                    raise ValueError("missing recorded_at")
                t = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
                age_days = (now - t).total_seconds() / 86400
                weight = 0.5 ** (age_days / BANDIT_RECENCY_DAYS)  # half-life decay
            except Exception:
                weight = 0.5  # unknown age -> half weight

            if won:
                wins += weight
            else:
                losses += weight
            n += 1

        sample = _thompson_sample(wins, losses)
        is_explore = False

        # If this strategy has very few samples (<5), flag it for exploration
        if n < 5:
            is_explore = True
            # Boost the sample slightly to encourage trying new strategies
            sample = max(sample, 0.3)

        # Even well-sampled strategies get occasional re-exploration
        # This handles non-stationarity (market conditions change)
        if n >= 15 and wins / max(1, wins + losses) < 0.35:
            # This strategy has been losing — but still give it a chance
            if random.random() < STRATEGY_EXPLORE_PCT:
                is_explore = True
                sample = max(sample, 0.25)  # floor so it doesn't get totally ignored
                print(f"[bandit] Re-exploring strategy '{strat}' "
                      f"(wr={wins/(wins+losses):.0%}, n={n}) — checking if conditions changed")

        result[strat] = {
            "sample": sample,
            "wins": round(wins, 1),
            "losses": round(losses, 1),
            "n": n,
            "explore": is_explore,
        }

    return result


def compute_exploration_targets(conn, candidates, n_total_slots):
    """Given scored candidates, use Thompson Sampling to allocate slots between
    exploit (proven categories) and explore (under-tested categories).
    Returns (exploit_candidates, explore_candidates)."""

    # How many explore slots?
    n_explore = max(1, int(n_total_slots * EXPLORE_BUDGET_PCT))
    n_exploit = n_total_slots - n_explore

    # Count settled trades per category with recency weighting
    cat_stats = {}  # {cat: {"wins": float, "losses": float, "n": int}}
    rows = conn.execute(
        "SELECT ticker, won, recorded_at FROM settlements"
    ).fetchall()

    now = datetime.now(timezone.utc)
    for t, won, recorded_at in rows:
        cat = categorize_market(t, "")
        if cat not in cat_stats:
            cat_stats[cat] = {"wins": 0.0, "losses": 0.0, "n": 0}
        try:
            if recorded_at is None:
                raise ValueError("missing recorded_at")
            t_dt = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            age_days = (now - t_dt).total_seconds() / 86400
            weight = 0.5 ** (age_days / BANDIT_RECENCY_DAYS)
        except Exception:
            weight = 0.5
        if won:
            cat_stats[cat]["wins"] += weight
        else:
            cat_stats[cat]["losses"] += weight
        cat_stats[cat]["n"] += 1

    total_settled = sum(s["n"] for s in cat_stats.values())
    if total_settled < 15:
        # Too early — don't explore yet, we need baseline data
        return candidates[:n_total_slots], []

    # Thompson sample each category
    cat_samples = {}
    for cat, stats in cat_stats.items():
        cat_samples[cat] = _thompson_sample(stats["wins"], stats["losses"])

    # Find under-explored: <5 trades or Thompson sample is very uncertain
    all_candidate_cats = set()
    for c in candidates:
        ticker = c[9].get("ticker", "")
        title = c[9].get("title", "") or c[9].get("subtitle", "") or ""
        all_candidate_cats.add(categorize_market(ticker, title))

    under_explored = set()
    for cat in all_candidate_cats:
        if cat not in cat_stats or cat_stats[cat]["n"] < 5:
            under_explored.add(cat)
        elif cat_stats[cat]["n"] < 20:
            # Few samples — Thompson sampling will naturally explore these
            # But explicitly flag if the posterior is wide (high uncertainty)
            w, l = cat_stats[cat]["wins"], cat_stats[cat]["losses"]
            if (w + l) < 10:  # effective sample size is small
                under_explored.add(cat)

    if not under_explored:
        return candidates[:n_total_slots], []

    # Split candidates into exploit and explore pools
    exploit = []
    explore_pool = []
    for c in candidates:
        ticker = c[9].get("ticker", "")
        title = c[9].get("title", "") or c[9].get("subtitle", "") or ""
        cat = categorize_market(ticker, title)
        volume = c[4]
        if cat in under_explored and volume >= EXPLORE_MIN_VOLUME:
            explore_pool.append(c)
        else:
            exploit.append(c)

    exploit_picks = exploit[:n_exploit]
    explore_picks = explore_pool[:n_explore]

    if explore_picks:
        explore_cats = set()
        for c in explore_picks:
            ticker = c[9].get("ticker", "")
            title = c[9].get("title", "") or ""
            explore_cats.add(categorize_market(ticker, title))
        print(f"[bandit] Exploring {len(explore_picks)} under-sampled categories: "
              f"{explore_cats}")

    return exploit_picks, explore_picks
