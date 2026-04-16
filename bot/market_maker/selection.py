"""Market-maker market selection and scoring.

Extracted from trade.py (lines ~6143-6297).
"""

import math
import re
from datetime import datetime, timezone

from bot.market_maker.inventory import mm_get_inventory
from bot.config import MM_MIN_SPREAD, MM_MIN_VOLUME, MM_MAX_INVENTORY, MM_MAX_MARKETS, MM_PREFERRED_CATS, MM_MAX_DAYS_TO_EXPIRY


# ---------------------------------------------------------------------------
# Category classification (extracted from trade.py:5255-5301)
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "economics": ["cpi", "inflation", "unemployment", "gdp", "nonfarm", "payroll",
                  "fed funds", "fomc", "interest rate", "jobs report",
                  "federal funds", "fed rate", "kxfed", "kxcpi", "kxgdp", "kxjob", "kxunrate"],
    "crypto":    ["btc", "bitcoin", "eth", "ether", "sol", "solana", "crypto", "coin"],
    "weather":   ["temperature", "temp", "weather", "degrees", "\u00b0f", "\u00b0c", "heat", "cold", "freeze",
                  "kxhigh", "kxhmonth", "kxhurr", "highest temperature", "nws"],
    "sports":    ["nba", "nfl", "mlb", "nhl", "ncaa", "mls", "epl", "nascar", "championship",
                  "playoff", "stanley cup", "finals", "world series"],
    "company":   ["deliveries", "production", "subscribers", "revenue", "earnings",
                  "daily active", "monthly active", "dau", "mau", "users",
                  "headcount", "total orders", "total rides", "total payers",
                  "total customers", "shipments", "bookings", "trips",
                  "gold sub", "semi truck", "ipo",
                  "tesla", "kxteslasemi", "boeing", "kxboeing",
                  "netflix", "kxearningsmentionnflx",
                  "meta", "kxmetaheadcount",
                  "spotify", "kxspotifymau", "uber", "kxubertrips",
                  "robinhood", "kxhood", "doordash", "kxdashorders",
                  "lyft", "kxlyft", "match group", "kxmtch",
                  "palantir", "kxpltr", "ferrari", "kxrace",
                  "philip morris", "zyn", "kxpm",
                  "airbnb", "kxabnb", "kxstripeipo", "kxismpmi",
                  "apple", "google", "alphabet", "amazon", "microsoft", "nvidia"],
}

_COMPANY_PREFIXES = [
    "kxboeing", "kxspotifymau", "kxubertrips", "kxmetaheadcount",
    "kxhood", "kxdashorders", "kxlyft", "kxmtch", "kxpltr",
    "kxrace", "kxpm", "kxabnb", "kxteslasemi", "kxismpmi",
    "kxearningsmention", "kxearningmention", "kxstripeipo",
]


# ---------------------------------------------------------------------------
# Defense 1: Adverse selection blocklist
# ---------------------------------------------------------------------------
# Series prefixes with structurally informed counterparties proven by data:
#   KXETH/KXBTC: 50-54% adverse selection (crypto bots with sub-second feeds)
#   KXNBATOTAL:  sports insider flow — one-sided fills at 100%
#   KXPOLITICSMENTION: asymmetric info from live broadcast monitoring
#   KXHIGH*/KXHMONTHRANGE/KXHURR: weather families — $375 of $400 total losses (94%).
#     Counterparties have real-time METAR/NWS data and reprice faster than our 2-min cycle.
#     Even with bracket width fix + METAR gating, structural adverse selection persists.
# Reviewed 2026-04-16 based on 116 MM settlement postmortems.
MM_BLOCKLIST_PREFIXES = frozenset({
    "KXETH", "KXBTC", "KXNBATOTAL", "KXPOLITICSMENTION",
    # Weather families — structurally adversely selected
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHAUS", "KXHIGHMIA",
    "KXHIGHHOU", "KXHIGHPHX", "KXHIGHDEN", "KXHIGHSF",
    "KXHMONTHRANGE", "KXHURR",
})


