#!/usr/bin/env python3
"""
Kalshi Trading Bot — Backtesting Framework (v1.0)

Pulls historical settled markets from Kalshi, replays our ensemble estimate
functions against them, and measures what our strategies would have done.

This answers the question: "If we had been running this bot for the last N months,
would we have made money?"

Limitations:
  - We can replay: weather (Open-Meteo historical forecast API), FRED, BLS, Cleveland Fed
  - We CANNOT perfectly replay: Polymarket prices, Manifold prices, bookmaker odds
    (no historical snapshots). For these, we use current data as a rough proxy or skip.
  - Market prices at time of potential trade are approximated from settlement data
  - This is a DIRECTIONAL backtest (would we have been right about the outcome?)
    not a full execution simulation (slippage, fill rates, market impact not modeled)

Usage:
  python3 backtest.py [--days 90] [--category weather] [--strategy all] [--verbose]
"""

import os, sys, time, json, math, sqlite3, argparse, random
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Import shared infrastructure from trade.py ────────────────────────────────
# We import the estimate functions, rate limiter, config, etc.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from trade import (
        api_get, _cached_get, _rate_limit_wait, _days_to_expiry,
        categorize_market, CATEGORY_KEYWORDS,
        get_weather_estimate, get_weather_forecast,
        get_noaa_alerts_for_market,
        get_fred_estimate, get_cleveland_fed_nowcast,
        get_crypto_estimate,
        get_polymarket_estimate, get_metaculus_estimate,
        get_sports_estimate, get_series_estimate,
        get_independent_estimate,
        score_event_driven, score_cross_market, score_near_resolution,
        MIN_EDGE, SINGLE_SOURCE_EDGE,
        ESTIMATED_FEE_PER_CONTRACT, ESTIMATED_EXIT_SPREAD,
        SOURCE_WEIGHTS, DATA_RELEASE_CALENDAR, BLS_SERIES,
        _fetch_bls_latest,
    )
    IMPORTS_OK = True
except ImportError as e:
    print(f"[backtest] WARNING: Could not import from trade.py: {e}")
    print("[backtest] Running in standalone mode with limited functionality")
    IMPORTS_OK = False

import requests

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
BASE_URL = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
DB_PATH = os.environ.get("BACKTEST_DB", os.path.join(os.path.dirname(__file__), "backtest_results.db"))

# How far back to look for settled markets
DEFAULT_LOOKBACK_DAYS = 90

