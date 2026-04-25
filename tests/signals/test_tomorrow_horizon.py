"""Regression guard for CLAUDE.md Known Bug Pattern #8.

Tomorrow.io's API will happily return forecast data 14+ days out, but
accuracy degrades sharply past 7 days. Using the long-horizon points poisons
the ensemble — Tomorrow.io carries weight 0.82 in ``SOURCE_WEIGHTS`` and
gets routed into the bracket CDF math at face value.

The previous implementation only checked ``day_idx >= len(temps_max)`` —
i.e., whatever the API returned was trusted. This test pins an explicit
``day_idx > 7`` clamp inside ``get_tomorrow_weather_estimate``.
"""

from __future__ import annotations

import pytest

from bot.signals.sources import weather as weather_mod


@pytest.fixture
def fake_forecast_14_days() -> dict:
    """Tomorrow.io forecast payload with 14 days of data — what the API can
    return when you don't constrain the horizon."""
    return {
        "daily": {
            "temperature_2m_max": [70.0 + i for i in range(14)],
            "temperature_2m_min": [50.0 + i for i in range(14)],
            "time": [f"2026-04-{20 + i:02d}" for i in range(14)],
        }
    }


@pytest.fixture
def weather_market() -> dict:
    return {
        "title": "Will the high temperature in Miami be above 75 today?",
        "subtitle": "",
    }


def _patch_inner_calls(monkeypatch, day_idx: int, forecast: dict) -> None:
    monkeypatch.setattr(weather_mod, "_detect_city", lambda *a, **kw: "miami")
    monkeypatch.setattr(weather_mod, "_determine_day_index", lambda *a, **kw: day_idx)
    monkeypatch.setattr(weather_mod, "get_tomorrow_forecast", lambda *a, **kw: forecast)


@pytest.mark.parametrize("day_idx", [0, 1, 5, 7])
def test_within_horizon_returns_estimate(
    monkeypatch, weather_market, fake_forecast_14_days, day_idx: int
) -> None:
    _patch_inner_calls(monkeypatch, day_idx, fake_forecast_14_days)
    prob, src = weather_mod.get_tomorrow_weather_estimate(
        "KXHIGHMIA-26APR-T75", weather_market
    )
    assert prob is not None and src is not None, f"day_idx={day_idx} should be in horizon"


@pytest.mark.parametrize("day_idx", [8, 9, 13])
def test_beyond_horizon_returns_none(
    monkeypatch, weather_market, fake_forecast_14_days, day_idx: int
) -> None:
    _patch_inner_calls(monkeypatch, day_idx, fake_forecast_14_days)
    prob, src = weather_mod.get_tomorrow_weather_estimate(
        "KXHIGHMIA-26APR-T75", weather_market
    )
    assert prob is None and src is None, (
        f"day_idx={day_idx} is beyond Tomorrow.io's 7-day reliable horizon "
        f"and must be rejected (got prob={prob}, src={src})"
    )
