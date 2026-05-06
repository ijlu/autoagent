"""Tests for bot/signals/sources/adp_nfp.py.

The ADP source has three failure surfaces worth guarding:
  1. Gating: it should only respond to KXJOB tickers or payroll titles.
  2. Threshold parsing: `-T150`, "above 175k", "below 100,000" variants.
  3. FRED data shaping: latest - previous = monthly change, not absolute level.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bot.signals.sources import adp_nfp


def test_gate_rejects_non_jobs_ticker():
    p, s = adp_nfp.get_adp_estimate("KXETH-26APR", {"title": "ETH price"})
    assert p is None and s is None


def test_gate_accepts_kxjob_ticker(monkeypatch):
    monkeypatch.setattr(adp_nfp, "_latest_monthly_change_k", lambda: 180.0)
    p, s = adp_nfp.get_adp_estimate("KXJOB-26MAY-T150", {"title": "NFP"})
    assert p is not None
    assert s.startswith("adp_nfp:")


def test_gate_accepts_nfp_title_even_without_kxjob_prefix(monkeypatch):
    monkeypatch.setattr(adp_nfp, "_latest_monthly_change_k", lambda: 200.0)
    p, s = adp_nfp.get_adp_estimate(
        "MARKET-XY", {"title": "Will nonfarm payrolls come in above 150k?"}
    )
    assert p is not None


def test_threshold_from_ticker_suffix(monkeypatch):
    monkeypatch.setattr(adp_nfp, "_latest_monthly_change_k", lambda: 200.0)
    p, s = adp_nfp.get_adp_estimate("KXJOB-26MAY-T150", {})
    # ADP estimate (200k) > threshold (150k) → P(above) > 0.5
    assert p > 0.5


def test_threshold_below_direction(monkeypatch):
    monkeypatch.setattr(adp_nfp, "_latest_monthly_change_k", lambda: 50.0)
    # "below 100k" with ADP saying 50k → P(below) > 0.5
    p, s = adp_nfp.get_adp_estimate(
        "MKT", {"title": "Will NFP come in below 100k?"}
    )
    assert p > 0.5
    assert "below" in s


def test_probability_clamped(monkeypatch):
    # Extreme ADP number should still clamp to [0.02, 0.98]
    monkeypatch.setattr(adp_nfp, "_latest_monthly_change_k", lambda: 1000.0)
    p, s = adp_nfp.get_adp_estimate("KXJOB-26MAY-T150", {})
    assert 0.02 <= p <= 0.98


def test_no_threshold_returns_none(monkeypatch):
    monkeypatch.setattr(adp_nfp, "_latest_monthly_change_k", lambda: 200.0)
    # No -T suffix, no threshold words
    p, s = adp_nfp.get_adp_estimate("KXJOB-26MAY", {"title": "Jobs report"})
    assert p is None


def test_no_adp_data_returns_none(monkeypatch):
    monkeypatch.setattr(adp_nfp, "_latest_monthly_change_k", lambda: None)
    p, s = adp_nfp.get_adp_estimate("KXJOB-26MAY-T150", {})
    assert p is None and s is None


def test_empty_market_data():
    p, s = adp_nfp.get_adp_estimate("KXJOB-26MAY-T150", None)
    assert p is None and s is None


def test_gaussian_cdf_monotonic():
    # Sanity check on Gaussian math
    assert adp_nfp._gaussian_cdf(100, 100, 10) == pytest.approx(0.5)
    assert adp_nfp._gaussian_cdf(110, 100, 10) > 0.5
    assert adp_nfp._gaussian_cdf(90, 100, 10) < 0.5


def test_monthly_change_prefers_monthly_over_weekly(monkeypatch):
    def fake_fetch(series_id, limit=12):
        if series_id == adp_nfp._ADP_MONTHLY_SERIES_ID:
            return [{"value": "1000"}, {"value": "800"}]
        return [{"value": str(i * 10)} for i in range(12)]
    monkeypatch.setattr(adp_nfp, "_fetch_fred_series", fake_fetch)
    out = adp_nfp._latest_monthly_change_k()
    assert out == 200.0  # 1000 - 800


def test_monthly_change_falls_back_to_weekly(monkeypatch):
    def fake_fetch(series_id, limit=12):
        if series_id == adp_nfp._ADP_MONTHLY_SERIES_ID:
            return None  # monthly unavailable
        # weekly: 12 observations, descending (most recent first)
        return [{"value": str(x)} for x in [50, 40, 30, 20, 10, 15, 20, 25, 5, 5, 5, 5]]
    monkeypatch.setattr(adp_nfp, "_fetch_fred_series", fake_fetch)
    out = adp_nfp._latest_monthly_change_k()
    assert out is not None