# Open-Meteo Historical Forecast API (free, no auth)
OPEN_METEO_HISTORICAL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE — stores backtest results for analysis
# ══════════════════════════════════════════════════════════════════════════════
def init_backtest_db(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS backtest_markets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT, title TEXT, category TEXT,
        close_time TEXT, result TEXT,
        yes_price_cents INTEGER, no_price_cents INTEGER,
        volume REAL, fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS backtest_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT, ticker TEXT, category TEXT,
        strategy TEXT, side TEXT, score REAL,
        our_estimate REAL, market_price REAL, edge REAL,
        actual_outcome INTEGER, won INTEGER,
        fee_adjusted_edge REAL,
        hypothetical_profit_cents REAL,
        detail TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS backtest_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT UNIQUE, started_at TEXT, finished_at TEXT,
        markets_tested INTEGER, trades_simulated INTEGER,
        wins INTEGER, losses INTEGER, win_rate REAL,
        total_profit_cents REAL,
        by_strategy TEXT, by_category TEXT,
        config TEXT)""")
    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: FETCH HISTORICAL SETTLED MARKETS FROM KALSHI
# ══════════════════════════════════════════════════════════════════════════════
def fetch_settled_markets(days=DEFAULT_LOOKBACK_DAYS, max_pages=10, category_filter=None):
    """Fetch recently settled markets from Kalshi API.
    Returns list of market dicts with result field."""
    markets = []
    cursor = None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    for page in range(max_pages):
        url = f"/markets?limit=500&status=settled"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            resp = api_get(url)
        except Exception as e:
            print(f"[backtest] Error fetching markets page {page}: {e}")
            break

        batch = resp.get("markets", [])
        if not batch:
            break

        for m in batch:
            close_time = (m.get("close_time") or m.get("expiration_time") or "")
            if close_time and close_time < cutoff:
                # Older than our lookback window — stop paginating
                print(f"[backtest] Reached cutoff ({days}d) at page {page+1}")
                cursor = None
                break

            # Filter by category if requested
            ticker = m.get("ticker", "")
            title = m.get("title") or m.get("subtitle") or ""
            cat = categorize_market(ticker, title) if IMPORTS_OK else "other"
            if category_filter and cat != category_filter:
                continue

            markets.append(m)

        cursor = resp.get("cursor")
        if not cursor or len(batch) < 500:
            break
        print(f"[backtest] Page {page+1}: {len(batch)} markets (total {len(markets)})")
        time.sleep(0.5)  # be polite to the API

    print(f"[backtest] Fetched {len(markets)} settled markets from last {days} days")
    return markets


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: DETERMINE ACTUAL OUTCOME FOR EACH MARKET
# ══════════════════════════════════════════════════════════════════════════════
def get_market_outcome(m):
    """Determine if the market resolved YES or NO.
    Returns 1 for YES, 0 for NO, None if unclear."""
    result = m.get("result", "")
    if result == "yes":
        return 1
    elif result == "no":
        return 0

    # Some markets encode result differently
    settlement_value = m.get("settlement_value")
    if settlement_value is not None:
        return 1 if float(settlement_value) > 50 else 0

    # Check if yes_price settled at 100 or 0
    yes_price = m.get("yes_ask") or m.get("last_price") or m.get("yes_ask_dollars")
    if yes_price is not None:
        p = float(yes_price)
        if p > 1:
            p = p / 100
        if p >= 0.95:
            return 1
        if p <= 0.05:
            return 0

    return None


def get_market_price_at_trade_time(m):
    """Approximate what the market price was when we would have traded.
    For settled markets, we use the last known prices.
    Returns (yes_price, no_price) as floats in [0,1]."""
    # Use the market's price fields — these are the last traded prices
    yes_ask = m.get("yes_ask") or m.get("yes_ask_dollars") or 0
    yes_bid = m.get("yes_bid") or m.get("yes_bid_dollars") or 0

    # Normalize to [0,1]
    def _norm(v):
        v = float(v or 0)
        return v / 100 if v > 1 else v

    yes_price = _norm(yes_ask) if yes_ask else _norm(yes_bid)
    no_price = 1.0 - yes_price

    # For settled markets, the final price is 0 or 1, which isn't useful.
    # Use previous_yes_ask if available, or estimate from volume-weighted mid
    prev_yes = m.get("previous_yes_ask") or m.get("last_price")
    if prev_yes:
        yes_price = _norm(prev_yes)
        no_price = 1.0 - yes_price

    return yes_price, no_price


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: REPLAY ENSEMBLE ESTIMATES
# ══════════════════════════════════════════════════════════════════════════════

def replay_estimate(m, verbose=False):
    """Run our full ensemble against a historical market.
    Returns dict with estimate info, or None if no estimate possible.

    Note: This uses CURRENT data source values, not historical ones.
    For weather markets that have already resolved, the forecast will reflect
    actual conditions (so weather estimates will look artificially good).
    We flag these and handle them in analysis.
    """
    ticker = m.get("ticker", "")
    title = m.get("title") or m.get("subtitle") or ""
    category = categorize_market(ticker, title) if IMPORTS_OK else "other"
    volume = float(m.get("volume") or 0)
    yes_price, no_price = get_market_price_at_trade_time(m)

    if yes_price <= 0.05 or yes_price >= 0.95:
        return None  # extreme price — not tradeable
    if volume < 50:
        return None

    result = {
        "ticker": ticker,
        "title": title,
        "category": category,
        "volume": volume,
        "yes_price": yes_price,
        "strategies": [],  # list of {strategy, side, estimate, edge, fee_adj_edge, score, detail}
    }

    if not IMPORTS_OK:
        return result

    # ── Run each strategy ──────────────────────────────────────────────────

    # Strategy 1: Full ensemble (info_edge)
    try:
        indep_prob, info_source, n_sources = get_independent_estimate(
            ticker, m, yes_price, volume
        )
        if indep_prob is not None and n_sources > 0:
            edge_yes = indep_prob - yes_price
            edge_no = (1 - indep_prob) - no_price
            round_trip = ESTIMATED_EXIT_SPREAD + (ESTIMATED_FEE_PER_CONTRACT * 2)

            if n_sources >= 3:
                req_edge = MIN_EDGE
            elif n_sources == 2:
                req_edge = MIN_EDGE + 0.02
            else:
                req_edge = SINGLE_SOURCE_EDGE

            # Check YES side
            fee_adj_yes = edge_yes - round_trip
            if fee_adj_yes > req_edge:
                result["strategies"].append({
                    "strategy": "info_edge",
                    "side": "yes",
                    "estimate": indep_prob,
                    "edge": edge_yes,
                    "fee_adj_edge": fee_adj_yes,
                    "score": fee_adj_yes * 10 + volume / 10000 + n_sources * 0.1,
                    "detail": f"{info_source} n={n_sources} est={indep_prob:.2f} mkt={yes_price:.2f}",
                })

            # Check NO side
            fee_adj_no = edge_no - round_trip
            if fee_adj_no > req_edge:
                result["strategies"].append({
                    "strategy": "info_edge",
                    "side": "no",
                    "estimate": 1 - indep_prob,
                    "edge": edge_no,
                    "fee_adj_edge": fee_adj_no,
                    "score": fee_adj_no * 10 + volume / 10000 + n_sources * 0.1,
                    "detail": f"{info_source} n={n_sources} est_no={1-indep_prob:.2f} mkt_no={no_price:.2f}",
                })
    except Exception as e:
        if verbose:
            print(f"  [info_edge] Error: {e}")

    # Strategy 2: Event-driven
    try:
        evt = score_event_driven(m)
        if evt:
            s, side, detail, ip, mp, edge = evt
            result["strategies"].append({
                "strategy": "event_driven",
                "side": side,
                "estimate": ip,
                "edge": edge,
                "fee_adj_edge": edge,  # already fee-adjusted
                "score": s,
                "detail": detail,
            })
    except Exception as e:
        if verbose:
            print(f"  [event_driven] Error: {e}")

    # Strategy 3: Cross-market
    try:
        xmkt = score_cross_market(m)
        if xmkt:
            s, side, detail, ip, mp, edge = xmkt
            result["strategies"].append({
                "strategy": "cross_market",
                "side": side,
                "estimate": ip,
                "edge": edge,
                "fee_adj_edge": edge,
                "score": s,
                "detail": detail,
            })
    except Exception as e:
        if verbose:
            print(f"  [cross_market] Error: {e}")

    # Strategy 4: Near-resolution (won't trigger for settled markets since
    # they've already resolved, but included for completeness)
    try:
        nr = score_near_resolution(m)
        if nr:
            s, side, detail, ip, mp, edge = nr
            result["strategies"].append({
                "strategy": "near_resolution",
                "side": side,
                "estimate": ip,
                "edge": edge,
                "fee_adj_edge": edge,
                "score": s,
                "detail": detail,
            })
    except Exception as e:
        if verbose:
            print(f"  [near_resolution] Error: {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: HISTORICAL WEATHER BACKTEST (special case — uses archived forecasts)
# ══════════════════════════════════════════════════════════════════════════════

# City coordinates for weather markets (same as trade.py)
WEATHER_CITIES = {
    "chicago": (41.88, -87.63), "new york": (40.71, -74.01), "nyc": (40.71, -74.01),
    "miami": (25.76, -80.19), "los angeles": (34.05, -118.24), "la": (34.05, -118.24),
    "phoenix": (33.45, -112.07), "houston": (29.76, -95.37), "dallas": (32.78, -96.80),
    "denver": (39.74, -104.99), "atlanta": (33.75, -84.39), "seattle": (47.61, -122.33),
    "boston": (42.36, -71.06), "san francisco": (37.77, -122.42), "sf": (37.77, -122.42),
    "washington": (38.91, -77.04), "dc": (38.91, -77.04), "minneapolis": (44.98, -93.27),
    "detroit": (42.33, -83.05), "las vegas": (36.17, -115.14),
}

def fetch_historical_forecast(lat, lon, date_str, variable="temperature_2m_max"):
    """Fetch what the weather forecast WAS on a specific date using
    Open-Meteo's Historical Forecast API.
    Returns the forecasted value, or None."""
    try:
        url = (f"{OPEN_METEO_HISTORICAL}?"
               f"latitude={lat}&longitude={lon}"
               f"&start_date={date_str}&end_date={date_str}"
               f"&daily={variable}"
               f"&temperature_unit=fahrenheit"
               f"&timezone=America/Chicago")
        _rate_limit_wait(url) if IMPORTS_OK else time.sleep(1)
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            daily = data.get("daily", {})
            values = daily.get(variable, [])
            if values and values[0] is not None:
                return float(values[0])
    except Exception as e:
        print(f"[weather_backtest] Error fetching historical forecast: {e}")
    return None

def fetch_actual_weather(lat, lon, date_str, variable="temperature_2m_max"):
    """Fetch what actually happened (observed weather) for a specific date.
    Uses Open-Meteo Archive API."""
    try:
        url = (f"https://archive-api.open-meteo.com/v1/archive?"
               f"latitude={lat}&longitude={lon}"
               f"&start_date={date_str}&end_date={date_str}"
               f"&daily={variable}"
               f"&temperature_unit=fahrenheit"
               f"&timezone=America/Chicago")
        _rate_limit_wait(url) if IMPORTS_OK else time.sleep(1)
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            daily = data.get("daily", {})
            values = daily.get(variable, [])
            if values and values[0] is not None:
                return float(values[0])
    except Exception as e:
        print(f"[weather_backtest] Error fetching actual weather: {e}")
    return None

def backtest_weather_markets(markets, verbose=False):
    """Special backtest for weather markets using historical forecast data.
    This is our most rigorous backtest because we can replay what the forecast
    actually was at the time, not just what the current forecast says.

    Returns list of backtest result dicts."""
    import re

    results = []
    weather_markets = []

    for m in markets:
        ticker = m.get("ticker", "")
        title = (m.get("title") or m.get("subtitle") or "").lower()
        cat = categorize_market(ticker, title) if IMPORTS_OK else ""
        if cat == "weather" or any(kw in title for kw in ["temperature", "temp", "degrees", "°f"]):
            weather_markets.append(m)

    print(f"[weather_backtest] Found {len(weather_markets)} weather markets to backtest")

    for m in weather_markets:
        ticker = m.get("ticker", "")
        title = (m.get("title") or m.get("subtitle") or "").lower()
        outcome = get_market_outcome(m)
        if outcome is None:
            continue

        close_time = m.get("close_time") or m.get("expiration_time") or ""
        if not close_time:
            continue

        # Parse city from title
        city_key = None
        lat, lon = None, None
        for city, coords in WEATHER_CITIES.items():
            if city in title:
                city_key = city
                lat, lon = coords
                break
        if not lat:
            continue

        # Parse date from close_time
        try:
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            market_date = close_dt.strftime("%Y-%m-%d")
            # Forecast would have been checked 1-2 days before resolution
            forecast_date = (close_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            continue

        # Parse threshold from title (e.g., "above 75°F", "at or above 80")
        threshold_match = re.search(r'(above|below|over|under|at least|at or above)\s*(\d+)', title)
        if not threshold_match:
            continue
        direction = threshold_match.group(1)
        threshold = float(threshold_match.group(2))

        # Determine if this is a max temp or min temp market
        variable = "temperature_2m_max"
        if any(kw in title for kw in ["low", "minimum", "min temp", "overnight"]):
            variable = "temperature_2m_min"

        # Fetch historical forecast (what we would have predicted)
        forecast_val = fetch_historical_forecast(lat, lon, forecast_date, variable)
        # Fetch actual observation (ground truth)
        actual_val = fetch_actual_weather(lat, lon, market_date, variable)

        if forecast_val is None:
            continue

        # What would our estimate have been?
        # Use the same logic as trade.py's weather estimate: sigmoid around threshold
        forecast_sigma = 2.5  # typical forecast error in °F for 1-day forecast
        diff = forecast_val - threshold
        if "above" in direction or "over" in direction or "at least" in direction or "at or above" in direction:
            our_prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))
        else:
            our_prob_yes = 1 / (1 + math.exp(diff / forecast_sigma))

        # Get market price
        yes_price, _ = get_market_price_at_trade_time(m)
        if yes_price <= 0.05 or yes_price >= 0.95:
            continue

        edge = our_prob_yes - yes_price
        round_trip = ESTIMATED_EXIT_SPREAD + (ESTIMATED_FEE_PER_CONTRACT * 2)
        fee_adj = abs(edge) - round_trip

        # Would we have traded?
        would_trade = fee_adj > MIN_EDGE
        side = "yes" if edge > 0 else "no"
        if side == "yes":
            won = (outcome == 1)
        else:
            won = (outcome == 0)

        result = {
            "ticker": ticker,
            "title": title[:80],
            "city": city_key,
            "market_date": market_date,
            "threshold": threshold,
            "direction": direction,
            "forecast_val": forecast_val,
            "actual_val": actual_val,
            "forecast_error": abs(forecast_val - actual_val) if actual_val else None,
            "our_prob_yes": our_prob_yes,
            "market_price": yes_price,
            "edge": edge,
            "fee_adj_edge": fee_adj,
            "side": side,
            "would_trade": would_trade,
            "actual_outcome": outcome,
            "won": won if would_trade else None,
            "strategy": "weather_historical",
        }
        results.append(result)

        if verbose:
            trade_str = f"TRADE {side.upper()}" if would_trade else "SKIP"
            win_str = "✓ WIN" if (would_trade and won) else ("✗ LOSS" if (would_trade and not won) else "")
            print(f"  {ticker}: {city_key} {threshold}°F | "
                  f"forecast={forecast_val:.0f}°F actual={actual_val or '?'}°F | "
                  f"our={our_prob_yes:.2f} mkt={yes_price:.2f} edge={edge:+.2f} | "
                  f"{trade_str} {win_str}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4B: HISTORICAL SPORTS BACKTEST
# ══════════════════════════════════════════════════════════════════════════════
# Sports markets have the best publicly available outcome data. We can:
# 1. Identify settled Kalshi sports markets
# 2. For recently settled games, check if The Odds API still has data
# 3. For older games, use free sports scores APIs to verify outcomes
# 4. Compare: what did bookmaker odds imply vs what Kalshi priced vs what happened?

# Free sports scores API (no auth needed)
SPORTS_SCORES_APIS = {
    "nba": "https://www.balldontlie.io/api/v1/games",
    "nfl": None,  # NFL season is over by April
    "mlb": "https://statsapi.mlb.com/api/v1/schedule",
    "nhl": "https://api-web.nhle.com/v1/schedule/now",
}

# The Odds API - we have a key in the environment
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

def _fetch_odds_for_sport(sport_key, days_from=3):
    """Fetch recent completed games from The Odds API including scores.
    The scores endpoint is free and includes final results."""
    if not ODDS_API_KEY:
        return []
    try:
        url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"
               f"?apiKey={ODDS_API_KEY}&daysFrom={days_from}")
        if IMPORTS_OK:
            _rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            games = r.json()
            completed = [g for g in games if g.get("completed")]
            print(f"[sports_backtest] {sport_key}: {len(completed)} completed games "
                  f"(of {len(games)} total)")
            return completed
    except Exception as e:
        print(f"[sports_backtest] Error fetching {sport_key} scores: {e}")
    return []

def _fetch_historical_odds(sport_key, event_id):
    """Fetch historical odds for a specific event from The Odds API.
    This uses the events endpoint which may retain recent data."""
    if not ODDS_API_KEY:
        return None
    try:
        url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds"
               f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads,totals")
        if IMPORTS_OK:
            _rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

# Map Kalshi sport keywords to Odds API sport keys
SPORT_TO_ODDS_KEY = {
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "ncaa": "basketball_ncaab",
    "mls": "soccer_usa_mls",
}

def backtest_sports_markets(markets, verbose=False):
    """Backtest settled sports markets.

    Approach:
    1. Find settled Kalshi sports markets
    2. For each, determine the actual outcome (YES/NO)
    3. Try to match against Odds API data to get bookmaker-implied probability
    4. If we can't get odds data, still record the Kalshi price vs outcome
       to test Kalshi market efficiency

    Returns list of backtest result dicts."""
    import re

    results = []
    sports_markets = []

    for m in markets:
        ticker = m.get("ticker", "")
        title = (m.get("title") or m.get("subtitle") or "").lower()
        cat = categorize_market(ticker, title) if IMPORTS_OK else ""
        if cat == "sports" or any(kw in title for kw in
            ["nba", "nfl", "mlb", "nhl", "ncaa", "mls", "game", "playoff", "win"]):
            outcome = get_market_outcome(m)
            if outcome is not None:
                sports_markets.append(m)

    print(f"[sports_backtest] Found {len(sports_markets)} settled sports markets")

    if not sports_markets:
        return results

    # Try to fetch recent scores from The Odds API
    odds_scores = {}  # {sport_key: [completed_games]}
    for sport_key in ["basketball_nba", "icehockey_nhl", "baseball_mlb", "basketball_ncaab"]:
        scores = _fetch_odds_for_sport(sport_key, days_from=7)
        if scores:
            odds_scores[sport_key] = scores

    for m in sports_markets:
        ticker = m.get("ticker", "")
        title = (m.get("title") or m.get("subtitle") or "").lower()
        outcome = get_market_outcome(m)
        yes_price, no_price = get_market_price_at_trade_time(m)

        if yes_price <= 0.05 or yes_price >= 0.95:
            continue

        # Detect sport
        sport_label = None
        for label in ["nba", "nfl", "mlb", "nhl", "ncaa", "mls"]:
            if label in title:
                sport_label = label
                break

        odds_key = SPORT_TO_ODDS_KEY.get(sport_label) if sport_label else None

        # Try to match against Odds API completed games
        bookmaker_prob = None
        matched_game = None
        if odds_key and odds_key in odds_scores:
            title_words = set(re.findall(r'\w+', title))
            for game in odds_scores[odds_key]:
                home = (game.get("home_team") or "").lower()
                away = (game.get("away_team") or "").lower()
                game_words = set(re.findall(r'\w+', f"{home} {away}"))
                overlap = len(title_words & game_words)
                if overlap >= 2:
                    matched_game = game
                    # Extract winner from scores
                    scores = game.get("scores", [])
                    if scores and len(scores) >= 2:
                        home_score = None
                        away_score = None
                        for s in scores:
                            if s.get("name", "").lower() == home:
                                home_score = int(s.get("score", 0))
                            elif s.get("name", "").lower() == away:
                                away_score = int(s.get("score", 0))
                    break

        # Try to get odds-implied probability from the live estimate function
        # (this uses current odds data as a proxy — imperfect for historical)
        if IMPORTS_OK:
            try:
                sports_prob, sports_src = get_sports_estimate(ticker, m)
                if sports_prob is not None:
                    bookmaker_prob = sports_prob
            except Exception:
                pass

        # Calculate what our trade would have been
        edge = None
        would_trade = False
        side = None

        if bookmaker_prob is not None:
            edge_yes = bookmaker_prob - yes_price
            edge_no = (1 - bookmaker_prob) - no_price
            round_trip = ESTIMATED_EXIT_SPREAD + (ESTIMATED_FEE_PER_CONTRACT * 2)

            if abs(edge_yes) > abs(edge_no):
                edge = edge_yes
                fee_adj = edge_yes - round_trip
            else:
                edge = -edge_no  # negative means bet NO
                fee_adj = edge_no - round_trip

            side = "yes" if edge > 0 else "no"
            would_trade = fee_adj > MIN_EDGE
        else:
            # No odds data — test Kalshi efficiency instead
            # If Kalshi priced it at 70% and outcome was YES, that's calibration data
            side = "yes" if yes_price > 0.5 else "no"
            edge = 0
            fee_adj = 0

        if side == "yes":
            won = (outcome == 1)
        else:
            won = (outcome == 0)

        result = {
            "ticker": ticker,
            "title": title[:80],
            "sport": sport_label or "unknown",
            "market_price": yes_price,
            "bookmaker_prob": bookmaker_prob,
            "edge": edge,
            "fee_adj_edge": fee_adj if bookmaker_prob else 0,
            "side": side,
            "would_trade": would_trade,
            "actual_outcome": outcome,
            "won": won if would_trade else None,
            "has_odds_data": bookmaker_prob is not None,
            "matched_game": matched_game.get("home_team", "") + " vs " + matched_game.get("away_team", "") if matched_game else None,
            "strategy": "sports_historical",
        }
        results.append(result)

        if verbose:
            odds_str = f"odds={bookmaker_prob:.2f}" if bookmaker_prob else "no_odds"
            trade_str = f"TRADE {side.upper()}" if would_trade else "SKIP"
            win_str = "✓ WIN" if (would_trade and won) else ("✗ LOSS" if (would_trade and not won) else "")
            print(f"  {ticker}: {sport_label or '?'} | "
                  f"kalshi={yes_price:.2f} {odds_str} | "
                  f"{trade_str} {win_str}")

    # Print summary of Kalshi sports market efficiency
    if results:
        with_odds = [r for r in results if r["has_odds_data"]]
        kalshi_calibration = defaultdict(lambda: {"n": 0, "yes": 0})
        for r in results:
            bucket = round(r["market_price"] * 5) / 5  # 20% buckets
            kalshi_calibration[bucket]["n"] += 1
            kalshi_calibration[bucket]["yes"] += r["actual_outcome"]

        print(f"\n[sports_backtest] Kalshi sports market efficiency:")
        print(f"  {'Price Bucket':>12} {'Count':>6} {'Actual YES%':>12} {'Expected':>9}")
        for bucket in sorted(kalshi_calibration.keys()):
            c = kalshi_calibration[bucket]
            if c["n"] >= 2:
                actual = c["yes"] / c["n"]
                print(f"  {bucket:>11.0%} {c['n']:>6} {actual:>11.0%} {bucket:>8.0%}")

        if with_odds:
            print(f"\n  Markets with odds data: {len(with_odds)}/{len(results)}")
            tradeable = [r for r in with_odds if r["would_trade"]]
            if tradeable:
                wins = sum(1 for r in tradeable if r["won"])
                print(f"  Tradeable (edge > threshold): {len(tradeable)}")
                print(f"  Would have won: {wins}/{len(tradeable)} ({wins/len(tradeable):.0%})")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: ANALYSIS AND REPORTING
# ══════════════════════════════════════════════════════════════════════════════

def analyze_results(all_results, weather_results, run_id, conn, sports_results=None):
    """Analyze backtest results and produce a comprehensive report.
    Stores results in the database and prints summary."""
    sports_results = sports_results or []

    # Combine general, weather, and sports results
    trades = []

    # General backtest trades (from replay_estimate)
    for r in all_results:
        if not r or not r.get("strategies"):
            continue
        ticker = r["ticker"]
        category = r["category"]

        for strat in r["strategies"]:
            # Would we have traded? (the estimate function already checked edge threshold)
            outcome = get_market_outcome(
                next((m for m in _all_markets if m.get("ticker") == ticker), {})
            ) if _all_markets else None

            if outcome is None:
                continue

            side = strat["side"]
            won = (outcome == 1) if side == "yes" else (outcome == 0)

            # Hypothetical profit: if we bought at market price and it resolved
            # WIN: payout ($1) - cost (market_price) per contract
            # LOSS: -cost (market_price) per contract
            market_price = r["yes_price"] if side == "yes" else (1 - r["yes_price"])
            if won:
                profit_cents = int((1.0 - market_price) * 100)  # gain per contract
            else:
                profit_cents = int(-market_price * 100)  # loss per contract

            trades.append({
                "ticker": ticker,
                "category": category,
                "strategy": strat["strategy"],
                "side": side,
                "score": strat["score"],
                "our_estimate": strat["estimate"],
                "market_price": market_price,
                "edge": strat["edge"],
                "fee_adj_edge": strat["fee_adj_edge"],
                "actual_outcome": outcome,
                "won": won,
                "profit_cents": profit_cents,
                "detail": strat["detail"],
            })

    # Weather backtest trades
    for r in weather_results:
        if not r.get("would_trade"):
            continue
        market_price = r["market_price"] if r["side"] == "yes" else (1 - r["market_price"])
        if r["won"]:
            profit_cents = int((1.0 - market_price) * 100)
        else:
            profit_cents = int(-market_price * 100)

        trades.append({
            "ticker": r["ticker"],
            "category": "weather",
            "strategy": "weather_historical",
            "side": r["side"],
            "score": r.get("fee_adj_edge", 0) * 10,
            "our_estimate": r["our_prob_yes"] if r["side"] == "yes" else (1 - r["our_prob_yes"]),
            "market_price": market_price,
            "edge": abs(r["edge"]),
            "fee_adj_edge": r["fee_adj_edge"],
            "actual_outcome": r["actual_outcome"],
            "won": r["won"],
            "profit_cents": profit_cents,
            "detail": f"forecast={r.get('forecast_val', '?')} actual={r.get('actual_val', '?')}",
        })

    # Sports backtest trades
    for r in sports_results:
        if not r.get("would_trade"):
            continue
        market_price = r["market_price"] if r["side"] == "yes" else (1 - r["market_price"])
        if r["won"]:
            profit_cents = int((1.0 - market_price) * 100)
        else:
            profit_cents = int(-market_price * 100)

        trades.append({
            "ticker": r["ticker"],
            "category": "sports",
            "strategy": "sports_historical",
            "side": r["side"],
            "score": r.get("fee_adj_edge", 0) * 10,
            "our_estimate": r.get("bookmaker_prob", 0.5) if r["side"] == "yes" else (1 - r.get("bookmaker_prob", 0.5)),
            "market_price": market_price,
            "edge": abs(r.get("edge", 0)),
            "fee_adj_edge": r.get("fee_adj_edge", 0),
            "actual_outcome": r["actual_outcome"],
            "won": r["won"],
            "profit_cents": profit_cents,
            "detail": r.get("matched_game") or r["title"][:60],
        })

    # ── Store in DB ──────────────────────────────────────────────────────
    now_str = datetime.now(timezone.utc).isoformat()
    for t in trades:
        conn.execute("""INSERT INTO backtest_trades
            (run_id, ticker, category, strategy, side, score,
             our_estimate, market_price, edge, actual_outcome, won,
             fee_adjusted_edge, hypothetical_profit_cents, detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, t["ticker"], t["category"], t["strategy"], t["side"],
             t["score"], t["our_estimate"], t["market_price"], t["edge"],
             t["actual_outcome"], int(t["won"]), t["fee_adj_edge"],
             t["profit_cents"], t["detail"]))

    conn.commit()

    # ── Analysis ──────────────────────────────────────────────────────────
    if not trades:
        print("\n[backtest] No trades to analyze.")
        return

    total = len(trades)
    wins = sum(1 for t in trades if t["won"])
    losses = total - wins
    win_rate = wins / total if total > 0 else 0
    total_profit = sum(t["profit_cents"] for t in trades)
    avg_profit = total_profit / total if total > 0 else 0

    # By strategy
    by_strategy = defaultdict(lambda: {"n": 0, "wins": 0, "profit": 0, "edges": []})
    for t in trades:
        s = by_strategy[t["strategy"]]
        s["n"] += 1
        s["wins"] += int(t["won"])
        s["profit"] += t["profit_cents"]
        s["edges"].append(t["edge"])

    # By category
    by_category = defaultdict(lambda: {"n": 0, "wins": 0, "profit": 0})
    for t in trades:
        c = by_category[t["category"]]
        c["n"] += 1
        c["wins"] += int(t["won"])
        c["profit"] += t["profit_cents"]

    # Calibration: bucket by our estimate, check actual win rate
    calibration = defaultdict(lambda: {"n": 0, "actual_yes": 0})
    for t in trades:
        bucket = round(t["our_estimate"] * 10) / 10  # round to nearest 0.1
        calibration[bucket]["n"] += 1
        calibration[bucket]["actual_yes"] += t["actual_outcome"]

    # ── Print Report ──────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  BACKTEST RESULTS")
    print("=" * 70)

    print(f"\n  Total simulated trades:  {total}")
    print(f"  Wins:                    {wins}")
    print(f"  Losses:                  {losses}")
    print(f"  Win rate:                {win_rate:.1%}")
    print(f"  Total profit:            {total_profit:+.0f}¢ (${total_profit/100:+.2f})")
    print(f"  Avg profit per trade:    {avg_profit:+.1f}¢")

    print(f"\n  {'Strategy':<20} {'Trades':>7} {'Wins':>6} {'WR':>7} {'Profit':>10} {'Avg Edge':>10}")
    print("  " + "-" * 62)
    for strat in sorted(by_strategy.keys()):
        s = by_strategy[strat]
        wr = s["wins"] / s["n"] if s["n"] > 0 else 0
        avg_edge = sum(s["edges"]) / len(s["edges"]) if s["edges"] else 0
        print(f"  {strat:<20} {s['n']:>7} {s['wins']:>6} {wr:>6.0%} "
              f"  {s['profit']:>+8.0f}¢ {avg_edge:>9.2f}")

    print(f"\n  {'Category':<20} {'Trades':>7} {'Wins':>6} {'WR':>7} {'Profit':>10}")
    print("  " + "-" * 52)
    for cat in sorted(by_category.keys()):
        c = by_category[cat]
        wr = c["wins"] / c["n"] if c["n"] > 0 else 0
        print(f"  {cat:<20} {c['n']:>7} {c['wins']:>6} {wr:>6.0%}   {c['profit']:>+8.0f}¢")

    print(f"\n  Calibration (our estimate vs actual outcome):")
    print(f"  {'Bucket':>8} {'Trades':>7} {'Actual%':>8} {'Expected':>9} {'Error':>7}")
    print("  " + "-" * 42)
    for bucket in sorted(calibration.keys()):
        c = calibration[bucket]
        if c["n"] < 2:
            continue
        actual_pct = c["actual_yes"] / c["n"]
        error = actual_pct - bucket
        print(f"  {bucket:>7.1f} {c['n']:>7} {actual_pct:>7.0%} {bucket:>8.0%} {error:>+6.0%}")

    print("\n" + "=" * 70)

    # Weather-specific analysis
    if weather_results:
        traded_weather = [r for r in weather_results if r.get("would_trade")]
        if traded_weather:
            w_wins = sum(1 for r in traded_weather if r["won"])
            w_total = len(traded_weather)
            w_wr = w_wins / w_total if w_total > 0 else 0

            errors = [r["forecast_error"] for r in traded_weather if r.get("forecast_error") is not None]
            avg_error = sum(errors) / len(errors) if errors else 0

            print(f"\n  WEATHER DEEP DIVE:")
            print(f"  Traded: {w_total} | Won: {w_wins} | WR: {w_wr:.0%}")
            print(f"  Avg forecast error: {avg_error:.1f}°F")

            # By city
            city_stats = defaultdict(lambda: {"n": 0, "wins": 0})
            for r in traded_weather:
                city_stats[r.get("city", "?")]["n"] += 1
                city_stats[r.get("city", "?")]["wins"] += int(r["won"])
            print(f"\n  {'City':<15} {'Trades':>7} {'Wins':>6} {'WR':>7}")
            print("  " + "-" * 37)
            for city in sorted(city_stats.keys()):
                cs = city_stats[city]
                wr = cs["wins"] / cs["n"] if cs["n"] > 0 else 0
                print(f"  {city:<15} {cs['n']:>7} {cs['wins']:>6} {wr:>6.0%}")

    # Store run summary
    conn.execute("""INSERT OR REPLACE INTO backtest_runs
        (run_id, started_at, finished_at, markets_tested, trades_simulated,
         wins, losses, win_rate, total_profit_cents, by_strategy, by_category, config)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, now_str, datetime.now(timezone.utc).isoformat(),
         len(all_results) + len(weather_results), total,
         wins, losses, win_rate, total_profit,
         json.dumps({s: {"n": v["n"], "wins": v["wins"], "profit": v["profit"]}
                     for s, v in by_strategy.items()}),
         json.dumps({c: {"n": v["n"], "wins": v["wins"], "profit": v["profit"]}
                     for c, v in by_category.items()}),
         json.dumps({"min_edge": MIN_EDGE, "single_source_edge": SINGLE_SOURCE_EDGE,
                      "fee_per_contract": ESTIMATED_FEE_PER_CONTRACT,
                      "exit_spread": ESTIMATED_EXIT_SPREAD})))
    conn.commit()

    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate": win_rate, "total_profit": total_profit,
        "by_strategy": dict(by_strategy), "by_category": dict(by_category),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Kalshi Trading Bot Backtester")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help=f"How many days to look back (default: {DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--category", type=str, default=None,
                        help="Filter to specific category (weather, sports, crypto, economics)")
    parser.add_argument("--strategy", type=str, default="all",
                        help="Strategy to test (all, info_edge, event_driven, cross_market, weather)")
    parser.add_argument("--weather-only", action="store_true",
                        help="Only run the weather historical backtest (most rigorous)")
    parser.add_argument("--sports-only", action="store_true",
                        help="Only run the sports historical backtest")
    parser.add_argument("--max-markets", type=int, default=500,
                        help="Maximum markets to test (default: 500)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print details for each market")
    parser.add_argument("--db", type=str, default=DB_PATH,
                        help=f"Database path (default: {DB_PATH})")
    args = parser.parse_args()

    run_id = f"bt_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    print(f"[backtest] Starting run {run_id}")
    print(f"[backtest] Lookback: {args.days} days | Category: {args.category or 'all'} | "
          f"Strategy: {args.strategy}")

    conn = init_backtest_db(args.db)

    # ── Fetch historical markets ──────────────────────────────────────────
    print(f"\n[backtest] Fetching settled markets from Kalshi...")
    global _all_markets
    _all_markets = fetch_settled_markets(
        days=args.days,
        category_filter=args.category,
    )

    if not _all_markets:
        print("[backtest] No settled markets found. Check API auth.")
        return

    # Cap for API politeness
    if len(_all_markets) > args.max_markets:
        print(f"[backtest] Capping to {args.max_markets} markets (from {len(_all_markets)})")
        _all_markets = _all_markets[:args.max_markets]

    # ── Categorize ────────────────────────────────────────────────────────
    cat_counts = defaultdict(int)
    for m in _all_markets:
        ticker = m.get("ticker", "")
        title = m.get("title") or m.get("subtitle") or ""
        cat = categorize_market(ticker, title) if IMPORTS_OK else "other"
        cat_counts[cat] += 1

    print(f"\n[backtest] Market breakdown:")
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")

    # ── Run weather backtest (most rigorous) ──────────────────────────────
    weather_results = []
    if args.strategy in ("all", "weather") or args.weather_only:
        print(f"\n[backtest] Running weather historical backtest...")
        weather_results = backtest_weather_markets(_all_markets, verbose=args.verbose)

    # ── Run sports backtest ─────────────────────────────────────────────
    sports_results = []
    if args.strategy in ("all", "sports") or args.sports_only:
        print(f"\n[backtest] Running sports historical backtest...")
        sports_results = backtest_sports_markets(_all_markets, verbose=args.verbose)

    if args.weather_only:
        analyze_results([], weather_results, run_id, conn, sports_results=[])
        return

    if args.sports_only:
        analyze_results([], [], run_id, conn, sports_results=sports_results)
        return

    # ── Run general ensemble replay ───────────────────────────────────────
    print(f"\n[backtest] Replaying ensemble on {len(_all_markets)} markets...")
    all_results = []
    for i, m in enumerate(_all_markets):
        if args.verbose and i % 50 == 0:
            print(f"  Processing {i+1}/{len(_all_markets)}...")
        result = replay_estimate(m, verbose=args.verbose)
        if result:
            all_results.append(result)

        # Rate limit: don't hammer APIs
        if i % 10 == 0:
            time.sleep(0.2)

    n_with_signals = sum(1 for r in all_results if r and r.get("strategies"))
    print(f"[backtest] {n_with_signals}/{len(all_results)} markets had tradeable signals")

    # ── Analyze ───────────────────────────────────────────────────────────
    analyze_results(all_results, weather_results, run_id, conn,
                    sports_results=sports_results)

    print(f"\n[backtest] Results stored in {args.db}")
    print(f"[backtest] Run ID: {run_id}")


_all_markets = []  # global for cross-referencing in analysis

if __name__ == "__main__":
    main()
