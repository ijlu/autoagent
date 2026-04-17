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


# ── Runtime per-family live state (graduated promotion) ──────────────────
# Three states per family, stored as a JSON blob in kv_cache under
# `directional_live:<FAMILY>`:
#
#   shadow       — size 0, log-only (default for unseen families)
#   live_canary  — 0.5× Kelly, used for the first 30 live settlements
#   live_full    — 1.0× Kelly, reached after canary clears its own gate
#
# Symmetric auto-demotion: full → canary → shadow on kill-switch trip.
# Once a family hits shadow via auto-demote, re-promotion is MANUAL
# (operator-only `set_live_state`). Canary → full is the one auto
# transition after demotion doesn't fire: prevents same-signal flip-flop.
import json as _json
import time as _time
from dataclasses import asdict

_KV_PREFIX = "directional_live:"
_KV_TTL_S = 30 * 24 * 3600  # 30 days


class LiveState:
    SHADOW = "shadow"
    LIVE_CANARY = "live_canary"
    LIVE_FULL = "live_full"


_ALL_STATES: frozenset[str] = frozenset({
    LiveState.SHADOW, LiveState.LIVE_CANARY, LiveState.LIVE_FULL,
})

# Kelly size multiplier per state. Canary = half-Kelly matches the staged
# canary-rollout convention from Option G discussion (2026-04-17).
_KELLY_MULTIPLIER: dict[str, float] = {
    LiveState.SHADOW: 0.0,
    LiveState.LIVE_CANARY: 0.5,
    LiveState.LIVE_FULL: 1.0,
}


@dataclass(frozen=True)
class LiveFlag:
    """Full state of a family's live flag.

    `since_ts_unix` is the moment the family entered `state` — used by the
    promotion job to count "settlements in current state" and drive the
    30-settlement canary → full transition.

    `manual` distinguishes operator-set flags from auto-promotions. A manual
    flag is protected from the auto-demote ratchet during the 24h after set
    (operator override window).
    """
    state: str
    since_ts_unix: float
    manual: bool = False


def _kv_key(family: str) -> str:
    return f"{_KV_PREFIX}{family.upper()}"


def _parse_live_flag(raw: object) -> Optional[LiveFlag]:
    """Decode a kv_cache value into a LiveFlag. Tolerates legacy bool values.

    Legacy rows written pre-graduation (`True`/`False`) are mapped to
    `live_full`/`shadow` so an in-place upgrade doesn't reset state.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        state = LiveState.LIVE_FULL if raw else LiveState.SHADOW
        return LiveFlag(state=state, since_ts_unix=_time.time(), manual=False)
    if isinstance(raw, dict):
        state = raw.get("state")
        if state not in _ALL_STATES:
            return None
        return LiveFlag(
            state=state,
            since_ts_unix=float(raw.get("since_ts_unix") or _time.time()),
            manual=bool(raw.get("manual", False)),
        )
    return None


def get_live_state(conn, family: str) -> LiveFlag:
    """Return the full live-flag state. Defaults to SHADOW for unseen families
    and hard-blocked families (block list wins over any kv write).
    """
    family_u = family.upper()
    if family_u in DIRECTIONAL_BLOCKED_FAMILIES:
        return LiveFlag(state=LiveState.SHADOW, since_ts_unix=0.0, manual=False)
    try:
        raw = kv_get(conn, _kv_key(family_u))
    except Exception:
        return LiveFlag(state=LiveState.SHADOW, since_ts_unix=0.0, manual=False)
    parsed = _parse_live_flag(raw)
    return parsed or LiveFlag(state=LiveState.SHADOW, since_ts_unix=0.0,
                              manual=False)


def should_trade_live(conn, family: str) -> bool:
    """True if the family is in any live state (canary or full).

    Callers that need to size-scale should use `get_kelly_multiplier` rather
    than this bool — a canary family "trades live" but at half size.
    """
    return get_live_state(conn, family).state != LiveState.SHADOW


def get_kelly_multiplier(conn, family: str) -> float:
    """Return the Kelly size multiplier for this family's current state.

    0.0 = shadow (no order posted), 0.5 = canary, 1.0 = full.
    """
    flag = get_live_state(conn, family)
    return _KELLY_MULTIPLIER.get(flag.state, 0.0)


def set_live_state(
    conn, family: str, state: str, *, manual: bool = False,
) -> None:
    """Persist a new live state. Used by the promotion job and operator tools.

    `manual=True` marks the write as operator-sourced — only path to re-enter
    live after an auto-demote to shadow (the ratchet in `should_go_live`
    rejects auto re-promotion to keep a bled family from flip-flopping back).
    """
    if state not in _ALL_STATES:
        raise ValueError(f"invalid live state {state!r}")
    family_u = family.upper()
    if family_u in DIRECTIONAL_BLOCKED_FAMILIES and state != LiveState.SHADOW:
        raise ValueError(
            f"family {family_u} is on the hard block list; cannot promote"
        )
    flag = LiveFlag(state=state, since_ts_unix=_time.time(), manual=manual)
    try:
        kv_set(conn, _kv_key(family_u), asdict(flag), _KV_TTL_S)
    except Exception as exc:  # pragma: no cover — best-effort persistence
        logger.warning("[directional-shadow] set_live_state(%s, %s) failed: %s",
                       family_u, state, exc)


def set_live_flag(conn, family: str, enabled: bool) -> None:
    """Back-compat wrapper — bool write is equivalent to SHADOW / LIVE_FULL.

    New code should call `set_live_state` so the canary path is reachable.
    """
    set_live_state(
        conn, family,
        LiveState.LIVE_FULL if enabled else LiveState.SHADOW,
        manual=True,
    )


# ── Step-9 graduation thresholds ──────────────────────────────────────────
# Tiered per strategy (option D): directional uses Brier + realized-P&L +
# out-of-sample. Weather MM stays manual until Phase 2 ships a fill-model.
DEFAULT_MIN_SETTLED = 50          # N floor for first promotion (shadow → canary)
DEFAULT_MIN_CANARY_SETTLED = 30   # N floor for canary → full auto-promote
DEFAULT_MIN_EDGE_BEAT = 0.005     # 0.5pp Brier beat over baseline (first gate)
DEFAULT_RATCHETED_EDGE_BEAT = 0.010  # 1.0pp beat required after an auto-demote


def should_go_live(
    conn,
    family: str,
    *,
    min_settled: int = DEFAULT_MIN_SETTLED,
    min_edge_beat: float = DEFAULT_MIN_EDGE_BEAT,
) -> bool:
    """Thin convenience wrapper — returns True iff the family is currently in a
    live state. The real promotion/demotion decisions live in
    `bot.learning.shadow_promotion.run_promotion_sweep()` which reads
    `alpha_backtest` + `settlements` and writes via `set_live_state`.

    Kept here as a stable call-site for legacy code.
    """
    return should_trade_live(conn, family)
