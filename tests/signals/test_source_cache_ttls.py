"""Pin the cache TTLs on Open-Meteo-backed forecast sources to match
the underlying model cadences.

Each source caches its Open-Meteo response in `bot.api._CACHE` (the
shared per-process TTLCache). The TTL constant on each source must
match the model's actual update interval — otherwise we re-fetch
identical bytes between model cycles, burning Open-Meteo's daily quota
and triggering 429s.

Models + cadences (per Open-Meteo's data tables, 2026-04-30):
  HRRR:   hourly runs (HH:00Z), ~30min publish latency → cycle = 60min
  ICON:   4×/day (00/06/12/18Z), ~3h latency → cycle = 6h
  UKMO:   4×/day, ~3-5h latency → cycle = 6h
  NBM:    4×/day, ~2-3h latency → cycle = 6h
  GFS (default Open-Meteo): 4×/day → cycle = 6h

The TTLs we set must be ≥ one cycle length but ≤ a small safety margin
above so a stuck cache doesn't keep stale data forever.
"""
from __future__ import annotations

from bot.signals.sources import hrrr, icon, ukmo, ndfd_nbm, weather


def test_hrrr_ttl_aligns_to_hourly_cycle():
    """HRRR is hourly with ~30min publish lag → 1h cycle. TTL should
    sit just under one full cycle so we refresh once per cycle but
    don't outlive it."""
    assert 3000 <= hrrr._HRRR_CACHE_TTL <= 3600


def test_icon_ttl_aligns_to_6h_cycle():
    """ICON is 6-hourly. TTL should fit inside one cycle."""
    assert 18000 <= icon._ICON_CACHE_TTL <= 21600


def test_ukmo_ttl_aligns_to_6h_cycle():
    assert 18000 <= ukmo._UKMO_CACHE_TTL <= 21600


def test_nbm_ttl_aligns_to_6h_cycle():
    assert 18000 <= ndfd_nbm._NBM_CACHE_TTL <= 21600


def test_weather_open_meteo_ttl_aligns_to_6h_cycle():
    """Open-Meteo's default blend uses GFS at US lat/lons; 6h cycle."""
    assert 18000 <= weather._WEATHER_OM_CACHE_TTL <= 21600


def test_no_short_ttls_remain():
    """Regression guard: ensure none of the forecast sources reverted
    to the legacy 30-minute (1800s) TTL. The 30-min value is what
    drove ~10× redundant fetches and the persistent 429 cluster on
    2026-04-30.
    """
    assert hrrr._HRRR_CACHE_TTL > 1800
    assert icon._ICON_CACHE_TTL > 1800
    assert ukmo._UKMO_CACHE_TTL > 1800
    assert ndfd_nbm._NBM_CACHE_TTL > 1800
    assert weather._WEATHER_OM_CACHE_TTL > 1800
