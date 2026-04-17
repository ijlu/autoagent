"""Prediction market cross-reference sources: Polymarket and Metaculus.

Extracted from trade.py. Provides independent price signals by fuzzy-matching
Kalshi markets against Polymarket and Metaculus questions.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

import requests

from bot.api import rate_limit_wait


# ══════════════════════════════════════════════════════════════════════════════
# POLYMARKET — cross-market arbitrage
# ══════════════════════════════════════════════════════════════════════════════

_POLY_MARKETS = None
_POLY_TS = 0


def _load_polymarket():
    """Fetch active Polymarket markets. Cache for 5 min since it's a big list."""
    global _POLY_MARKETS, _POLY_TS
    now = time.time()
    if _POLY_MARKETS is not None and now - _POLY_TS < 300:
        return _POLY_MARKETS
    try:
        rate_limit_wait("https://gamma-api.polymarket.com/markets")
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&limit=500",
                         timeout=10)
        markets = r.json()
        _POLY_MARKETS = markets if isinstance(markets, list) else []
        _POLY_TS = now
        print(f"[poly] Loaded {len(_POLY_MARKETS)} Polymarket markets")
        return _POLY_MARKETS
    except Exception as e:
        print(f"[poly] Failed to load: {e}")
        return []


def _fuzzy_match_polymarket(kalshi_title):
    """Find best Polymarket match for a Kalshi market title."""
    if not kalshi_title:
        return None
    poly_markets = _load_polymarket()
    if not poly_markets:
        return None

    kalshi_lower = kalshi_title.lower()
    # Extract key terms (skip common words)
    stop = {"will","the","be","a","an","in","on","at","to","of","by","for","is","it","or","and"}
    kalshi_words = set(w for w in re.findall(r'\w+', kalshi_lower) if w not in stop and len(w) > 2)

    best_match = None
    best_score = 0

    for pm in poly_markets:
        pm_title = (pm.get("question") or pm.get("title") or "").lower()
        if not pm_title:
            continue
        pm_words = set(w for w in re.findall(r'\w+', pm_title) if w not in stop and len(w) > 2)
        if not pm_words:
            continue
        # Jaccard similarity
        overlap = len(kalshi_words & pm_words)
        union = len(kalshi_words | pm_words)
        score = overlap / union if union > 0 else 0
        if score > best_score and score > 0.60:  # require 60% word overlap (tightened to reduce false matches)
            best_score = score
            best_match = pm

    return best_match


def _validate_polymarket_match(kalshi_market, poly_match):
    """Structural validation beyond title matching: check resolution timing
    and outcome structure are compatible. Returns True if match is trustworthy."""
    # 1. Check resolution date proximity — markets should resolve around the same time
    kalshi_close = (kalshi_market.get("close_time") or kalshi_market.get("expiration_time")
                    or kalshi_market.get("expected_expiration_time") or "")
    poly_close = poly_match.get("endDate") or poly_match.get("end_date_iso") or ""
    if kalshi_close and poly_close:
        try:
            k_dt = datetime.fromisoformat(kalshi_close.replace("Z", "+00:00"))
            p_dt = datetime.fromisoformat(poly_close.replace("Z", "+00:00"))
            days_apart = abs((k_dt - p_dt).total_seconds()) / 86400
            if days_apart > 14:
                # Markets resolve more than 2 weeks apart — likely different events
                return False
        except Exception:
            pass

    # 2. Check that Polymarket market is binary (2 outcomes) — matching multi-outcome
    # markets to binary Kalshi markets creates mismatches
    outcomes = poly_match.get("outcomes") or poly_match.get("outcomePrices")
    if outcomes:
        try:
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if len(outcomes) > 2:
                return False  # multi-outcome market, not a clean binary match
        except Exception:
            pass

    # 3. Check liquidity — very illiquid Polymarket markets have unreliable prices
    poly_volume = float(poly_match.get("volume") or poly_match.get("volumeNum") or 0)
    if poly_volume > 0 and poly_volume < 1000:
        return False  # too thin to trust

    return True


