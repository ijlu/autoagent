"""Crypto price source: CoinGecko live prices + volatility-calibrated estimates.

Extracted from trade.py. Uses CoinGecko for spot prices and realized vol,
Deribit for implied vol, then a volatility-calibrated sigmoid for probability.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

import requests

from bot.api import cached_get, rate_limit_wait, _CACHE  # noqa: F401
from bot.signals.sources.deribit_vol import get_deribit_term_vol, get_deribit_implied_prob


# ══════════════════════════════════════════════════════════════════════════════
# CRYPTO — CoinGecko live prices + volatility
# ══════════════════════════════════════════════════════════════════════════════


def fetch_crypto_price(symbol="bitcoin"):
    data = cached_get(f"crypto_{symbol}",
        f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd")
    return data.get(symbol, {}).get("usd") if data else None


def _fetch_realized_vol(symbol, days=30):
    """Fetch realized annualized volatility from CoinGecko historical prices.
    Returns daily vol as a fraction (e.g. 0.03 = 3% daily moves).
    Cache for 1 hour since vol changes slowly."""
    cache_key = f"crypto_vol_{symbol}_{days}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 3600:
        return _CACHE[cache_key][0]
    try:
        url = (f"https://api.coingecko.com/api/v3/coins/{symbol}/market_chart?"
               f"vs_currency=usd&days={days}&interval=daily")
        rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        data = r.json()
        prices = [p[1] for p in data.get("prices", [])]
        if len(prices) < 5:
            return None
        # Compute daily log returns and their std dev
        log_returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices))
                       if prices[i-1] > 0]
        if len(log_returns) < 3:
            return None
        daily_vol = (sum(r**2 for r in log_returns) / len(log_returns)) ** 0.5
        _CACHE[cache_key] = (daily_vol, now)
        print(f"[vol] {symbol} realized daily vol = {daily_vol:.3f} ({daily_vol*100:.1f}%/day) "
              f"from {len(log_returns)} returns")
        return daily_vol
    except Exception as e:
        print(f"[vol] Failed to fetch vol for {symbol}: {e}")
        return None


def _fetch_deribit_iv(symbol):
    """Fetch Deribit implied volatility index (DVOL) for BTC/ETH.
    This is the market's forward-looking vol estimate -- much better than
    realized vol for pricing near-term expiries. Free public API, no auth.
    Returns annualized IV as a fraction (e.g. 0.60 = 60% annual vol).
    Convert to daily: daily_vol = annual_vol / sqrt(365)."""
    deribit_map = {"bitcoin": "BTC", "ethereum": "ETH"}
    deribit_sym = deribit_map.get(symbol)
    if not deribit_sym:
        return None
    cache_key = f"deribit_iv_{deribit_sym}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 1800:
        return _CACHE[cache_key][0]
    try:
        # Deribit public ticker endpoint — returns mark_iv for the DVOL index
        url = f"https://www.deribit.com/api/v2/public/get_index_price?index_name={deribit_sym.lower()}_usd"
        # Use the volatility index instead
        vol_url = (f"https://www.deribit.com/api/v2/public/ticker?"
                   f"instrument_name={deribit_sym}-PERPETUAL")
        rate_limit_wait(vol_url)
        r = requests.get(vol_url, timeout=8)
        if r.status_code == 200:
            data = r.json().get("result", {})
            # Try to get mark_iv from options, fallback to estimated_delivery_price movement
            # For the perpetual, compute recent price movement as a vol proxy
            last = float(data.get("last_price", 0))
            stats = data.get("stats", {})
            high = float(stats.get("high", last))
            low = float(stats.get("low", last))
            if last > 0 and high > 0 and low > 0:
                # Daily range as vol proxy: (high-low)/mid / 4 ≈ daily vol
                daily_range_vol = (high - low) / ((high + low) / 2) / 4
                _CACHE[cache_key] = (daily_range_vol, now)
                print(f"[deribit] {deribit_sym} 24h range vol proxy = {daily_range_vol:.4f} "
                      f"({daily_range_vol*100:.2f}%/day)")
                return daily_range_vol
    except Exception as e:
        print(f"[deribit] Failed for {symbol}: {e}")
    return None


# Fallback daily vol estimates if CoinGecko historical and Deribit both fail
_DEFAULT_DAILY_VOL = {"bitcoin": 0.025, "ethereum": 0.035, "solana": 0.05}


def _days_to_expiry(market_data):
    """Extract days until market closes. Returns None if unknown."""
    close_str = (market_data.get("close_time") or market_data.get("expiration_time")
                 or market_data.get("expected_expiration_time") or "")
    if not close_str:
        return None
    try:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        delta = (close_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(0.01, delta)  # floor at ~15 min
    except:
        return None


def get_crypto_estimate(ticker, market_data):
    ticker_upper = ticker.upper()
    symbol = None
    if "BTC" in ticker_upper or "BITCOIN" in ticker_upper: symbol = "bitcoin"
    elif "ETH" in ticker_upper or "ETHER" in ticker_upper: symbol = "ethereum"
    elif "SOL" in ticker_upper or "SOLANA" in ticker_upper: symbol = "solana"
    else: return None, None

    title = market_data.get("title", "") or market_data.get("subtitle", "") or ""
    strike = None
    for match in re.findall(r'\$?([\d,]+(?:\.\d+)?)', title):
        try:
            val = float(match.replace(",", ""))
            if val > 100: strike = val; break
        except: continue
    if not strike: return None, None

    current_price = fetch_crypto_price(symbol)
    if not current_price: return None, None

    pct_distance = (current_price - strike) / strike
    days = _days_to_expiry(market_data)

    # -- Try Black-Scholes implied probability from Deribit options chain --
    # This is the gold standard: real options data with full vol surface.
    if days is not None and days > 0:
        bs_prob = get_deribit_implied_prob(symbol, strike, days)
        if bs_prob is not None:
            prob_yes = max(0.02, min(0.98, bs_prob))
            print(f"[info] Crypto: {symbol} ${current_price:,.0f} vs strike ${strike:,.0f} "
                  f"({pct_distance:+.1%}) days={days:.1f} → {prob_yes:.2f} [deribit-bs]")
            return prob_yes, f"crypto:{symbol}"

    # -- Fallback: Volatility-calibrated sigmoid --
    # Use Deribit term vol > Deribit perp range > realized vol > default.
    daily_vol = None
    if days is not None and days > 0:
        daily_vol = get_deribit_term_vol(symbol, days)
    if daily_vol is None:
        daily_vol = (_fetch_deribit_iv(symbol)
                     or _fetch_realized_vol(symbol)
                     or _DEFAULT_DAILY_VOL.get(symbol, 0.03))

    if days is not None and days > 0:
        expected_range = daily_vol * math.sqrt(max(days, 0.1))
        k = 1.0 / max(expected_range, 0.005)
    else:
        k = 1.0 / max(daily_vol, 0.005)

    prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-k * pct_distance))))
    vol_src = "deribit-term" if get_deribit_term_vol(symbol, days or 1) else "realized"
    days_str = f" days={days:.1f}" if days else ""
    print(f"[info] Crypto: {symbol} ${current_price:,.0f} vs strike ${strike:,.0f} "
          f"({pct_distance:+.1%}) vol={daily_vol:.3f} k={k:.1f}{days_str} → {prob_yes:.2f} [{vol_src}]")
    return prob_yes, f"crypto:{symbol}"
