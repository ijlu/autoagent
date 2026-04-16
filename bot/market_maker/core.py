"""Market-making orchestrator.

Extracted from trade.py mm_run() (lines 6772-7013).
This is the top-level MM entry point called once per cycle after directional trading.

Flow:
  0. Liquidate expiring positions
  1. Check fills from last cycle -> update inventory
  2. Cancel all stale MM orders
  3. Fetch targeted series, select candidate markets
  4. For each market: get fair value from ensemble, post two-sided quotes
  5. QA loop on inventory positions
  6. Log session stats
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from bot.market_maker.inventory import mm_get_inventory
from bot.market_maker.fills import mm_check_fills, mm_cancel_all_orders
from bot.market_maker.selection import mm_select_markets
from bot.market_maker.quotes import mm_post_quotes, mm_reset_adverse_cache
from bot.market_maker.liquidation import mm_liquidate_expiring
from bot.market_maker.adverse_selection import mm_compute_adverse_selection, mm_compute_postmortem_risk_scores
from bot.signals.ensemble import get_independent_estimate
from bot.api import api_get
from bot.db import kv_get as _kv_get, kv_set as _kv_set
from bot.config import (
    MM_ENABLED, MM_DRY_RUN, MM_CAPITAL_PCT, MM_MAX_MARKETS, MM_ORDER_TAG,
    MM_PREFERRED_CATS,
)


def _compute_adverse_selection_signals(conn):
    """Compute and cache fill rate, one-sided fill, and postmortem risk signals.

    These signals are stored in kv_cache so quotes.py can read them when
    deciding spread widths. Must run AFTER mm_check_fills() so this cycle's
    fills are included.
    """
    # ── Defense 2: Per-family fill rate ──
    # Fill rate = filled_orders / total_orders. High fill rate on maker-only
    # strategy = adverse selection (informed traders are picking us off).
    try:
        rows = conn.execute("""
            SELECT ticker, COUNT(*) as total,
                   SUM(CASE WHEN fill_qty > 0 THEN 1 ELSE 0 END) as filled
            FROM mm_orders
            WHERE timestamp > datetime('now', '-48 hours')
            GROUP BY ticker
            HAVING total >= 4
        """).fetchall()

        family_fill_rates = {}
        for ticker, total, filled in rows:
            family = ticker.split("-")[0] if "-" in ticker else ticker
            if family not in family_fill_rates:
                family_fill_rates[family] = {"total": 0, "filled": 0}
            family_fill_rates[family]["total"] += total
            family_fill_rates[family]["filled"] += filled

        fill_rate_data = {}
        for family, data in family_fill_rates.items():
            if data["total"] >= 8:  # need enough orders to be meaningful
                rate = data["filled"] / data["total"]
                fill_rate_data[family] = round(rate, 3)
                if rate > 0.20:
                    print(f"[adv-signal] Fill rate {family}: {rate:.0%} "
                          f"({data['filled']}/{data['total']} orders filled)")

        _kv_set(conn, "mm_fill_rates", fill_rate_data, 600)  # 10 min TTL
    except Exception as e:
        print(f"[adv-signal] Fill rate computation failed: {e}")

    # ── Defense 3: One-sided fill detection ──
    # If all fills in recent cycles are on one side (all buys or all sells),
    # informed traders are selectively picking off one side of our quotes.
    try:
        rows = conn.execute("""
            SELECT ticker, side, SUM(fill_qty) as filled_qty
            FROM mm_orders
            WHERE fill_qty > 0
              AND timestamp > datetime('now', '-4 hours')
            GROUP BY ticker, side
        """).fetchall()

        per_ticker = {}
        for ticker, side, filled_qty in rows:
            family = ticker.split("-")[0] if "-" in ticker else ticker
            if family not in per_ticker:
                per_ticker[family] = {"yes": 0, "no": 0}
            per_ticker[family][side] = per_ticker[family].get(side, 0) + filled_qty

        onesided_data = {}
        for family, sides in per_ticker.items():
            total = sides.get("yes", 0) + sides.get("no", 0)
            if total < 3:
                continue
            imbalance = abs(sides.get("yes", 0) - sides.get("no", 0)) / total
            onesided_data[family] = round(imbalance, 3)
            if imbalance > 0.80:
                dominant = "YES" if sides.get("yes", 0) > sides.get("no", 0) else "NO"
                print(f"[adv-signal] One-sided fills {family}: {imbalance:.0%} "
                      f"imbalance toward {dominant} "
                      f"(yes={sides.get('yes', 0)}, no={sides.get('no', 0)})")

        # Track consecutive one-sided cycles for escalation
        prev_onesided = _kv_get(conn, "mm_onesided_consec") or {}
        new_consec = {}
        for family, imbalance in onesided_data.items():
            prev_count = prev_onesided.get(family, 0)
            if imbalance > 0.80:
                new_consec[family] = prev_count + 1
            else:
                new_consec[family] = 0
        _kv_set(conn, "mm_onesided_consec", new_consec, 3600)
        _kv_set(conn, "mm_onesided_imbalance", onesided_data, 600)
    except Exception as e:
        print(f"[adv-signal] One-sided fill computation failed: {e}")

    # ── Defense 5: Postmortem risk scores ──
    try:
        postmortem_scores = mm_compute_postmortem_risk_scores(conn)
        if postmortem_scores:
            _kv_set(conn, "mm_postmortem_risk", postmortem_scores, 3600)
    except Exception as e:
        print(f"[adv-signal] Postmortem risk computation failed: {e}")


def mm_run(conn, markets, balance_cents, portfolio_value,
           adaptive_weights=None, calibration_corrections=None,
           disabled_sources=None,
           *,
           categorize_fn: Optional[Callable] = None,
           category_edges: Optional[Dict[str, Any]] = None,
           init_mm_tables_fn: Optional[Callable] = None):
    """Main market-making pass. Called from the main loop after directional trading.

    Flow:
    1. Check fills from last cycle -> update inventory
    2. Cancel all stale MM orders
    3. Select suitable markets
    4. For each market: get fair value, calculate quotes, post orders
    5. Log session stats

    Args:
        conn: SQLite connection.
        markets: List of market dicts from the generic /markets fetch.
        balance_cents: Available balance in cents.
        portfolio_value: Total portfolio value (unused currently but kept for API compat).
        adaptive_weights: Source weight overrides from learning module.
        calibration_corrections: Calibration corrections per source.
        disabled_sources: Set of source names to skip.
        categorize_fn: Optional callable(ticker, market) -> category string.
            If None, category-edge gating in mm_select_markets is skipped.
        category_edges: Optional dict of category -> edge thresholds. If None and
            categorize_fn is provided, will be computed if possible.
        init_mm_tables_fn: Optional callable(conn) to create MM tables.
            If None, assumes tables already exist.

    Returns dict with MM stats for the session."""
    if not MM_ENABLED:
        return {"mm_enabled": False}

    # Reset per-cycle caches
    mm_reset_adverse_cache()

    now = datetime.now(timezone.utc).isoformat()
    if init_mm_tables_fn is not None:
        init_mm_tables_fn(conn)

    stats = {
        "mm_enabled": True,
        "fills_detected": 0,
        "orders_cancelled": 0,
        "markets_quoted": 0,
        "orders_posted": 0,
        "capital_deployed": 0,
    }

    print("\n[mm] \u2550\u2550\u2550 Market Making Pass \u2550\u2550\u2550")

    # Step 0: Liquidate expiring positions to avoid settlement risk
    try:
        liq_count = mm_liquidate_expiring(conn)
        if liq_count:
            print(f"[mm] Posted {liq_count} liquidation orders for expiring markets")
    except Exception as e:
        print(f"[mm] Liquidation check failed: {e}")

    # Step 1: Check fills from last cycle
    stats["fills_detected"] = mm_check_fills(conn)
    if stats["fills_detected"]:
        print(f"[mm] {stats['fills_detected']} fills detected since last cycle")

    # Step 1b: Compute adverse selection signals (fill rates, one-sided, postmortems)
    # Must run AFTER mm_check_fills so this cycle's fills are included in stats.
    _compute_adverse_selection_signals(conn)

    # Step 2: Cancel stale MM orders (we'll re-post at updated prices)
    stats["orders_cancelled"] = mm_cancel_all_orders(conn)
    print(f"[mm] Cancelled {stats['orders_cancelled']} stale orders")

    # Step 3: Supplement with targeted series fetching for active markets
    # The generic /markets endpoint returns newest-first (mostly parlays).
    # Targeted fetching ensures we see actual high-activity markets.
    MM_TARGET_SERIES = [
        # KXBTC/KXETH removed: blocklisted (50%+ adverse selection from crypto bots)
        "KXINX", "KXGDP", "KXCPI", "KXJOB", "KXUNRATE",
        "KXFED", "KXGAS",
        # Weather — ALL REMOVED 2026-04-16: $375 of $400 total losses (94%).
        # Counterparties have real-time METAR/NWS and reprice faster than our 2-min cycle.
        # Even with bracket width fix + METAR gating, structural adverse selection persists.
        # Also added to MM_BLOCKLIST_PREFIXES in selection.py as defense-in-depth.
        # "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHAUS", "KXHIGHMIA",
        # "KXHIGHHOU", "KXHIGHPHX", "KXHIGHDEN", "KXHIGHSF",
        # "KXHMONTHRANGE", "KXHURR",
        # Sports — DISABLED: odds source never matches Kalshi titles in practice.
        # Re-enable when Odds API integration is fixed with proper game/team matching.
        # "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXMMA", "KXSOCCER", "KXNCAA",
        # Company KPIs — DISABLED: data sources (SensorTower, Finnhub) unreliable/403.
        # Re-enable when live data feeds are verified working end-to-end.
        # "KXBOEING", "KXSPOTIFYMAU", "KXUBERTRIPS", "KXMETAHEADCOUNT", "KXHOOD",
        # "KXDASHORDERS", "KXLYFT", "KXMTCH", "KXPLTR", "KXRACE", "KXPM",
        # "KXABNB", "KXTESLASEMI", "KXEARNINGSMENTIONNFLX", "KXSTRIPEIPO",
        "KXISMPMI",          # ISM Manufacturing PMI — uses FRED data (reliable)
    ]
    seen_tickers = {m.get("ticker") for m in markets}
    targeted_count = 0
    series_counts = {}
    for series in MM_TARGET_SERIES:
        try:
            resp = api_get(f"/markets?limit=200&status=open&series_ticker={series}")
            batch = resp.get("markets", [])
            new_count = 0
            for m in batch:
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    markets.append(m)
                    seen_tickers.add(t)
                    targeted_count += 1
                    new_count += 1
            if new_count > 0:
                series_counts[series] = new_count
        except Exception:
            pass  # series might not exist or have no open markets
    if targeted_count:
        print(f"[mm] Fetched {targeted_count} additional markets from {len(MM_TARGET_SERIES)} targeted series")
        print(f"[mm] Series breakdown: {dict(series_counts)}")

    # Pass category_edges so mm_select_markets can skip unprofitable categories
    _cat_edges = category_edges if category_edges is not None else {}
    mm_candidates = mm_select_markets(markets, conn, balance_cents, category_edges=_cat_edges)
    print(f"[mm] {len(mm_candidates)} markets eligible for market making")

    if not mm_candidates:
        print("[mm] No suitable markets found this cycle")
        return stats

    # Step 4: Quote each market
    total_capital_used = 0
    max_mm_capital = int(balance_cents * MM_CAPITAL_PCT)

    for score, m, ticker, spread, mid, _stale_inventory, cat in mm_candidates:
        if total_capital_used >= max_mm_capital:
            print(f"[mm] Capital limit reached ({max_mm_capital/100:.2f})")
            break

        # CRITICAL: refresh inventory from DB (not stale snapshot from mm_select_markets)
        inventory, _ = mm_get_inventory(conn, ticker)

        # Get fair value from our ensemble estimate
        title = m.get("title", "") or m.get("subtitle", "") or ""
        yes_ask_f = float(m.get("yes_ask") or m.get("yes_ask_dollars") or 99)
        if yes_ask_f > 1:
            yes_ask_f /= 100
        vol = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)

        try:
            indep_prob, src_desc, n_sources = get_independent_estimate(
                ticker, m, yes_ask_f, vol,
                adaptive_weights=adaptive_weights,
                calibration_corrections=calibration_corrections,
                disabled_sources=disabled_sources)
        except Exception:
            indep_prob, src_desc, n_sources = None, None, 0

        # -- FAILSAFE: never market-make without REAL data --
        # LLM estimates are guesses, not data -- they provide no real edge for MM.
        # Only quote when we have at least 1 non-LLM source (weather, FRED, crypto, etc.)
        # Only EXOGENOUS data sources count -- "series" and "momentum" are endogenous
        # (derived from market prices, not independent information)
        _MM_REAL_SOURCES = {"weather", "fred", "crypto", "clevfed", "noaa",
                           "polymarket", "metaculus", "bls", "tomorrow",
                           "metar", "fedwatch"}
        src_desc_str = src_desc or ""
        has_real_source = (indep_prob is not None and n_sources >= 1 and
                          any(s in src_desc_str for s in _MM_REAL_SOURCES))

        if not has_real_source:
            stats.setdefault("skipped_no_data", 0)
            stats["skipped_no_data"] += 1
            reason = "no data source" if n_sources < 1 else "LLM-only (no real data)"
            print(f"  {ticker}: SKIP \u2014 {reason} [{cat}]")
            continue
        else:
            fair_value_cents = max(2, min(98, int(indep_prob * 100)))
            # Sanity check: fair value too extreme -> widen spread or skip
            if fair_value_cents <= 3 or fair_value_cents >= 85:
                print(f"  {ticker}: SKIP \u2014 extreme fair value {fair_value_cents}\u00a2 (prob={indep_prob:.3f})")
                stats.setdefault("skipped_extreme_fv", 0)
                stats["skipped_extreme_fv"] += 1
                continue
            print(f"  {ticker}: fair={fair_value_cents}\u00a2 mid={mid}\u00a2 spread={spread}\u00a2 "
                  f"inv={inventory:+d} [{cat}] ({n_sources} sources)")

        # Post two-sided quotes
        remaining_capital = max(0, balance_cents - total_capital_used)
        posted, capital = mm_post_quotes(conn, m, fair_value_cents, remaining_capital, inventory)
        stats["orders_posted"] += posted
        total_capital_used += capital
        if posted > 0:
            stats["markets_quoted"] += 1

    stats["capital_deployed"] = total_capital_used

    # Step 5: Compute total inventory stats
    inv_rows = conn.execute(
        "SELECT ticker, net_position, realized_pnl_cents, avg_entry_cents FROM mm_inventory"
    ).fetchall()
    total_inv_value = 0
    total_realized = 0
    for ticker, net, rpnl, avg_e in inv_rows:
        total_inv_value += abs(net) * int(avg_e)
        total_realized += rpnl

    stats["inventory_value_cents"] = total_inv_value
    stats["realized_pnl_cents"] = total_realized

    # -- Step 6: QA LOOP -- re-check MM inventory against fresh data --------
    # Check a rotating subset of inventory positions each run (max 5) to avoid
    # blowing the systemd timeout. Prioritize largest positions first.
    MAX_QA_PER_RUN = 5
    qa_flags = 0
    active_inv = [(t, n, r, a) for t, n, r, a in inv_rows if abs(n) > 0]
    # Sort by position size (largest first) -- check the riskiest positions first
    active_inv.sort(key=lambda x: abs(x[1]) * int(x[3]), reverse=True)
    # Rotate which positions get checked using run count
    run_count = conn.execute("SELECT COUNT(*) FROM mm_sessions").fetchone()[0] or 0
    start_idx = (run_count * MAX_QA_PER_RUN) % max(len(active_inv), 1)
    qa_batch = active_inv[start_idx:start_idx + MAX_QA_PER_RUN]
    # Wrap around if needed
    if len(qa_batch) < MAX_QA_PER_RUN and start_idx > 0:
        qa_batch += active_inv[:MAX_QA_PER_RUN - len(qa_batch)]

    # Track consecutive QA flags per ticker for auto-liquidation
    from bot.api import api_post
    _QA_FLAG_THRESHOLD = 3   # liquidate after 3 consecutive flagged cycles
    _QA_EDGE_THRESHOLD = 0.10  # fair value must be >10c against entry
    qa_liquidations = 0

    if qa_batch:
        print(f"[mm-qa] Checking {len(qa_batch)}/{len(active_inv)} inventory positions")
    for ticker, net, rpnl, avg_e in qa_batch:
        try:
            mkt = api_get(f"/markets/{ticker}")
            market = mkt.get("market", mkt)
            yes_ask_f = float(market.get("yes_ask") or market.get("yes_ask_dollars") or 99)
            if yes_ask_f > 1:
                yes_ask_f /= 100
            vol = float(market.get("volume") or market.get("volume_fp") or 0)
            fresh_prob, _, fresh_n = get_independent_estimate(
                ticker, market, yes_ask_f, vol,
                disabled_sources=disabled_sources)
            if fresh_prob is not None and fresh_n > 0:
                entry_f = int(avg_e) / 100
                is_losing = False
                loss_magnitude = 0.0

                if net > 0 and fresh_prob < entry_f - 0.05:
                    is_losing = True
                    loss_magnitude = entry_f - fresh_prob
                elif net < 0 and (1 - fresh_prob) < (1 - entry_f) - 0.05:
                    is_losing = True
                    loss_magnitude = (1 - entry_f) - (1 - fresh_prob)

                if is_losing:
                    flag_key = f"mm_qa_flags_{ticker}"
                    flag_data = _kv_get(conn, flag_key) or {"count": 0}
                    flag_data["count"] = flag_data.get("count", 0) + 1
                    flag_data["last_fair"] = round(fresh_prob, 4)
                    flag_data["loss_mag"] = round(loss_magnitude, 4)
                    _kv_set(conn, flag_key, flag_data, 3600)
                    consec = flag_data["count"]

                    if consec >= _QA_FLAG_THRESHOLD and loss_magnitude >= _QA_EDGE_THRESHOLD:
                        side = "yes" if net > 0 else "no"
                        qty = abs(net)
                        if side == "yes":
                            liq_price = max(1, int(fresh_prob * 100) - 2)
                        else:
                            liq_price = max(1, int((1 - fresh_prob) * 100) - 2)

                        print(f"[mm-qa] \U0001f534 {ticker}: AUTO-LIQUIDATING {side} x{qty} -- "
                              f"flagged {consec}x, entry={entry_f:.2f} fair={fresh_prob:.2f}")

                        if not MM_DRY_RUN:
                            try:
                                import time as _time, uuid as _uuid
                                client_id = f"mm_qa_liq_{ticker}_{int(_time.time())}".replace(".", "_")
                                resp = api_post("/portfolio/orders", {
                                    "ticker": ticker,
                                    "side": side,
                                    "type": "limit",
                                    "count": qty,
                                    ("yes_price" if side == "yes" else "no_price"): liq_price,
                                    "action": "sell",
                                    "expiration_ts": int(_time.time() + 300),
                                    "client_order_id": client_id,
                                })
                                order_id = resp.get("order", {}).get("order_id", "?")
                                print(f"[mm-qa]   + liquidation order {order_id}")
                                qa_liquidations += 1
                                _kv_set(conn, flag_key, {"count": 0, "liquidated": True}, 3600)
                            except Exception as e:
                                print(f"[mm-qa]   x liquidation failed: {e}")
                        else:
                            print(f"[mm-qa]   [DRY] would sell {qty}x {ticker} {side} @ {liq_price}c")
                            qa_liquidations += 1
                    else:
                        _side_str = "YES" if net > 0 else "NO"
                        print(f"[mm-qa] \u26a0\ufe0f  {ticker}: {_side_str} x{abs(net)} entry={entry_f:.2f} "
                              f"fair={fresh_prob:.2f} -- LOSING (flagged {consec}x)")
                    qa_flags += 1
                else:
                    flag_key = f"mm_qa_flags_{ticker}"
                    prev = _kv_get(conn, flag_key)
                    if prev and prev.get("count", 0) > 0:
                        _kv_set(conn, flag_key, {"count": 0}, 3600)
                    print(f"[mm-qa] \u2713 {ticker}: net={net:+d} entry={entry_f:.2f} "
                          f"fair={fresh_prob:.2f} -- OK ({fresh_n} sources)")
        except Exception:
            pass
    if qa_flags:
        print(f"[mm-qa] {qa_flags} positions flagged"
              f"{f', {qa_liquidations} auto-liquidated' if qa_liquidations else ''}")
    stats["qa_flags"] = qa_flags
    stats["qa_liquidations"] = qa_liquidations

    # Log MM session
    conn.execute("""INSERT INTO mm_sessions
        (recorded_at, markets_quoted, orders_posted, orders_cancelled,
         fills_detected, inventory_value_cents, realized_pnl_cents, unrealized_pnl_cents)
        VALUES (?,?,?,?,?,?,?,?)""",
        (now, stats["markets_quoted"], stats["orders_posted"], stats["orders_cancelled"],
         stats["fills_detected"], total_inv_value, total_realized, 0))
    conn.commit()

    skipped = stats.get('skipped_no_data', 0)
    print(f"[mm] Summary: {stats['markets_quoted']} markets quoted, {skipped} skipped (no data), "
          f"{stats['orders_posted']} orders, {stats['fills_detected']} fills, "
          f"inventory=${total_inv_value/100:.2f}, realized P&L=${total_realized/100:+.2f}"
          f"{f', {qa_flags} QA flags' if qa_flags else ''}")

    return stats
