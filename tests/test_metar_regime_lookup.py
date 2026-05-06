"""Tests for the regime-aware METAR σ resolution path.

Pins:
- WEATHER_REGIME_SIGMA off → σ used IS pooled (production behavior unchanged)
- WEATHER_REGIME_SIGMA on  → tier 1 fit wins when present
- _RESIDUAL_TIER_META is populated regardless of flag (for snapshot capture)
- get_residual_tier_meta pops the entry (no unbounded growth)
- Health counter tracks the chosen tier
"""
from __future__ import annotations

from unittest import mock

import pytest

from bot.db import init_db, kv_set
from bot.signals.sources import metar_observations as m


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c


@pytest.fixture(autouse=True)
def _reset_state():
    m._RESIDUAL_TIER_META.clear()
    for k in list(m._REGIME_TIER_COUNTS):
        m._REGIME_TIER_COUNTS[k] = 0
    yield
    m._RESIDUAL_TIER_META.clear()
    for k in list(m._REGIME_TIER_COUNTS):
        m._REGIME_TIER_COUNTS[k] = 0


def test_flag_off_uses_pooled_even_when_regime_present(conn):
    """Production behavior must be byte-identical when the flag is off."""
    # Persist a pooled fit AND a regime fit.
    kv_set(conn, "weather_metar_residual_sigma_KMIA_14",
           {"sigma": 1.5, "n": 30}, 86400)
    kv_set(conn, "weather_metar_residual_sigma_regime_KMIA_14_E|partly",
           {"sigma": 0.5, "n": 8, "tier": "regime_hour"}, 86400)

    # WEATHER_REGIME_SIGMA is read inside _resolve_residual_sigma via
    # `from bot.config import WEATHER_REGIME_SIGMA`, so the patch target
    # is bot.config.WEATHER_REGIME_SIGMA at the time of import.
    with mock.patch("bot.config.WEATHER_REGIME_SIGMA", False):
        sig = m._sigma_for_hours(
            hours_left=2.0, station="KMIA", lst_hour=14,
            regime_label="E|partly",
        )
    assert sig == pytest.approx(1.5)  # pooled, not regime
    # But meta is captured — regime_sigma_f reflects the would-have-been
    meta = m.get_residual_tier_meta("KMIA")
    assert meta is not None
    assert meta["regime_sigma_f"] == pytest.approx(0.5)
    assert meta["pooled_sigma_f"] == pytest.approx(1.5)
    assert meta["regime_label"] == "E|partly"


def test_flag_on_uses_regime_when_available(conn):
    kv_set(conn, "weather_metar_residual_sigma_KMIA_14",
           {"sigma": 1.5, "n": 30}, 86400)
    kv_set(conn, "weather_metar_residual_sigma_regime_KMIA_14_E|partly",
           {"sigma": 0.5, "n": 8, "tier": "regime_hour"}, 86400)

    with mock.patch("bot.config.WEATHER_REGIME_SIGMA", True):
        sig = m._sigma_for_hours(
            hours_left=2.0, station="KMIA", lst_hour=14,
            regime_label="E|partly",
        )
    assert sig == pytest.approx(0.5)


def test_flag_on_falls_back_to_tier2_when_no_tier1(conn):
    """Hierarchical: when tier 1 has no fit, walk to tier 2."""
    kv_set(conn, "weather_metar_residual_sigma_KMIA_14",
           {"sigma": 1.5, "n": 30}, 86400)
    kv_set(conn, "weather_metar_residual_sigma_station_regime_KMIA_E|partly",
           {"sigma": 0.7, "n": 50, "tier": "station_regime"}, 86400)
    # No tier 1 entry for hour 14

    with mock.patch("bot.config.WEATHER_REGIME_SIGMA", True):
        sig = m._sigma_for_hours(
            hours_left=2.0, station="KMIA", lst_hour=14,
            regime_label="E|partly",
        )
    assert sig == pytest.approx(0.7)
    meta = m.get_residual_tier_meta("KMIA")
    assert meta["regime_tier_used"] == "station_regime"


