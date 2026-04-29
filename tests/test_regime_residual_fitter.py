"""Tests for ``bot.learning.regime_residual_fitter``.

Pins:
- Tier 1 keys = `weather_metar_residual_sigma_regime_<station>_<hour>_<label>`
- Tier 2 keys = `weather_metar_residual_sigma_station_regime_<station>_<label>`
- Cells with n < _MIN_FIT_N are skipped (not persisted at all)
- σ is winsorized + floored
- Hierarchical lookup walks tier 1 → tier 2 → none
- Per-city taxonomy honored (KAUS uses wind+ddep, others wind+sky / wind)
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from bot.db import init_db, kv_get
from bot.learning import regime_residual_fitter as rf


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _seed_temp_row(c, station, lst_date, lst_hour, temp_f, daily_high):
    c.execute(
        """INSERT INTO weather_metar_hourly_backfill
           (created_at, station, lst_date, lst_hour, temp_f, daily_high_f)
           VALUES ('t', ?, ?, ?, ?, ?)""",
        (station, lst_date, lst_hour, temp_f, daily_high),
    )


def _seed_regime_row(c, station, lst_date, lst_hour, *,
                    drct=None, skyc1=None, dwpf=None, sknt=None):
    c.execute(
        """INSERT INTO weather_metar_hourly_regime
           (created_at, station, lst_date, lst_hour, dwpf, drct, sknt, skyc1)
           VALUES ('t', ?, ?, ?, ?, ?, ?, ?)""",
        (station, lst_date, lst_hour, dwpf, drct, sknt, skyc1),
    )


def _seed_day(c, station, lst_date, *, drct, skyc1, daily_high,
              hourly_temps, dwpf=70.0):
    """Seed all 24 hours of one (station, date) with the same regime."""
    for h, t in enumerate(hourly_temps):
        _seed_temp_row(c, station, lst_date, h, t, daily_high)
        _seed_regime_row(c, station, lst_date, h,
                         drct=drct, skyc1=skyc1, dwpf=dwpf)


def test_writes_tier1_and_tier2_keys(conn):
    # Build a KMDW dataset (wind+sky taxonomy) where 6 days have W|partly
    # regime with consistent residual peak ≈ 1°F at hour 14.
    # Hours 0-13: 60°F; hour 14: 64°F (running max=64); daily high=65.
    # Residual at hour 14 = 65-64 = 1°F.
    station = "KMDW"
    for i in range(8):  # 8 W|partly days
        date = f"2026-04-{10+i:02d}"
        hourly = [60.0] * 14 + [64.0] * 10
        _seed_day(conn, station, date, drct=270, skyc1="SCT",
                  daily_high=65.0, hourly_temps=hourly)
    # Add 6 days of S|clear regime — consistent residual peak ≈ 2°F
    for i in range(6):
        date = f"2026-04-{18+i:02d}"
        hourly = [55.0] * 14 + [63.0] * 10  # running max at h14=63
        _seed_day(conn, station, date, drct=180, skyc1="CLR",
                  daily_high=65.0, hourly_temps=hourly)
    conn.commit()

    stats = rf.fit_and_persist_regime_residual_sigma(conn)
    assert stats["tier1_keys_written"] > 0
    assert stats["tier2_keys_written"] >= 2  # W|partly and S|clear

    # Check tier 1 key shape + content
    key = f"{rf.TIER1_KEY_PREFIX}{station}_14_W|partly"
    payload = kv_get(conn, key)
    assert payload is not None
    assert payload["tier"] == "regime_hour"
    assert payload["taxonomy"] == "wind+sky"
    assert payload["n"] >= rf._MIN_FIT_N
    # Residual is exactly 1°F across 8 days → σ = 0 → floored
    assert payload["sigma"] == pytest.approx(rf._SIGMA_FLOOR_F, abs=1e-6)

    # Check tier 2 — pools across hours
    key2 = f"{rf.TIER2_KEY_PREFIX}{station}_S|clear"
    payload2 = kv_get(conn, key2)
    assert payload2 is not None
    assert payload2["tier"] == "station_regime"


def test_skips_thin_cells(conn):
    # Only 3 days of data — below _MIN_FIT_N
    station = "KMIA"
    for i in range(3):
        date = f"2026-04-{20+i:02d}"
        hourly = [70.0] * 14 + [80.0] * 10
        _seed_day(conn, station, date, drct=90, skyc1="FEW",
                  daily_high=82.0, hourly_temps=hourly)
    conn.commit()

    stats = rf.fit_and_persist_regime_residual_sigma(conn)
    assert stats["tier1_keys_written"] == 0
    # Tier 2 pools 3 days × 24 hours = 72 samples — should fit.
    assert stats["tier2_keys_written"] >= 1
    assert stats["tier1_thin"] > 0  # thin cells counted


def test_get_regime_sigma_walks_hierarchy(conn):
    # Persist a tier 1 key directly via the fitter, then look it up.
    station = "KNYC"
    for i in range(8):
        date = f"2026-04-{1+i:02d}"
        hourly = [50.0] * 14 + [58.0] * 10
        _seed_day(conn, station, date, drct=90, skyc1="CLR",  # E (KNYC uses wind only)
                  daily_high=60.0, hourly_temps=hourly)
    conn.commit()
    rf.fit_and_persist_regime_residual_sigma(conn)

    # KNYC taxonomy is "wind" only — label is just "E"
    sig, tier = rf.get_regime_sigma(conn, station, lst_hour=14, regime_label="E")
    assert sig is not None
    assert tier == "regime_hour"

    # Hour without a tier 1 fit → tier 2 fallback
    # (hour 23 cell exists but with the same data, so still fits)
    # Use a fake hour that has no tier 1 entry.
    sig2, tier2 = rf.get_regime_sigma(conn, station, lst_hour=99, regime_label="E")
    assert sig2 is not None
    assert tier2 == "station_regime"

    # Unknown regime → no fit
    sig3, tier3 = rf.get_regime_sigma(conn, station, lst_hour=14, regime_label="unknown")
    assert sig3 is None
    assert tier3 == "none"


def test_idempotent(conn):
    """Re-running the fitter overwrites the same keys, doesn't create dups."""
    station = "KAUS"
    for i in range(8):
        date = f"2026-04-{1+i:02d}"
        hourly = [70.0] * 14 + [85.0] * 10
        _seed_day(conn, station, date, drct=180, skyc1="CLR",  # KAUS uses wind+ddep
                  daily_high=88.0, hourly_temps=hourly, dwpf=60.0)
    conn.commit()

    rf.fit_and_persist_regime_residual_sigma(conn)
    n_after_first = conn.execute(
        "SELECT COUNT(*) FROM kv_cache WHERE key LIKE ?",
        (f"{rf.TIER1_KEY_PREFIX}%",),
    ).fetchone()[0]
    rf.fit_and_persist_regime_residual_sigma(conn)
    n_after_second = conn.execute(
        "SELECT COUNT(*) FROM kv_cache WHERE key LIKE ?",
        (f"{rf.TIER1_KEY_PREFIX}%",),
    ).fetchone()[0]
    assert n_after_first == n_after_second


