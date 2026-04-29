"""Pin the source list of ``_collect_gaussians`` to its 4-source state.

2026-04-29: NBM and MADIS removed from the production combine.

NBM removal: live probe (NY lat/lon) showed the Open-Meteo default and
``models=gfs_seamless`` endpoints return values identical to 0.0°F across
7 forecast days. The "nbm" source was never NBM — it was Open-Meteo's
default forced to GFS, which is what Open-Meteo's default already is for
US lat/lons. Including both halved the effective weight of every other
source in the precision-weighted combine.

MADIS removal: its ``+8°F flat warming`` heuristic under-models spring
morning-to-peak swings (20-30°F). Observed -8 to -17°F bias in production
snapshots driven by morning observations + 8°F bump. Its replacement is
``metar_observations.get_metar_gaussian`` which uses learned residual-σ.

If you re-add a source here, update the test below — and make sure you've
verified independence (NBM should be NOAA's actual NBM endpoint, not an
Open-Meteo proxy aliased to GFS).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import GaussianForecast


def _gauss(name, mu=70.0, sigma=2.0, hours=24):
    return GaussianForecast(
        mean_f=mu, sigma_f=sigma, horizon_hours=hours,
        source_name=name, source_tag=f"{name}:test",
    )


def _market():
    return {
        "ticker": "KXHIGHNY-26APR30-T75",
        "title": "Will the high temperature in NYC be above 75 on April 30?",
        "subtitle": "high temp",
        "yes_sub_title": "75 or above",
        "close_time": "2030-04-30T23:59:59Z",
    }


class TestCollectGaussiansSourceList:
    def test_six_sources_called(self):
        """Patching every getter to return None should result in exactly
        6 sources being attempted: HRRR, NWS Point, Open-Meteo,
        ICON, UKMO, METAR.

        IEM 1-min is intentionally absent — discovered 2026-04-29 that
        IEM's asos1min.py endpoint has ~24h publication latency, so it
        can never produce live data. METAR is the only real-time
        observation channel."""
        ticker = "KXHIGHNY-26APR30-T75"
        market = _market()

        called: list[str] = []

        def make_getter(name):
            def fn(_t, _m):
                called.append(name)
                return None
            return fn

        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian", make_getter("hrrr")), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian", make_getter("metar")), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian", make_getter("nws_point")), \
             patch("bot.signals.sources.weather.get_weather_gaussian", make_getter("weather")), \
             patch("bot.signals.sources.icon.get_icon_gaussian", make_getter("icon")), \
             patch("bot.signals.sources.ukmo.get_ukmo_gaussian", make_getter("ukmo")):
            result = v2._collect_gaussians(ticker, market)

        assert result == []
        assert sorted(called) == [
            "hrrr", "icon", "metar", "nws_point", "ukmo", "weather"
        ], (
            f"Expected 6 sources, got {sorted(called)}"
        )

    def test_nbm_not_in_getters_tuple(self):
        """If we accidentally re-add NBM to the source list, this test
        and the docstring at the top of this file should remind us why.

        We pattern-match the (name, getter) tuple shape, not bare
        substrings — the explanatory comments in _collect_gaussians
        legitimately mention ``get_nbm_gaussian`` for context."""
        import re, inspect
        src = inspect.getsource(v2._collect_gaussians)
        # Match the literal getter tuple: ("nbm", get_nbm_gaussian)
        assert not re.search(r'\(\s*"nbm"\s*,', src), (
            "NBM source must not be re-added to _collect_gaussians until "
            "real NBM (NOAA endpoint) is wired up. See file docstring."
        )

    def test_madis_not_in_getters_tuple(self):
        import re, inspect
        src = inspect.getsource(v2._collect_gaussians)
        assert not re.search(r'\(\s*"madis"\s*,', src), (
            "MADIS warming-heuristic must stay out of _collect_gaussians. "
            "Use metar residual-σ for spatial-ensemble coverage instead."
        )


class TestIEM1MinNotInLiveCombine:
    """IEM 1-min is registered in CANONICAL_WEATHER_SOURCES but
    deliberately NOT in GAUSSIAN_COMBINE_SOURCES — discovered 2026-04-29
    that IEM 1-min has ~24h publication latency and can never serve
    live data for the current day's market. METAR is the only real-time
    observation channel."""

    def test_iem_1min_getter_not_called(self):
        ticker = "KXHIGHNY-26APR30-T75"
        market = _market()
        iem_called = []
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian", lambda *a: None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian", lambda *a: None), \
             patch("bot.signals.sources.weather.get_weather_gaussian", lambda *a: None), \
             patch("bot.signals.sources.icon.get_icon_gaussian", lambda *a: None), \
             patch("bot.signals.sources.ukmo.get_ukmo_gaussian", lambda *a: None), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian", lambda *a: None), \
             patch("bot.signals.sources.iem_1min_asos.get_iem_1min_gaussian",
                   side_effect=lambda *a: iem_called.append(1) or None):
            v2._collect_gaussians(ticker, market)
        assert len(iem_called) == 0, (
            "IEM 1-min was called from _collect_gaussians but it shouldn't be — "
            "IEM has 24h publication latency, METAR is the only real-time obs"
        )
