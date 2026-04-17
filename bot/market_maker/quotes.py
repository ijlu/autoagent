"""Quote posting and spread logic for market making.

Handles two-sided limit order posting with:
  - Category-aware base spreads
  - Economic calendar event widening / pause
  - Adaptive spread learning from adverse selection
  - Fee-aware spread floor
  - Inventory skew and hard-cap enforcement
  - Series profitability gate for bracket markets
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from bot.api import api_get, api_post
from bot.core.money import kalshi_maker_fee
from bot.db import kv_get as _kv_get
from bot.market_maker.inventory import mm_get_inventory, mm_calculate_quotes
from bot.market_maker.series_profitability import mm_check_series_profitability
from bot.market_maker.adverse_selection import mm_compute_adverse_selection
from bot.config import (
    MM_DRY_RUN,
    MM_HALF_SPREAD,
    MM_MAX_INVENTORY,
    MM_ORDER_SIZE,
    MM_ORDER_TAG,
    MM_CAPITAL_PCT,
    MM_MAX_MARKETS,
)
from bot.api import _CACHE as _API_CACHE


# ── Economic Calendar: scheduled release awareness ──────────────────────────
# When a major data release is imminent, widen spreads or pause quoting to
# avoid adverse selection from informed traders who reprice instantly.
_ECON_CALENDAR_2026 = {
    # FOMC decisions (Wed 2pm ET) -- pause quoting +/-15 min, widen 2x for +/-2h
    "fomc": [
        "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
        "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
    ],
    # CPI releases (typically 8:30am ET, 2nd week of month)
    "cpi": [
        "2026-01-14", "2026-02-12", "2026-03-11", "2026-04-14",
        "2026-05-13", "2026-06-10", "2026-07-14", "2026-08-12",
        "2026-09-10", "2026-10-13", "2026-11-12", "2026-12-10",
    ],
    # Jobs report (typically 8:30am ET, 1st Friday)
    "jobs": [
        "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
        "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
        "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
    ],
}


# ── Category-specific spread overrides ──────────────────────────────────────
# Markets with high informed-trader participation need wider spreads.
_CATEGORY_SPREAD_OVERRIDES = {
    "economics": 7,   # KXFED, KXCPI, KXJOB -- high informed flow -> 7c half-spread
    "crypto":    6,    # KXBTC, KXETH -- fast-moving, high adverse selection
    "weather":   8,    # Weather -- wider spread (was blocked, now METAR-gated)
    "company":   5,    # Company KPIs -- moderate, keep at default
    "sports":    5,    # Sports -- moderate
}

# Categories that should NEVER be market-made (unconditionally).
# Weather is no longer unconditionally blocked -- it's gated on METAR freshness
# in mm_post_quotes() instead. We only quote weather when we have real-time
# station observations (< 10 min old), giving us data parity with counterparties.
MM_BLOCKED_CATEGORIES: set[str] = set()  # empty -- weather gated conditionally below


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _econ_release_proximity():
    """Check if a major economic release is happening soon.
    Returns (event_type, hours_until_release) or (None, None).
    Release times: FOMC=18:00 UTC (2pm ET), CPI/Jobs=12:30 UTC (8:30am ET)."""
    now = datetime.now(timezone.utc)

    for event_type, dates in _ECON_CALENDAR_2026.items():
        for date_str in dates:
            if abs((datetime.fromisoformat(date_str + "T00:00:00+00:00") - now).days) > 1:
                continue  # not today or tomorrow
            # Set release time
            release_hour = 18 if event_type == "fomc" else 12  # UTC
            release_min = 0 if event_type == "fomc" else 30
            release_dt = datetime.fromisoformat(
                f"{date_str}T{release_hour:02d}:{release_min:02d}:00+00:00")
            hours_until = (release_dt - now).total_seconds() / 3600
            if -0.5 <= hours_until <= 3.0:  # within 3h before to 30min after
                return event_type, hours_until
    return None, None


_ADVERSE_CACHE = None  # computed once per cycle, shared across tickers


def mm_reset_adverse_cache():
    """Reset per-cycle adverse selection cache. Call at start of mm_run()."""
    global _ADVERSE_CACHE
    _ADVERSE_CACHE = None


def mm_adaptive_spread(conn, ticker, base_half_spread):
    """Adjust spread width based on learned adverse selection rate.
    Markets with high adverse selection get wider spreads.
    Returns adjusted half-spread in cents.
    Returns -1 to signal "block quoting" for extremely toxic flow."""
    global _ADVERSE_CACHE
    if _ADVERSE_CACHE is None:
        _ADVERSE_CACHE = mm_compute_adverse_selection(conn)

    # Check exact ticker first, then series prefix (e.g. KXFED-27APR)
    rate = _ADVERSE_CACHE.get(ticker)
    if rate is None:
        # Check if ANY ticker in the same series has high adverse selection
        # This catches new brackets in a series with known toxic flow
        prefix = ticker[:12] if len(ticker) > 12 else ticker[:6]
        series_rates = [r for t, r in _ADVERSE_CACHE.items() if t.startswith(prefix)]
        rate = sum(series_rates) / len(series_rates) if series_rates else 0.5

    if rate > 0.85:
        # Extremely toxic -- but only block if we have enough data
        prefix = ticker[:12] if len(ticker) > 12 else ticker[:6]
        try:
            fill_count = conn.execute(
                "SELECT COUNT(*) FROM mm_orders WHERE ticker LIKE ? AND fill_qty > 0",
                (prefix + "%",)).fetchone()[0]
        except Exception:
            fill_count = 0
        if fill_count >= 30:  # Only block with 30+ fills of evidence
            print(f"    [adverse] {ticker}: rate={rate:.0%} ({fill_count} fills) -- BLOCKING quotes")
            return -1
        else:
            print(f"    [adverse] {ticker}: rate={rate:.0%} but only {fill_count} fills -- widening 3x")
            return min(20, base_half_spread * 3)
    elif rate > 0.7:
        # Very high -- triple the spread
        return min(20, base_half_spread * 3)
    elif rate > 0.6:
        # High -- double the spread
        return min(15, base_half_spread * 2)
    elif rate > 0.5:
        # Moderate -- widen by 50%
        return min(12, int(base_half_spread * 1.5))
    elif rate < 0.35:
        # Low adverse selection -- tighten slightly for more fills
        return max(2, base_half_spread - 1)
    return base_half_spread


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mm_get_effective_spread(conn, ticker, category):
    """Get effective half-spread considering: base -> category override ->
    adaptive (adverse selection learning) -> economic calendar multiplier."""
    # Start with category override or default
    base_hs = _CATEGORY_SPREAD_OVERRIDES.get(category, MM_HALF_SPREAD)
    if base_hs is None:
        return -1  # Blocked category — signal caller to skip

    # Apply adaptive spread (learned adverse selection)
    adaptive_hs = mm_adaptive_spread(conn, ticker, base_hs)
    if adaptive_hs < 0:
        return -1  # blocked by adverse selection learning

    # ── Defense 2: Fill-rate-based spread widening ──
    # High fill rate on maker-only strategy = adverse selection.
    # Read from kv_cache (computed in core.py _compute_adverse_selection_signals)
    family = ticker.split("-")[0] if "-" in ticker else ticker
    fill_rates = _kv_get(conn, "mm_fill_rates") or {}
    fr = fill_rates.get(family)
    if fr is not None:
        if fr >= 0.35:
            print(f"    [fill-rate] {ticker}: {fr:.0%} fill rate -- BLOCKING (>35%)")
            return -1  # extremely high — being used as exit liquidity
        elif fr >= 0.20:
            adaptive_hs = min(20, int(adaptive_hs * 2.0))
            print(f"    [fill-rate] {ticker}: {fr:.0%} fill rate -- spread 2x to {adaptive_hs}c")
        elif fr >= 0.10:
            adaptive_hs = min(15, int(adaptive_hs * 1.5))

    # ── Defense 3: One-sided fill detection ──
    # All fills on one side = informed traders selectively picking off stale quotes.
    onesided_consec = _kv_get(conn, "mm_onesided_consec") or {}
    onesided_imbalance = _kv_get(conn, "mm_onesided_imbalance") or {}
    consec = onesided_consec.get(family, 0)
    imb = onesided_imbalance.get(family, 0)
    if consec >= 4 and imb > 0.80:
        print(f"    [one-sided] {ticker}: {consec} consecutive one-sided cycles -- BLOCKING")
        return -1
    elif consec >= 2 and imb > 0.80:
        adaptive_hs = min(18, int(adaptive_hs * 1.75))
        print(f"    [one-sided] {ticker}: {consec} consecutive one-sided cycles -- "
              f"spread 1.75x to {adaptive_hs}c")

    # ── Defense 5: Postmortem risk feedback ──
    # Families with high adverse selection in settlement postmortems get wider spreads.
    postmortem_risk = _kv_get(conn, "mm_postmortem_risk") or {}
    pm_score = postmortem_risk.get(family)
    if pm_score is not None:
        if pm_score >= 0.70:
            print(f"    [postmortem] {ticker}: risk={pm_score:.0%} -- BLOCKING (>70%)")
            return -1
        elif pm_score >= 0.50:
            adaptive_hs = min(18, int(adaptive_hs * 1.75))
            print(f"    [postmortem] {ticker}: risk={pm_score:.0%} -- spread 1.75x to {adaptive_hs}c")
        elif pm_score >= 0.30:
            adaptive_hs = min(15, int(adaptive_hs * 1.3))

    # Widen for economic releases
    event_type, hours_until = _econ_release_proximity()
    if event_type is not None:
        if hours_until is not None and -0.25 <= hours_until <= 0.25:
            # Within +/-15 min of release -- return -1 to signal "pause quoting"
            print(f"    [econ] {event_type} release in {hours_until*60:.0f} min -- PAUSING quotes")
            return -1  # sentinel: skip this market
        elif hours_until is not None and hours_until <= 2.0:
            # Within 2 hours -- double the spread
            adaptive_hs = min(15, adaptive_hs * 2)
            print(f"    [econ] {event_type} in {hours_until:.1f}h -- spread doubled to {adaptive_hs}c")

    return adaptive_hs


def mm_post_quotes(conn, m, fair_value_cents, balance_cents, inventory):
    """Post two-sided limit orders for a market.
    Returns (orders_posted, capital_used_cents)."""
    ticker = m.get("ticker", "")
    now = datetime.now(timezone.utc).isoformat()
    orders_posted = 0
    capital_used = 0

    # Calculate skewed bid/ask with category-aware + event-aware spread
    # Import categorize_market from the monolith (not yet extracted to bot package)
    from trade import categorize_market
    cat = categorize_market(ticker, (m.get("title") or m.get("subtitle") or "").lower())

    # Hard block: categories where we're structurally behind on information
    if cat in MM_BLOCKED_CATEGORIES:
        print(f"    SKIP {ticker}: category '{cat}' blocked from MM (adverse selection risk)")
        return 0, 0

    # Weather MM gate: only quote when we have fresh METAR observations (< 10 min old).
    # Without real-time station data, counterparties with METARs have an information edge.
    if cat == "weather":
        metar_cache_key = "metar_obs"
        metar_fresh = False
        if metar_cache_key in _API_CACHE:
            _cached_data, _cached_ts = _API_CACHE[metar_cache_key]
            if _cached_data and (time.time() - _cached_ts) < 600:  # < 10 min old
                metar_fresh = True
        if not metar_fresh:
            print(f"    SKIP {ticker}: weather MM blocked (no fresh METAR data)")
            return 0, 0

        # Defense 4: Weather conditional quoting — block when forecast disagrees
        # with METAR observations by >3°F past solar noon (12 LST).
        # If our forecast was wrong, our ensemble fair value is polluted.
        _TICKER_STATION = {
            "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHLAX": "KLAX",
            "KXHIGHAUS": "KAUS", "KXHIGHMIA": "KMIA", "KXHIGHDEN": "KDEN",
        }
        _STATION_LST = {
            "KNYC": -5, "KMDW": -6, "KLAX": -8, "KAUS": -6, "KMIA": -5, "KDEN": -7,
        }
        _wx_station = None
        ticker_upper = ticker.upper()
        for _pfx, _stn in _TICKER_STATION.items():
            if _pfx in ticker_upper:
                _wx_station = _stn
                break
        if _wx_station:
            from datetime import timedelta
            _lst_offset = _STATION_LST.get(_wx_station, -5)
            _lst_tz = timezone(timedelta(hours=_lst_offset))
            _now_lst = datetime.now(_lst_tz)
            _lst_date = _now_lst.strftime("%Y-%m-%d")
            _hour_lst = _now_lst.hour

            if _hour_lst >= 12:  # past solar noon — most daily high already observed
                _fc_high = _kv_get(conn, f"metar_forecast_high_{_wx_station}_{_lst_date}")
                _obs_high_rec = _kv_get(conn, f"metar_daily_high_{_wx_station}_{_lst_date}")
                _obs_high = _obs_high_rec.get("high_f") if isinstance(_obs_high_rec, dict) else None

                if _fc_high is not None and _obs_high is not None:
                    _divergence = abs(float(_fc_high) - float(_obs_high))
                    if _divergence > 5.0:
                        print(f"    SKIP {ticker}: forecast/METAR divergence "
                              f"{_divergence:.1f}°F > 5°F past noon — BLOCKING")
                        return 0, 0
                    elif _divergence > 3.0:
                        print(f"    [wx-gate] {ticker}: forecast/METAR divergence "
                              f"{_divergence:.1f}°F > 3°F — will widen spread")

    # Skip if fair value couldn't be computed (prevents penny-bid trap)
    if fair_value_cents <= 2 or fair_value_cents >= 98:
        print(f"    SKIP {ticker}: extreme fair value {fair_value_cents}c (no reliable estimate)")
        return 0, 0

    adaptive_hs = mm_get_effective_spread(conn, ticker, cat)
    if adaptive_hs < 0:
        # Economic release imminent -- skip quoting this cycle
        print(f"    SKIP {ticker}: economic release imminent, pausing quotes")
        return 0, 0
    bid, ask = mm_calculate_quotes(fair_value_cents, inventory, adaptive_hs)

    # Fee-aware spread floor: ensure round-trip spread > expected maker fees
    # Kalshi maker fee ~ roundup(0.0175 * C * P * (1-P)) per side
    # For C=1 contract, worst case is P=0.50 -> fee ~ 0.44c/side ~ 0.88c round-trip
    # At P=0.30 or P=0.70 -> fee ~ 0.37c/side ~ 0.74c round-trip
    # With our order sizes (3-6 contracts), round-trip fee ~ 2-5c
    mid_price = (bid + ask) / 200.0  # as fraction [0,1]
    expected_fee_per_side = max(1, 0.0175 * MM_ORDER_SIZE * mid_price * (1 - mid_price) * 100)
    expected_round_trip_fee = expected_fee_per_side * 2
    full_spread = ask - bid
    if full_spread <= expected_round_trip_fee:
        # Spread doesn't cover fees -- widen to break even + 1c margin
        min_hs = int(expected_round_trip_fee / 2) + 1
        if min_hs > adaptive_hs:
            print(f"    [fee] {ticker}: spread {full_spread}c < fees ~{expected_round_trip_fee:.1f}c -- "
                  f"widening half-spread from {adaptive_hs}c to {min_hs}c")
            adaptive_hs = min_hs
            bid, ask = mm_calculate_quotes(fair_value_cents, inventory, adaptive_hs)

    # Adjust order size based on inventory: if we're heavy one side, reduce that side
    buy_size = MM_ORDER_SIZE
    sell_size = MM_ORDER_SIZE
    if inventory > MM_MAX_INVENTORY * 0.5:
        buy_size = max(2, MM_ORDER_SIZE // 2)   # reduce buying
        sell_size = min(MM_ORDER_SIZE * 2, MM_MAX_INVENTORY - inventory)  # increase selling
    elif inventory < -MM_MAX_INVENTORY * 0.5:
        sell_size = max(2, MM_ORDER_SIZE // 2)
        buy_size = min(MM_ORDER_SIZE * 2, MM_MAX_INVENTORY + inventory)

    buy_size = max(0, buy_size)
    sell_size = max(0, sell_size)

    # Hard-cap: never exceed MM_MAX_INVENTORY even if current fill + resting order fills
    # Include resting (unfilled) orders in exposure calculation to avoid over-commitment
    resting_buy_qty = 0
    resting_sell_qty = 0
    try:
        resting_rows = conn.execute(
            "SELECT side, SUM(contracts - fill_qty) FROM mm_orders "
            "WHERE ticker=? AND status IN ('posted', 'resting', 'cancel_failed') AND contracts > fill_qty "
            "GROUP BY side", (ticker,)
        ).fetchall()
        for side, qty in resting_rows:
            if side == "yes":
                resting_buy_qty = int(qty or 0)
            elif side == "no":
                resting_sell_qty = int(qty or 0)
    except Exception as e:
        # FAIL CLOSED: if we can't see resting exposure, block new orders for this ticker
        print(f"    [risk] \u26a0\ufe0f Cannot query resting orders for {ticker}: {e} -- blocking new orders")
        return 0, 0  # post nothing this cycle for safety

    effective_long_exposure = inventory + resting_buy_qty  # could end up this long if all buys fill
    effective_short_exposure = -inventory + resting_sell_qty  # could end up this short if all sells fill
    headroom_long = max(0, MM_MAX_INVENTORY - effective_long_exposure)
    headroom_short = max(0, MM_MAX_INVENTORY - effective_short_exposure)
    buy_size = min(buy_size, headroom_long)
    sell_size = min(sell_size, headroom_short)

    # Capital check
    buy_cost = buy_size * bid
    sell_cost = sell_size * (100 - ask)  # buying NO at (100-ask) cents
    total_cost = buy_cost + sell_cost
    if total_cost > balance_cents * MM_CAPITAL_PCT / max(1, MM_MAX_MARKETS):
        # Scale down proportionally -- let size go to 0 if budget is exhausted
        scale = (balance_cents * MM_CAPITAL_PCT / max(1, MM_MAX_MARKETS)) / max(1, total_cost)
        buy_size = int(buy_size * scale)   # no max(1,...) -- 0 is correct when budget is 0
        sell_size = int(sell_size * scale)

    # Reapply hard cap after capital scaling (audit fix: scaling could re-inflate)
    buy_size = min(buy_size, max(0, MM_MAX_INVENTORY - effective_long_exposure))
    sell_size = min(sell_size, max(0, MM_MAX_INVENTORY - effective_short_exposure))

    # Post BID (buy YES at bid price)
    if buy_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        # Series profitability gate: reject if adding this position makes ALL outcomes unprofitable
        sp_ok, sp_reason = mm_check_series_profitability(conn, ticker, "yes", buy_size, bid)
        if not sp_ok:
            print(f"    BID {ticker} BLOCKED: {sp_reason}")
            buy_size = 0
    if buy_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        order_body = {
            "ticker": ticker, "side": "yes", "type": "limit",
            "count": buy_size, "yes_price": bid,
            "action": "buy",
            "expiration_ts": int(time.time() + 110),
            "client_order_id": f"mm_bid_{ticker.replace('.', '_')}_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            "post_only": True,
        }
        try:
            if not MM_DRY_RUN:
                resp = api_post("/portfolio/orders", order_body)
                oid = resp.get("order", {}).get("order_id", "")
                if not oid:
                    print(f"    BID {ticker} WARNING: API returned empty order_id -- skipping DB insert")
                    raise ValueError("empty order_id from API")
            else:
                oid = f"dry_mm_bid_{ticker}_{int(time.time())}"
            conn.execute("""INSERT INTO mm_orders
                (timestamp, ticker, side, price_cents, contracts, order_id,
                 fair_value_cents, inventory_at_post, tag)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, ticker, "yes", bid, buy_size, oid,
                 fair_value_cents, inventory, MM_ORDER_TAG))
            orders_posted += 1
            capital_used += buy_size * bid
            print(f"    BID {ticker} YES x{buy_size} @ {bid}c")
        except Exception as e:
            print(f"    BID {ticker} FAILED: {e}")

    # Post ASK (buy NO at (100-ask) = effectively selling YES at ask price)
    if sell_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        no_price = 100 - ask
        # Series profitability gate: reject if adding this NO position makes ALL outcomes unprofitable
        sp_ok, sp_reason = mm_check_series_profitability(conn, ticker, "no", sell_size, no_price)
        if not sp_ok:
            print(f"    ASK {ticker} BLOCKED: {sp_reason}")
            sell_size = 0
    if sell_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        no_price = 100 - ask
        order_body = {
            "ticker": ticker, "side": "no", "type": "limit",
            "count": sell_size, "no_price": no_price,
            "action": "buy",
            "expiration_ts": int(time.time() + 110),
            "client_order_id": f"mm_ask_{ticker.replace('.', '_')}_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            "post_only": True,
        }
        try:
            if not MM_DRY_RUN:
                resp = api_post("/portfolio/orders", order_body)
                oid = resp.get("order", {}).get("order_id", "")
                if not oid:
                    print(f"    ASK {ticker} WARNING: API returned empty order_id -- skipping DB insert")
                    raise ValueError("empty order_id from API")
            else:
                oid = f"dry_mm_ask_{ticker}_{int(time.time())}"
            conn.execute("""INSERT INTO mm_orders
                (timestamp, ticker, side, price_cents, contracts, order_id,
                 fair_value_cents, inventory_at_post, tag)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, ticker, "no", no_price, sell_size, oid,
                 fair_value_cents, inventory, MM_ORDER_TAG))
            orders_posted += 1
            capital_used += sell_size * no_price
            print(f"    ASK {ticker} NO x{sell_size} @ {no_price}c (YES ask={ask}c)")
        except Exception as e:
            print(f"    ASK {ticker} FAILED: {e}")

    conn.commit()
    return orders_posted, capital_used
