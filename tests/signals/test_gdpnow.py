"""Tests for bot/signals/sources/gdpnow.py."""

from __future__ import annotations

import pytest

from bot.signals.sources import gdpnow


def test_gate_rejects_non_gdp_ticker():
    p, s = gdpnow.get_gdpnow_estimate("KXETH-26APR", {"title": "ETH"})
    assert p is None and s is None


def test_gate_accepts_kxgdp(monkeypatch):
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: 2.7)
    p, s = gdpnow.get_gdpnow_estimate("KXGDP-26Q2-T2.5", {})
    assert p is not None
    assert s.startswith("gdpnow:")


def test_gate_accepts_gdp_title(monkeypatch):
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: 3.0)
    p, s = gdpnow.get_gdpnow_estimate(
        "MKT", {"title": "Will Q2 GDP grow more than 2.5%?"}
    )
    assert p is not None


def test_probability_reflects_nowcast_direction(monkeypatch):
    # Nowcast 3.0 > threshold 2.0 → P(above) > 0.5
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: 3.0)
    p, _ = gdpnow.get_gdpnow_estimate("KXGDP-26Q2-T2.0", {})
    assert p > 0.5

    # Nowcast 1.0 < threshold 2.0 → P(above) < 0.5
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: 1.0)
    p, _ = gdpnow.get_gdpnow_estimate("KXGDP-26Q2-T2.0", {})
    assert p < 0.5


def test_below_direction(monkeypatch):
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: 0.5)
    p, s = gdpnow.get_gdpnow_estimate(
        "MKT", {"title": "Will GDP grow less than 1%?"}
    )
    assert p > 0.5
    assert "below" in s


def test_probability_clamped(monkeypatch):
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: 50.0)
    p, _ = gdpnow.get_gdpnow_estimate("KXGDP-26Q2-T2.0", {})
    assert 0.02 <= p <= 0.98


def test_no_threshold_returns_none(monkeypatch):
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: 2.5)
    p, s = gdpnow.get_gdpnow_estimate("KXGDP-26Q2", {"title": "GDP release"})
    assert p is None


def test_no_nowcast_returns_none(monkeypatch):
    monkeypatch.setattr(gdpnow, "_fetch_gdpnow", lambda: None)
    p, s = gdpnow.get_gdpnow_estimate("KXGDP-26Q2-T2.5", {})
    assert p is None and s is None


def test_empty_market_data():
    p, s = gdpnow.get_gdpnow_estimate("KXGDP-26Q2-T2.5", None)
    assert p is None and s is None
