"""Tests for the predict_v2 → apply_calibration_correction wiring
(Phase 2 item 2).

Default behavior (WEATHER_V2_PLATT_ENABLED=false) MUST be a no-op vs
pre-Phase-2 production behavior — v2 output returns raw clamped to
[0.02, 0.98]. Flag-on path exercises the Platt correction with a
representative curve and confirms the transform fires when the global
CALIBRATION_ENABLED gate is also on.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _weather_market() -> dict:
    """Minimal market dict that predict_v2 + ensemble accept."""
    return {
        "ticker": "KXHIGHNY-26MAY15-T70.0",
        "title": "Will the high in NY exceed 70 on May 15?",
        "yes_ask_dollars": "0.50",
        "yes_bid_dollars": "0.48",
        "no_ask_dollars": "0.52",
        "no_bid_dollars": "0.50",
        "volume": 100,
        "close_time": "2026-05-15T23:59:00+00:00",
    }


def _curve_shrink_weather() -> dict:
    """Representative Platt curve matching the production VPS state
    pulled 2026-05-10: weather-family A=0.5, B≈-1.13."""
    return {
        "method": "platt",
        "A": 0.5, "B": -1.245,
        "families": {
            "KXHIGHNY": {"A": 0.5, "B": -1.13},
        },
    }


# ── Default-off path: behavior unchanged ──────────────────────────────────


def test_default_flag_off_returns_raw_v2(monkeypatch):
    """WEATHER_V2_PLATT_ENABLED default = false → no calibration call,
    raw v2 prob returned (clamped)."""
    monkeypatch.setattr("bot.config.WEATHER_ENSEMBLE_V2", True)
    monkeypatch.setattr("bot.config.WEATHER_V2_PLATT_ENABLED", False)
    monkeypatch.setattr("bot.config.CALIBRATION_ENABLED", True)

    from bot.signals.ensemble import get_independent_estimate

    with patch(
        "bot.signals.weather_ensemble_v2.predict_v2",
        return_value=(0.85, "v2:metar+hrrr+nbm"),
    ), patch(
        "bot.signals.ensemble.apply_calibration_correction"
    ) as mock_cal:
        prob, tag, n = get_independent_estimate(
            "KXHIGHNY-26MAY15-T70.0",
            _weather_market(),
            yes_ask=0.50,
            volume=100,
            calibration_corrections=_curve_shrink_weather(),
        )
        assert prob == pytest.approx(0.85), \
            f"flag-off must return raw v2 prob, got {prob}"
        # Critical: calibration must NOT be invoked on v2 path when flag is off
        mock_cal.assert_not_called()


def test_no_curve_passed_returns_raw_v2(monkeypatch):
    """Flag-on but no calibration_corrections dict → no transform attempted,
    raw returned. (Defensive: prevents NoneType crashes if scheduler hasn't
    fit the curve yet.)"""
    monkeypatch.setattr("bot.config.WEATHER_ENSEMBLE_V2", True)
    monkeypatch.setattr("bot.config.WEATHER_V2_PLATT_ENABLED", True)

    from bot.signals.ensemble import get_independent_estimate

    with patch(
        "bot.signals.weather_ensemble_v2.predict_v2",
        return_value=(0.85, "v2:metar"),
    ):
        prob, tag, n = get_independent_estimate(
            "KXHIGHNY-26MAY15-T70.0",
            _weather_market(),
            yes_ask=0.50,
            volume=100,
            calibration_corrections=None,
        )
        assert prob == pytest.approx(0.85)


# ── Flag-on path: Platt actually fires ────────────────────────────────────


def test_flag_on_applies_platt_with_calibration_enabled(monkeypatch):
    """WEATHER_V2_PLATT_ENABLED=true AND CALIBRATION_ENABLED=true →
    v2 output flows through Platt and gets shrunk per the curve."""
    monkeypatch.setattr("bot.config.WEATHER_ENSEMBLE_V2", True)
    monkeypatch.setattr("bot.config.WEATHER_V2_PLATT_ENABLED", True)
    monkeypatch.setattr("bot.config.CALIBRATION_ENABLED", True)

    from bot.signals.ensemble import get_independent_estimate

    with patch(
        "bot.signals.weather_ensemble_v2.predict_v2",
        return_value=(0.85, "v2:metar+hrrr"),
    ):
        prob, tag, n = get_independent_estimate(
            "KXHIGHNY-26MAY15-T70.0",
            _weather_market(),
            yes_ask=0.50,
            volume=100,
            calibration_corrections=_curve_shrink_weather(),
        )
    # With A=0.5, B=-1.13: logit(0.85)=1.7346, *0.5=0.8673, +(-1.13)=-0.2627,
    # sigmoid(-0.2627)≈0.4347
    assert prob == pytest.approx(0.435, abs=0.005), \
        f"Expected ~0.435 from Platt with A=0.5 B=-1.13, got {prob}"


def test_flag_on_but_calibration_disabled_is_noop(monkeypatch):
    """WEATHER_V2_PLATT_ENABLED=true but CALIBRATION_ENABLED=false →
    apply_calibration_correction is invoked but returns raw (gated by
    the global flag inside apply_calibration). End-to-end behavior =
    raw passthrough."""
    monkeypatch.setattr("bot.config.WEATHER_ENSEMBLE_V2", True)
    monkeypatch.setattr("bot.config.WEATHER_V2_PLATT_ENABLED", True)
    monkeypatch.setattr("bot.config.CALIBRATION_ENABLED", False)

    from bot.signals.ensemble import get_independent_estimate

    with patch(
        "bot.signals.weather_ensemble_v2.predict_v2",
        return_value=(0.85, "v2:metar"),
    ):
        prob, tag, n = get_independent_estimate(
            "KXHIGHNY-26MAY15-T70.0",
            _weather_market(),
            yes_ask=0.50,
            volume=100,
            calibration_corrections=_curve_shrink_weather(),
        )
    # CALIBRATION_ENABLED=false inside apply_calibration → returns raw
    assert prob == pytest.approx(0.85, abs=0.005), \
        f"Calibration disabled gate must dominate; got {prob}"


def test_flag_on_identity_curve_is_noop(monkeypatch):
    """Identity curve method → returns input unchanged even if flag on."""
    monkeypatch.setattr("bot.config.WEATHER_ENSEMBLE_V2", True)
    monkeypatch.setattr("bot.config.WEATHER_V2_PLATT_ENABLED", True)
    monkeypatch.setattr("bot.config.CALIBRATION_ENABLED", True)

    from bot.signals.ensemble import get_independent_estimate

    identity_curve = {"method": "identity"}
    with patch(
        "bot.signals.weather_ensemble_v2.predict_v2",
        return_value=(0.85, "v2:metar"),
    ):
        prob, tag, n = get_independent_estimate(
            "KXHIGHNY-26MAY15-T70.0",
            _weather_market(),
            yes_ask=0.50,
            volume=100,
            calibration_corrections=identity_curve,
        )
    assert prob == pytest.approx(0.85, abs=0.005)


# ── Family-router path unchanged ──────────────────────────────────────────


def test_family_router_path_still_calibrates(monkeypatch):
    """The family-router branch (e.g., non-weather tickers) must continue
    to call apply_calibration_correction regardless of the v2 flag. This
    test guards against an inadvertent regression where the v2 wiring
    accidentally bypasses family-router calibration."""
    monkeypatch.setattr("bot.config.WEATHER_ENSEMBLE_V2", False)
    monkeypatch.setattr("bot.config.WEATHER_V2_PLATT_ENABLED", False)
    monkeypatch.setattr("bot.config.CALIBRATION_ENABLED", True)

    from bot.signals.ensemble import get_independent_estimate

    # Force the family router to return a known value; calibration_corrections
    # is a real curve.
    with patch(
        "bot.signals.ensemble.route_family",
        return_value=(0.85, "adp_nfp:test"),
    ):
        prob, tag, n = get_independent_estimate(
            "KXJOB-26MAY-200K",
            {"ticker": "KXJOB-26MAY-200K", "title": "non-weather",
             "yes_ask_dollars": "0.50", "yes_bid_dollars": "0.48",
             "no_ask_dollars": "0.52", "no_bid_dollars": "0.50",
             "volume": 100,
             "close_time": "2026-05-15T23:59:00+00:00"},
            yes_ask=0.50,
            volume=100,
            calibration_corrections=_curve_shrink_weather(),
        )
    # Family router output 0.85 should be Platt-transformed by the
    # GLOBAL curve segment (A=0.5, B=-1.245, since KXJOB has no
    # family-specific segment in the test curve).
    # logit(0.85)=1.7346 * 0.5 = 0.8673 + (-1.245) = -0.3777 →
    # sigmoid(-0.3777) ≈ 0.4067
    assert prob == pytest.approx(0.407, abs=0.005), \
        (f"Family router output must continue to be Platt-corrected; "
         f"got {prob} (expected ~0.407)")
