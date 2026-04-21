"""MM Thompson-sampled sizing gate — Phase 1 step 10 (option A.7).

Per-series sizing for weather MM. Each series is either SHADOW (sizing
multiplier = 0) or LIVE (multiplier drawn from a normal posterior over
realized shadow-P&L). No CANARY middle state — the SHADOW→LIVE promotion
is a single N-floor (`MM_SIZING_MIN_N` settled fills), and the Thompson
posterior handles all further gradation by shrinking the multiplier when
variance is wide or mean is weak. See bot/core/sizing.py for the math;
the legacy three-state constants (`LiveState.LIVE_CANARY`) remain only
for back-compat with existing kv_cache rows.

Gate metric stays **realized shadow P&L** after fill-matching and fee
subtraction — not Brier, because the Apr 17 backtest proved fair-value
calibration is fine; the losing piece is position-lifetime P&L.

Data flow:

    WeatherQuoter.shadow_requote_*()
        → weather_mm_shadow row (proposed bid/ask, market snapshot)
            → match_shadow_fills()    # scan later snapshots, set bid/ask_filled
                → annotate_shadow_pnl()  # on settlement, compute PnL net of fees
                    → evaluate_mm_promotion / kill_switch / graduation
                        → run_mm_promotion_sweep()     # daily, daemon task

Fill model (simple, data we already capture):
    A posted BID at price P is 'shadow-filled' iff a subsequent
    weather_mm_shadow row on the same ticker shows `market_yes_ask <= P`
    within the quote lifetime (default 5 min). Symmetrically for ASKs vs
    market_yes_bid. This is a trade-conservative approximation — we only
    count a fill when the market actually crossed our price, not merely
    touched it. Queue-position effects are not modeled; the gate compensates
    by requiring realized P&L, not expected P&L.

T.6 (paired logging): the same table holds both shadow and live fills
(disambiguated by `live_mode=1` and `live_order_id_*`). That lets the
kill-switch + threshold-tuner compute `live_pnl / shadow_pnl` ratio over
time as the shadow-fill model's calibration monitor.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from bot.core.money import kalshi_maker_fee
from bot.daemon.locks import DB_WRITE_LOCK
from bot.db import db_write_ctx, kv_get, kv_set
from bot.learning.directional_shadow import LiveFlag, LiveState, _parse_live_flag

logger = logging.getLogger(__name__)


# ── Per-series live state (kv_cache backed) ─────────────────────────────
# Mirrors directional_shadow's `directional_live:<FAMILY>` pattern but
# keyed by weather *series* (KXHIGHNY, KXHIGHCHI, …). Reuses the same
# LiveState constants and LiveFlag dataclass — one mental model for the
# operator covering both directional and MM.

_KV_PREFIX = "mm_live:"
_KV_TTL_S = 30 * 24 * 3600

# Weather MM is blocked-by-default at the series level until the Phase 0
# backtest analysis is revisited. Start empty: *any* series can be
# promoted once shadow data supports it. (Contrast with directional's
# catastrophic-calibration block list — MM has no families where the
# fair-value is broken, only families where structure held through
# directional moves. That's what the gate itself catches.)
MM_BLOCKED_SERIES: frozenset[str] = frozenset()

# kv_cache key for the last-sampled Thompson multiplier. Getter reads
# this first; on miss, samples from the posterior and writes it back.
# Stored payload: {"multiplier": float, "n": int, "mean_cents": float,
# "std_cents": float, "mu_sample_cents": float, "ts_unix": float}.
_KV_MULT_PREFIX = "mm_mult:"


def _kv_key(series: str) -> str:
    return f"{_KV_PREFIX}{series.upper()}"


def _kv_mult_key(series: str) -> str:
    return f"{_KV_MULT_PREFIX}{series.upper()}"


def get_mm_live_state(conn: sqlite3.Connection, series: str) -> LiveFlag:
    """Current live-state flag for a weather series. Defaults to SHADOW."""
    series_u = series.upper()
    if series_u in MM_BLOCKED_SERIES:
        return LiveFlag(state=LiveState.SHADOW, since_ts_unix=0.0, manual=False)
    try:
        raw = kv_get(conn, _kv_key(series_u))
    except Exception:
        return LiveFlag(state=LiveState.SHADOW, since_ts_unix=0.0, manual=False)
    return (_parse_live_flag(raw)
            or LiveFlag(state=LiveState.SHADOW, since_ts_unix=0.0, manual=False))


def set_mm_live_state(
    conn: sqlite3.Connection, series: str, state: str, *, manual: bool = False,
) -> None:
    """Persist a new MM live state. Raises on hard-blocked promotions."""
    if state not in (LiveState.SHADOW, LiveState.LIVE_CANARY, LiveState.LIVE_FULL):
        raise ValueError(f"invalid MM live state {state!r}")
    series_u = series.upper()
    if series_u in MM_BLOCKED_SERIES and state != LiveState.SHADOW:
        raise ValueError(
            f"series {series_u} is on the MM hard block list; cannot promote"
        )
    payload = {
        "state": state,
        "since_ts_unix": time.time(),
        "manual": bool(manual),
    }
    try:
        kv_set(conn, _kv_key(series_u), payload, _KV_TTL_S)
    except Exception as exc:  # pragma: no cover
        logger.warning("[mm-promotion] set_mm_live_state(%s,%s) failed: %s",
                       series_u, state, exc)


def is_mm_live(conn: sqlite3.Connection, series: str) -> bool:
    """True iff the series is currently in any live state (canary/full).

    Default for unseen series is SHADOW — Thompson promotion happens via
    `run_mm_promotion_sweep` once N_fills crosses `MM_SIZING_MIN_N`.
    """
    return get_mm_live_state(conn, series).state != LiveState.SHADOW


def get_mm_order_size_multiplier(
    conn: sqlite3.Connection,
    series: str,
    *,
    force_resample: bool = False,
) -> float:
    """Order-size multiplier for the series, state-dependent:

    - SHADOW (or blocked)  → 0.0  (quoter routes to shadow path)
    - LIVE_CANARY          → 1 / MM_ORDER_SIZE  (one contract fixed)
    - LIVE_FULL            → Thompson-sampled from shadow-P&L posterior

    Canary multiplier is computed against the current `MM_ORDER_SIZE`
    (which is equity-scaled at start of each cycle) so the effective
    order size is always exactly 1 contract regardless of equity. This
    caps canary-phase exposure per series to 1 × 99¢ × K-paired-rows
    (~$20 in the worst case before graduation or kill-switch).

    For FULL: reads the cached multiplier from kv_cache; on miss or if
    `force_resample`, draws a fresh sample from the posterior over
    realized shadow P&L and caches it (`MM_SIZING_CACHE_TTL_S` seconds).
    Between sweeps the quoter sees a stable value; on TTL expiry the
    next call re-samples. Deterministic in tests via module-level RNG
    seeding of `bot.core.sizing`.
    """
    flag = get_mm_live_state(conn, series)
    if flag.state == LiveState.SHADOW:
        return 0.0

    if flag.state == LiveState.LIVE_CANARY:
        # Fixed 1-contract target. Consumer rounds `MM_ORDER_SIZE * mult`
        # with a min-of-1 floor, so any multiplier that evaluates to ≤1
        # contract lands on exactly 1.
        from bot.config import MM_ORDER_SIZE
        return 1.0 / max(1, MM_ORDER_SIZE)

    series_u = series.upper()
    if not force_resample:
        try:
            cached = kv_get(conn, _kv_mult_key(series_u))
        except Exception:
            cached = None
        if isinstance(cached, dict) and "multiplier" in cached:
            try:
                return float(cached["multiplier"])
            except (TypeError, ValueError):
                pass

    # Cache miss or forced → re-sample
    decision = _sample_mm_multiplier(conn, series_u)
    _write_mm_multiplier_cache(conn, series_u, decision)
    return decision.multiplier


def _sample_mm_multiplier(conn: sqlite3.Connection, series: str):
    """Draw a fresh Thompson sample from the posterior for `series`."""
    from bot.config import (
        MM_SIZING_CAP_MULTIPLIER,
        MM_SIZING_MIN_N,
        MM_SIZING_TARGET_EDGE_CENTS,
    )
    from bot.core.sizing import thompson_mm_size_multiplier

    rows = _fetch_shadow_rows(conn, series)
    # Use only filled rows — zero-fill rows convey no signal about our
    # posted-quote P&L distribution; including them would bias the mean
    # toward 0 and deflate the multiplier artificially.
    pnls = [int(r["shadow_pnl_cents"] or 0) for r in rows
            if (r["shadow_bid_filled"] or 0) + (r["shadow_ask_filled"] or 0) > 0]
    return thompson_mm_size_multiplier(
        pnls,
        target_edge_cents=MM_SIZING_TARGET_EDGE_CENTS,
        cap_multiplier=MM_SIZING_CAP_MULTIPLIER,
        min_n=MM_SIZING_MIN_N,
    )


def _write_mm_multiplier_cache(
    conn: sqlite3.Connection, series: str, decision,
) -> None:
    """Cache a Thompson decision for fast subsequent reads."""
    from bot.config import MM_SIZING_CACHE_TTL_S
    payload = {
        "multiplier": float(decision.multiplier),
        "n": int(decision.n),
        "mean_cents": float(decision.mean_cents),
        "std_cents": float(decision.std_cents),
        "mu_sample_cents": float(decision.mu_sample_cents),
        "reason": str(decision.reason),
        "ts_unix": time.time(),
    }
    try:
        kv_set(conn, _kv_mult_key(series), payload, MM_SIZING_CACHE_TTL_S)
    except Exception as exc:  # pragma: no cover
        logger.warning("[mm-promotion] multiplier cache write failed: %s", exc)


# ── Shadow fill matcher ─────────────────────────────────────────────────
# A shadow-posted BID at price P is considered filled if a *later* shadow
# row on the same ticker (within `lifetime_s` seconds) shows the observed
# market YES-ask drop to ≤ P. Mirror for the NO side using market YES-bid.
# We only need rows we already write ourselves — no extra API calls.

DEFAULT_QUOTE_LIFETIME_S = 300.0  # 5 minutes — conservative upper bound


def match_shadow_fills(
    conn: sqlite3.Connection,
    *,
    lifetime_s: float = DEFAULT_QUOTE_LIFETIME_S,
    max_rows: int = 5000,
) -> dict[str, int]:
    """Populate shadow_bid_filled / shadow_ask_filled for unmatched rows.

    Idempotent — only inspects rows with `shadow_bid_filled IS NULL`. Safe
    to run on every cycle; cost is O(unmatched_rows × rows_in_window).

    Returns a summary dict used by the daemon health log.
    """
    summary = {"checked": 0, "bid_fills": 0, "ask_fills": 0, "no_fill": 0}

    # Pull candidate rows grouped by ticker so we can batch subsequent-row
    # lookups per ticker.
    rows = conn.execute(
        "SELECT id, ticker, ts_unix, proposed_bid_cents, proposed_ask_cents, "
        "       gate_should_quote "
        "FROM weather_mm_shadow "
        "WHERE shadow_bid_filled IS NULL "
        "  AND ts_unix < ? "                    # only close quote windows
        "ORDER BY ts_unix DESC LIMIT ?",
        (int(time.time() - lifetime_s), max_rows),
    ).fetchall()

    per_ticker_windows: dict[str, list] = {}
    with db_write_ctx(conn):
        for rid, ticker, ts0, bid_c, ask_c, gate_ok in rows:
            summary["checked"] += 1
            # If the gate said "don't quote," treat as unfilled and stop here.
            if not gate_ok:
                conn.execute(
                    "UPDATE weather_mm_shadow SET shadow_bid_filled=0, "
                    "shadow_ask_filled=0 WHERE id=?", (rid,),
                )
                summary["no_fill"] += 1
                continue

            if ticker not in per_ticker_windows:
                per_ticker_windows[ticker] = conn.execute(
                    "SELECT ts_unix, market_yes_bid, market_yes_ask "
                    "FROM weather_mm_shadow "
                    "WHERE ticker=? "
                    "ORDER BY ts_unix ASC",
                    (ticker,),
                ).fetchall()

            window = per_ticker_windows[ticker]
            bid_fill_ts: Optional[float] = None
            ask_fill_ts: Optional[float] = None
            # Track whether we saw any usable book observation in the row's
            # lifetime window. Kalshi's `/markets` response omits yes_bid/
            # yes_ask when a side has no resting liquidity — `_safe_cents`
            # stores that as NULL. Treating 0 the same as NULL is defensive:
            # Kalshi's minimum quoted price is 1¢, so 0 can only mean
            # "no observation". Truthiness check covers both.
            saw_bid_obs = False
            saw_ask_obs = False
            for ts_i, m_bid, m_ask in window:
                if ts_i <= ts0 or ts_i - ts0 > lifetime_s:
                    continue
                if m_ask:
                    saw_ask_obs = True
                    if (bid_fill_ts is None and bid_c is not None
                            and m_ask <= bid_c):
                        bid_fill_ts = float(ts_i)
                if m_bid:
                    saw_bid_obs = True
                    if (ask_fill_ts is None and ask_c is not None
                            and m_bid >= ask_c):
                        ask_fill_ts = float(ts_i)
                if bid_fill_ts is not None and ask_fill_ts is not None:
                    break

            # If the lifetime window contained no valid book observations on
            # either side, leave the row unmatched (do not UPDATE). A future
            # run of the matcher with better data can still resolve it. This
            # prevents the 2026-04-17→2026-04-21 data-loss episode from
            # permanently locking 20k+ rows as "no_fill".
            if not saw_bid_obs and not saw_ask_obs:
                summary["no_fill"] += 1
                continue

            conn.execute(
                "UPDATE weather_mm_shadow "
                "SET shadow_bid_filled=?, shadow_bid_fill_ts_unix=?, "
                "    shadow_ask_filled=?, shadow_ask_fill_ts_unix=? "
                "WHERE id=?",
                (
                    1 if bid_fill_ts is not None else 0, bid_fill_ts,
                    1 if ask_fill_ts is not None else 0, ask_fill_ts,
                    rid,
                ),
            )
            if bid_fill_ts is not None:
                summary["bid_fills"] += 1
            if ask_fill_ts is not None:
                summary["ask_fills"] += 1
            if bid_fill_ts is None and ask_fill_ts is None:
                summary["no_fill"] += 1
    return summary


# ── Settlement-time P&L annotation ──────────────────────────────────────
# At settlement: for each shadow row on this ticker where a side was filled,
# compute realized P&L net of maker fees. BID fill = bought YES at bid;
# ASK fill = bought NO at (100-ask). Both filled = spread capture minus
# both fees. Settlement side determines which contract pays.
#
# YES settles:  long-YES pays $1, long-NO pays $0
# NO settles:   long-YES pays $0, long-NO pays $1


def _pnl_for_side_fill(
    side: str, fill_price_cents: int, contracts: int, won: bool,
) -> int:
    """P&L in cents for a filled MM quote leg, net of maker fee.

    `side` is 'yes' (bid fill) or 'no' (ask fill → bought NO @ 100-ask).
    `fill_price_cents` is what we paid (YES cents for yes-side, NO cents
    for no-side). `won` is True if that side's contract resolves YES.
    """
    settlement_pay = 100 if won else 0
    gross = (settlement_pay - fill_price_cents) * contracts
    fee = kalshi_maker_fee(contracts, fill_price_cents)
    return int(round(gross - fee))


def _compute_live_pnl_cents_for_fill(
    *, side: str, yes_price: int, no_price: int, contracts: int,
    fee_cents: int, won_yes: bool,
) -> int:
    """Per-fill realized P&L in cents, net of the already-paid fee.

    Mirrors `_pnl_for_side_fill` but takes the fee directly from
    fills_ledger (Kalshi's own maker/taker classification) instead of
    recomputing from side-price + MM_ORDER_SIZE. Keeps us correct if the
    fee formula changes under us.
    """
    if side == "yes":
        won_this = won_yes
        our_price = yes_price
    else:
        won_this = not won_yes
        our_price = no_price
    settlement_pay = 100 if won_this else 0
    gross = (settlement_pay - our_price) * contracts
    return int(round(gross - fee_cents))


def _attribute_live_fills_to_shadow_rows(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    lifetime_s: float,
    won_yes: bool,
) -> dict[int, int]:
    """Sum realized live fill P&L per live-mode shadow row for ``ticker``.

    For each Kalshi fill on this ticker in fills_ledger with
    ``source='mm_quote'`` and ``live_mode=1``, attribute it to the shadow
    row with the greatest ``ts_unix`` ≤ ``fill_ts_unix`` (the quote that
    was resting on the book when the fill happened). Drop fills that
    don't fall inside any shadow row's lifetime window.

    Returns a dict ``{shadow_row_id: live_pnl_cents}`` covering only rows
    that attracted at least one fill. Rows with no attributed fill are
    intentionally left absent from the dict so callers can distinguish
    "no fill" from "fill but P&L == 0".

    Attribution is 1:1: each fill lands in exactly one shadow row. Sum
    errors on individual rows are tolerated — `evaluate_mm_graduation`
    only needs the aggregate sum across paired rows to be correct, and
    a deterministic attribution rule guarantees that.
    """
    # Pull live-mode shadow rows for this ticker, sorted by ts_unix so we
    # can scan in order. We only need id + ts_unix for attribution.
    shadow_rows = conn.execute(
        "SELECT id, ts_unix FROM weather_mm_shadow "
        "WHERE ticker=? AND live_mode=1 ORDER BY ts_unix ASC",
        (ticker,),
    ).fetchall()
    if not shadow_rows:
        return {}

    fills = conn.execute(
        "SELECT side, yes_price_cents, no_price_cents, contracts, "
        "       fee_cents, fill_ts_unix "
        "FROM fills_ledger "
        "WHERE ticker=? AND source='mm_quote' AND live_mode=1 "
        "ORDER BY fill_ts_unix ASC",
        (ticker,),
    ).fetchall()
    if not fills:
        return {}

    shadow_ts_by_id = [(int(rid), float(ts)) for rid, ts in shadow_rows]
    pnl_by_rid: dict[int, int] = {}

    # Two-pointer scan — both arrays are ASC. For each fill, advance
    # `idx` while the NEXT shadow row still has ts_unix ≤ fill_ts_unix.
    idx = 0
    for side, yes_p, no_p, contracts, fee, fill_ts in fills:
        if side not in ("yes", "no"):
            # Defensive — source_tagger guarantees only mm_quote entries,
            # but malformed rows shouldn't crash annotation.
            continue
        fill_ts = float(fill_ts)
        while (idx + 1 < len(shadow_ts_by_id)
               and shadow_ts_by_id[idx + 1][1] <= fill_ts):
            idx += 1
        rid, shadow_ts = shadow_ts_by_id[idx]
        if shadow_ts > fill_ts:
            # Fill predates the earliest shadow row — nothing to attach.
            continue
        if fill_ts - shadow_ts > lifetime_s:
            # Fill is outside every shadow row's lifetime. Live order
            # would have been cancelled by the next requote before this.
            # Attribute nothing; fill is orphaned.
            continue
        pnl = _compute_live_pnl_cents_for_fill(
            side=str(side),
            yes_price=int(yes_p),
            no_price=int(no_p),
            contracts=int(contracts),
            fee_cents=int(fee or 0),
            won_yes=won_yes,
        )
        pnl_by_rid[rid] = pnl_by_rid.get(rid, 0) + pnl

    return pnl_by_rid


def annotate_shadow_pnl(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    won_yes: bool,
    ts_settle_unix: float,
    lifetime_s: float = DEFAULT_QUOTE_LIFETIME_S,
) -> int:
    """Stamp ticker_settled_yes + shadow_pnl_cents on all shadow rows for
    this ticker, plus live_pnl_cents for live-mode rows paired with
    fills_ledger entries. Idempotent — skips rows where ts_settle_unix
    is already set.

    Returns number of rows updated.

    `contracts` on shadow rows is implicitly MM_ORDER_SIZE at quote time
    (shadow rows aren't real positions; we assume default size). Live
    P&L uses the actual contract count from fills_ledger rather than
    MM_ORDER_SIZE — CANARY's 1-contract cap, any size overrides, or
    partial fills are reflected honestly.
    """
    from bot.config import MM_ORDER_SIZE
    live_pnl_by_rid = _attribute_live_fills_to_shadow_rows(
        conn, ticker, lifetime_s=lifetime_s, won_yes=won_yes,
    )

    n = 0
    rows = conn.execute(
        "SELECT id, proposed_bid_cents, proposed_ask_cents, "
        "       shadow_bid_filled, shadow_ask_filled, live_mode "
        "FROM weather_mm_shadow "
        "WHERE ticker=? AND ts_settle_unix IS NULL",
        (ticker,),
    ).fetchall()
    with db_write_ctx(conn):
        for rid, bid_c, ask_c, bid_f, ask_f, live_mode in rows:
            pnl = 0
            if bid_f == 1 and bid_c is not None:
                # Bought YES @ bid_c. YES wins → gross = 100-bid_c; NO wins → -bid_c
                pnl += _pnl_for_side_fill("yes", int(bid_c), MM_ORDER_SIZE, won_yes)
            if ask_f == 1 and ask_c is not None:
                no_price = 100 - int(ask_c)
                # Bought NO @ no_price. NO wins (won_yes=False) → 100-no_price
                pnl += _pnl_for_side_fill("no", no_price, MM_ORDER_SIZE, not won_yes)
            # live_pnl_cents is only meaningful for rows that were posted
            # live. Shadow-only rows leave the column NULL so the
            # graduation gate's `live_pnl_cents IS NOT NULL` filter
            # continues to identify true paired rows.
            # Live-mode rows always get a non-NULL live_pnl_cents, even
            # when no fills were attributed — 0 is a valid datum meaning
            # "shadow predicted P&L we didn't realize." This is exactly
            # the drift case evaluate_mm_graduation is designed to catch.
            if live_mode == 1:
                live_pnl = live_pnl_by_rid.get(int(rid), 0)
            else:
                live_pnl = None
            if live_pnl is None:
                conn.execute(
                    "UPDATE weather_mm_shadow "
                    "SET ticker_settled_yes=?, ts_settle_unix=?, "
                    "    shadow_pnl_cents=? "
                    "WHERE id=?",
                    (1 if won_yes else 0, float(ts_settle_unix),
                     int(pnl), rid),
                )
            else:
                conn.execute(
                    "UPDATE weather_mm_shadow "
                    "SET ticker_settled_yes=?, ts_settle_unix=?, "
                    "    shadow_pnl_cents=?, live_pnl_cents=? "
                    "WHERE id=?",
                    (1 if won_yes else 0, float(ts_settle_unix),
                     int(pnl), int(live_pnl), rid),
                )
            n += 1
    return n


# ── Per-series stats ────────────────────────────────────────────────────
@dataclass(frozen=True)
class MMFamilyStats:
    """Aggregates over settled shadow rows for one series."""
    n: int                              # rows with >=1 side filled
    n_total: int                        # all settled rows (filled or not)
    fill_rate: float                    # (bids+asks filled) / (2 * n_total)
    pnl_cents: int                      # sum shadow_pnl_cents
    pnl_per_fill_cents: float           # pnl / max(1, n)
    max_single_loss_cents: int          # most-negative row pnl
    live_pnl_cents: int                 # sum live_pnl_cents (paired T.6)
    live_n: int                         # rows with live_pnl_cents NOT NULL


def _compute_mm_stats(rows: list[sqlite3.Row]) -> MMFamilyStats:
    if not rows:
        return MMFamilyStats(0, 0, 0.0, 0, 0.0, 0, 0, 0)
    n_total = len(rows)
    n_filled = 0
    fills = 0
    pnl = 0
    max_loss = 0
    live_pnl = 0
    live_n = 0
    for r in rows:
        bf = r["shadow_bid_filled"] or 0
        af = r["shadow_ask_filled"] or 0
        fills += int(bf) + int(af)
        if bf or af:
            n_filled += 1
        row_pnl = int(r["shadow_pnl_cents"] or 0)
        pnl += row_pnl
        if row_pnl < max_loss:
            max_loss = row_pnl
        lpnl = r["live_pnl_cents"]
        if lpnl is not None:
            live_pnl += int(lpnl)
            live_n += 1
    return MMFamilyStats(
        n=n_filled,
        n_total=n_total,
        fill_rate=fills / max(1, 2 * n_total),
        pnl_cents=pnl,
        pnl_per_fill_cents=pnl / max(1, n_filled),
        max_single_loss_cents=max_loss,
        live_pnl_cents=live_pnl,
        live_n=live_n,
    )


def _fetch_shadow_rows(
    conn: sqlite3.Connection,
    series: str,
    *,
    since_unix: Optional[float] = None,
    only_live_mode: Optional[bool] = None,
    only_shadow_mode: Optional[bool] = None,
) -> list[sqlite3.Row]:
    """Pull settled weather_mm_shadow rows for one series."""
    # Local cursor avoids mutating the shared daemon connection's row_factory
    # (would race with other threads; see T0.2 / regression 15 in CLAUDE.md).
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    sql = (
        "SELECT shadow_bid_filled, shadow_ask_filled, shadow_pnl_cents, "
        "       live_pnl_cents, ts_unix, live_mode "
        "FROM weather_mm_shadow "
        "WHERE series=? AND ts_settle_unix IS NOT NULL "
    )
    params: list[Any] = [series.upper()]
    if since_unix is not None:
        sql += " AND ts_unix >= ? "
        params.append(since_unix)
    if only_live_mode:
        sql += " AND live_mode=1 "
    if only_shadow_mode:
        sql += " AND live_mode=0 "
    sql += "ORDER BY ts_unix ASC"
    return list(cursor.execute(sql, params).fetchall())


# ── Tunable thresholds ──────────────────────────────────────────────────
@dataclass(frozen=True)
class MMKillSwitchConfig:
    pnl_window_n: int = 40                 # last-N settled live rows
    pnl_floor_dollars: float = 20.0        # or equity_pct, whichever larger
    pnl_floor_equity_pct: float = 0.02
    fill_rate_window_n: int = 40
    fill_rate_floor: float = 0.10          # trip if no-one-crosses-us regime
    single_loss_equity_pct: float = 0.03   # hard stop — 3% equity on one row
    # T.6 calibration monitor: if live_pnl << shadow_pnl, the shadow fill
    # model is optimistic; refuse to use it for further promotion decisions.
    shadow_vs_live_ratio_floor: float = 0.4  # live should be ≥ 40% of shadow
    shadow_vs_live_min_n: int = 20


DEFAULT_MM_KILL_SWITCH = MMKillSwitchConfig()


MIN_LIVE_SETTLED_FOR_DEMOTION = 20


# ── SHADOW → CANARY promotion + CANARY → FULL graduation ────────────────
# Two-step gate (plan B+D, 2026-04-21). The old single-criterion
# "n_fills >= 5 → LIVE_FULL" rule was load-bearing on the assumption that
# shadow data was real. The 2026-04-17 _safe_cents bug generated 700+
# spurious fills with near-zero P&L, which would have auto-promoted two
# families to LIVE_FULL had the master kill-switch not been engaged.
# B: block promotion on non-positive shadow P&L per fill.
# D: CANARY state at fixed 1-contract size; graduate to FULL only after
# K paired (live, shadow) rows show the shadow model predicts realized
# P&L within a safe ratio.
def evaluate_mm_promotion(
    conn: sqlite3.Connection,
    series: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Check whether a SHADOW series crosses the gate to go LIVE_CANARY.

    Gates:
      1. `n_fills >= MM_SIZING_MIN_N` — posterior has a non-degenerate sample.
      2. `pnl_per_fill_cents >= MM_CANARY_MIN_PNL_PER_FILL_CENTS` — shadow
         model shows positive realized P&L on average. Blocks the class of
         bugs where spurious fills produce zero-or-negative P&L.

    Passing promotes SHADOW → LIVE_CANARY (NOT LIVE_FULL). Graduation to
    FULL is a separate check (`evaluate_mm_graduation`) that runs on
    paired live-vs-shadow rows accumulated during canary.
    """
    from bot.config import MM_CANARY_MIN_PNL_PER_FILL_CENTS, MM_SIZING_MIN_N
    rows = _fetch_shadow_rows(conn, series)
    stats = _compute_mm_stats(rows)
    metrics = {
        "n_settled": stats.n_total,
        "n_fills": stats.n,
        "pnl_cents": stats.pnl_cents,
        "pnl_per_fill_cents": stats.pnl_per_fill_cents,
    }
    if stats.n < MM_SIZING_MIN_N:
        return (False,
                f"insufficient_fills={stats.n}<{MM_SIZING_MIN_N}",
                metrics)
    if stats.pnl_per_fill_cents < MM_CANARY_MIN_PNL_PER_FILL_CENTS:
        return (False,
                f"unprofitable_shadow:pnl_per_fill={stats.pnl_per_fill_cents:.2f}"
                f"<{MM_CANARY_MIN_PNL_PER_FILL_CENTS}",
                metrics)
    return (True,
            f"canary_gate_passed:n={stats.n}>={MM_SIZING_MIN_N},"
            f"pnl_per_fill={stats.pnl_per_fill_cents:.2f}"
            f">={MM_CANARY_MIN_PNL_PER_FILL_CENTS}",
            metrics)


def evaluate_mm_graduation(
    conn: sqlite3.Connection,
    series: str,
    *,
    since_ts_unix: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Check whether a LIVE_CANARY series graduates to LIVE_FULL.

    Reads paired rows accumulated since the series entered CANARY:
    a "paired row" is one where the quoter wrote a shadow entry and the
    same event also produced a live entry (captured by the WeatherQuoter's
    T.6 paired-logging — the live path writes a row with live_mode=1 and
    live_pnl_cents populated at settlement).

    Gates:
      1. Paired count >= MM_GRADUATION_MIN_PAIRED_N.
      2. sum(shadow_pnl) > 0 — the canary-period shadow model was positive
         in aggregate. Graduating on a losing shadow is perverse.
      3. sum(live_pnl) / sum(shadow_pnl) >= MM_GRADUATION_MIN_PNL_RATIO
         — live captures at least half the predicted P&L. Guards against
         queue-position / adverse-selection eroding the shadow edge.
    """
    from bot.config import (
        MM_GRADUATION_MIN_PAIRED_N,
        MM_GRADUATION_MIN_PNL_RATIO,
    )

    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(
        "SELECT shadow_pnl_cents, live_pnl_cents "
        "FROM weather_mm_shadow "
        "WHERE series=? AND ts_unix >= ? "
        "  AND shadow_pnl_cents IS NOT NULL "
        "  AND live_pnl_cents IS NOT NULL",
        (series.upper(), since_ts_unix),
    ).fetchall()

    n_paired = len(rows)
    shadow_sum = sum(int(r["shadow_pnl_cents"]) for r in rows)
    live_sum = sum(int(r["live_pnl_cents"]) for r in rows)
    ratio = (live_sum / shadow_sum) if shadow_sum > 0 else 0.0
    metrics = {
        "n_paired": n_paired,
        "shadow_pnl_cents": shadow_sum,
        "live_pnl_cents": live_sum,
        "live_over_shadow_ratio": ratio,
        "since_ts_unix": since_ts_unix,
    }

    if n_paired < MM_GRADUATION_MIN_PAIRED_N:
        return (False,
                f"insufficient_paired={n_paired}<{MM_GRADUATION_MIN_PAIRED_N}",
                metrics)
    if shadow_sum <= 0:
        return (False,
                f"shadow_nonpositive:sum={shadow_sum}c",
                metrics)
    if ratio < MM_GRADUATION_MIN_PNL_RATIO:
        return (False,
                f"ratio_below_floor:live/shadow={ratio:.2f}"
                f"<{MM_GRADUATION_MIN_PNL_RATIO}",
                metrics)
    return (True,
            f"graduated:n_paired={n_paired},"
            f"live/shadow={ratio:.2f}>={MM_GRADUATION_MIN_PNL_RATIO}",
            metrics)


def evaluate_mm_kill_switch(
    conn: sqlite3.Connection,
    series: str,
    equity_dollars: float,
    *,
    cfg: MMKillSwitchConfig = DEFAULT_MM_KILL_SWITCH,
) -> tuple[bool, str, dict[str, Any]]:
    """Trifecta kill-switch on LIVE rows plus a T.6 calibration guard.

    Triggers (any one fires):
      1. Single-row realized loss > single_loss_equity_pct × equity
      2. Rolling P&L on last pnl_window_n < −max($floor, equity_pct × equity)
      3. Fill rate on last fill_rate_window_n < fill_rate_floor
         (no-one-crosses-us regime — we're posting but nobody trades)
      4. shadow_vs_live calibration: live_pnl / shadow_pnl < ratio_floor
         over ≥ shadow_vs_live_min_n paired rows (shadow-model drift)
    """
    rows = _fetch_shadow_rows(conn, series, only_live_mode=True)
    stats = _compute_mm_stats(rows)
    metrics: dict[str, Any] = {
        "equity_dollars": equity_dollars,
        "n_live_settled": stats.n_total,
        "n_live_fills": stats.n,
    }

    # Trigger 1 — single-trade hard stop (fires at N=1)
    hard_stop_cents = -abs(cfg.single_loss_equity_pct * equity_dollars * 100.0)
    for r in rows:
        row_pnl = int(r["shadow_pnl_cents"] or 0)
        if row_pnl <= hard_stop_cents:
            metrics["tripped_single_loss_cents"] = row_pnl
            metrics["hard_stop_cents"] = hard_stop_cents
            return (True,
                    f"single_trade_loss={row_pnl}c<={hard_stop_cents:.0f}c",
                    metrics)

    if stats.n_total < MIN_LIVE_SETTLED_FOR_DEMOTION:
        return (False,
                f"insufficient_live_n={stats.n_total}"
                f"<{MIN_LIVE_SETTLED_FOR_DEMOTION}",
                metrics)

    # Trigger 2 — rolling P&L floor
    window = rows[-cfg.pnl_window_n:]
    w_stats = _compute_mm_stats(window)
    floor_cents = -max(cfg.pnl_floor_dollars * 100.0,
                       cfg.pnl_floor_equity_pct * equity_dollars * 100.0)
    metrics["live_pnl_window_cents"] = w_stats.pnl_cents
    metrics["live_pnl_floor_cents"] = floor_cents
    if w_stats.pnl_cents <= floor_cents:
        return (True,
                f"live_pnl={w_stats.pnl_cents}c<=floor={floor_cents:.0f}c",
                metrics)

    # Trigger 3 — fill-rate regime shift
    fr_window = rows[-cfg.fill_rate_window_n:]
    fr_stats = _compute_mm_stats(fr_window)
    metrics["live_fill_rate"] = fr_stats.fill_rate
    if (fr_stats.n_total >= cfg.fill_rate_window_n
            and fr_stats.fill_rate < cfg.fill_rate_floor):
        return (True,
                f"fill_rate={fr_stats.fill_rate:.3f}<{cfg.fill_rate_floor:.3f}",
                metrics)

    # Trigger 4 — T.6 shadow-vs-live calibration drift
    # Only fires when we have paired data (live_mode rows with both live and
    # shadow P&L recorded). Uses all live-mode rows here since the shadow
    # side was also computed for those.
    paired_rows = [r for r in rows
                   if r["live_pnl_cents"] is not None
                   and r["shadow_pnl_cents"] is not None]
    if len(paired_rows) >= cfg.shadow_vs_live_min_n:
        live_sum = sum(int(r["live_pnl_cents"]) for r in paired_rows)
        shadow_sum = sum(int(r["shadow_pnl_cents"]) for r in paired_rows)
        if shadow_sum > 0:
            ratio = live_sum / shadow_sum
            metrics["shadow_vs_live_ratio"] = ratio
            metrics["shadow_vs_live_n"] = len(paired_rows)
            if ratio < cfg.shadow_vs_live_ratio_floor:
                return (True,
                        f"shadow_vs_live={ratio:.2f}"
                        f"<{cfg.shadow_vs_live_ratio_floor:.2f}",
                        metrics)

    return (False, "kill_switch_clear", metrics)


# ── Event log + orchestration ───────────────────────────────────────────
def _log_mm_event(
    conn: sqlite3.Connection,
    series: str,
    *,
    old_state: str,
    new_state: str,
    reason: str,
    trigger: str,
    metrics: dict[str, Any],
    manual: bool,
) -> None:
    """Append to shared `promotion_events` table. Trigger strings start with
    'mm_' so directional and MM events are distinguishable in one query."""
    now = time.time()
    iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    try:
        with db_write_ctx(conn):
            conn.execute(
                "INSERT INTO promotion_events "
                "(ts_unix,ts_iso,family,old_state,new_state,reason,"
                " trigger,metrics_json,manual) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (now, iso, series.upper(), old_state, new_state, reason,
                 trigger, json.dumps(metrics), int(bool(manual))),
            )
    except Exception as exc:  # pragma: no cover
        logger.warning("[mm-promotion] event log failed: %s", exc)


def _mm_transition(
    conn: sqlite3.Connection,
    series: str,
    *,
    from_state: str,
    to_state: str,
    reason: str,
    trigger: str,
    metrics: dict[str, Any],
) -> None:
    set_mm_live_state(conn, series, to_state, manual=False)
    _log_mm_event(
        conn, series,
        old_state=from_state, new_state=to_state,
        reason=reason, trigger=trigger, metrics=metrics, manual=False,
    )
    logger.info(
        "[mm-promotion] %s: %s → %s (%s / %s)",
        series, from_state, to_state, trigger, reason,
    )


def _mm_candidate_series(conn: sqlite3.Connection) -> list[str]:
    """All distinct series present in weather_mm_shadow, minus hard blocks."""
    rows = conn.execute(
        "SELECT DISTINCT series FROM weather_mm_shadow "
        "WHERE series IS NOT NULL AND series != ''"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in MM_BLOCKED_SERIES]


def run_mm_promotion_sweep(
    conn: sqlite3.Connection,
    *,
    equity_dollars: float,
    series_list: Optional[list[str]] = None,
    kill_switch: MMKillSwitchConfig = DEFAULT_MM_KILL_SWITCH,
) -> dict[str, Any]:
    """Per-sweep: kill-switch → graduation → promotion → multiplier refresh.

    Flow per series (2026-04-21 B+D rewrite):
      1. If LIVE (CANARY or FULL), run kill-switch. If tripped, demote to SHADOW.
      2. If LIVE_CANARY, check graduation. If passed, promote to LIVE_FULL.
      3. If SHADOW, check promotion gate (N-floor + P&L). If passed, promote
         to LIVE_CANARY.
      4. For LIVE_FULL, refresh Thompson multiplier into cache. CANARY
         multiplier is a fixed 1-contract target (see
         `get_mm_order_size_multiplier`) and is not resampled.
    """
    series = (series_list if series_list is not None
              else _mm_candidate_series(conn))
    summary: dict[str, Any] = {
        "checked": len(series),
        "promoted": [],
        "graduated": [],
        "demoted": [],
        "resampled": [],
        "unchanged": [],
    }
    for ser in series:
        flag = get_mm_live_state(conn, ser)

        # 1. Kill switch (any LIVE state) — demote straight to SHADOW
        if flag.state != LiveState.SHADOW:
            tripped, reason, metrics = evaluate_mm_kill_switch(
                conn, ser, equity_dollars, cfg=kill_switch,
            )
            if tripped:
                _mm_transition(
                    conn, ser,
                    from_state=flag.state, to_state=LiveState.SHADOW,
                    reason=reason, trigger="mm_kill_switch", metrics=metrics,
                )
                # Clear any cached multiplier so the next getter returns 0
                # immediately rather than a stale LIVE value.
                try:
                    kv_set(conn, _kv_mult_key(ser),
                           {"multiplier": 0.0, "reason": "kill_switch"}, 60)
                except Exception:
                    pass
                summary["demoted"].append(
                    {"series": ser, "from": flag.state,
                     "to": LiveState.SHADOW, "reason": reason}
                )
                continue

        # 2. CANARY → FULL graduation
        if flag.state == LiveState.LIVE_CANARY:
            graduate, reason, metrics = evaluate_mm_graduation(
                conn, ser, since_ts_unix=flag.since_ts_unix,
            )
            if graduate:
                _mm_transition(
                    conn, ser,
                    from_state=LiveState.LIVE_CANARY,
                    to_state=LiveState.LIVE_FULL,
                    reason=reason, trigger="mm_canary_graduation",
                    metrics=metrics,
                )
                flag = get_mm_live_state(conn, ser)  # refresh for step 4
                summary["graduated"].append(
                    {"series": ser, "reason": reason, **metrics}
                )
                # Fall through to step 4 (Thompson resample on FULL).
            else:
                summary["unchanged"].append(
                    {"series": ser, "state": flag.state, "reason": reason}
                )
                continue

        # 3. SHADOW → CANARY promotion (N-floor + positive P&L)
        just_promoted = False
        if flag.state == LiveState.SHADOW:
            promote, reason, metrics = evaluate_mm_promotion(conn, ser)
            if promote:
                _mm_transition(
                    conn, ser,
                    from_state=LiveState.SHADOW,
                    to_state=LiveState.LIVE_CANARY,
                    reason=reason, trigger="mm_canary_promotion",
                    metrics=metrics,
                )
                flag = get_mm_live_state(conn, ser)  # refresh for step 4
                just_promoted = True
                summary["promoted"].append(
                    {"series": ser, "reason": reason, "to": LiveState.LIVE_CANARY}
                )
            else:
                summary["unchanged"].append(
                    {"series": ser, "state": flag.state, "reason": reason}
                )
                continue

        # 4. Refresh Thompson multiplier (FULL only — CANARY is fixed size)
        if flag.state == LiveState.LIVE_FULL:
            decision = _sample_mm_multiplier(conn, ser)
            _write_mm_multiplier_cache(conn, ser, decision)
            entry = {
                "series": ser,
                "state": flag.state,
                "multiplier": decision.multiplier,
                "n": decision.n,
                "mean_cents": decision.mean_cents,
                "reason": decision.reason,
            }
            if just_promoted:
                summary["promoted"][-1].update({
                    "multiplier": decision.multiplier,
                    "n": decision.n,
                })
            else:
                summary["resampled"].append(entry)

    return summary
