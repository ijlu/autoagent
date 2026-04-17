"""Price momentum signal source.

Analyzes Kalshi's own trade history for a market to compute recent
price momentum. Uses Kalshi API (api_get) to fetch trade data.
"""

from __future__ import annotations

from bot.api import api_get


def get_price_momentum(ticker):
    """Fetch recent trades for *ticker* and compute momentum.

    Returns a dict ``{"last_price", "avg_price", "momentum"}`` where
    momentum = last_price - avg_price (positive = price rising),
    or ``None`` if insufficient data.
    """
    try:
        resp = api_get(f"/markets/{ticker}/trades?limit=20")
        trades = resp.get("trades", [])
        if len(trades) < 2: return None
        prices = []
        for t in trades:
            p = float(t.get("yes_price") or t.get("price") or 0)
            if p > 1: p /= 100
            if p > 0: prices.append(p)
        if len(prices) < 2: return None
        return {"last_price": prices[0], "avg_price": sum(prices)/len(prices),
                "momentum": prices[0] - sum(prices)/len(prices)}
    except: return None
