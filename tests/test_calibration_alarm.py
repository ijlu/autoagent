"""Tests for the daily |z| calibration alarm (Phase 2 item 3)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bot.learning.calibration_alarm import (
    MIN_SETTLEMENTS,
    THRESHOLDS,
    _bucket_for,
    _date_to_ticker_pattern,
    _format_drift_alert,
    _format_spike_alert,
    _settle_date_from_ticker,
    evaluate_calibration_z,
    run_calibration_alarm,
)


# ── Pure helper tests ─────────────────────────────────────────────────────


def test_bucket_for_long_horizon():
    assert _bucket_for(72) == ">48h"
    assert _bucket_for(49) == ">48h"


def test_bucket_for_mid_horizon():
    assert _bucket_for(48) == "12-48h"
    assert _bucket_for(20) == "12-48h"
    assert _bucket_for(12) == "12-48h"


def test_bucket_for_short_horizon():
    assert _bucket_for(11) == "<12h"
    assert _bucket_for(1) == "<12h"
    assert _bucket_for(0) == "<12h"


def test_settle_date_parsing():
    assert _settle_date_from_ticker("KXHIGHNY-26MAY08-T65.0") == "2026-05-08"
    assert _settle_date_from_ticker("KXHIGHCHI-26JAN01-B72.5") == "2026-01-01"
    assert _settle_date_from_ticker("KXHIGHLAX-26DEC31-T100") == "2026-12-31"


def test_settle_date_parse_failure():
    assert _settle_date_from_ticker("malformed") is None
    assert _settle_date_from_ticker("KXFED-26MAY-T2.00") is None  # no day
    assert _settle_date_from_ticker("KXHIGHNY-26ABC08-T65.0") is None  # bad month


def test_date_to_ticker_pattern():
    d = datetime(2026, 5, 8)
    assert _date_to_ticker_pattern(d) == "%-26MAY08-%"
    d2 = datetime(2026, 1, 1)
    assert _date_to_ticker_pattern(d2) == "%-26JAN01-%"


def test_thresholds_have_all_buckets():
    """Sanity: every bucket produced by _bucket_for has a threshold entry."""
    assert set(THRESHOLDS.keys()) == {">48h", "12-48h", "<12h"}
    for b, t in THRESHOLDS.items():
        assert "drift" in t and "spike" in t
        assert t["drift"] > 0 and t["spike"] > 0


# ── DB-driven evaluator tests ─────────────────────────────────────────────


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    # Minimal schemas mirroring production. Only the columns the alarm reads.
    conn.executescript(
        """
        CREATE TABLE weather_forecast_snapshots (
            id INTEGER PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            forecast_high_f REAL,
            sigma_f REAL,
            hours_out INTEGER
        );
        CREATE TABLE weather_metar_hourly_backfill (
            id INTEGER PRIMARY KEY,
            station TEXT NOT NULL,
            lst_date TEXT NOT NULL,
            lst_hour INTEGER NOT NULL,
            temp_f REAL NOT NULL,
            daily_high_f REAL,
            UNIQUE(station, lst_date, lst_hour)
        );
        """
    )
    yield conn
    conn.close()


def _insert_actual(conn, station: str, date_str: str, high_f: float):
    conn.execute(
        "INSERT INTO weather_metar_hourly_backfill "
        "(station, lst_date, lst_hour, temp_f, daily_high_f) "
        "VALUES (?, ?, 12, ?, ?)",
        (station, date_str, high_f, high_f),
    )


def _insert_snapshot(
    conn, ticker: str, mu: float, sigma: float, hours_out: int,
    *, recorded_at: str = "2026-05-09T00:00:00+00:00",
    source: str = "combined_v2",
):
    conn.execute(
        "INSERT INTO weather_forecast_snapshots "
        "(recorded_at, ticker, source, forecast_high_f, sigma_f, hours_out) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recorded_at, ticker, source, mu, sigma, hours_out),
    )


def test_empty_db_returns_empty(memory_db):
    """No actuals → return empty dict."""
    assert evaluate_calibration_z(memory_db) == {}


def test_z_computation_simple_case(memory_db):
    """One ticker, one snapshot, known z."""
    _insert_actual(memory_db, "KNYC", "2026-05-08", high_f=70.0)
    # μ=68, σ=2, actual=70 → z = (70-68)/2 = 1.0 → |z|=1.0
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=68.0, sigma=2.0, hours_out=8,
    )
    out = evaluate_calibration_z(memory_db)
    assert "<12h" in out
    assert out["<12h"]["n_snapshots"] == 1
    assert out["<12h"]["n_settlements"] == 1
    assert out["<12h"]["drift_3d"] == pytest.approx(1.0)
    assert out["<12h"]["spike_1d"] == pytest.approx(1.0)


def test_dedup_keeps_latest_per_ticker_hours_out(memory_db):
    """Two snapshots at same (ticker, hours_out) → keep latest only."""
    _insert_actual(memory_db, "KNYC", "2026-05-08", high_f=70.0)
    # Earlier snapshot, far from truth
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=60.0, sigma=2.0, hours_out=8,
        recorded_at="2026-05-08T10:00:00+00:00",
    )
    # Later snapshot (should win), close to truth
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=69.0, sigma=2.0, hours_out=8,
        recorded_at="2026-05-08T20:00:00+00:00",
    )
    out = evaluate_calibration_z(memory_db)
    assert out["<12h"]["n_snapshots"] == 1
    # |z| from latest snapshot: (70-69)/2 = 0.5
    assert out["<12h"]["drift_3d"] == pytest.approx(0.5)


def test_bucketing_routes_to_right_buckets(memory_db):
    """One ticker with snapshots at different hours_out → each bucket gets one."""
    _insert_actual(memory_db, "KNYC", "2026-05-08", high_f=70.0)
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=70.0, sigma=2.0, hours_out=72,
        recorded_at="2026-05-05T00:00:00+00:00",
    )
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=70.0, sigma=2.0, hours_out=24,
        recorded_at="2026-05-07T00:00:00+00:00",
    )
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=70.0, sigma=2.0, hours_out=6,
        recorded_at="2026-05-08T18:00:00+00:00",
    )
    out = evaluate_calibration_z(memory_db)
    assert out[">48h"]["n_snapshots"] == 1
    assert out["12-48h"]["n_snapshots"] == 1
    assert out["<12h"]["n_snapshots"] == 1


def test_skips_snapshots_without_actuals(memory_db):
    """Snapshot exists but no metar backfill → silently skip that ticker."""
    _insert_snapshot(memory_db, "KXHIGHNY-26MAY08-T65", mu=68.0, sigma=2.0, hours_out=8)
    # No backfill insert → no actual → empty result
    assert evaluate_calibration_z(memory_db) == {}


def test_skips_zero_sigma(memory_db):
    """sigma=0 would div-by-zero → SQL filter excludes it."""
    _insert_actual(memory_db, "KNYC", "2026-05-08", high_f=70.0)
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=68.0, sigma=0.0, hours_out=8,
    )
    out = evaluate_calibration_z(memory_db)
    # No snapshot survived the sigma>0 filter → bucket has 0 snapshots
    assert out["<12h"]["n_snapshots"] == 0


def test_non_combined_v2_sources_excluded(memory_db):
    """Per-source rows (metar, hrrr, etc.) should not be evaluated."""
    _insert_actual(memory_db, "KNYC", "2026-05-08", high_f=70.0)
    _insert_snapshot(
        memory_db, "KXHIGHNY-26MAY08-T65", mu=60.0, sigma=2.0, hours_out=8,
        source="metar",
    )
    out = evaluate_calibration_z(memory_db)
    assert out["<12h"]["n_snapshots"] == 0


# ── Alarm firing tests ────────────────────────────────────────────────────


def _seed_drift_breach(conn, bucket_hours_out: int, n: int):
    """Seed n distinct settled tickers at a given hours_out with mean(|z|)=2.0."""
    # All for KNYC station; settle on 2026-05-08
    _insert_actual(conn, "KNYC", "2026-05-08", high_f=70.0)
    for i in range(n):
        # Each ticker has actual=70, μ=66, σ=2 → |z| = 2.0
        _insert_snapshot(
            conn, f"KXHIGHNY-26MAY08-T{i + 60}", mu=66.0, sigma=2.0,
            hours_out=bucket_hours_out,
        )


def test_alarm_fires_on_drift_breach(memory_db):
    """<12h bucket: drift threshold = 0.8, our |z|=2.0 → fire drift + spike."""
    _seed_drift_breach(memory_db, bucket_hours_out=8, n=MIN_SETTLEMENTS + 2)
    with patch("bot.learning.calibration_alarm.send_alert") as mock_send:
        run_calibration_alarm(memory_db)
        assert mock_send.call_count == 2  # both drift + spike fire (same data)
        msgs = [c.args[0] for c in mock_send.call_args_list]
        assert any("drift" in m.lower() for m in msgs)
        assert any("spike" in m.lower() for m in msgs)
        assert all("<12h" in m for m in msgs)
        # all alerts at warning level
        for c in mock_send.call_args_list:
            assert c.args[1] == "warning"


def test_alarm_skips_below_min_settlements(memory_db):
    """Too few settlements → no fire even if |z| is huge."""
    _seed_drift_breach(memory_db, bucket_hours_out=8, n=MIN_SETTLEMENTS - 1)
    with patch("bot.learning.calibration_alarm.send_alert") as mock_send:
        run_calibration_alarm(memory_db)
        mock_send.assert_not_called()


def test_no_alarm_under_threshold(memory_db):
    """Normal regime, mean(|z|) = 0.5 in <12h bucket → no fire."""
    _insert_actual(memory_db, "KNYC", "2026-05-08", high_f=70.0)
    for i in range(MIN_SETTLEMENTS + 5):
        # actual=70, μ=69, σ=2 → |z| = 0.5; below 0.8 drift, below 1.5 spike
        _insert_snapshot(
            memory_db, f"KXHIGHNY-26MAY08-T{i + 60}",
            mu=69.0, sigma=2.0, hours_out=8,
        )
    with patch("bot.learning.calibration_alarm.send_alert") as mock_send:
        run_calibration_alarm(memory_db)
        mock_send.assert_not_called()


def test_message_format_drift():
    metrics = {
        "n_snapshots": 47,
        "n_settlements": 12,
        "drift_3d": 1.23,
        "spike_1d": 1.59,
        "daily_means": {"2026-05-06": 0.65, "2026-05-07": 1.45, "2026-05-08": 1.59},
        "spike_date": "2026-05-08",
    }
    out = _format_drift_alert("<12h", metrics, threshold=0.8)
    assert "<12h" in out
    assert "1.23" in out
    assert "0.80" in out
    assert "47" in out and "12" in out
    assert "2026-05-06=0.65" in out
    assert "2026-05-08=1.59" in out


def test_message_format_spike():
    metrics = {
        "n_snapshots": 12,
        "n_settlements": 4,
        "drift_3d": 0.5,
        "spike_1d": 1.78,
        "daily_means": {"2026-05-08": 1.78},
        "spike_date": "2026-05-08",
    }
    out = _format_spike_alert("12-48h", metrics, threshold=2.0)
    assert "12-48h" in out
    assert "2026-05-08" in out
    assert "1.78" in out
    assert "2.00" in out