def test_per_city_taxonomy_honored(conn):
    """KAUS uses wind+ddep; the persisted payload reflects that."""
    station = "KAUS"
    for i in range(8):
        date = f"2026-04-{1+i:02d}"
        hourly = [70.0] * 14 + [88.0] * 10
        _seed_day(conn, station, date, drct=180, skyc1="CLR",
                  daily_high=90.0, hourly_temps=hourly, dwpf=58.0)  # ddep≈30 = dry
    conn.commit()
    rf.fit_and_persist_regime_residual_sigma(conn)

    # Label should be 'S|dry' (wind+ddep), not 'S|clear' (wind+sky).
    payload = kv_get(conn, f"{rf.TIER2_KEY_PREFIX}KAUS_S|dry")
    assert payload is not None
    assert payload["taxonomy"] == "wind+ddep"


def test_unknown_taxonomy_skipped(conn):
    """Stations not in _CITY_TAXONOMY get no fits (e.g., new station added
    without a taxonomy decision yet)."""
    station = "KFAKE"  # not in taxonomy map
    for i in range(8):
        date = f"2026-04-{1+i:02d}"
        hourly = [70.0] * 24
        _seed_day(conn, station, date, drct=180, skyc1="CLR",
                  daily_high=80.0, hourly_temps=hourly)
    conn.commit()
    stats = rf.fit_and_persist_regime_residual_sigma(conn)
    assert stats["tier1_keys_written"] == 0
    assert stats["tier2_keys_written"] == 0


def test_unknown_regime_skipped(conn):
    """Rows with NULL drct/skyc1 produce label='unknown' → not persisted."""
    station = "KMIA"
    for i in range(8):
        date = f"2026-04-{1+i:02d}"
        # No drct/skyc1 → unknown
        for h in range(24):
            _seed_temp_row(conn, station, date, h,
                           70.0 if h < 14 else 80.0, 82.0)
            _seed_regime_row(conn, station, date, h,
                             drct=None, skyc1=None)
    conn.commit()
    stats = rf.fit_and_persist_regime_residual_sigma(conn)
    assert stats["tier1_keys_written"] == 0
    assert stats["tier2_keys_written"] == 0
