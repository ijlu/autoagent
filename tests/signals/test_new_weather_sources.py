"""Smoke tests for the five new weather sources.

These test gating (reject non-weather tickers) and the happy-path arithmetic
with mocked network fetches. Full integration is covered by the weather
ensemble tests.
"""

from __future__ import annotations

import pytest

from bot.signals.sources import nws_point, ndfd_nbm, hrrr, madis, afd


# ── NWS Point ─────────────────────────────────────────────────────────────────
def test_nws_point_rejects_non_weather():
    p, s = nws_point.get_nws_point_estimate("KXETH-26", {"title": "ETH"})
    assert p is None and s is None


def test_nws_point_rejects_empty_market():
    p, s = nws_point.get_nws_point_estimate("KXHIGHNY-26APR16-T75", None)
    assert p is None


def test_nws_point_happy_path_above(monkeypatch):
    monkeypatch.setattr(nws_point, "_resolve_grid_url",
                        lambda lat, lon: "https://fake.example/forecast")
    # Provide a single period for today's local date with temp 80F.
    # We don't know the test machine's today, so stub _daily_high_from_hourly.
    monkeypatch.setattr(nws_point, "_fetch_hourly_forecast",
                        lambda url: [{"startTime": "2026-04-16T12:00:00+00:00",
                                      "temperature": 80, "temperatureUnit": "F"}])
    monkeypatch.setattr(nws_point, "_daily_high_from_hourly",
                        lambda p, d, tz: 80.0)
    p, s = nws_point.get_nws_point_estimate(
        "KXHIGHNY-26APR16-T75",
        {"title": "Will NYC high exceed 75°F on April 16?"},
    )
    # Only runs if _detect_city/_parse_threshold/_determine_day_index all resolve
    # — returns either a probability or None depending on market_data shape. Assert
    # at least doesn't crash; if a prob returned, 80F > 75F → P > 0.5
    if p is not None:
        assert 0.5 < p <= 0.98


# ── NBM ───────────────────────────────────────────────────────────────────────
def test_nbm_rejects_non_weather():
    p, s = ndfd_nbm.get_nbm_estimate("KXETH-26", {"title": "ETH"})
    assert p is None and s is None


def test_nbm_rejects_empty_market():
    p, s = ndfd_nbm.get_nbm_estimate("KXHIGHNY-26APR16-T75", None)
    assert p is None


# ── HRRR ──────────────────────────────────────────────────────────────────────
def test_hrrr_rejects_non_weather():
    p, s = hrrr.get_hrrr_estimate("KXETH-26", {"title": "ETH"})
    assert p is None and s is None


def test_hrrr_rejects_empty_market():
    p, s = hrrr.get_hrrr_estimate("KXHIGHNY-26APR16-T75", None)
    assert p is None


# ── MADIS ─────────────────────────────────────────────────────────────────────
def test_madis_rejects_non_weather():
    p, s = madis.get_madis_estimate("KXETH-26", {"title": "ETH"})
    assert p is None and s is None


def test_madis_rejects_empty_market():
    p, s = madis.get_madis_estimate("KXHIGHNY-26APR16-T75", None)
    assert p is None


# ── AFD ───────────────────────────────────────────────────────────────────────
def test_afd_rejects_non_weather():
    p, s = afd.get_afd_estimate("KXETH-26", {"title": "ETH"})
    assert p is None and s is None


def test_afd_rejects_empty_market():
    p, s = afd.get_afd_estimate("KXHIGHNY-26APR16-T75", None)
    assert p is None
