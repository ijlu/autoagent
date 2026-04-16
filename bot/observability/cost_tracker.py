"""Cost tracking for API calls, fees, and infrastructure.

Tracks:
- OpenRouter/OpenAI LLM costs per call
- Kalshi trading fees (already tracked per trade)
- Odds API credits used
- VPS cost amortization
"""

from __future__ import annotations

import time

_COSTS = {}  # {category: [(timestamp, amount_usd), ...]}


def record_cost(category: str, amount_usd: float):
    """Record an operational cost."""
    if category not in _COSTS:
        _COSTS[category] = []
    _COSTS[category].append((time.time(), amount_usd))


def get_daily_costs() -> dict[str, float]:
    """Get today's costs by category."""
    cutoff = time.time() - 86400
    result = {}
    for cat, entries in _COSTS.items():
        total = sum(amt for ts, amt in entries if ts > cutoff)
        if total > 0:
            result[cat] = round(total, 4)
    return result


def get_total_costs(days: int = 30) -> float:
    """Get total costs over last N days."""
    cutoff = time.time() - days * 86400
    return sum(amt for entries in _COSTS.values() for ts, amt in entries if ts > cutoff)


# Pre-configured cost estimates
LLM_COSTS = {
    "gpt-4o-mini": 0.00015,  # per 1K input tokens
    "gpt-4o": 0.005,
    "claude-sonnet": 0.003,
    "gemini-pro": 0.00125,
}

def estimate_llm_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost of an LLM call."""
    base = LLM_COSTS.get(model, 0.001)
    return base * (input_tokens / 1000) + base * 3 * (output_tokens / 1000)