def categorize_market(ticker, title):
    """Assign a market to a risk category based on ticker and title.
    Company tickers get priority -- e.g. KXEARNINGSMENTIONNFLX-26APR16-MLB
    should be 'company' not 'sports' despite containing 'mlb'."""
    text = (ticker + " " + title).lower()
    ticker_lower = ticker.lower()

    # Priority check: company ticker prefixes always win
    if any(ticker_lower.startswith(p) for p in _COMPANY_PREFIXES):
        return "company"

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return "other"


# ---------------------------------------------------------------------------
# Market selection
# ---------------------------------------------------------------------------

def mm_select_markets(markets, conn, balance_cents, category_edges=None):
    """Select markets suitable for market making.
    Criteria: wide spread, adequate volume, low adverse-selection category,
    not already at inventory limit. Uses category_edges to skip categories
    that need unsustainably wide spreads."""
    # Portfolio-level series concentration: count total inventory per series prefix.
    # Prevents accumulating 30+ positions in one correlated cluster (e.g., all KXFED).
    MM_MAX_SERIES_INVENTORY = 50  # max total |net_position| across all tickers in one series
    series_inventory = {}
    try:
        inv_rows = conn.execute(
            "SELECT ticker, net_position FROM mm_inventory WHERE net_position != 0"
        ).fetchall()
        for t, net in inv_rows:
            series_pfx = t.split("-")[0] if "-" in t else t
            series_inventory[series_pfx] = series_inventory.get(series_pfx, 0) + abs(int(net))
    except Exception:
        pass

    candidates = []
    filter_stats = {"total": 0, "cat_skip": 0, "no_ask": 0, "no_bid_narrow": 0,
                    "tight_spread": 0, "low_vol": 0, "extreme_price": 0,
                    "inv_full": 0, "expiring": 0, "series_full": 0, "passed": 0}
    for m in markets:
        filter_stats["total"] += 1
        ticker = m.get("ticker", "")
        title = (m.get("title", "") or m.get("subtitle", "") or "").lower()

        # Skip multi-leg parlay/combo markets (MVE) — synthetic, not real tradeable markets
        if "KXMVE" in ticker or "MULTIGAME" in ticker or m.get("mve_collection_ticker"):
            filter_stats["cat_skip"] += 1
            continue

        # Defense 1: Adverse selection blocklist — skip series with proven toxic flow
        series_pfx_bl = ticker.split("-")[0] if "-" in ticker else ticker
        if series_pfx_bl in MM_BLOCKLIST_PREFIXES:
            filter_stats["blocklisted"] = filter_stats.get("blocklisted", 0) + 1
            continue

        # Category filter: prefer low-adverse-selection markets
        cat = categorize_market(ticker, title)
        if cat not in MM_PREFERRED_CATS:
            filter_stats["cat_skip"] += 1
            continue

        # Skip categories where learned edge multiplier is too high (unprofitable to MM)
        if category_edges:
            cat_mult = category_edges.get(cat, 1.0)
            if cat_mult > 2.5:
                filter_stats["cat_skip"] += 1
                continue  # category needs >2.5x edge — not worth quoting

        # Parse prices
        def _pc(v):
            """Convert price to cents. Handles both cent ints (65) and dollar strings ('0.65')."""
            v = float(v or 0)
            return int(round(v * 100)) if v <= 1.0 else int(v)
        yes_ask = _pc(m.get("yes_ask") or m.get("yes_ask_dollars"))
        yes_bid = _pc(m.get("yes_bid") or m.get("yes_bid_dollars"))

        # Use last_price as fair value hint when orderbook is thin
        last_price = _pc(m.get("last_price") or m.get("last_price_dollars"))

        # Calculate spread from available prices
        # Empty orderbook handling — NEVER quote off stale last_price alone.
        # Markets with no live book are too dangerous: we have no idea where
        # real liquidity sits, and quoting off a stale print invites adverse selection.
        if yes_ask <= 0 and yes_bid <= 0:
            # No live book at all — skip entirely. Do NOT use last_price as anchor.
            filter_stats["no_ask"] += 1
            continue
        elif yes_ask <= 0:
            spread = 99 - yes_bid  # no sellers = wide spread
            mid = yes_bid + 10  # anchor to live bid, NOT stale last_price
        elif yes_bid <= 0:
            spread = yes_ask  # no buyers = wide spread
            mid = max(yes_ask - 5, 1)  # anchor to live ask, NOT stale last_price
        else:
            spread = yes_ask - yes_bid
            mid = (yes_ask + yes_bid) // 2

        if spread < MM_MIN_SPREAD:
            filter_stats["tight_spread"] += 1
            continue  # spread too tight — a better MM is already here

        volume = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)
        open_interest = float(m.get("open_interest") or m.get("open_interest_fp") or 0)
        activity = max(volume, open_interest)  # OI shows someone holds positions even if no recent trades
        if activity < MM_MIN_VOLUME:
            filter_stats["low_vol"] += 1
            continue

        # Skip markets too close to 0 or 100 (high adverse selection near resolution)
        if mid < 10 or mid > 90:
            filter_stats["extreme_price"] += 1
            continue

        # Check current inventory — skip if already at limit
        inv, _ = mm_get_inventory(conn, ticker)
        if abs(inv) >= MM_MAX_INVENTORY:
            filter_stats["inv_full"] += 1
            continue

        # Portfolio-level series concentration check
        series_pfx = ticker.split("-")[0] if "-" in ticker else ticker
        if series_inventory.get(series_pfx, 0) >= MM_MAX_SERIES_INVENTORY:
            filter_stats["series_full"] += 1
            continue

        # Check time to expiration — prefer markets with > 6h left
        close_time = m.get("close_time") or m.get("expiration_time")
        hours_left = 999
        if close_time:
            try:
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                hours_left = (ct - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                pass
        if hours_left < 2:
            filter_stats["expiring"] += 1
            continue  # too close to resolution — high adverse selection
        if hours_left > MM_MAX_DAYS_TO_EXPIRY * 24:
            filter_stats["too_far_out"] = filter_stats.get("too_far_out", 0) + 1
            continue  # too far from resolution — forecast uncertainty too high, capital lock-up

        filter_stats["passed"] += 1
        # Score: balance spread profit vs fill probability (volume)
        # Cap spread contribution at 30¢ — beyond that, extra spread doesn't help much
        # because the market is just illiquid, not more profitable per fill
        spread_score = min(spread, 30)
        vol_score = math.log1p(volume + open_interest * 0.3)  # OI counts but less than volume
        time_mult = 1.0 if hours_left > 12 else 0.7
        # Prefer markets that resolve sooner — faster capital recycling
        days_to_resolution = hours_left / 24
        recycling_bonus = max(0, 5 - days_to_resolution) * 0.5  # up to 2.5 bonus for <5 day markets
        score = spread_score * vol_score * time_mult + recycling_bonus
        candidates.append((score, m, ticker, spread, mid, inv, cat))

    # Log filter funnel
    print(f"[mm] Filter funnel: {filter_stats['total']} total \u2192 "
          f"{filter_stats.get('blocklisted', 0)} blocklisted, "
          f"{filter_stats['cat_skip']} wrong category, "
          f"{filter_stats['no_ask']} no ask, "
          f"{filter_stats['tight_spread']} tight spread, "
          f"{filter_stats['low_vol']} low volume, "
          f"{filter_stats['extreme_price']} extreme price, "
          f"{filter_stats['expiring']} expiring, "
          f"{filter_stats['inv_full']} inv full, "
          f"{filter_stats.get('series_full', 0)} series full, "
          f"{filter_stats.get('too_far_out', 0)} too far out \u2192 "
          f"{filter_stats['passed']} passed")

    # Sort by score descending, then diversify: max 3 markets per series
    # This prevents concentration (e.g., all 16 KXFED strikes filling to max)
    MAX_PER_SERIES = 3
    candidates.sort(key=lambda x: x[0], reverse=True)
    selected = []
    series_count = {}
    for c in candidates:
        ticker = c[2]  # ticker is 3rd element
        # Extract series prefix (e.g., "KXFED" from "KXFED-27APR-T2.50")
        series = ticker.split("-")[0] if "-" in ticker else ticker
        series_count[series] = series_count.get(series, 0) + 1
        if series_count[series] > MAX_PER_SERIES:
            continue  # skip — already have enough from this series
        selected.append(c)
        if len(selected) >= MM_MAX_MARKETS:
            break
    return selected
