"""Thompson-sampled order-size multiplier for weather MM.

Replaces the prior SHADOW → CANARY → FULL step gate with a continuous
posterior over per-fill P&L. Each sweep draws one sample from the
posterior mean; that sample, normalized by a target-edge constant and
clamped to a cap, is the multiplier applied to `MM_ORDER_SIZE` /
`MM_MAX_INVENTORY` for the next quoting interval.

Why Thompson instead of UCB/LCB:
    Thompson's draw itself provides exploration — no extra epsilon.
    Posterior variance shrinks with n, so a well-characterized series
    converges to its mean sizing; an under-sampled series' multiplier
    swings wildly, which surfaces as visible variance to the operator
    rather than a silent wrong-size regime.

Why normal (not Student-t):
    At n ≥ min_n (default 5) the t_{n-1} tails are close enough to
    Normal for sizing purposes; we clamp to [0, cap] anyway, so the
    difference at the tails is absorbed by the clamp. Python stdlib
    has `random.gauss`; no numpy needed.

Pure-function surface — caller provides the per-fill P&L list, we
return a dataclass. Seed the RNG in tests for determinism.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class ThompsonSizeDecision:
    """Output of one sizing draw.

    `multiplier` is the clamped value the quoter should use. The other
    fields are diagnostics suitable for JSON logging to
    `promotion_events.metrics_json`.
    """
    multiplier: float
    n: int
    mean_cents: float
    std_cents: float
    se_cents: float
    mu_sample_cents: float
    reason: str  # "insufficient_n" | "degenerate_variance" | "sampled"


def thompson_mm_size_multiplier(
    pnls_cents: Sequence[float],
    *,
    target_edge_cents: float = 2.0,
    cap_multiplier: float = 1.0,
    min_n: int = 5,
    rng: Optional[random.Random] = None,
) -> ThompsonSizeDecision:
    """Draw a sizing multiplier from the posterior over per-fill P&L.

    Parameters
    ----------
    pnls_cents : sequence of float
        One entry per settled shadow row with ≥1 filled leg. Negative
        values are losses; we do NOT filter them.
    target_edge_cents : float
        P&L-per-fill value that maps to the cap multiplier. Default 2¢ —
        a series realizing 2¢/fill in expectation is sized at full cap.
    cap_multiplier : float
        Upper clamp on the returned multiplier. 1.0 means the configured
        `MM_ORDER_SIZE`; >1 would over-size relative to config.
    min_n : int
        Below this many observations, abstain (multiplier=0) — posterior
        variance is too wide to trust a draw.
    rng : random.Random, optional
        Seedable RNG for deterministic tests. Module default if None.

    Returns
    -------
    ThompsonSizeDecision
        `multiplier` ∈ [0, cap_multiplier]. All diagnostic fields are
        floats safe to JSON-serialize.
    """
    n = len(pnls_cents)
    if n < min_n:
        return ThompsonSizeDecision(
            multiplier=0.0, n=n, mean_cents=0.0, std_cents=0.0,
            se_cents=0.0, mu_sample_cents=0.0, reason="insufficient_n",
        )

    xbar = statistics.fmean(pnls_cents)
    try:
        s = statistics.stdev(pnls_cents)  # ddof=1
    except statistics.StatisticsError:
        s = 0.0

    # All-equal series → posterior collapses to a point mass at x̄.
    # Return the deterministic multiplier without sampling.
    if s == 0.0 or not math.isfinite(s):
        mult = max(0.0, min(cap_multiplier, xbar / target_edge_cents))
        return ThompsonSizeDecision(
            multiplier=mult, n=n, mean_cents=xbar, std_cents=0.0,
            se_cents=0.0, mu_sample_cents=xbar,
            reason="degenerate_variance",
        )

    _rng = rng or random.Random()
    se = s / math.sqrt(n)
    mu_sample = _rng.gauss(xbar, se)
    mult = max(0.0, min(cap_multiplier, mu_sample / target_edge_cents))
    return ThompsonSizeDecision(
        multiplier=mult, n=n, mean_cents=xbar, std_cents=s,
        se_cents=se, mu_sample_cents=mu_sample, reason="sampled",
    )
