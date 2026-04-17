"""Tests for bot/signals/sources/commodity_futures.py."""

from __future__ import annotations

import pytest

from bot.signals.sources import commodity_futures as cf


def test_gate_rejects_non_cpi():
    p, s = cf.get_commodity_cpi_estimate("KXETH-26APR", {"title": "ETH"})
    assert p is None and s is None


def test_gate_accepts_kxcpi(monkeypatch):
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: 0.05)
    p, s = cf.get_commodity_cpi_estimate("KXCPI-26MAY-T3.0", {})
    assert p is not None
    assert s.startswith("commodity:")


def test_gate_accepts_inflation_title(monkeypatch):
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: 0.02)
    p, s = cf.get_commodity_cpi_estimate(
        "MKT", {"title": "Will CPI come in above 3%?"}
    )
    assert p is not None


def test_positive_commodity_move_raises_prob_above(monkeypatch):
    # Strong commodity rally → CPI surprise pushes prob_above higher.
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: 0.20)  # +20%
    p_hot, _ = cf.get_commodity_cpi_estimate("KXCPI-26MAY-T2.5", {})
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: -0.10)  # -10%
    p_cold, _ = cf.get_commodity_cpi_estimate("KXCPI-26MAY-T2.5", {})
    assert p_hot > p_cold


def test_probability_clamped(monkeypatch):
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: 5.0)  # absurd +500%
    p, _ = cf.get_commodity_cpi_estimate("KXCPI-26MAY-T2.5", {})
    assert 0.02 <= p <= 0.98


def test_below_direction(monkeypatch):
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: -0.10)
    p, s = cf.get_commodity_cpi_estimate(
        "MKT", {"title": "Will CPI be below 2%?"}
    )
    assert "below" in s


def test_no_commodity_data_returns_none(monkeypatch):
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: None)
    p, s = cf.get_commodity_cpi_estimate("KXCPI-26MAY-T3.0", {})
    assert p is None and s is None


def test_no_threshold_returns_none(monkeypatch):
    monkeypatch.setattr(cf, "_basket_30d_change", lambda: 0.05)
    p, s = cf.get_commodity_cpi_estimate("KXCPI-26MAY", {"title": "CPI"})
    assert p is None


def test_empty_market_data():
    p, s = cf.get_commodity_cpi_estimate("KXCPI-26MAY-T3.0", None)
    assert p is None and s is None


def test_basket_weight_normalization(monkeypatch):
    # When only one of three commodities returns data, the basket should
    # still normalize to that commodity's pct change (not divide by 3).
    def fake_fetch(sym, days=35):
        if sym == "CL=F":
            return [100.0] * 10 + [110.0] * 20  # +10% move
        return None
    monkeypatch.setattr(cf, "_fetch_yahoo_range", fake_fetch)
    # Clear cache so the fake takes effect
    from bot.api import _CACHE
    for k in list(_CACHE.keys()):
        if k.startswith("commodity::"):
            del _CACHE[k]
    out = cf._basket_30d_change()
    assert out == pytest.approx(0.10, rel=1e-3)
