"""NWS Area Forecast Discussion (AFD) text source.

AFDs are plain-English forecast discussions written by meteorologists at each
Weather Forecast Office (WFO). They contain high-signal insight that's not in
the numerical models — e.g. "inversion may suppress daytime heating below model
guidance" or "marine layer expected to erode by noon, allowing afternoon highs
to exceed 90F". These are real human edge over the ensemble.

We fetch the latest AFD text and optionally summarize to a numerical probability
adjustment using OpenAI (when OPENAI_API_KEY is set). Without the LLM, we fall
back to keyword heuristics (e.g. "cooler than forecast" → adjustment -2°F).

Free text fetch, no auth. LLM summary ~$0.01/day across all WFOs if enabled.

API: api.weather.gov/products/types/AFD/locations/{wfo}
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from bot.api import _CACHE, rate_limit_wait
from bot.config import OPENAI_API_KEY
from bot.signals.sources.weather import (
    WEATHER_CITIES,
    _CITY_LST_OFFSET,
    _detect_city,
    _parse_threshold,
    _determine_day_index,
)


_AFD_CACHE_TTL = 21600  # 6h — AFDs are issued ~4x/day
_AFD_USER_AGENT = "KalshiTradingBot/1.0 (contact: joshlu@a16z.com)"

# Kalshi cities → responsible WFO (Weather Forecast Office 3-letter ID)
_CITY_WFO_MAP = {
    "nyc":         "OKX",  # Upton, NY
    "new york":    "OKX",
    "chicago":     "LOT",  # Chicago, IL
    "los angeles": "LOX",  # Oxnard, CA
    "la":          "LOX",
    "austin":      "EWX",  # Austin/San Antonio, TX
    "miami":       "MFL",  # Miami, FL
    "denver":      "BOU",  # Boulder/Denver, CO
}


def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _fetch_latest_afd(wfo: str) -> Optional[str]:
    """Fetch the latest AFD text for a WFO. Returns the raw discussion text."""
    cache_key = f"afd::{wfo}"
    now = time.time()
    if cache_key in _CACHE:
        data, ts = _CACHE[cache_key]
        if now - ts < _AFD_CACHE_TTL:
            return data

    # Step 1: list products to get most recent ID
    list_url = f"https://api.weather.gov/products/types/AFD/locations/{wfo}"
    headers = {"User-Agent": _AFD_USER_AGENT, "Accept": "application/ld+json"}
    try:
        rate_limit_wait(list_url)
        r = requests.get(list_url, timeout=8, headers=headers)
        if r.status_code != 200:
            print(f"[afd] list HTTP {r.status_code} for {wfo}")
            return None
        products = r.json().get("@graph", [])
        if not products:
            return None
        # Most recent first
        latest_id = products[0].get("id") or products[0].get("@id")
        if not latest_id:
            return None
        # Extract productId if we got a URL
        prod_id = latest_id.rsplit("/", 1)[-1]

        # Step 2: fetch the actual text
        text_url = f"https://api.weather.gov/products/{prod_id}"
        rate_limit_wait(text_url)
        r2 = requests.get(text_url, timeout=8, headers=headers)
        if r2.status_code != 200:
            print(f"[afd] text HTTP {r2.status_code} for {wfo}")
            return None
        text = r2.json().get("productText", "")
        if not text:
            return None
        _CACHE[cache_key] = (text, now)
        return text
    except Exception as e:
        print(f"[afd] error: {type(e).__name__}: {e}")
        return None


# Heuristic keyword model — used when OPENAI_API_KEY is absent.
# Returns a signed temperature adjustment (°F) vs generic model forecast.
_AFD_COOLER_PATTERNS = [
    r"cooler than (forecast|guidance|models|expected)",
    r"(marine layer|fog|onshore flow).*persist",
    r"model.*(overdon|too warm|biased warm)",
    r"below (forecast|normal|average)",
    r"capping.*(temperatures|highs)",
    r"trend.*cooler",
    r"(overcast|cloud).*limit.*(warming|heating)",
]
_AFD_WARMER_PATTERNS = [
    r"warmer than (forecast|guidance|models|expected)",
    r"subsidence.*warming",
    r"model.*(underdon|too cool|biased cool)",
    r"above (forecast|normal|average)",
    r"downsloping.*warming",
    r"trend.*warmer",
    r"(clearing|sun).*enhance.*(warming|heating)",
]


def _afd_keyword_adjustment(text: str) -> float:
    """Return a signed temperature adjustment (°F) based on keyword heuristics.

    Looks at the first ~3000 chars of AFD text (the synopsis + near-term
    discussion) to avoid weighting long-range speculation.
    """
    snippet = text[:3000].lower()
    cooler = sum(1 for pat in _AFD_COOLER_PATTERNS if re.search(pat, snippet))
    warmer = sum(1 for pat in _AFD_WARMER_PATTERNS if re.search(pat, snippet))
    # Each match moves ~1°F. Cap at ±3°F (forecaster disagreement is real but bounded).
    delta = warmer - cooler
    return max(-3.0, min(3.0, delta * 1.0))


def _afd_llm_adjustment(text: str, city_key: str) -> Optional[float]:
    """Optional LLM-based summarization. Returns adjustment in °F or None.

    Uses OpenAI gpt-4o-mini (cheapest model with structured outputs).
    Falls back to keyword heuristics if OPENAI_API_KEY is unset or call fails.
    """
    if not OPENAI_API_KEY:
        return None
    snippet = text[:4000]
    prompt = (
        f"You are a forecaster reading an NWS Area Forecast Discussion for "
        f"{city_key}. Based on the text, output a single number: how much "
        f"(in °F) you expect today's daily high to deviate from generic "
        f"model guidance. Positive = warmer than model, negative = cooler "
        f"than model. Cap at ±5°F. If no opinion, output 0.\n\n"
        f"AFD text:\n{snippet}\n\n"
        f"Output a single number only, e.g. '-1.5' or '2' or '0'."
    )
    try:
        import requests as _r
        r = _r.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 8,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        out = r.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r"-?\d+(\.\d+)?", out)
        if m:
            val = float(m.group(0))
            return max(-5.0, min(5.0, val))
    except Exception as e:
        print(f"[afd] LLM error: {type(e).__name__}: {e}")
    return None


def get_afd_estimate(ticker: str, market_data: dict) -> tuple:
    """AFD-based estimate. Uses NBM as the baseline and applies a forecaster
    adjustment from AFD text. Returns (prob, source) or (None, None).
    """
    if market_data is None:
        return None, None
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = (ticker or "").upper()
    is_weather = "KXHIGH" in ticker_upper or any(
        kw in title for kw in ("temperature", "temp", "°f", "degrees", "high")
    )
    if not is_weather:
        return None, None

    city_key = _detect_city(ticker_upper, title)
    if not city_key:
        return None, None
    wfo = _CITY_WFO_MAP.get(city_key)
    if not wfo:
        return None, None

    threshold, is_above = _parse_threshold(ticker, title)
    if threshold is None or threshold < -40 or threshold > 140:
        return None, None

    day_idx = _determine_day_index(title, market_data, city_key)
    # AFD covers short-range only; past day-1 the discussion goes vague
    if day_idx is None or day_idx > 1:
        return None, None

    # Need an NBM baseline to adjust. We import lazily to avoid circular import.
    from bot.signals.sources.ndfd_nbm import _fetch_nbm_forecast

    baseline = _fetch_nbm_forecast(city_key)
    if not baseline:
        return None, None
    highs = baseline.get("daily", {}).get("temperature_2m_max", [])
    if day_idx >= len(highs):
        return None, None
    baseline_high = highs[day_idx]
    if baseline_high is None:
        return None, None

    text = _fetch_latest_afd(wfo)
    if not text:
        return None, None

    adj = _afd_llm_adjustment(text, city_key)
    llm_used = adj is not None
    if adj is None:
        adj = _afd_keyword_adjustment(text)

    adjusted_high = baseline_high + adj
    # AFD-adjusted estimate widens sigma slightly to reflect forecaster
    # subjectivity.
    sigma = 2.2 + day_idx * 0.5

    is_bracket = "-B" in ticker_upper
    if is_bracket:
        bracket_floor = threshold
        bracket_cap = threshold + 2.0
        _fs = market_data.get("floor_strike")
        _cs = market_data.get("cap_strike")
        if _fs is not None and _cs is not None:
            try:
                bracket_floor = float(_fs)
                bracket_cap = float(_cs)
            except (ValueError, TypeError):
                pass
        else:
            m = re.search(r"(\d+\.?\d*)\s*°?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)", title)
            if m:
                bracket_floor = float(m.group(1))
                bracket_cap = float(m.group(2))
        cdf_upper = _logistic_cdf(bracket_cap, adjusted_high, sigma)
        cdf_lower = _logistic_cdf(bracket_floor, adjusted_high, sigma)
        prob = max(0.02, min(0.98, cdf_upper - cdf_lower))
    elif is_above:
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-(adjusted_high - threshold) / sigma))))
    else:
        prob = max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-(threshold - adjusted_high) / sigma))))

    method = "llm" if llm_used else "keyword"
    print(
        f"[afd] {city_key} wfo={wfo} baseline={baseline_high:.0f}°F "
        f"adj={adj:+.1f}°F ({method}) -> high={adjusted_high:.0f}°F "
        f"threshold={threshold}°F -> {prob:.3f}"
    )
    return prob, f"afd:{city_key}_{wfo}_{method}"