def test_flag_on_falls_back_to_tier3_pooled_when_regime_missing(conn):
    """No regime fit → use pooled (same as flag-off behavior)."""
    kv_set(conn, "weather_metar_residual_sigma_KMIA_14",
           {"sigma": 1.5, "n": 30}, 86400)

    with mock.patch("bot.config.WEATHER_REGIME_SIGMA", True):
        sig = m._sigma_for_hours(
            hours_left=2.0, station="KMIA", lst_hour=14,
            regime_label="E|partly",
        )
    assert sig == pytest.approx(1.5)


def test_no_station_no_lookup_just_schedule():
    """Backward-compat: caller without station/lst_hour gets schedule."""
    sig = m._sigma_for_hours(hours_left=5.0)
    assert sig == 5.0  # 4-6h → 5.0 from the schedule


def test_get_residual_tier_meta_pops(conn):
    """get_residual_tier_meta is a pop, not a peek."""
    kv_set(conn, "weather_metar_residual_sigma_KMIA_14",
           {"sigma": 1.5, "n": 30}, 86400)
    m._sigma_for_hours(
        hours_left=2.0, station="KMIA", lst_hour=14,
        regime_label="E|partly",
    )
    first = m.get_residual_tier_meta("KMIA")
    assert first is not None
    second = m.get_residual_tier_meta("KMIA")
    assert second is None


def test_health_counter_tracks_chosen_tier(conn):
    kv_set(conn, "weather_metar_residual_sigma_KMIA_14",
           {"sigma": 1.5, "n": 30}, 86400)
    kv_set(conn, "weather_metar_residual_sigma_regime_KMIA_14_E|partly",
           {"sigma": 0.5, "n": 8, "tier": "regime_hour"}, 86400)

    # Flag off — counter records pooled
    with mock.patch("bot.config.WEATHER_REGIME_SIGMA", False):
        m._sigma_for_hours(
            hours_left=2.0, station="KMIA", lst_hour=14,
            regime_label="E|partly",
        )
    counts = m.get_and_reset_regime_health_stats()
    assert counts["pooled_hour"] == 1
    assert counts["regime_hour"] == 0

    # Flag on — counter records regime_hour
    with mock.patch("bot.config.WEATHER_REGIME_SIGMA", True):
        m._sigma_for_hours(
            hours_left=2.0, station="KMIA", lst_hour=14,
            regime_label="E|partly",
        )
    counts = m.get_and_reset_regime_health_stats()
    assert counts["regime_hour"] == 1
    assert counts["pooled_hour"] == 0


def test_extract_regime_features_handles_missing_fields():
    """The METAR API sometimes omits wdir/dewp/clouds."""
    obs = {"temp": 20.0}
    feats = m._extract_regime_features(obs)
    assert feats == {"drct": None, "skyc1": None, "dwpf": None}

    obs2 = {"temp": 20.0, "wdir": 90, "dewp": 15.0,
            "clouds": [{"cover": "BKN", "base": 5000}]}
    feats2 = m._extract_regime_features(obs2)
    assert feats2["drct"] == pytest.approx(90.0)
    assert feats2["dwpf"] == pytest.approx(59.0)  # 15°C → 59°F
    assert feats2["skyc1"] == "BKN"


def test_compute_regime_label_returns_none_on_missing():
    """Unknown regime → None (caller treats as no-regime; falls back)."""
    feats = {"drct": None, "skyc1": None, "dwpf": None}
    label = m._compute_regime_label("KMIA", feats, tmpf=80.0)
    assert label is None


def test_compute_regime_label_for_known_station():
    feats = {"drct": 90.0, "skyc1": "BKN", "dwpf": 70.0}
    label = m._compute_regime_label("KMIA", feats, tmpf=80.0)
    assert label == "E|partly"
