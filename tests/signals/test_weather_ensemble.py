"""Tests for bot/signals/weather_ensemble.py.

Gates we care about:
  1. Only fires for weather tickers (KXHIGH / KXHMONTHRANGE / KXHURR).
  2. Combines multiple sources into a weighted probability.
  3. Gracefully handles any subset of sources returning None.
  4. Weights fall back to priors when no learned weights exist.

weather_ensemble.predict() uses *lazy* (function-local) imports to avoid
circular deps. That means we can't patch attributes on the module itself —
we have to patch the *source* modules that predict() imports lazily.
"""

from __future__ import annotations

import pytest

from bot.signals import weather_ensemble as we
from bot.signals.sources import (
    metar_observations, weather as wxmod, nws_point, ndfd_nbm, hrrr,
    madis, afd,
)


def _patch_all(monkeypatch, metar=None, nws=None, nbm=None, hrrr_=None,
               tmrw=None, wx=None, madis_=None, afd_=None, noaa=None):
    """Stub every source the ensemble imports lazily."""
    monkeypatch.setattr(metar_observations, "get_metar_observation_estimate",
                        lambda t, m: metar if metar is not None else (None, None))
    monkeypatch.setattr(nws_point, "get_nws_point_estimate",
                        lambda t, m: nws if nws is not None else (None, None))
    monkeypatch.setattr(ndfd_nbm, "get_nbm_estimate",
                        lambda t, m: nbm if nbm is not None else (None, None))
    monkeypatch.setattr(hrrr, "get_hrrr_estimate",
                        lambda t, m: hrrr_ if hrrr_ is not None else (None, None))
    monkeypatch.setattr(wxmod, "get_tomorrow_weather_estimate",
                        lambda t, m: tmrw if tmrw is not None else (None, None))
    monkeypatch.setattr(wxmod, "get_weather_estimate",
                        lambda t, m: wx if wx is not None else (None, None))
    monkeypatch.setattr(wxmod, "get_noaa_alerts_for_market",
                        lambda t, m: noaa if noaa is not None else (None, None))
    monkeypatch.setattr(madis, "get_madis_estimate",
                        lambda t, m: madis_ if madis_ is not None else (None, None))
    monkeypatch.setattr(afd, "get_afd_estimate",
                        lambda t, m: afd_ if afd_ is not None else (None, None))
    # Disable DB side effects + learned-weight lookup
    monkeypatch.setattr(we, "_get_learned_weights", lambda series: we.DEFAULT_WEATHER_PRIORS)
    # Swallow DB writes
    monkeypatch.setattr(we, "db_write", lambda fn: None)


def test_non_weather_ticker_returns_none(monkeypatch):
    _patch_all(monkeypatch, metar=(0.8, "metar"))
    out = we.predict("KXFED-26JUL-T5.0", {"title": "Fed rate"})
    assert out == (None, None)


def test_weather_ticker_with_sources_returns_prob(monkeypatch):
    _patch_all(monkeypatch,
               metar=(0.60, "metar:75"), nws=(0.58, "nws:76"),
               nbm=(0.57, "nbm:76"), hrrr_=(0.62, "hrrr:77"))
    p, s = we.predict("KXHIGHNY-26APR16", {"title": "NYC high"})
    assert p is not None
    assert 0.0 < p < 1.0
    assert s.startswith("weather_ensemble:")


def test_all_sources_none_returns_none(monkeypatch):
    _patch_all(monkeypatch)
    out = we.predict("KXHIGHNY-26APR16", {"title": "NYC high"})
    assert out == (None, None)


def test_metar_only_equals_metar_prob(monkeypatch):
    # Only METAR has an opinion → combined probability must equal METAR's.
    _patch_all(monkeypatch, metar=(0.85, "metar:cur=90"))
    p, _ = we.predict("KXHIGHNY", {"title": "NYC"})
    assert p == pytest.approx(0.85, rel=1e-6)


def test_weighted_average_biased_toward_high_weight(monkeypatch):
    # METAR (w=1.0) at 0.80 and Open-Meteo (w=0.65) at 0.40.
    # Expected: (0.80*1.0 + 0.40*0.65) / (1.0 + 0.65) = 0.6424…
    _patch_all(monkeypatch, metar=(0.80, "m"), wx=(0.40, "w"))
    p, _ = we.predict("KXHIGHNY", {"title": "NYC"})
    w_m = we.DEFAULT_WEATHER_PRIORS["metar"]
    w_w = we.DEFAULT_WEATHER_PRIORS["weather"]
    expected = (0.80 * w_m + 0.40 * w_w) / (w_m + w_w)
    assert p == pytest.approx(expected, abs=1e-3)


def test_output_clamped_upper(monkeypatch):
    _patch_all(monkeypatch,
               metar=(0.999, "m"), nws=(0.999, "n"), nbm=(0.999, "b"),
               hrrr_=(0.999, "h"), wx=(0.999, "w"))
    p, _ = we.predict("KXHIGHNY", {"title": "NYC"})
    assert p <= 0.98


def test_output_clamped_lower(monkeypatch):
    _patch_all(monkeypatch,
               metar=(0.001, "m"), nws=(0.001, "n"), nbm=(0.001, "b"))
    p, _ = we.predict("KXHIGHNY", {"title": "NYC"})
    assert p >= 0.02


def test_kxhmonthrange_fires(monkeypatch):
    _patch_all(monkeypatch, metar=(0.55, "m"))
    p, _ = we.predict("KXHMONTHRANGENYC-26APR", {})
    assert p is not None


def test_kxhurr_fires(monkeypatch):
    _patch_all(monkeypatch, metar=(0.55, "m"))
    p, _ = we.predict("KXHURRCAT5-26", {})
    assert p is not None


def test_source_exception_is_swallowed(monkeypatch):
    def boom(t, m):
        raise RuntimeError("simulated source blew up")
    _patch_all(monkeypatch, metar=(0.60, "m"))
    monkeypatch.setattr(hrrr, "get_hrrr_estimate", boom)
    # Must still return metar-based estimate — a single bad source doesn't
    # poison the ensemble.
    p, _ = we.predict("KXHIGHNY", {"title": "NYC"})
    assert p == pytest.approx(0.60, rel=1e-6)
