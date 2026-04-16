"""Finnhub news sentiment signal source.

Currently disabled (Finnhub free tier returns HTTP 403 for the news-sentiment
endpoint). Returns fast None so pipeline health doesn't penalize this source.
"""

from __future__ import annotations

import re

from bot.api import cached_get
from bot.config import FINNHUB_KEY


def get_news_sentiment(ticker, market_data):
    """Check Finnhub news sentiment -- DISABLED: the news-sentiment endpoint requires
    a paid Finnhub plan (free tier returns HTTP 403). Returning fast None so pipeline
    health doesn't penalize this source."""
    return None, None
    if not FINNHUB_KEY: return None, None
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Only trigger for markets that look like they involve publicly traded companies
    # Look for known stock-related patterns in the title
    stock_keywords = ["stock", "share", "s&p", "nasdaq", "dow", "earnings", "ipo",
                      "market cap", "trading", "nyse"]
    has_stock_context = any(kw in title for kw in stock_keywords)

    # Also check if the ticker itself looks like a stock symbol (2-5 uppercase letters)
    ticker_upper = ticker.upper()
    known_stock_tickers = {"AAPL", "TSLA", "MSFT", "AMZN", "GOOGL", "META", "NVDA",
                           "NFLX", "AMD", "INTC", "BA", "DIS", "JPM", "GS", "V", "MA"}
    has_stock_ticker = any(st in ticker_upper for st in known_stock_tickers)

    if not has_stock_context and not has_stock_ticker:
        return None, None

    # Extract potential stock symbol from title
    stop_words = {"will", "the", "be", "in", "on", "at", "to", "of", "a", "an",
                  "or", "and", "for", "by", "this", "that", "what", "how", "when",
                  "yes", "no", "above", "below", "more", "less", "than", "over", "under",
                  "stock", "share", "price", "market"}
    words = [w for w in re.findall(r'\w{3,}', title) if w not in stop_words]
    if not words: return None, None

    # Try to find a valid stock symbol
    symbol = None
    for w in words:
        if w.upper() in known_stock_tickers:
            symbol = w.upper()
            break
    if not symbol:
        # Fall back to first non-stop word as potential symbol
        symbol = words[0].upper()

    cache_key = f"finnhub_sent_{symbol}"
    sentiment_data = cached_get(cache_key,
        f"https://finnhub.io/api/v1/news-sentiment?symbol={symbol}&token={FINNHUB_KEY}",
        timeout=8)

    if sentiment_data and sentiment_data.get("sentiment"):
        score = sentiment_data["sentiment"].get("bullishPercent", 0.5)
        if abs(score - 0.5) > 0.15:  # raised threshold from 0.1 -- require stronger signal
            print(f"[finnhub] {symbol} sentiment={score:.2f}")
            return score, f"finnhub:{symbol}"

    return None, None
