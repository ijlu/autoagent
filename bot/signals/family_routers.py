"""Family-specific source routing for Kalshi markets.

For some market families, a single data source is so dominant that treating
it as "one of N equal-weighted ensemble members" wastes signal. This module
lets the main ensemble delegate to a family-specific pipeline that:
  1. Uses the family's authoritative source as the primary estimate.
  2. Bounds combination with other sources (doesn't let noise override signal).
  3. Applies family-specific horizon / precision models.

Registered routers:
  KXHIGH* / KXHMONTHRANGE* / KXHURR*  → weather_ensemble.predict()
  KXJOB*                              → adp_nfp.get_adp_estimate()
  KXGDP*                              → gdpnow.get_gdpnow_estimate()
  KXCPI*                              → commodity_futures.get_commodity_cpi_estimate()

Unknown tickers fall through to the generic main ensemble with no delegation.
"""

from __future__ import annotations

from typing import Callable, Optional


def _weather(ticker: str, market_data: dict) -> tuple:
    from bot.signals.weather_ensemble import predict
    return predict(ticker, market_data)


def _kxjob(ticker: str, market_data: dict) -> tuple:
    from bot.signals.sources.adp_nfp import get_adp_estimate
    return get_adp_estimate(ticker, market_data)


def _kxgdp(ticker: str, market_data: dict) -> tuple:
    from bot.signals.sources.gdpnow import get_gdpnow_estimate
    return get_gdpnow_estimate(ticker, market_data)


def _kxcpi(ticker: str, market_data: dict) -> tuple:
    from bot.signals.sources.commodity_futures import get_commodity_cpi_estimate
    return get_commodity_cpi_estimate(ticker, market_data)


# Prefix → router function. Longest-prefix-wins.
FAMILY_ROUTERS: list[tuple[str, Callable[[str, dict], tuple]]] = [
    ("KXHIGH", _weather),
    ("KXHMONTHRANGE", _weather),
    ("KXHURR", _weather),
    ("KXJOB", _kxjob),
    ("KXGDP", _kxgdp),
    ("KXCPI", _kxcpi),
]


def route_family(ticker: str, market_data: dict) -> Optional[tuple]:
    """If this ticker has a family-specific router, call it and return its result.

    Returns:
        (prob, source_tag) from the router,
        or None if no router matches (caller should use the generic ensemble).
    """
    if not ticker:
        return None
    upper = ticker.upper()
    # Match longest-prefix first to avoid KXHURR being caught by a KXH catch-all.
    for prefix, fn in sorted(FAMILY_ROUTERS, key=lambda kv: -len(kv[0])):
        if upper.startswith(prefix):
            try:
                return fn(ticker, market_data)
            except Exception as e:
                print(f"[family_router] {prefix} raised {type(e).__name__}: {e}")
                return None
    return None
