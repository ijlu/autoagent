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


@pytest.fixture(autouse=True)
def _allow_all_source_prefetch(monkeypatch):
    monkeypatch.setattr(
        v2, "_source_state_allows_prefetch", lambda _source, _city: True
    )


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
    def test_ten_sources_called(self):
        """Patching every getter to return None should result in exactly
        10 sources being attempted.

        2026-05-02: nws_5min_diurnal added — METAR's diurnal fit
        applied to the 5-min observation feed.

        2026-05-02 (later): nws_5min_analog removed from live combine.

        2026-05-05: `weather` removed from live combine. Per-city scorecard
        analysis showed corr(hrrr, weather) = 0.994 (NY) / 1.000 (LAX) at
        peak window — both are Open-Meteo (gfs_hrrr vs default blend; the
        default blend IS GFS for US lat/lons). Was duplicating the HRRR
        signal and halving the effective weight of every other source.
        See reports/POSTFIX_REASSESSMENT_2026-05-05.md.

        IEM 1-min is intentionally absent — IEM's asos1min.py endpoint
        has ~24h publication latency."""
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
             patch("bot.signals.sources.icon.get_icon_gaussian", make_getter("icon")), \
             patch("bot.signals.sources.ukmo.get_ukmo_gaussian", make_getter("ukmo")), \
             patch("bot.signals.sources.gem.get_gem_gaussian", make_getter("gem")), \
             patch("bot.signals.sources.metno.get_metno_gaussian", make_getter("metno")), \
             patch("bot.signals.sources.ecmwf.get_ecmwf_gaussian", make_getter("ecmwf")), \
             patch("bot.signals.sources.nws_5min.get_nws_5min_gaussian", make_getter("nws_5min")), \
             patch("bot.signals.sources.nws_5min_diurnal.get_nws_5min_diurnal_gaussian",
                   make_getter("nws_5min_diurnal")):
            result = v2._collect_gaussians(ticker, market)

        assert result == []
        assert sorted(called) == [
            "ecmwf", "gem", "hrrr", "icon", "metar", "metno",
            "nws_5min", "nws_5min_diurnal",
            "nws_point", "ukmo",
        ], (
            f"Expected 10 sources, got {sorted(called)}"
        )

    def test_weather_not_in_getters_tuple(self):
        """`weather` (Open-Meteo default blend) must NOT be a live combine
        source — it duplicates HRRR (Open-Meteo gfs_hrrr) at corr 0.99-1.00.
        See reports/POSTFIX_REASSESSMENT_2026-05-05.md. Re-add only as a
        snapshot-only monitor, never to the combine."""
        import re, inspect
        src = inspect.getsource(v2._collect_gaussians)
        live = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        assert not re.search(r'\(\s*"weather"\s*,', live), (
            "`weather` re-added to live combine. It duplicates HRRR — "
            "both are Open-Meteo with the default model = GFS for US."
        )

    def test_analog_not_in_live_combine(self):
        """nws_5min_analog must NOT appear as an active (name, getter)
        tuple in _collect_gaussians until the feature-vintage bug is
        fixed and we have ~60+ historical days. The commented-out
        placeholder line is allowed (kept as a re-enable anchor with
        rationale)."""
        import re
        import inspect
        src = inspect.getsource(v2._collect_gaussians)
        # Strip comment-only lines so we only inspect live code.
        live = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        assert not re.search(r'\(\s*"nws_5min_analog"\s*,', live), (
            "nws_5min_analog re-added to live combine. Confirm the "
            "vintage strategy is fixed AND historical coverage spans "
            "the current regime before re-enabling."
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


# ── Per-city source filter (2026-05-04) ──────────────────────────────


class TestPerCityExclusions:
    def test_nws_point_skipped_for_nyc(self):
        """KXHIGHNY tickers must not include nws_point's Gaussian in the
        combine (regression: -5.86°F bias). The getter still runs (for
        snapshot logging) but its return value is filtered out."""
        from unittest.mock import patch

        ticker = "KXHIGHNY-26MAY04-T75"
        market = _market()

        called: list[str] = []
        def make_getter(name, mu=70.0):
            def fn(_t, _m):
                called.append(name)
                return GaussianForecast(
                    mean_f=mu, sigma_f=2.0, horizon_hours=8.0,
                    source_name=name, source_tag=f"{name}:test",
                )
            return fn

        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian", make_getter("hrrr", 70.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian", make_getter("metar", 70.0)), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian", make_getter("nws_point", 50.0)), \
             patch("bot.signals.sources.weather.get_weather_gaussian", make_getter("weather", 70.0)), \
             patch("bot.signals.sources.icon.get_icon_gaussian", make_getter("icon", 70.0)), \
             patch("bot.signals.sources.ukmo.get_ukmo_gaussian", make_getter("ukmo", 70.0)), \
             patch("bot.signals.sources.gem.get_gem_gaussian", make_getter("gem", 70.0)), \
             patch("bot.signals.sources.metno.get_metno_gaussian", make_getter("metno", 70.0)), \
             patch("bot.signals.sources.ecmwf.get_ecmwf_gaussian", make_getter("ecmwf", 70.0)), \
             patch("bot.signals.sources.nws_5min.get_nws_5min_gaussian", make_getter("nws_5min", 70.0)), \
             patch("bot.signals.sources.nws_5min_diurnal.get_nws_5min_diurnal_gaussian",
                   make_getter("nws_5min_diurnal", 70.0)):
            result = v2._collect_gaussians(ticker, market)

        # All 11 getters should have been called.
        assert "nws_point" in called
        # But nws_point should be ABSENT from the resulting Gaussian list.
        names_in_combine = sorted(g.source_name for g in result)
        assert "nws_point" not in names_in_combine, (
            f"nws_point should be filtered out for KNYC but appears in "
            f"the combine: {names_in_combine}"
        )

    def test_metno_and_gem_skipped_for_lax(self):
        """KXHIGHLAX tickers must drop metno + gem (regression: +9.67°F
        and +8.86°F warm bias respectively from marine-layer mismatch)."""
        from unittest.mock import patch

        ticker = "KXHIGHLAX-26MAY04-T70"
        market = _market()

        def make_getter(name):
            def fn(_t, _m):
                return GaussianForecast(
                    mean_f=70.0, sigma_f=2.0, horizon_hours=8.0,
                    source_name=name, source_tag=f"{name}:test",
                )
            return fn

        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian", make_getter("hrrr")), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian", make_getter("metar")), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian", make_getter("nws_point")), \
             patch("bot.signals.sources.weather.get_weather_gaussian", make_getter("weather")), \
             patch("bot.signals.sources.icon.get_icon_gaussian", make_getter("icon")), \
             patch("bot.signals.sources.ukmo.get_ukmo_gaussian", make_getter("ukmo")), \
             patch("bot.signals.sources.gem.get_gem_gaussian", make_getter("gem")), \
             patch("bot.signals.sources.metno.get_metno_gaussian", make_getter("metno")), \
             patch("bot.signals.sources.ecmwf.get_ecmwf_gaussian", make_getter("ecmwf")), \
             patch("bot.signals.sources.nws_5min.get_nws_5min_gaussian", make_getter("nws_5min")), \
             patch("bot.signals.sources.nws_5min_diurnal.get_nws_5min_diurnal_gaussian",
                   make_getter("nws_5min_diurnal")):
            result = v2._collect_gaussians(ticker, market)

        names = sorted(g.source_name for g in result)
        assert "metno" not in names
        assert "gem" not in names
        # Make sure other sources still come through.
        assert "hrrr" in names
        assert "metar" in names

    def test_no_exclusions_for_austin(self):
        """KXHIGHAUS has no city-specific exclusions per the regression —
        confirm all sources flow through."""
        from unittest.mock import patch

        ticker = "KXHIGHAUS-26MAY04-T80"
        market = _market()

        def make_getter(name):
            def fn(_t, _m):
                return GaussianForecast(
                    mean_f=80.0, sigma_f=2.0, horizon_hours=8.0,
                    source_name=name, source_tag=f"{name}:test",
                )
            return fn

        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian", make_getter("hrrr")), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian", make_getter("metar")), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian", make_getter("nws_point")), \
             patch("bot.signals.sources.weather.get_weather_gaussian", make_getter("weather")), \
             patch("bot.signals.sources.icon.get_icon_gaussian", make_getter("icon")), \
             patch("bot.signals.sources.ukmo.get_ukmo_gaussian", make_getter("ukmo")), \
             patch("bot.signals.sources.gem.get_gem_gaussian", make_getter("gem")), \
             patch("bot.signals.sources.metno.get_metno_gaussian", make_getter("metno")), \
             patch("bot.signals.sources.ecmwf.get_ecmwf_gaussian", make_getter("ecmwf")), \
             patch("bot.signals.sources.nws_5min.get_nws_5min_gaussian", make_getter("nws_5min")), \
             patch("bot.signals.sources.nws_5min_diurnal.get_nws_5min_diurnal_gaussian",
                   make_getter("nws_5min_diurnal")):
            result = v2._collect_gaussians(ticker, market)

        names = sorted(g.source_name for g in result)
        # All forecasters that returned a Gaussian should appear.
        assert "nws_point" in names
        assert "metno" in names
        assert "gem" in names
