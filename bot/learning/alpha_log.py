"""Atomic decision-time logging for the alpha_backtest table.

This is the Phase 1 mechanism for evaluating the Phase 0 gate's second leg
("ensemble beats market-mid by >= 0.005 on some identifiable slice"). Every
trading decision — MM quote, directional shadow, directional live, weather
shadow — writes one row capturing:

  - The ensemble's P(YES) estimate at decision time
  - The raw market snapshot (yes_bid, yes_ask, yes_last, spread, volume_fp)
  - A canonical market_prob_yes under a layered fallback rule
  - The decision outcome (posted / filled / discarded / shadow_only)
  - Later backfilled: settlement timestamp, outcome, realized P&L

Design choices
--------------

1. **Log raw market fields, not just the canonical mid.** The gate's
   sensitivity to market-price definition (bid/ask/2 vs last_price vs
   spread-weighted) is unknown going in, and on Kalshi the choice matters
   most in illiquid markets where it's hardest to get right. By storing
   yes_bid_cents, yes_ask_cents, yes_last_cents, spread_cents, and
   last_trade_age_s separately, analysis can re-evaluate the gate under
   any rule without re-collecting data. `market_prob_source` tags which
   rule fired for each row so we can filter to the "clean" slice.

2. **Canonical market_prob_yes resolution.** Layered fallback:

       'mid'       bid+ask both present, spread <= MAX_TIGHT_SPREAD_CENTS
       'last'      last_price present, age known and fresh (phase 2)
       'wide_mid'  bid+ask both present, spread exceeds the tight threshold
       'one_side'  only bid or only ask present
       'none'      nothing usable

   For the Phase 0 gate we'll evaluate on source IN ('mid', 'last') as
   the high-quality slice and widen as a sensitivity check.

3. **last_trade_age_s is NULL for now.** Kalshi's market snapshot does
   not include a trade timestamp. A follow-up change will track last-
   price changes via kv_cache to derive age; until then the 'last' path
   cannot fire and we fall through to 'wide_mid' on wide-spread markets.
   This is conservative — it over-uses mid — but analysis on the tight-
   mid slice is still correct.

4. **Never raise.** Logging is a side effect, not a dependency. If the
   DB is contended, the table missing, or the inputs malformed, we log
   a warning and return None. The trading loop must not break because
   instrumentation broke.

5. **Atomic write.** One INSERT per decision, under DB_WRITE_LOCK. No
   multi-statement transaction; no dependency on cycle-level commits.

6. **Idempotent backfill.** `fill_settlement()` updates rows where
   ts_settle IS NULL matching (ticker, side). Safe to call multiple times
   — subsequent calls see ts_settle already populated and no-op.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from bot.daemon.locks import DB_WRITE_LOCK

logger = logging.getLogger(__name__)


# ── Resolution thresholds ─────────────────────────────────────────────────
# A "tight" spread under which we trust the bid+ask midpoint as the market's
# point estimate. 5¢ is permissive enough for liquid markets (KXFED typically
# 1-2¢, day-of weather typically 2-4¢) but narrow enough to exclude the
# pathological 20-40¢ spreads seen on illiquid company markets or expiring
# weather brackets. Tunable via analysis — stored per-row in spread_cents so
# post-hoc re-slicing is free.
MAX_TIGHT_SPREAD_CENTS = 5

# Age beyond which a last_price is treated as stale and ignored as a proxy
# for current market belief. 10 minutes is long enough to cover quiet periods
# in otherwise-liquid markets; shorter than the typical "this market didn't
# trade for hours" regime on illiquid Kalshi tickers. Not yet enforceable —
# see module docstring note #3.
MAX_LAST_AGE_S = 600.0


# ── Enum-like constants (strings for sqlite portability) ──────────────────
class DecisionType:
    MM_QUOTE = "mm_quote"
    DIRECTIONAL_SHADOW = "directional_shadow"
    DIRECTIONAL_LIVE = "directional_live"
    WEATHER_QUOTER_SHADOW = "weather_quoter_shadow"
    SAFE_COMPOUNDER_SHADOW = "safe_compounder_shadow"
    SAFE_COMPOUNDER_LIVE = "safe_compounder_live"


class DecisionOutcome:
    POSTED = "posted"        # Order placed on exchange
    FILLED = "filled"        # Order filled (possibly partial — contracts = actual fill)
    DISCARDED = "discarded"  # Considered and rejected (skip_reason populated)
    SHADOW_ONLY = "shadow_only"  # Would have posted in live mode


class MarketProbSource:
    MID = "mid"
    LAST = "last"
    WIDE_MID = "wide_mid"
    ONE_SIDE = "one_side"
    NONE = "none"


# ── Data shapes ────────────────────────────────────────────────────────────
@dataclass
class MarketSnapshot:
    """Raw market state at decision time. All fields in cents or unit-typed.

    Callers should populate from Kalshi market dict — use `market_snapshot_from_dict`
    for the canonical mapping.
    """
    yes_bid_cents: Optional[int] = None
    yes_ask_cents: Optional[int] = None
    yes_last_cents: Optional[int] = None
    last_trade_age_s: Optional[float] = None
    volume_fp: Optional[int] = None


@dataclass
class EnsembleSnapshot:
    """Ensemble output at decision time. `p_yes` MUST be canonical P(YES),
    not per-side probability — caller is responsible for normalization."""
    p_yes: float
    confidence: Optional[float] = None
    source_count: Optional[int] = None
    sources: Optional[list[str]] = None
    source_estimates: Optional[dict[str, float]] = None


# ── Utilities ──────────────────────────────────────────────────────────────
def family_from_ticker(ticker: str) -> str:
    """Extract the family prefix. Kalshi tickers look like
    KXHIGHMIA-26APR18-T75 or KXFED-26MAY-T425. We take everything before
    the first hyphen as the family."""
    if not ticker:
        return ""
    idx = ticker.find("-")
    return ticker if idx == -1 else ticker[:idx]


def _parse_kalshi_cents(raw: Any) -> Optional[int]:
    """Coerce a Kalshi price field to integer cents.

    Kalshi returns both `*_dollars` (string, e.g. "0.47") and bare fields
    (might be int cents or float dollars depending on endpoint). Handle all:

      None / "" / 0  → None (no price, not zero cents)
      "0.47"         → 47
      0.47           → 47  (float < 1 is dollars)
      47             → 47  (int is cents)
      47.0           → 47  (float >= 1 is cents, floats are rounded)

    Uses round(), not int(), to avoid off-by-one from floating point —
    matches the convention called out in CLAUDE.md Known Bug Patterns #5.
    """
    if raw is None:
        return None
    if raw == 0 or raw == "" or raw == "0":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v < 1.0:
        return round(v * 100)
    return round(v)


def market_snapshot_from_dict(market: dict) -> MarketSnapshot:
    """Build a MarketSnapshot from a Kalshi market dict.

    Tolerates the various field name variants (bare / _dollars / _fp). Never
    raises — returns an empty snapshot on malformed input.

    NOTE: last_trade_age_s is always None for now — see module docstring #3.
    """
    if not isinstance(market, dict):
        return MarketSnapshot()

    yes_bid = _parse_kalshi_cents(
        market.get("yes_bid") or market.get("yes_bid_dollars") or market.get("yes_bid_cents")
    )
    yes_ask = _parse_kalshi_cents(
        market.get("yes_ask") or market.get("yes_ask_dollars") or market.get("yes_ask_cents")
    )
    yes_last = _parse_kalshi_cents(
        market.get("last_price") or market.get("yes_last_price") or market.get("last_price_cents")
    )

    vol_raw = market.get("volume") or market.get("volume_fp") or market.get("volume_24h")
    try:
        volume_fp = int(float(vol_raw)) if vol_raw else None
    except (TypeError, ValueError):
        volume_fp = None

    return MarketSnapshot(
        yes_bid_cents=yes_bid,
        yes_ask_cents=yes_ask,
        yes_last_cents=yes_last,
        last_trade_age_s=None,
        volume_fp=volume_fp,
    )


def resolve_market_prob(
    snapshot: MarketSnapshot,
    max_tight_spread_cents: int = MAX_TIGHT_SPREAD_CENTS,
    max_last_age_s: float = MAX_LAST_AGE_S,
) -> tuple[Optional[float], str, Optional[int]]:
    """Resolve canonical market_prob_yes from a market snapshot.

    Returns (prob_yes, source, spread_cents). prob_yes is in [0, 1] or None;
    source is one of the MarketProbSource constants; spread_cents is int or
    None when not both sides present.

    Resolution order:
      1. Both bid and ask present:
         a. If spread <= max_tight_spread_cents  → 'mid'
         b. Else if fresh last_price             → 'last'
         c. Else                                  → 'wide_mid'
      2. Only last_price (fresh):                 → 'last'
      3. Only one side (bid XOR ask):             → 'one_side'
      4. Only stale last_price (or unknown age):  → 'one_side'
      5. Nothing:                                  → 'none'
    """
    bid = snapshot.yes_bid_cents
    ask = snapshot.yes_ask_cents
    last = snapshot.yes_last_cents
    age = snapshot.last_trade_age_s

    bid_ok = bid is not None and bid > 0
    ask_ok = ask is not None and ask > 0
    last_ok = last is not None and last > 0
    last_fresh = last_ok and (age is not None) and (age < max_last_age_s)

    spread = None
    if bid_ok and ask_ok:
        spread = max(0, ask - bid)
        if spread <= max_tight_spread_cents:
            return ((bid + ask) / 200.0, MarketProbSource.MID, spread)
        if last_fresh:
            return (last / 100.0, MarketProbSource.LAST, spread)
        return ((bid + ask) / 200.0, MarketProbSource.WIDE_MID, spread)

    if last_fresh:
        return (last / 100.0, MarketProbSource.LAST, None)

    if bid_ok:
        return (bid / 100.0, MarketProbSource.ONE_SIDE, None)
    if ask_ok:
        return (ask / 100.0, MarketProbSource.ONE_SIDE, None)

    # As a last resort, accept a stale last_price under 'one_side' so rows
    # aren't completely unusable. Flagged via the source tag.
    if last_ok:
        return (last / 100.0, MarketProbSource.ONE_SIDE, None)

    return (None, MarketProbSource.NONE, None)


# ── Logging ────────────────────────────────────────────────────────────────
def log_decision(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    decision_type: str,
    decision_outcome: str,
    ensemble: EnsembleSnapshot,
    market: MarketSnapshot,
    side: Optional[str] = None,
    price_cents: Optional[int] = None,
    contracts: Optional[int] = None,
    skip_reason: Optional[str] = None,
    cycle_id: Optional[str] = None,
    notes: Optional[str] = None,
    ts_decision_unix: Optional[float] = None,
) -> Optional[int]:
    """Insert one row into alpha_backtest. Never raises.

    Returns the new row id on success, None on failure.

    Thread-safe — takes DB_WRITE_LOCK internally. Callers can use this from
    cycle-runner threads, poller threads, or anywhere else without holding
    the write lock themselves.
    """
    try:
        if ts_decision_unix is None:
            ts_decision_unix = time.time()
        ts_decision = (
            datetime.fromtimestamp(ts_decision_unix, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        family = family_from_ticker(ticker)
        market_prob, market_prob_source, spread_cents = resolve_market_prob(market)

        sources_json = (
            json.dumps(ensemble.sources) if ensemble.sources is not None else None
        )
        estimates_json = (
            json.dumps(ensemble.source_estimates)
            if ensemble.source_estimates is not None
            else None
        )

        with DB_WRITE_LOCK:
            cur = conn.execute(
                """INSERT INTO alpha_backtest (
                    ts_decision, ts_decision_unix, ticker, family,
                    decision_type, decision_outcome, side, price_cents,
                    contracts, skip_reason,
                    ensemble_p_yes, ensemble_confidence, source_count,
                    sources_json, source_estimates_json,
                    yes_bid_cents, yes_ask_cents, yes_last_cents,
                    last_trade_age_s, spread_cents, volume_fp,
                    market_prob_yes, market_prob_source,
                    cycle_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?)""",
                (
                    ts_decision, ts_decision_unix, ticker, family,
                    decision_type, decision_outcome, side, price_cents,
                    contracts, skip_reason,
                    float(ensemble.p_yes), ensemble.confidence, ensemble.source_count,
                    sources_json, estimates_json,
                    market.yes_bid_cents, market.yes_ask_cents, market.yes_last_cents,
                    market.last_trade_age_s, spread_cents, market.volume_fp,
                    market_prob, market_prob_source,
                    cycle_id, notes,
                ),
            )
            conn.commit()
            return cur.lastrowid
    except Exception as e:
        logger.warning("[alpha_log] log_decision(%s) failed: %s", ticker, e)
        return None


def fill_settlement(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    side: Optional[str],
    settlement_result: str,
    realized_pnl_cents: Optional[int] = None,
    ts_settle_unix: Optional[float] = None,
) -> int:
    """Backfill settlement fields on open alpha_backtest rows matching
    (ticker, [side]). Returns the number of rows updated.

    Idempotent: only updates rows where ts_settle_unix IS NULL, so repeated
    calls on a settled ticker are no-ops.

    If `side` is None, updates all open rows for the ticker regardless of
    side — useful when a settlement outcome applies to the whole market.

    `settlement_result` should be 'yes' or 'no'. Translates to won_yes: YES
    sides win when result='yes'; NO sides win when result='no'. For rows
    with side=None, won_yes is only populated when we can infer it from
    the decision_type context (we don't — set to NULL).
    """
    try:
        if ts_settle_unix is None:
            ts_settle_unix = time.time()
        ts_settle = (
            datetime.fromtimestamp(ts_settle_unix, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

        if side:
            if settlement_result == "yes":
                won_yes = 1 if side == "yes" else 0
            elif settlement_result == "no":
                won_yes = 0 if side == "yes" else 1
            else:
                won_yes = None
        else:
            won_yes = None

        with DB_WRITE_LOCK:
            if side:
                cur = conn.execute(
                    """UPDATE alpha_backtest
                       SET ts_settle=?, ts_settle_unix=?, settlement_result=?,
                           won_yes=?, realized_pnl_cents=?
                       WHERE ticker=? AND side=? AND ts_settle_unix IS NULL""",
                    (ts_settle, ts_settle_unix, settlement_result,
                     won_yes, realized_pnl_cents, ticker, side),
                )
            else:
                cur = conn.execute(
                    """UPDATE alpha_backtest
                       SET ts_settle=?, ts_settle_unix=?, settlement_result=?,
                           realized_pnl_cents=?
                       WHERE ticker=? AND ts_settle_unix IS NULL""",
                    (ts_settle, ts_settle_unix, settlement_result,
                     realized_pnl_cents, ticker),
                )
            conn.commit()
            return cur.rowcount or 0
    except Exception as e:
        logger.warning("[alpha_log] fill_settlement(%s) failed: %s", ticker, e)
        return 0


def fill_settlement_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    settlement_result: str,
    ts_settle_unix: Optional[float] = None,
) -> int:
    """Settle all open alpha_backtest rows for a ticker.

    Unlike `fill_settlement`, this computes per-row won_yes and counterfactual
    realized_pnl_cents from (side, price_cents, contracts) + market_result.
    Use this from record_settlements() — one call handles every shadow decision
    we logged for this ticker, across both sides.

    Counterfactual P&L (had we actually traded at price_cents × contracts):
      won:  contracts * (100 - price_cents)
      lost: -contracts * price_cents

    Rows with side IS NULL or price_cents IS NULL get settlement_result filled
    but won_yes / realized_pnl_cents left NULL (not enough info).

    Returns the number of rows updated. Idempotent (only rows where
    ts_settle_unix IS NULL are touched).
    """
    try:
        if settlement_result not in ("yes", "no"):
            logger.warning(
                "[alpha_log] fill_settlement_for_ticker(%s) bad result: %s",
                ticker, settlement_result,
            )
            return 0
        if ts_settle_unix is None:
            ts_settle_unix = time.time()
        ts_settle = (
            datetime.fromtimestamp(ts_settle_unix, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

        with DB_WRITE_LOCK:
            cur = conn.execute(
                """UPDATE alpha_backtest
                   SET ts_settle = ?,
                       ts_settle_unix = ?,
                       settlement_result = ?,
                       won_yes = CASE
                           WHEN side = 'yes' AND ? = 'yes' THEN 1
                           WHEN side = 'yes' AND ? = 'no'  THEN 0
                           WHEN side = 'no'  AND ? = 'no'  THEN 1
                           WHEN side = 'no'  AND ? = 'yes' THEN 0
                           ELSE NULL
                       END,
                       realized_pnl_cents = CASE
                           WHEN side IS NULL OR price_cents IS NULL OR contracts IS NULL
                               THEN NULL
                           WHEN (side = 'yes' AND ? = 'yes')
                             OR (side = 'no'  AND ? = 'no')
                               THEN contracts * (100 - price_cents)
                           ELSE -contracts * price_cents
                       END
                   WHERE ticker = ? AND ts_settle_unix IS NULL""",
                (
                    ts_settle, ts_settle_unix, settlement_result,
                    settlement_result, settlement_result,
                    settlement_result, settlement_result,
                    settlement_result, settlement_result,
                    ticker,
                ),
            )
            conn.commit()
            return cur.rowcount or 0
    except Exception as e:
        logger.warning(
            "[alpha_log] fill_settlement_for_ticker(%s) failed: %s", ticker, e
        )
        return 0