def get_polymarket_estimate(ticker, market_data):
    """Cross-reference Kalshi market with Polymarket for independent price."""
    title = market_data.get("title") or market_data.get("subtitle") or ""
    if not title:
        return None, None

    match = _fuzzy_match_polymarket(title)
    if not match:
        return None, None

    # Structural validation: ensure markets actually reference the same event
    if not _validate_polymarket_match(market_data, match):
        poly_title = (match.get("question") or match.get("title") or "")[:50]
        print(f"[poly] REJECTED structural mismatch: '{title[:40]}' ↔ '{poly_title}'")
        return None, None

    prices_raw = match.get("outcomePrices")
    if not prices_raw:
        return None, None

    try:
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw
        poly_yes = float(prices[0])
    except (json.JSONDecodeError, IndexError, TypeError):
        return None, None

    poly_title = (match.get("question") or match.get("title") or "")[:60]
    print(f"[poly] Match: '{title[:50]}' ↔ '{poly_title}' → poly_yes={poly_yes:.2f}")
    return poly_yes, f"polymarket:{poly_title[:40]}"


# ══════════════════════════════════════════════════════════════════════════════
# METACULUS — community prediction aggregation
# ══════════════════════════════════════════════════════════════════════════════

_METACULUS_CACHE = {}
_METACULUS_TS = 0


def _load_metaculus():
    """Fetch active binary Metaculus questions. Cache 10 min.
    As of 2025+, Metaculus API requires authentication."""
    from bot.config import METACULUS_API_TOKEN

    global _METACULUS_CACHE, _METACULUS_TS
    now = time.time()
    if _METACULUS_CACHE and now - _METACULUS_TS < 600: return _METACULUS_CACHE
    try:
        # Try the newer v1 API first (supports token auth), then fall back to v2
        headers = {"Accept": "application/json"}
        if METACULUS_API_TOKEN:
            headers["Authorization"] = f"Token {METACULUS_API_TOKEN}"

        urls_to_try = [
            "https://www.metaculus.com/api/questions/?type=forecast&status=open&limit=200&order_by=-activity",
            "https://www.metaculus.com/api2/questions/?type=forecast&status=open&limit=200&order_by=-activity",
        ]
        data = None
        for url in urls_to_try:
            rate_limit_wait(url)
            r = requests.get(url, timeout=10, headers=headers)
            if r.status_code == 200:
                data = r.json()
                break
            elif r.status_code == 403 and not METACULUS_API_TOKEN:
                # Auth required but no token — skip silently, don't spam logs
                return _METACULUS_CACHE

        if not data:
            return _METACULUS_CACHE

        questions = data.get("results", [])
        _METACULUS_CACHE = {
            q["id"]: q for q in questions
            if q.get("possibilities", {}).get("type") == "binary"
        }
        _METACULUS_TS = now
        if _METACULUS_CACHE:
            print(f"[metaculus] Loaded {len(_METACULUS_CACHE)} binary questions")
    except Exception as e:
        print(f"[metaculus] Failed: {e}")
    return _METACULUS_CACHE


def get_metaculus_estimate(ticker, market_data):
    """Fuzzy match Kalshi market to Metaculus question by title similarity."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    title_words = set(re.findall(r'\w{3,}', title))
    if len(title_words) < 3: return None, None

    questions = _load_metaculus()
    if not questions: return None, None

    best_q = None
    best_sim = 0
    for qid, q in questions.items():
        q_title = (q.get("title") or "").lower()
        q_words = set(re.findall(r'\w{3,}', q_title))
        if not q_words: continue
        sim = len(title_words & q_words) / len(title_words | q_words)
        if sim > best_sim:
            best_sim = sim
            best_q = q

    if not best_q or best_sim < 0.50: return None, None  # tightened from 0.30

    # Get community prediction
    prediction = best_q.get("community_prediction", {})
    prob = prediction.get("full", {}).get("q2")  # median
    if prob is None:
        prob = prediction.get("full", {}).get("avg")
    if prob is None or not (0.01 < prob < 0.99): return None, None

    q_title = best_q.get("title", "")[:50]
    print(f"[metaculus] Match (sim={best_sim:.2f}): '{title[:40]}' → '{q_title}' prob={prob:.2f}")
    return prob, f"metaculus:{best_q['id']}"
