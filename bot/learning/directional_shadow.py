"""Directional shadow evaluator — Phase 1 step 7.

Single pure-function gate for every directional candidate that survives
scoring. Replaces the ad-hoc `_alpha_log_decision` + `DIRECTIONAL_BLOCKED`
logic that was scattered through `trade.py`.

Three things live here:

1. **`evaluate()`** — pure function. Given a candidate's post-calibration
   `indep_prob`, Kelly-sized `contracts`, market snapshot, and target side,
   returns a `ShadowDecision` describing the outcome (`blocked`, `kelly_zero`,
   `below_edge`, or `shadow_pass`) plus the derived edge-vs-mid in pp.
   No DB writes. Callers decide whether to log + skip.

2. **`should_trade_live(conn, family)`** — runtime per-family kill switch
   read from `kv_cache` key `directional_live:<family>`. Default False.
   Step 9's shadow-to-live gate flips kv rows to True as individual
   families prove out; no code change required for promotion.

3. **`should_go_live(conn, family, ...)`** — step-9 graduation stub. Reads
   `alpha_backtest` + `calibration` and returns True once a family's
   settled shadow P&L clears the bar. Placeholder here (always False);
   step 9 implements the SQL.

The split matters: `evaluate` is the per-decision check (runs every candidate,
every cycle). `should_go_live` is the per-family graduation check (runs
once-a-day via the step-9 job, flips kv keys). `should_trade_live` is the
runtime read the hot path uses. All three colocated so the family-name
parsing and block-list lookups stay in one place.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from bot.config import DIRECTIONAL_BLOCKED_FAMILIES
from bot.db import kv_get, kv_set
from bot.learning.alpha_log import family_from_ticker

logger = logging.getLogger(__name__)


# ── Outcome constants ─────────────────────────────────────────────────────
# String values (not an enum) to match the other decision-enum styling
# in alpha_log.py, which keeps sqlite-side filtering grep-friendly.
class ShadowOutcome:
    BLOCKED = "blocked"        # Family on the hard-block list
    KELLY_ZERO = "kelly_zero"  # Kelly sizing rounds to 0 contracts
    BELOW_EDGE = "below_edge"  # Post-cal prob does not beat market by MIN_EDGE
    SHADOW_PASS = "shadow_pass"  # Would trade in live mode


_ALL_OUTCOMES: frozenset[str] = frozenset({
    ShadowOutcome.BLOCKED,
    ShadowOutcome.KELLY_ZERO,
    ShadowOutcome.BELOW_EDGE,
    ShadowOutcome.SHADOW_PASS,
})


@dataclass(frozen=True)
class ShadowDecision:
    """Result of evaluating one directional candidate.

    ``edge_vs_mid`` is the our-side probability minus the our-side market
    mid (as decimal points, not bps), **after** post-calibration. None when
    the market price is unknown.

    ``skip_reason`` mirrors the string the caller should write to
    ``alpha_backtest.skip_reason`` when outcome != SHADOW_PASS. We keep the
    two fields separate because one is an enum (joinable) and the other
    is a human-readable detail.
    """
    outcome: str
    family: str
    side: str
    price_cents: int
    contracts: int
    our_prob: float
    market_prob: Optional[float]
    edge_vs_mid: Optional[float]
    skip_reason: Optional[str]


def _our_side_market_prob(side: str, market_mid_cents: Optional[int]) -> Optional[float]:
    """Convert market mid (YES-side cents) to our-side probability.

    For a YES trade the market mid is already our-side. For a NO trade
    we're long NO, so P(our_side) = 1 - P(YES).
    """
    if market_mid_cents is None:
        return None
    yes_prob = max(0.0, min(1.0, market_mid_cents / 100.0))
    return yes_prob if side == "yes" else 1.0 - yes_prob


def evaluate(
    *,
    ticker: str,
    side: str,
    indep_prob: float,
    contracts: int,
    price_cents: int,
    market_mid_cents: Optional[int],
    min_edge: float,
    blocklist: frozenset[str] = DIRECTIONAL_BLOCKED_FAMILIES,
) -> ShadowDecision:
    """Apply the step-7 directional gate to a single candidate.

    Order of checks is significant — block-list first so we never spend
    Kelly/edge compute on a family we refuse to trade:

      1. Family block list (hard)
      2. Kelly size == 0
      3. Edge-vs-market-mid < `min_edge`
      4. Otherwise: SHADOW_PASS

    `indep_prob` is already P(our_side), post-calibration — matching the
    convention in `trade.py`'s directional candidate pipeline, where the
    NO-side flip happens at candidate construction (score_market.py:5320/5330).
    Passing the post-Platt our-side probability directly means the evaluator
    does not need to know YES↔NO orientation for the probability leg.
    `market_mid_cents`, however, is always YES-side cents straight from Kalshi,
    so `_our_side_market_prob()` does the side flip on the market leg.
    """
    family = family_from_ticker(ticker).upper()
    our_prob_side = float(indep_prob)
    market_prob_side = _our_side_market_prob(side, market_mid_cents)

    # ── 1. Hard block ─────────────────────────────────────────────────
    if family in blocklist:
        return ShadowDecision(
            outcome=ShadowOutcome.BLOCKED,
            family=family,
            side=side,
            price_cents=int(price_cents),
            contracts=0,
            our_prob=our_prob_side,
            market_prob=market_prob_side,
            edge_vs_mid=None,
            skip_reason=f"family_blocked:{family}",
        )

    # ── 2. Kelly sizing produced no position ──────────────────────────
    if contracts <= 0:
        return ShadowDecision(
            outcome=ShadowOutcome.KELLY_ZERO,
            family=family,
            side=side,
            price_cents=int(price_cents),
            contracts=0,
            our_prob=our_prob_side,
            market_prob=market_prob_side,
            edge_vs_mid=None,
            skip_reason="kelly_zero",
        )

    # ── 3. Edge-vs-mid gate (Phase 0 → Phase 1 graduation criterion) ──
    edge_vs_mid: Optional[float] = None
    if market_prob_side is not None:
        edge_vs_mid = our_prob_side - market_prob_side
        if edge_vs_mid < min_edge:
            return ShadowDecision(
                outcome=ShadowOutcome.BELOW_EDGE,
                family=family,
                side=side,
                price_cents=int(price_cents),
                contracts=int(contracts),
                our_prob=our_prob_side,
                market_prob=market_prob_side,
                edge_vs_mid=edge_vs_mid,
                skip_reason=f"edge_vs_mid={edge_vs_mid:+.3f}<{min_edge:.3f}",
            )

    # ── 4. Pass ───────────────────────────────────────────────────────
    return ShadowDecision(
        outcome=ShadowOutcome.SHADOW_PASS,
        family=family,
        side=side,
        price_cents=int(price_cents),
        contracts=int(contracts),
        our_prob=our_prob_side,
        market_prob=market_prob_side,
        edge_vs_mid=edge_vs_mid,
        skip_reason=None,
    )


# ── Runtime per-family live flag ──────────────────────────────────────────
# kv_cache schema: key="directional_live:<FAMILY>", value=True/False.
# Default False when missing — safety-first: new families are shadow-only.
_KV_PREFIX = "directional_live:"
_KV_TTL_S = 30 * 24 * 3600  # 30 days


def _kv_key(family: str) -> str:
    return f"{_KV_PREFIX}{family.upper()}"


def should_trade_live(conn, family: str) -> bool:
    """True if the per-family live flag has been flipped on in kv_cache.

    Default: False. Hard-blocked families are never live regardless of kv.
    """
    family_u = family.upper()
    if family_u in DIRECTIONAL_BLOCKED_FAMILIES:
        return False
    try:
        val = kv_get(conn, _kv_key(family_u))
    except Exception:
        return False
    return bool(val) if val is not None else False


def set_live_flag(conn, family: str, enabled: bool) -> None:
    """Persist the per-family live flag. Used by step-9 promotion job and tests.

    Writing a False is explicit — it shadows prior True values instead of
    relying on TTL expiry.
    """
    family_u = family.upper()
    try:
        kv_set(conn, _kv_key(family_u), bool(enabled), _KV_TTL_S)
    except Exception as exc:  # pragma: no cover — best-effort persistence
        logger.warning("[directional-shadow] set_live_flag(%s, %s) failed: %s",
                       family_u, enabled, exc)


# ── Step-9 graduation stub (will be fleshed out in step 9) ────────────────
DEFAULT_MIN_SETTLED = 50
DEFAULT_MIN_EDGE_BEAT = 0.005  # 0.5 percentage points over baseline


def should_go_live(
    conn,
    family: str,
    *,
    min_settled: int = DEFAULT_MIN_SETTLED,
    min_edge_beat: float = DEFAULT_MIN_EDGE_BEAT,
) -> bool:
    """Step-9 shadow-to-live gate — returns False until step 9 implements it.

    Contract:
      * Must return False for hard-blocked families.
      * Must return False when fewer than `min_settled` SHADOW_PASS rows
        exist in alpha_backtest for this family.
      * Otherwise compares realized shadow P&L vs baseline and flips to
        True once calibration + directional alpha clears `min_edge_beat`.

    Step 9 will read `alpha_backtest` + `calibration` to implement this.
    Defined here so the call-sites in step-7 code (and the eventual step-9
    cron) can stabilize now.
    """
    family_u = family.upper()
    if family_u in DIRECTIONAL_BLOCKED_FAMILIES:
        return False
    # Step 9 implements the SQL. For now, be paranoid: False unless the
    # operator explicitly flipped the live flag by hand.
    return False
