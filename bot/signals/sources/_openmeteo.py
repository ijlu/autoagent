"""Open-Meteo URL helper.

Single source of truth for which Open-Meteo endpoint our forecast
sources hit. The seven Gaussian-providing sources (HRRR, ICON, UKMO,
GEM, MetNo, ECMWF, weather) and the daemon's forecast_cache all go
through this helper, so flipping between free and commercial tiers
is one config change rather than nine.

Behavior:
  - When ``OPENMETEO_API_KEY`` env var is set:
    Routes to ``customer-api.open-meteo.com`` and appends
    ``apikey=<KEY>`` to the query string. This is Open-Meteo's
    commercial endpoint which removes the 10K/day, 5K/hour, 600/min
    rate caps that affect free-tier users.
  - When unset:
    Routes to ``api.open-meteo.com`` (free non-commercial).
    Trading is technically commercial use of Open-Meteo per their
    terms — set the key once we go live.

Sign-up: https://open-meteo.com/en/pricing
"""
from __future__ import annotations

from bot.config import OPENMETEO_API_KEY


_FREE_HOST: str = "https://api.open-meteo.com"
_COMMERCIAL_HOST: str = "https://customer-api.open-meteo.com"


def is_commercial() -> bool:
    """True when the commercial API key is set, so callers can log
    which tier they're hitting if they care.
    """
    return bool(OPENMETEO_API_KEY)


def base_url() -> str:
    """Return the host (without trailing slash) appropriate for the
    current tier. Used by paths like ``/v1/forecast``."""
    return _COMMERCIAL_HOST if is_commercial() else _FREE_HOST


def with_apikey(url: str) -> str:
    """Append the commercial ``apikey`` query param when set.

    The caller passes a fully-formed URL (including any existing query
    string); this helper either appends ``&apikey=<key>`` or returns
    the URL unchanged. Idempotent — caller may safely double-call,
    though there's no reason to.
    """
    if not OPENMETEO_API_KEY:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}apikey={OPENMETEO_API_KEY}"


def forecast_url(query: str) -> str:
    """Build a complete ``/v1/forecast`` URL with the given query string.

    ``query`` should be the full query portion *without* the leading
    ``?`` and *without* an apikey parameter — this helper adds those.
    Example::

        url = forecast_url("latitude=25.79&longitude=-80.29&daily=...")
    """
    return with_apikey(f"{base_url()}/v1/forecast?{query}")
