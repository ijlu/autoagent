"""Market avoidance filters (learned from settlement history).

The `categorize_market` / `CATEGORY_KEYWORDS` / `_COMPANY_PREFIXES` exports
used to live here and also in `bot.core.categorization`. After the MM deletion
pivot (2026-04-16) the canonical home is `bot.core.categorization`; this module
now re-exports them so existing callers keep working without touching their
imports.

Still owned here:
  - compute_avoid_filters() — settlement-history-driven avoidance filters
"""

from __future__ import annotations

from bot.config import MIN_SAMPLE_SIZE, MIN_WIN_RATE

# Re-exports (canonical home: bot.core.categorization)
from bot.core.categorization import (  # noqa: F401
    CATEGORY_KEYWORDS,
    _COMPANY_PREFIXES,
    categorize_market,
)


# ══════════════════════════════════════════════════════════════════════════════
# Avoidance filters -- learned from settlement history
# ══════════════════════════════════════════════════════════════════════════════

def compute_avoid_filters(conn):
    """Analyze settlement history to build volume/strategy/prefix avoidance filters.

    Returns a dict with keys:
      - low_volume_threshold: int or None
      - wide_spread_threshold: int or None
      - avoided_strategies: set of strategy names
      - avoided_prefixes: set of ticker prefixes
      - calibration: dict of bucket -> {avg_estimate, actual_rate, bias, n}
      - summary: list of human-readable log lines
    """
    filters = {"low_volume_threshold": None, "wide_spread_threshold": None,
               "avoided_strategies": set(), "avoided_prefixes": set(), "summary": []}
    rows = conn.execute(
        "SELECT volume, spread_cents, strategy, ticker, won FROM settlements WHERE volume IS NOT NULL"
    ).fetchall()
    if not rows:
        print("[learn] No settlement history yet"); return filters
    print(f"[learn] Analyzing {len(rows)} settled trades \u2026")

    buckets = {"tiny": ([], 50), "low": ([], 500), "medium": ([], 5000), "high": ([], None)}
    for vol, sp, strat, tick, won in rows:
        v = vol or 0
        if v < 50: buckets["tiny"][0].append(won)
        elif v < 500: buckets["low"][0].append(won)
        elif v < 5000: buckets["medium"][0].append(won)
        else: buckets["high"][0].append(won)
    for name, (outcomes, thresh) in buckets.items():
        if len(outcomes) < MIN_SAMPLE_SIZE: continue
        wr = sum(outcomes)/len(outcomes)
        msg = f"  vol[{name}] wr={wr:.0%} n={len(outcomes)}"
        if wr < MIN_WIN_RATE and thresh:
            filters["low_volume_threshold"] = max(filters["low_volume_threshold"] or 0, thresh)
            msg += f" \u2192 AVOID"
        filters["summary"].append(msg); print(msg)

    strat_map, prefix_map = {}, {}
    for vol, sp, strat, tick, won in rows:
        if strat: strat_map.setdefault(strat, []).append(won)
        if tick: prefix_map.setdefault(tick[:6], []).append(won)
    for strat, outcomes in strat_map.items():
        if len(outcomes) < MIN_SAMPLE_SIZE: continue
        wr = sum(outcomes)/len(outcomes)
        msg = f"  strat[{strat[:20]}] wr={wr:.0%} n={len(outcomes)}"
        if wr < MIN_WIN_RATE: filters["avoided_strategies"].add(strat); msg += " \u2192 AVOID"
        filters["summary"].append(msg); print(msg)
    for pfx, outcomes in prefix_map.items():
        if len(outcomes) < MIN_SAMPLE_SIZE: continue
        wr = sum(outcomes)/len(outcomes)
        msg = f"  prefix[{pfx}] wr={wr:.0%} n={len(outcomes)}"
        if wr < MIN_WIN_RATE: filters["avoided_prefixes"].add(pfx); msg += " \u2192 AVOID"
        filters["summary"].append(msg); print(msg)

    # -- Calibration analysis: are our probability estimates accurate? --------
    # Group settled trades by estimated probability bucket and check if
    # actual win rate matches the estimate. Overconfident buckets get flagged.
    cal_rows = conn.execute(
        "SELECT bucket, estimated_prob, actual_outcome FROM calibration WHERE bucket IS NOT NULL"
    ).fetchall()
    if cal_rows:
        cal_buckets = {}
        for bucket, est, actual in cal_rows:
            cal_buckets.setdefault(bucket, []).append((est, actual))
        filters["calibration"] = {}
        print(f"[calibration] Analyzing {len(cal_rows)} settled predictions:")
        for bucket in sorted(cal_buckets.keys()):
            entries = cal_buckets[bucket]
            n = len(entries)
            if n < 3: continue  # need minimum samples
            avg_est = sum(e for e, _ in entries) / n
            actual_rate = sum(a for _, a in entries) / n
            bias = avg_est - actual_rate  # positive = overconfident
            filters["calibration"][bucket] = {
                "avg_estimate": avg_est, "actual_rate": actual_rate,
                "bias": bias, "n": n}
            status = "OK" if abs(bias) < 0.10 else ("OVERCONFIDENT" if bias > 0 else "UNDERCONFIDENT")
            msg = (f"  cal[{bucket}] est={avg_est:.2f} actual={actual_rate:.2f} "
                   f"bias={bias:+.2f} n={n} {status}")
            filters["summary"].append(msg); print(msg)

    return filters
