"""Sports odds source: The-Odds-API for moneyline, totals, and spreads.

Extracted from trade.py. Matches Kalshi market titles to live sports odds
for h2h, totals, and spreads markets.
"""

from __future__ import annotations

import re
import time

import requests

from bot.api import rate_limit_wait
from bot.config import ODDS_API_KEY


# ══════════════════════════════════════════════════════════════════════════════
# SPORTS — The-Odds-API (free tier, 500 credits/month)
# ══════════════════════════════════════════════════════════════════════════════

SPORT_KEYS = {
    "nba": "basketball_nba",  "nfl": "americanfootball_nfl",
    "mlb": "baseball_mlb",    "nhl": "icehockey_nhl",
    "ncaa": "americanfootball_ncaaf", "mls": "soccer_usa_mls",
    "epl": "soccer_epl",      "nascar": "motorsport_nascar_cup",
}

_ODDS_CACHE = {}  # {sport_label: (data, timestamp)}


def _load_sport_odds(sport_label, market_types="h2h,totals,spreads"):
    """Load odds for a SINGLE sport on demand. Cache per-sport for 30 min to save API credits.
    Now fetches h2h, totals, and spreads in a single call (1 credit, 3 market types).
    Old approach loaded all 8 sports at once, burning ~8 credits per cache miss.
    With 500 credits/month free tier, that exhausted the budget in 1-2 days."""
    now = time.time()
    cache_key = f"{sport_label}_{market_types}"
    if cache_key in _ODDS_CACHE:
        data, ts = _ODDS_CACHE[cache_key]
        if now - ts < 1800:  # 30 min cache
            return data
    if not ODDS_API_KEY:
        return []
    sport_key = SPORT_KEYS.get(sport_label)
    if not sport_key:
        return []
    try:
        url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?"
               f"apiKey={ODDS_API_KEY}&regions=us&markets={market_types}&oddsFormat=decimal")
        rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            _ODDS_CACHE[cache_key] = (data, now)
            # Also cache under the base label for backwards compat
            _ODDS_CACHE[sport_label] = (data, now)
            print(f"[odds] Loaded {len(data)} games for {sport_label} (markets={market_types})")
            return data
    except Exception as e:
        print(f"[odds] Failed to load {sport_label}: {e}")
    return []


def _load_sports_odds():
    """Compat wrapper — returns dict of all cached sports data."""
    return {label: data for label, (data, ts) in _ODDS_CACHE.items()}


def get_sports_estimate(ticker, market_data):
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Detect sport from title keywords first (free — no API call)
    sport = None
    for label in SPORT_KEYS:
        if label in title: sport = label; break

    # If no sport keyword found, don't do expensive team-name search across all sports.
    # Only scan cached sports data (no new API calls).
    if not sport:
        cached = _load_sports_odds()
        for label, games in cached.items():
            for game in games:
                home = (game.get("home_team") or "").lower()
                away = (game.get("away_team") or "").lower()
                if (home and home in title) or (away and away in title):
                    sport = label; break
            if sport: break

    if not sport: return None, None

    # Lazy-load only the matched sport (saves API credits)
    games = _load_sport_odds(sport)
    if not games: return None, None

    # Find matching game
    title_words = set(re.findall(r'\w+', title))
    best_game = None
    best_overlap = 0

    for game in games:
        home = (game.get("home_team") or "").lower()
        away = (game.get("away_team") or "").lower()
        game_words = set(re.findall(r'\w+', f"{home} {away}"))
        overlap = len(title_words & game_words)
        if overlap > best_overlap:
            best_overlap = overlap
            best_game = game

    if not best_game or best_overlap < 2:
        return None, None

    bookmakers = best_game.get("bookmakers", [])
    if not bookmakers: return None, None

    # -- Detect market type from Kalshi title --
    # "Will the total be over 210?" -> totals market
    # "Will Lakers win by more than 5?" -> spreads market
    # "Will Lakers win?" -> h2h market
    totals_match = re.search(r'(over|under|total|combined)\s+(\d+\.?\d*)', title)
    spread_match = re.search(r'(spread|by more than|by at least|margin)\s+(\d+\.?\d*)', title)

    prob = None
    detail_str = ""

    if totals_match:
        # -- Totals market --
        direction = totals_match.group(1)
        threshold = float(totals_match.group(2))
        is_over = direction in ("over", "total", "combined")

        for bm in bookmakers:
            for mkt in bm.get("markets", []):
                if mkt.get("key") == "totals":
                    for outcome in mkt.get("outcomes", []):
                        point = float(outcome.get("point", 0))
                        price = float(outcome.get("price", 0))
                        name = (outcome.get("name") or "").lower()
                        # Match the closest point to our threshold
                        if abs(point - threshold) <= 1.5 and price > 0:
                            impl_prob = 1 / price
                            if (is_over and name == "over") or (not is_over and name == "under"):
                                prob = impl_prob
                                detail_str = f"totals:{point}"
                                break
                    if prob: break
            if prob: break

        # Normalize if we got a probability > 1 (overround)
        if prob and prob > 0.99:
            prob = 0.95

    elif spread_match:
        # -- Spreads market --
        spread_val = float(spread_match.group(2))
        home = (best_game.get("home_team") or "").lower()
        away = (best_game.get("away_team") or "").lower()
        target_team = home if home in title else (away if away in title else None)

        if target_team:
            for bm in bookmakers:
                for mkt in bm.get("markets", []):
                    if mkt.get("key") == "spreads":
                        for outcome in mkt.get("outcomes", []):
                            name = (outcome.get("name") or "").lower()
                            point = abs(float(outcome.get("point", 0)))
                            price = float(outcome.get("price", 0))
                            if name == target_team and abs(point - spread_val) <= 1.5 and price > 0:
                                prob = 1 / price
                                detail_str = f"spreads:{target_team}@{point}"
                                break
                        if prob: break
                if prob: break

            if prob and prob > 0.99:
                prob = 0.95

    if prob is None:
        # -- H2H (moneyline) market -- original logic --
        h2h = None
        for bm in bookmakers:
            for mkt in bm.get("markets", []):
                if mkt.get("key") == "h2h":
                    h2h = mkt.get("outcomes", [])
                    break
            if h2h: break

        if not h2h or len(h2h) < 2: return None, None

        probs = {}
        total_impl = 0
        for outcome in h2h:
            name = (outcome.get("name") or "").lower()
            price = float(outcome.get("price", 0))
            if price > 0:
                impl = 1 / price
                probs[name] = impl
                total_impl += impl
        if total_impl > 0:
            for name in probs:
                probs[name] /= total_impl

        home = (best_game.get("home_team") or "").lower()
        away = (best_game.get("away_team") or "").lower()
        if home in title:
            prob = probs.get(home, probs.get(best_game.get("home_team","").lower()))
        elif away in title:
            prob = probs.get(away, probs.get(best_game.get("away_team","").lower()))
        else:
            print(f"[odds] Can't determine team for '{title[:50]}' — skipping")
            return None, None
        detail_str = "h2h"

    if prob is None: return None, None

    teams = f"{best_game.get('home_team','')} vs {best_game.get('away_team','')}"
    print(f"[odds] Match: '{title[:50]}' → {teams} ({detail_str}) prob={prob:.2f}")
    return prob, f"odds:{teams[:30]}:{detail_str}"
