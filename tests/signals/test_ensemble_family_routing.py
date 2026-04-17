"""Test the ensemble's family-router short-circuit path.

When a ticker matches a registered family prefix (KXHIGH, KXJOB, KXGDP, KXCPI),
get_independent_estimate() should return the router's output directly as a
single-source estimate and *skip* the generic 15-source sweep.
"""

from __future__ import annotations

import pytest

from bot.signals import ensemble


def test_family_routed_ticker_bypasses_generic_sweep(monkeypatch):
    """KXHIGH ticker → router returns (0.65, 'weather_ensemble:...') →
    get_independent_estimate should return that verbatim (modulo clamp)
    with n_sources = 1, and NOT call any of the generic sources."""
    router_called = {"count": 0}
    generic_called = {"count": 0}

    def fake_router(ticker, market_data):
        router_called["count"] += 1
        return (0.65, "weather_ensemble:metar+hrrr+nbm")

    def never_call(*a, **kw):
        generic_called["count"] += 1
        return (0.5, "should_not_be_called")

    monkeypatch.setattr(ensemble, "route_family", fake_router)
    # Stub every generic source — none should be called.
    for attr in ("get_polymarket_estimate", "get_crypto_estimate",
                 "get_weather_estimate", "get_tomorrow_weather_estimate",
                 "get_noaa_alerts_for_market", "get_metar_observation_estimate",
                 "get_fred_estimate", "get_cleveland_fed_nowcast",
                 "get_bls_estimate", "get_fedwatch_estimate",
                 "get_sports_estimate", "get_metaculus_estimate",
                 "get_news_sentiment", "get_company_kpi_estimate",
                 "get_sensortower_estimate", "get_series_estimate"):
        monkeypatch.setattr(ensemble, attr, never_call)

    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY-26APR16", {"title": "NYC high"}, 0.5, 1000.0,
    )
    assert prob == pytest.approx(0.65, rel=1e-6)
    assert src.startswith("weather_ensemble:")
    assert n == 1
    assert router_called["count"] == 1
    assert generic_called["count"] == 0


def test_router_returning_none_falls_through(monkeypatch):
    """Router None → generic sources get called."""
    monkeypatch.setattr(ensemble, "route_family", lambda t, m: None)

    sentinel_called = {"n": 0}
    def fake_poly(t, m):
        sentinel_called["n"] += 1
        return (None, None)
    monkeypatch.setattr(ensemble, "get_polymarket_estimate", fake_poly)

    # Others return None so we don't have to stub them deeply
    for attr in ("get_crypto_estimate", "get_weather_estimate",
                 "get_tomorrow_weather_estimate", "get_noaa_alerts_for_market",
                 "get_metar_observation_estimate", "get_fred_estimate",
                 "get_cleveland_fed_nowcast", "get_bls_estimate",
                 "get_fedwatch_estimate", "get_sports_estimate",
                 "get_metaculus_estimate", "get_news_sentiment",
                 "get_company_kpi_estimate", "get_sensortower_estimate",
                 "get_series_estimate"):
        monkeypatch.setattr(ensemble, attr, lambda t, m: (None, None))

    ensemble.get_independent_estimate("KXRANDOM-26", {}, 0.5, 1000.0)
    assert sentinel_called["n"] == 1


def test_router_returning_none_prob_falls_through(monkeypatch):
    """Router returns (None, None) tuple → treat as 'no signal', fall through."""
    monkeypatch.setattr(ensemble, "route_family", lambda t, m: (None, None))

    poly_called = {"n": 0}
    def fake_poly(t, m):
        poly_called["n"] += 1
        return (None, None)
    monkeypatch.setattr(ensemble, "get_polymarket_estimate", fake_poly)
    for attr in ("get_crypto_estimate", "get_weather_estimate",
                 "get_tomorrow_weather_estimate", "get_noaa_alerts_for_market",
                 "get_metar_observation_estimate", "get_fred_estimate",
                 "get_cleveland_fed_nowcast", "get_bls_estimate",
                 "get_fedwatch_estimate", "get_sports_estimate",
                 "get_metaculus_estimate", "get_news_sentiment",
                 "get_company_kpi_estimate", "get_sensortower_estimate",
                 "get_series_estimate"):
        monkeypatch.setattr(ensemble, attr, lambda t, m: (None, None))

    ensemble.get_independent_estimate("KXHIGHNY", {}, 0.5, 1000.0)
    assert poly_called["n"] == 1  # fell through


def test_router_exception_does_not_crash_ensemble(monkeypatch):
    def boom(t, m):
        raise RuntimeError("router blew up")
    monkeypatch.setattr(ensemble, "route_family", boom)

    # Generic sources all return None — should still yield a graceful return
    for attr in ("get_polymarket_estimate", "get_crypto_estimate",
                 "get_weather_estimate", "get_tomorrow_weather_estimate",
                 "get_noaa_alerts_for_market", "get_metar_observation_estimate",
                 "get_fred_estimate", "get_cleveland_fed_nowcast",
                 "get_bls_estimate", "get_fedwatch_estimate",
                 "get_sports_estimate", "get_metaculus_estimate",
                 "get_news_sentiment", "get_company_kpi_estimate",
                 "get_sensortower_estimate", "get_series_estimate"):
        monkeypatch.setattr(ensemble, attr, lambda t, m: (None, None))

    # Should not raise
    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY", {}, 0.5, 100.0
    )
    # No sources returned anything → None
    assert prob is None


def test_router_applies_calibration_correction(monkeypatch):
    """Calibration correction should still modify the router output."""
    monkeypatch.setattr(ensemble, "route_family",
                        lambda t, m: (0.70, "weather_ensemble:x"))

    # Fake calibration: always push prob down by 0.10
    def fake_cal(prob, corrections):
        return prob - 0.10
    monkeypatch.setattr(ensemble, "apply_calibration_correction", fake_cal)

    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY", {}, 0.5, 100.0,
        calibration_corrections={"buckets": "placeholder"},
    )
    assert prob == pytest.approx(0.60, abs=1e-6)
    assert n == 1
