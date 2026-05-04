"""Tests for `bot.learning.mos_bias_regime_fitter`.

Builds a deterministic in-memory DB with synthetic backfill rows + regime
rows, runs the fitter, and asserts the kv keys come out shaped right.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.db import init_db, kv_get
from bot.learning.mos_bias_regime_fitter import (
    fit_and_persist_mos_bias_by_regime,
)


@pytest.fixture
def db():
    """In-memory DB with the schema initialized.

    Same pattern as `tests/signals/test_weather_ensemble_v2.py::memdb` —
    init_db() takes a path and returns the connection.
    """
    conn = init_db(":memory:")
    yield conn
    conn.close()


def _seed_backfill_row(conn, source: str, city: str, date_iso: str,
                       forecast_f: float, observed_f: float):
    """Add one row to weather_gaussian_snapshots_backfill."""
    conn.execute(
        """INSERT OR REPLACE INTO weather_gaussian_snapshots_backfill
              (created_at, source, city, settlement_date, forecast_mean_f,
               forecast_sigma_f, observed_high_f, lead_hours)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("2026-04-30T00:00:00Z", source, city, date_iso,
         forecast_f, 1.5, observed_f, 12),
    )


def _seed_regime_row(conn, station: str, lst_date: str, lst_hour: int,
                     drct: float, skyc1: str = "CLR", dwpf: float = 50.0):
    """Add one row to weather_metar_hourly_regime."""
    conn.execute(
        """INSERT OR REPLACE INTO weather_metar_hourly_regime
              (created_at, station, lst_date, lst_hour, dwpf, drct, sknt, skyc1)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("2026-04-30T00:00:00Z", station, lst_date, lst_hour,
         dwpf, drct, 5.0, skyc1),
    )


def test_fitter_persists_regime_conditional_keys(db):
    """End-to-end: seed enough samples in two regimes for one city,
    run the fitter, confirm separate keys are written with the right
    biases."""
    # KMIA uses "wind+sky" taxonomy. We need _MIN_FIT_N=30 distinct
    # backfill rows per regime cell. Use months Jan + Feb for clear,
    # Mar + Apr for overcast (60+ unique dates each, well above 30).
    clear_dates = [f"2026-01-{d:02d}" for d in range(1, 32)] + \
                  [f"2026-02-{d:02d}" for d in range(1, 11)]
    overcast_dates = [f"2026-03-{d:02d}" for d in range(1, 32)] + \
                     [f"2026-04-{d:02d}" for d in range(1, 11)]
    for date in clear_dates:
        _seed_backfill_row(db, "hrrr", "miami", date,
                            forecast_f=85.5, observed_f=85.0)
        _seed_regime_row(db, "KMIA", date, 14, drct=180.0, skyc1="CLR")
    for date in overcast_dates:
        _seed_backfill_row(db, "hrrr", "miami", date,
                            forecast_f=86.8, observed_f=85.0)
        _seed_regime_row(db, "KMIA", date, 14, drct=180.0, skyc1="OVC")
    db.commit()

    stats = fit_and_persist_mos_bias_by_regime(db)
    assert stats["keys_written"] >= 2
    assert stats["rows_processed"] >= 70

    clear_payload = kv_get(db, "weather_mos_bias_hrrr_miami_S|clear")
    overcast_payload = kv_get(db, "weather_mos_bias_hrrr_miami_S|overcast")
    assert clear_payload is not None
    assert overcast_payload is not None
    assert clear_payload["bias"] == pytest.approx(0.5, abs=0.01)
    assert overcast_payload["bias"] == pytest.approx(1.8, abs=0.01)
    # Both should record the regime + sample count for audit.
    assert clear_payload["regime"] == "S|clear"
    # Sample count tracks the number of unique dates we seeded for
    # the overcast cell (Mar 1-31 + Apr 1-10 = 41 dates).
    assert overcast_payload["n"] == 41


def test_fitter_skips_thin_cells(db):
    """Cells with n < _MIN_FIT_N (30) should not be persisted — bias
    estimates from few samples are too noisy."""
    for i in range(10):  # well below the 30 threshold
        date = f"2026-04-{i+1:02d}"
        _seed_backfill_row(db, "hrrr", "nyc", date, 65.0, 64.5)
        _seed_regime_row(db, "KNYC", date, 14, drct=180.0)
    db.commit()
    stats = fit_and_persist_mos_bias_by_regime(db)
    assert stats["keys_written"] == 0
    assert stats["cells_thin"] >= 1


def test_fitter_skips_unknown_regime(db):
    """Days with no regime row should be skipped (regime=None) — they
    can't be classified, so they don't enrich any cell."""
    for i in range(35):
        date = f"2026-04-{(i % 28) + 1:02d}"
        _seed_backfill_row(db, "hrrr", "miami", date, 86.0, 85.0)
        # NO regime row seeded — fitter can't classify
    db.commit()
    stats = fit_and_persist_mos_bias_by_regime(db)
    assert stats["keys_written"] == 0


def test_fitter_excludes_retired_sources(db):
    """MADIS rows must be filtered out — even if data exists, the
    fitter restricts to GAUSSIAN_COMBINE_SOURCES.
    """
    for i in range(35):
        date = f"2026-04-{(i % 28) + 1:02d}"
        _seed_backfill_row(db, "madis", "miami", date, 90.0, 85.0)
        _seed_regime_row(db, "KMIA", date, 14, drct=180.0, skyc1="CLR")
    db.commit()
    stats = fit_and_persist_mos_bias_by_regime(db)
    assert stats["keys_written"] == 0
    # Specifically verify no madis key was written.
    assert kv_get(db, "weather_mos_bias_madis_miami_S|clear") is None


def test_fitter_clamps_outlier_bias(db):
    """A single freak cell with a 10°F average error gets clamped at
    ±_BIAS_MAX_ABS_F (5°F) on persist.
    """
    dates = [f"2026-{m:02d}-{d:02d}" for m in (1, 2) for d in range(1, 16)]
    for date in dates:
        _seed_backfill_row(db, "hrrr", "miami", date,
                            forecast_f=95.0, observed_f=85.0)  # +10°F bias
        _seed_regime_row(db, "KMIA", date, 14, drct=180.0, skyc1="CLR")
    db.commit()
    stats = fit_and_persist_mos_bias_by_regime(db)
    assert stats["keys_written"] >= 1
    payload = kv_get(db, "weather_mos_bias_hrrr_miami_S|clear")
    assert payload is not None
    # Raw bias was 10.0 — must be clamped to 5.0.
    assert payload["bias"] == pytest.approx(5.0, abs=0.01)


def test_fitter_falls_back_to_alt_peak_hour(db):
    """If lst_hour=14 has no regime row but lst_hour=15 does, the
    fitter uses 15. Otherwise we'd lose data on days where the 14:00
    METAR was missed.
    """
    dates = [f"2026-{m:02d}-{d:02d}" for m in (1, 2) for d in range(1, 16)]
    for date in dates:
        _seed_backfill_row(db, "hrrr", "miami", date, 86.0, 85.0)
        # Skip lst_hour=14, only seed lst_hour=15
        _seed_regime_row(db, "KMIA", date, 15, drct=180.0, skyc1="CLR")
    db.commit()
    stats = fit_and_persist_mos_bias_by_regime(db)
    assert stats["keys_written"] >= 1
    assert kv_get(db, "weather_mos_bias_hrrr_miami_S|clear") is not None
