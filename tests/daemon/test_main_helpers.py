"""Tests for the small pure helpers in ``bot.daemon.main``.

The daemon-internal task runners (``_run_hourly_backfill``,
``_run_mos_materializer``, etc.) call into network IO and are covered by
the deploy-time import battery + first-tick log inspection. Pure helpers
that encode correctness-critical math live here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bot.daemon.main import _seconds_until_utc_hour


def _at(year: int, month: int, day: int, hour: int, minute: int = 0,
        second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "now,target_hour,expected_seconds",
    [
        # Now is 03:00 UTC, target 06:00 UTC same day → 3 hours away.
        (_at(2026, 4, 29, 3, 0), 6, 3 * 3600.0),
        # Now is 06:00:01 UTC, target 06:00 UTC → already past, roll to
        # next day = 23:59:59 away.
        (_at(2026, 4, 29, 6, 0, 1), 6, 23 * 3600.0 + 59 * 60 + 59),
        # Now is 05:59:59 UTC, target 06:00 UTC → 1 second away.
        (_at(2026, 4, 29, 5, 59, 59), 6, 1.0),
        # Now exactly equals target → roll to next day (target <= now).
        (_at(2026, 4, 29, 6, 0, 0), 6, 86400.0),
        # Cross-month boundary: April 30 18:00 UTC, target 06:00 UTC →
        # next fire is May 1 06:00 UTC, 12 hours away.
        (_at(2026, 4, 30, 18, 0), 6, 12 * 3600.0),
    ],
)
def test_seconds_until_utc_hour(
    now: datetime, target_hour: int, expected_seconds: float,
):
    """The alignment math is the only correctness-critical piece in the
    hourly_backfill scheduling — if it's off by 24h or rolls the wrong
    direction, the backfill silently never fires (or fires immediately
    on every restart, churning IEM)."""
    with patch("bot.daemon.main.datetime") as mock_dt:
        mock_dt.now.return_value = now
        # Make the patched datetime's other constructors fall through to
        # the real ones — _seconds_until_utc_hour calls .replace() on the
        # returned now, which works on the unpatched dt instance.
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _seconds_until_utc_hour(target_hour)
    assert result == pytest.approx(expected_seconds, abs=0.5)


def test_seconds_until_utc_hour_returns_positive():
    """Regardless of current wall-clock time, the result must always be
    in (0, 86400]. A value ≤ 0 would make the scheduler fire immediately
    every restart; a value > 86400 would skip a day."""
    secs = _seconds_until_utc_hour(6)
    assert 0 < secs <= 86400
