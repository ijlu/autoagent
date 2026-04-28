"""Tests for ``bot.signals.sources._freshness``.

Schedule-derived ``latest_cycle_issued_at`` for forecast sources. These
feed ``GaussianForecast.issued_at`` and drive staleness inflation in
``weather_ensemble_v2._apply_staleness_inflation``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.signals.sources._freshness import (
    hrrr_latest_issued_at,
    latest_cycle_issued_at,
    nbm_latest_issued_at,
    nws_point_latest_issued_at,
    open_meteo_latest_issued_at,
)


# ── Core helper: explicit inputs, deterministic outputs ─────────────────────

def test_hourly_cycle_returns_previous_hour_when_lag_unmet():
    """At H:15 with 30-min completion lag, the H:00 cycle isn't done yet —
    must return the (H-1):00 cycle."""
    now = datetime(2026, 4, 27, 14, 15, 0, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=tuple(range(24)),
        completion_lag_minutes=30,
        now_utc=now,
    )
    expected = datetime(2026, 4, 27, 13, 0, 0, tzinfo=timezone.utc).timestamp()
    assert result == pytest.approx(expected, abs=0.01)


def test_hourly_cycle_picks_latest_completed_when_lag_met():
    """At H:35 with 30-min completion lag, the H:00 cycle IS done."""
    now = datetime(2026, 4, 27, 14, 35, 0, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=tuple(range(24)),
        completion_lag_minutes=30,
        now_utc=now,
    )
    expected = datetime(2026, 4, 27, 14, 0, 0, tzinfo=timezone.utc).timestamp()
    assert result == pytest.approx(expected, abs=0.01)


def test_six_hourly_nbm_picks_correct_cycle():
    """NBM cycles at 01z/07z/13z/19z. At 14:35 with 60-min lag, the 13z
    cycle is complete (13:00 + 60min = 14:00 has passed). Expected = 13z."""
    now = datetime(2026, 4, 27, 14, 35, 0, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=(1, 7, 13, 19),
        completion_lag_minutes=60,
        now_utc=now,
    )
    expected = datetime(2026, 4, 27, 13, 0, 0, tzinfo=timezone.utc).timestamp()
    assert result == pytest.approx(expected, abs=0.01)


def test_six_hourly_nbm_falls_back_to_yesterday_19z_pre_dawn():
    """Just past midnight UTC — only yesterday's 19z cycle is available."""
    now = datetime(2026, 4, 27, 0, 30, 0, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=(1, 7, 13, 19),
        completion_lag_minutes=60,
        now_utc=now,
    )
    expected = datetime(2026, 4, 26, 19, 0, 0, tzinfo=timezone.utc).timestamp()
    assert result == pytest.approx(expected, abs=0.01)


def test_pre_first_cycle_of_day_uses_previous_day():
    """At 00:30 UTC with 6h cycles + 60m lag, today's 01z cycle isn't even
    started — must fall back to yesterday's 19z."""
    now = datetime(2026, 4, 27, 0, 5, 0, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=(1, 7, 13, 19),
        completion_lag_minutes=60,
        now_utc=now,
    )
    expected = datetime(2026, 4, 26, 19, 0, 0, tzinfo=timezone.utc).timestamp()
    assert result == pytest.approx(expected, abs=0.01)


def test_empty_schedule_falls_back_to_24h_ago():
    """Defensive: empty schedule yields a "very stale" sentinel so the
    consumer's staleness clamp pins σ at the ceiling."""
    now = datetime(2026, 4, 27, 14, 0, 0, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=(),
        completion_lag_minutes=30,
        now_utc=now,
    )
    assert result == pytest.approx(now.timestamp() - 86400.0, abs=0.01)


# ── Per-source helpers: smoke tests against now ─────────────────────────────

def test_hrrr_helper_returns_recent_timestamp():
    """HRRR is hourly so the result must be within ~2 hours of now."""
    import time as _time
    ts = hrrr_latest_issued_at()
    age_h = (_time.time() - ts) / 3600.0
    # Worst case: 1h ago + 30m lag = 1.5h. Allow 2h slack for clock skew.
    assert 0.0 <= age_h <= 2.0, f"HRRR issued_at age {age_h:.2f}h not in [0, 2]"


def test_nbm_helper_returns_at_most_six_hours_old():
    """NBM is 6-hourly, so worst case the cycle is just-completed-but-old."""
    import time as _time
    ts = nbm_latest_issued_at()
    age_h = (_time.time() - ts) / 3600.0
    # Worst case: just before next cycle = 6h - lag = ~5h. Allow 7h slack.
    assert 0.0 <= age_h <= 7.0, f"NBM issued_at age {age_h:.2f}h not in [0, 7]"


def test_nws_point_helper_returns_recent():
    import time as _time
    ts = nws_point_latest_issued_at()
    age_h = (_time.time() - ts) / 3600.0
    assert 0.0 <= age_h <= 2.0


def test_open_meteo_helper_returns_recent():
    import time as _time
    ts = open_meteo_latest_issued_at()
    age_h = (_time.time() - ts) / 3600.0
    assert 0.0 <= age_h <= 2.5


# ── Boundary: completion_lag matters ────────────────────────────────────────

def test_completion_lag_blocks_in_progress_cycle():
    """At exactly H:00:01 with 30-min lag, H:00 cycle is just starting —
    NOT yet completed. Should return the (H-1):00 cycle."""
    now = datetime(2026, 4, 27, 14, 0, 1, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=tuple(range(24)),
        completion_lag_minutes=30,
        now_utc=now,
    )
    expected_13z = datetime(2026, 4, 27, 13, 0, 0, tzinfo=timezone.utc).timestamp()
    assert result == pytest.approx(expected_13z, abs=0.01)


def test_zero_completion_lag_uses_cycle_immediately():
    """With completion_lag=0, every cycle is "complete" the moment it
    starts. At H:00:01, the H:00 cycle is the latest."""
    now = datetime(2026, 4, 27, 14, 0, 1, tzinfo=timezone.utc)
    result = latest_cycle_issued_at(
        cycle_hours_utc=tuple(range(24)),
        completion_lag_minutes=0,
        now_utc=now,
    )
    expected_14z = datetime(2026, 4, 27, 14, 0, 0, tzinfo=timezone.utc).timestamp()
    assert result == pytest.approx(expected_14z, abs=0.01)
