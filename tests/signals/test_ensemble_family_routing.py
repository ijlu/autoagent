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
    def fake_cal(prob, corrections, ticker=None):
        return prob - 0.10
    monkeypatch.setattr(ensemble, "apply_calibration_correction", fake_cal)

    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY", {}, 0.5, 100.0,
        calibration_corrections={"buckets": "placeholder"},
    )
    assert prob == pytest.approx(0.60, abs=1e-6)
    assert n == 1


# ── Weather v2 short-circuit (overrides family router) ──────────────────────


def test_weather_v2_short_circuits_when_flag_enabled(monkeypatch):
    """When WEATHER_ENSEMBLE_V2 is true and the ticker is weather, we should
    call predict_v2 directly and skip the family router AND the generic
    sources AND the Platt calibration."""
    import bot.config as _config
    monkeypatch.setattr(_config, "WEATHER_ENSEMBLE_V2", True)

    router_called = {"n": 0}
    monkeypatch.setattr(ensemble, "route_family",
                        lambda t, m: (router_called.update(n=router_called["n"] + 1) or (0.65, "weather_ensemble:x")))

    cal_called = {"n": 0}
    def fake_cal(prob, corrections, ticker=None):
        cal_called["n"] += 1
        return prob - 0.10
    monkeypatch.setattr(ensemble, "apply_calibration_correction", fake_cal)

    # Stub predict_v2 to return a known value.
    import bot.signals.weather_ensemble_v2 as v2
    monkeypatch.setattr(
        v2, "predict_v2",
        lambda t, m: (0.83, "weather_ensemble_v2:hrrr+nbm+nws_point+weather+metar+madis+afd"),
    )

    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY-26APR27-T75", {"title": "high"}, 0.5, 100.0,
        calibration_corrections={"buckets": "x"},
    )
    assert prob == pytest.approx(0.83, abs=1e-6)
    assert src.startswith("weather_ensemble_v2:")
    # Tag has 7 plus-separated tokens but we cap at 5 independent groups.
    assert n == 5
    # Router was NOT called because v2 short-circuited.
    assert router_called["n"] == 0
    # Platt was NOT called either.
    assert cal_called["n"] == 0


def test_weather_v2_falls_through_when_flag_disabled(monkeypatch):
    """When WEATHER_ENSEMBLE_V2 is false (default), we hit the family router
    as before — no v2 call."""
    import bot.config as _config
    monkeypatch.setattr(_config, "WEATHER_ENSEMBLE_V2", False)

    v2_called = {"n": 0}
    import bot.signals.weather_ensemble_v2 as v2
    def fake_v2(t, m):
        v2_called["n"] += 1
        return (0.83, "weather_ensemble_v2:x")
    monkeypatch.setattr(v2, "predict_v2", fake_v2)

    monkeypatch.setattr(ensemble, "route_family",
                        lambda t, m: (0.65, "weather_ensemble:legacy"))

    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY-26APR27-T75", {"title": "high"}, 0.5, 100.0,
    )
    assert prob == pytest.approx(0.65, abs=1e-6)
    assert v2_called["n"] == 0


def test_weather_v2_falls_through_on_predict_v2_exception(monkeypatch):
    """If predict_v2 raises, we fall back to the family router cleanly —
    never leave the caller without an estimate."""
    import bot.config as _config
    monkeypatch.setattr(_config, "WEATHER_ENSEMBLE_V2", True)

    import bot.signals.weather_ensemble_v2 as v2
    def boom(t, m):
        raise RuntimeError("simulated v2 failure")
    monkeypatch.setattr(v2, "predict_v2", boom)

    monkeypatch.setattr(ensemble, "route_family",
                        lambda t, m: (0.65, "weather_ensemble:fallback"))

    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY-26APR27-T75", {"title": "high"}, 0.5, 100.0,
    )
    assert prob == pytest.approx(0.65, abs=1e-6)
    assert src.startswith("weather_ensemble:fallback")


def test_weather_v2_skipped_for_non_weather_ticker(monkeypatch):
    """v2 is weather-only; non-weather tickers should still hit the router."""
    import bot.config as _config
    monkeypatch.setattr(_config, "WEATHER_ENSEMBLE_V2", True)

    v2_called = {"n": 0}
    import bot.signals.weather_ensemble_v2 as v2
    def fake_v2(t, m):
        v2_called["n"] += 1
        return (0.83, "weather_ensemble_v2:x")
    monkeypatch.setattr(v2, "predict_v2", fake_v2)

    monkeypatch.setattr(ensemble, "route_family",
                        lambda t, m: (0.55, "fedwatch:legacy"))

    prob, src, n = ensemble.get_independent_estimate(
        "KXFED-27APR-T2.00", {"title": "fed funds"}, 0.5, 100.0,
    )
    assert prob == pytest.approx(0.55, abs=1e-6)
    assert v2_called["n"] == 0  # never called for non-weather


def test_weather_v2_returns_none_falls_through(monkeypatch):
    """If predict_v2 returns None (e.g. no sources fired), fall back to
    the router rather than returning None outright."""
    import bot.config as _config
    monkeypatch.setattr(_config, "WEATHER_ENSEMBLE_V2", True)

    import bot.signals.weather_ensemble_v2 as v2
    monkeypatch.setattr(v2, "predict_v2", lambda t, m: (None, None))

    monkeypatch.setattr(ensemble, "route_family",
                        lambda t, m: (0.65, "weather_ensemble:legacy"))

    prob, src, n = ensemble.get_independent_estimate(
        "KXHIGHNY-26APR27-T75", {"title": "high"}, 0.5, 100.0,
    )
    assert prob == pytest.approx(0.65, abs=1e-6)
    assert src.startswith("weather_ensemble:legacy")
