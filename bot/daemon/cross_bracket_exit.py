"""Cross-bracket position exit logic.

The cross-bracket strategy posts buy-and-hold tail bets. Without exit
logic, we forgo realized gains when the market moves substantially in
our favor before settlement. This module monitors our open
cross-bracket positions and exits when realized gain crosses a
threshold of the max-held upside.

Exit decision rule (each position evaluated every cycle):
  - Identify our cross-bracket positions via:
      1. Kalshi /portfolio/positions returns current net positions
      2. Filter to positions whose ticker has a fills_ledger BUY row
         with client_order_id LIKE 'mm_xb_%' (cross-bracket prefix)
  - For each position, fetch current orderbook
  - Compute realized gain at current best bid:
      gain = best_bid - avg_entry - exit_fee_per_contract
  - Trigger exit if EITHER:
      * realized_gain / max_held_gain >= EXIT_PCT_THRESHOLD (50%)
      * realized_gain >= EXIT_ABS_CENTS (25¢)
  - Don't exit at a loss; don't exit within EXIT_MIN_TTE_HOURS of settle
  - Idempotent: check for active mm_xbexit_<ticker>_* orders before posting

Order placement uses the synthetic-sell pattern (CLAUDE.md §"Synthetic
Sell"): to close a NO position, BUY YES at (100 - exit_price). This
avoids the taker-fee penalty on the original side. Currently posts
as taker (crosses the spread) for guaranteed fill; future v2 could
use maker pricing for fee savings if responsiveness is acceptable.

Gated globally by ``CROSS_BRACKET_LIVE`` (same kill switch as posts).
When the global gate is off, this is a no-op.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from bot.api import api_get, api_post
from bot.config import CROSS_BRACKET_LIVE
from bot.core.money import kalshi_taker_fee

logger = logging.getLogger(__name__)


# Tunables (env-overridable for hot adjustment)
EXIT_PCT_THRESHOLD: float = float(
    os.environ.get("CROSS_BRACKET_EXIT_PCT", "0.5")
)
EXIT_ABS_CENTS: int = int(
    os.environ.get("CROSS_BRACKET_EXIT_ABS_CENTS", "25")
)
EXIT_MIN_TTE_HOURS: float = float(
    os.environ.get("CROSS_BRACKET_EXIT_MIN_TTE_HOURS", "0.5")
)


# ── Position identification ────────────────────────────────────────────────────

def _fetch_cb_order_ids() -> set[str]:
    """Fetch our cross-bracket Kalshi order_ids by filtering /portfolio/orders
    to the ``mm_xb_`` client_order_id prefix. Returns set of Kalshi order_ids.

    We need this because Kalshi's /portfolio/fills response doesn't include
    client_order_id (it's a property of the ORDER, not the fill). So we
    can't filter fills_ledger directly by our prefix — fills_ledger.
    client_order_id is null for fills synced from /portfolio/fills. The
    canonical attribution comes from joining fills.order_id to orders.
    """
    out: set[str] = set()
    cursor = None
    # Bounded loop in case of pagination misbehavior
    for _ in range(20):
        path = "/portfolio/orders?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            resp = api_get(path)
        except Exception as exc:
            logger.warning("[cb_exit] /portfolio/orders fetch failed: %s", exc)
            return out
        for order in resp.get("orders", []):
            coid = order.get("client_order_id") or ""
            if coid.startswith("mm_xb_"):
                oid = order.get("order_id")
                if oid:
                    out.add(oid)
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    return out


def get_open_cb_positions(conn) -> list[dict]:
    """Return open cross-bracket positions with avg entry + total fees.

    Cross-bracket attribution flow:
      1. Query Kalshi /portfolio/orders to find orders with
         client_order_id LIKE 'mm_xb_%' (our cross-bracket POSTs)
      2. Use their Kalshi-assigned order_ids to filter fills_ledger
         (which has order_id but lacks client_order_id since the
         fills endpoint doesn't include it)
      3. Cross-reference with /portfolio/positions for net qty
    """
    cb_order_ids = _fetch_cb_order_ids()
    if not cb_order_ids:
        return []

    # Fetch current Kalshi positions
    try:
        resp = api_get("/portfolio/positions?limit=200")
    except Exception as exc:
        logger.warning("[cb_exit] portfolio/positions fetch failed: %s", exc)
        return []
    kalshi_positions = resp.get("market_positions") or resp.get("positions") or []

    # Get the tickers we care about (where we have CB order activity)
    cb_tickers = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT ticker FROM fills_ledger "
            "WHERE order_id IN ({}) AND action='buy'".format(
                ",".join("?" * len(cb_order_ids))
            ),
            tuple(cb_order_ids),
        ).fetchall()
    }
    if not cb_tickers:
        return []

    out: list[dict] = []
    for pos in kalshi_positions:
        ticker = pos.get("ticker") or ""
        if ticker not in cb_tickers:
            continue
        pos_raw = pos.get("position_fp") or pos.get("position", 0)
        try:
            pos_val = round(float(pos_raw)) if pos_raw is not None else 0
        except (TypeError, ValueError):
            continue
        if pos_val == 0:
            continue

        side = "yes" if pos_val > 0 else "no"
        contracts = abs(pos_val)

        # Compute avg entry from fills_ledger filtered to our CB orders
        price_col = "no_price_cents" if side == "no" else "yes_price_cents"
        placeholders = ",".join("?" * len(cb_order_ids))
        params = (ticker, side, *cb_order_ids)
        agg = conn.execute(
            f"SELECT SUM(contracts * {price_col}), SUM(contracts), SUM(fee_cents) "
            f"FROM fills_ledger "
            f"WHERE ticker = ? AND action='buy' AND side = ? "
            f"  AND order_id IN ({placeholders})",
            params,
        ).fetchone()
        if not agg or not agg[1] or agg[1] == 0:
            continue
        total_cost, total_contracts, total_fees = agg
        avg_entry_cents = total_cost / max(1, total_contracts)
        fee_per_contract = (total_fees or 0) / max(1, total_contracts)

        out.append({
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "avg_entry_cents": avg_entry_cents,
            "fee_per_contract": fee_per_contract,
        })
    return out


# ── Orderbook fetch ────────────────────────────────────────────────────────────

def get_best_bid_for_side(ticker: str, side: str) -> Optional[int]:
    """Return current best bid for owning side (i.e., the price someone
    is willing to pay to buy our position from us).

    For a NO holder selling: best NO bid = best price someone bids for NO.
    For a YES holder selling: best YES bid = best price someone bids for YES.

    Kalshi orderbook returns BIDS keyed by 'yes' and 'no'. The highest
    price in each list is the best bid.
    """
    try:
        resp = api_get(f"/markets/{ticker}/orderbook")
    except Exception as exc:
        logger.warning("[cb_exit] orderbook fetch %s: %s", ticker, exc)
        return None

    book = resp.get("orderbook") or resp.get("orderbook_fp") or resp
    levels = book.get(side) or book.get(f"{side}_dollars") or []
    if not levels:
        return None

    best_bid: Optional[int] = None
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        raw_p = level[0]
        try:
            if isinstance(raw_p, str):
                p = round(float(raw_p) * 100)
            elif isinstance(raw_p, float) and raw_p < 1.0:
                p = round(raw_p * 100)
            else:
                p = int(raw_p)
        except (TypeError, ValueError):
            continue
        if p < 0 or p > 100:
            continue
        if best_bid is None or p > best_bid:
            best_bid = p
    return best_bid


# ── Decision rule ──────────────────────────────────────────────────────────────

def evaluate_exit(position: dict, current_best_bid: Optional[int]) -> Optional[dict]:
    """Decide whether to exit the position. Returns exit spec or None.

    The exit spec contains:
      - exit_price_cents (the bid we'd hit/cross to sell)
      - realized_pct (gain as % of max held upside)
      - realized_cents (absolute gain after exit fee)
      - exit_fee_per_contract
    """
    if current_best_bid is None or current_best_bid <= 0:
        return None  # no book → can't exit

    contracts = position["contracts"]
    avg_entry = position["avg_entry_cents"]
    fee_per_contract = position["fee_per_contract"]

    if current_best_bid <= avg_entry:
        return None  # selling at a loss; let it ride to settle

    # Exit fee: we'd cross the spread as taker for guaranteed fill.
    # Conservative — actual maker exit fee would be lower, so this
    # makes the threshold harder (good for safety).
    exit_fee_per_contract = kalshi_taker_fee(1, current_best_bid)

    realized_gain_per_contract = (
        current_best_bid - avg_entry - exit_fee_per_contract
    )
    if realized_gain_per_contract <= 0:
        return None  # after-fee gain is non-positive

    # Max held gain: if we hold and our side wins, we get 100¢ - entry - entry_fee
    max_held_gain = 100 - avg_entry - fee_per_contract
    if max_held_gain <= 0:
        return None  # paid more than max possible payoff (shouldn't happen)

    realized_pct = realized_gain_per_contract / max_held_gain
    realized_cents_total = realized_gain_per_contract * contracts

    trigger = (
        realized_pct >= EXIT_PCT_THRESHOLD
        or realized_gain_per_contract >= EXIT_ABS_CENTS
    )
    if not trigger:
        return None

    return {
        "exit_price_cents": current_best_bid,
        "exit_fee_per_contract": exit_fee_per_contract,
        "realized_pct": realized_pct,
        "realized_cents_per_contract": realized_gain_per_contract,
        "realized_cents_total": realized_cents_total,
        "max_held_gain": max_held_gain,
    }


# ── Idempotence: skip if exit already pending ─────────────────────────────────

def has_pending_exit(ticker: str) -> bool:
    """True if there's already an unfilled exit order on this ticker."""
    try:
        resp = api_get(
            f"/portfolio/orders?status=resting&ticker={ticker}&limit=50"
        )
    except Exception as exc:
        logger.warning("[cb_exit] orders fetch %s: %s", ticker, exc)
        # Fail closed: assume there is, don't double-post
        return True
    orders = resp.get("orders", [])
    for order in orders:
        coid = (order.get("client_order_id") or "")
        # Accept both prefixes: legacy ``mm_xbexit_`` (pre-2026-05-12)
        # and current ``mm_xb_exit_``. Any resting exit order tagged
        # with either should block a new exit on the same ticker.
        if coid.startswith("mm_xb_exit_") or coid.startswith("mm_xbexit_"):
            return True
    return False


# ── Order placement ───────────────────────────────────────────────────────────

def post_exit_order(
    conn, position: dict, exit_spec: dict,
) -> Optional[dict]:
    """Place a synthetic-sell limit order to exit the position.

    Synthetic sell: to close a NO position, BUY YES at (100 - exit_price_cents)
    on the YES side. Fills against existing YES asks at that price level.
    """
    side = position["side"]
    ticker = position["ticker"]
    contracts = position["contracts"]
    exit_price = exit_spec["exit_price_cents"]

    # Convert to opposite-side BUY price
    if side == "no":
        opposite_side = "yes"
        opposite_price = 100 - exit_price
    else:
        opposite_side = "no"
        opposite_price = 100 - exit_price

    if opposite_price <= 0 or opposite_price >= 100:
        logger.warning(
            "[cb_exit] %s %s: opposite_price=%d out of bounds; skip",
            ticker, side, opposite_price,
        )
        return None

    # client_order_id: ``mm_xb_exit_`` prefix matches fills_writer's
    # default_source_tagger which routes to ``cross_bracket_exit``.
    # The earlier ``mm_xbexit_`` form was ambiguous with ``mm_xb_*``
    # (cross_bracket entries) under the tagger's prefix tree.
    safe_ticker = ticker.replace(".", "_")
    coid = f"mm_xb_exit_{safe_ticker}_{int(time.time() * 1000)}"
    if len(coid) > 64:
        coid = coid[:64]

    body = {
        "ticker": ticker,
        "client_order_id": coid,
        "side": opposite_side,
        "action": "buy",
        "type": "limit",
        "count": contracts,
        f"{opposite_side}_price": opposite_price,
    }
    try:
        resp = api_post("/portfolio/orders", body)
    except Exception as exc:
        logger.warning("[cb_exit] post failed %s: %s", ticker, exc)
        return None
    order_id = (resp or {}).get("order", {}).get("order_id")
    if order_id:
        # Record (order_id, client_order_id) so fills_writer can
        # recover attribution when Kalshi's /portfolio/fills response
        # omits client_order_id (2026-05-10+ format drift). See
        # bot.daemon.fills_writer.record_posted_order.
        from bot.daemon.fills_writer import record_posted_order
        record_posted_order(
            conn,
            order_id=order_id,
            client_order_id=coid,
            ticker=ticker,
            side=opposite_side,
            action="buy",
            count=contracts,
            price_cents=opposite_price,
            source_hint="cross_bracket_exit",
            live_mode=True,
        )
    logger.info(
        "[cb_exit] EXIT %s %s %d contracts: synthetic-sell as %s @ %d¢ "
        "(implied %s sell @ %d¢, realized=+%.1f¢/ct = %.0f%% of max), "
        "order_id=%s",
        ticker, side, contracts, opposite_side, opposite_price,
        side, exit_price, exit_spec["realized_cents_per_contract"],
        100 * exit_spec["realized_pct"], order_id,
    )
    return resp


# ── TTE guard ──────────────────────────────────────────────────────────────────

def _get_settle_unix(ticker: str) -> Optional[int]:
    """Compute the settlement-unix for this ticker via the same DST-correct
    logic as cross_bracket_shadow. Imported lazily to avoid circular dep."""
    from bot.daemon.cross_bracket_shadow import _settlement_unix_from_key
    # Settlement key is the ticker stripped of bracket suffix:
    # 'KXHIGHNY-26MAY04-B72.5' → 'KXHIGHNY-26MAY04'
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    key = "-".join(parts[:2])
    return _settlement_unix_from_key(key)


# ── Main entrypoint ───────────────────────────────────────────────────────────

def run_exit_check(conn) -> dict:
    """Evaluate all open cross-bracket positions; place exit orders where
    decision rule triggers. Returns stats for telemetry."""
    stats = {
        "positions_checked": 0,
        "exits_posted": 0,
        "exits_skipped_no_book": 0,
        "exits_skipped_no_gain": 0,
        "exits_skipped_below_threshold": 0,
        "exits_skipped_close_to_settle": 0,
        "exits_skipped_pending": 0,
        "exits_failed": 0,
    }
    if not CROSS_BRACKET_LIVE:
        return stats

    positions = get_open_cb_positions(conn)
    stats["positions_checked"] = len(positions)
    if not positions:
        return stats

    now_unix = time.time()
    for pos in positions:
        ticker = pos["ticker"]

        # TTE guard
        settle_unix = _get_settle_unix(ticker)
        if settle_unix is not None:
            tte_h = (settle_unix - now_unix) / 3600.0
            if tte_h < EXIT_MIN_TTE_HOURS:
                stats["exits_skipped_close_to_settle"] += 1
                logger.info(
                    "[cb_exit] %s skip — TTE=%.2fh below %.2fh floor",
                    ticker, tte_h, EXIT_MIN_TTE_HOURS,
                )
                continue

        best_bid = get_best_bid_for_side(ticker, pos["side"])
        if best_bid is None:
            stats["exits_skipped_no_book"] += 1
            continue

        exit_spec = evaluate_exit(pos, best_bid)
        if exit_spec is None:
            if best_bid <= pos["avg_entry_cents"]:
                stats["exits_skipped_no_gain"] += 1
            else:
                stats["exits_skipped_below_threshold"] += 1
            continue

        # Idempotence: don't double-post
        if has_pending_exit(ticker):
            stats["exits_skipped_pending"] += 1
            logger.info(
                "[cb_exit] %s already has pending exit order; skip", ticker,
            )
            continue

        result = post_exit_order(conn, pos, exit_spec)
        if result is None:
            stats["exits_failed"] += 1
        else:
            stats["exits_posted"] += 1

    return stats
