"""LLM-based market analysis signal source.

Uses GPT-4o-mini as a last-resort estimator for markets that no other
regex-based source can parse. Expensive and slow -- only triggered when
all other sources returned None.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import requests

from bot.api import rate_limit_wait
from bot.config import OPENAI_API_KEY


_LLM_CACHE = {}  # {ticker: (prob, timestamp)}


def get_llm_estimate(ticker, market_data):
    """Use GPT-4o-mini to analyze markets that no other source matched.
    Only called when all 8 regex-based sources returned None.
    Returns (probability, source_desc) or (None, None).

    IMPORTANT: This is expensive (~$0.001/call) and slow (~1-2s), so it's
    only triggered as a last resort for markets with good volume/spread."""
    if not OPENAI_API_KEY:
        return None, None

    title = market_data.get("title") or market_data.get("subtitle") or ""
    if not title or len(title) < 10:
        return None, None

    # Cache LLM results for 30 min (these markets don't change fast)
    now = time.time()
    if ticker in _LLM_CACHE and now - _LLM_CACHE[ticker][1] < 1800:
        cached_prob = _LLM_CACHE[ticker][0]
        if cached_prob is not None:
            return cached_prob, f"llm_cached:{ticker[:20]}"
        return None, None

    close_time = market_data.get("close_time") or market_data.get("expiration_time") or ""
    yes_ask_raw = market_data.get("yes_ask") or market_data.get("yes_ask_dollars") or 0
    yes_ask_val = float(yes_ask_raw)
    if yes_ask_val > 1: yes_ask_val /= 100

    prompt = f"""You are a prediction market analyst. Estimate the probability that this Kalshi market resolves YES.

Market: "{title}"
Current market price (implied probability): {yes_ask_val:.0%}
Resolution date: {close_time or 'unknown'}
Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Think step by step about what publicly available information suggests. Consider:
- Recent news and trends
- Historical base rates
- Time until resolution
- Whether the current market price seems too high or too low

Respond with ONLY a JSON object: {{"probability": 0.XX, "reasoning": "brief 1-sentence reason"}}
Do not include any other text."""

    try:
        rate_limit_wait("https://api.openai.com/v1/chat/completions")
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 150,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            _LLM_CACHE[ticker] = (None, now)
            return None, None

        content = resp.json()["choices"][0]["message"]["content"].strip()
        # Parse JSON response -- try multiple formats robustly
        parsed = None
        # Attempt 1: raw JSON parse (no code blocks)
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            pass
        # Attempt 2: extract from markdown code blocks
        if parsed is None and "```" in content:
            try:
                block = content.split("```")[1]
                if block.startswith("json"):
                    block = block[4:]
                parsed = json.loads(block.strip())
            except (json.JSONDecodeError, ValueError, IndexError):
                pass
        # Attempt 3: regex extract first JSON object
        if parsed is None:
            try:
                json_match = re.search(r'\{[^}]+\}', content)
                if json_match:
                    parsed = json.loads(json_match.group())
            except (json.JSONDecodeError, ValueError):
                pass
        if parsed is None:
            _LLM_CACHE[ticker] = (None, now)
            return None, None
        prob = float(parsed.get("probability", 0))
        reasoning = parsed.get("reasoning", "")[:80]

        # Sanity checks
        if not (0.02 < prob < 0.98):
            _LLM_CACHE[ticker] = (None, now)
            return None, None

        # Don't trust LLM if it just parrots the market price back
        if abs(prob - yes_ask_val) < 0.03:
            _LLM_CACHE[ticker] = (None, now)
            return None, None

        _LLM_CACHE[ticker] = (prob, now)
        print(f"[llm] {ticker}: '{title[:40]}' -> prob={prob:.2f} ({reasoning})")
        return prob, f"llm:{reasoning[:30]}"

    except Exception as e:
        print(f"[llm] Failed for {ticker}: {e}")
        _LLM_CACHE[ticker] = (None, now)
        return None, None
