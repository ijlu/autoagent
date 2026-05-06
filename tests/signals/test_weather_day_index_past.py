"""Regression: ``_determine_day_index`` must return None for past-close markets.

The pre-2026-04-29 implementation fell through to title-based heuristics
when the expiry field couldn't be matched into the [0, 7) day window. For
already-settled markets (close_time in the past, delta < 0), the title
typically has no date hint — so the function defaulted to ``return 0``,
asking every weather source for "today's" forecast for yesterday's market.

Cascading effect: predict_v2 was called on stale tickers, all sources
produced today's data for yesterday's settle date, and
``weather_forecast_snapshots`` was poisoned with stale rows showing μ
5-17°F off the actual settled high.

This test pins the corrected behaviour:
- close_time in the past → None
- close_time beyond 7 days → None
- close_time within [0, 7) days → that day index
- close_time absent + no title hint → 0 (today; legacy fallback for
  test fixtures and malformed inputs)
- close_time present but unparseable → fall through to title (legacy
  resilience)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.signals.sources.weather import _determine_day_index


def _close_time_iso_at_lst_offset_from_today(
    days_from_today: int, lst_offset_hours: int = -5
) -> str:
    """Construct a Kalshi-style close_time ISO string that resolves to
    exactly ``days_from_today`` days from today in the city's LST,
    regardless of when this test runs.

    Strategy: anchor the close_time at LST 18:00 of the target date.
    18:00 LST = 23:00 UTC (for offset=-5), well clear of midnight in
    any reasonable timezone shift, so the LST date of the close_time
    deterministically equals ``today_local + days_from_today``.
    """
    lst_tz = timezone(timedelta(hours=lst_offset_hours))
    today_lst = datetime.now(lst_tz).date()
    target_lst = today_lst + timedelta(days=days_from_today)
    target_dt_utc = datetime(
        target_lst.year, target_lst.month, target_lst.day, 18, 0, 0, tzinfo=lst_tz
    ).astimezone(timezone.utc)
    return target_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestPastDateGuard:
    def test_yesterday_close_time_returns_none(self):
        past = _close_time_iso_at_lst_offset_from_today(-1)
        result = _determine_day_index(
            title="Will the high temperature be above 70?",
            market_data={"close_time": past},
            city_key="nyc",
        )
        assert result is None, (
            "Past close_time MUST return None — no forecast for yesterday's "
            "settled market"
        )

    def test_two_days_past_returns_none(self):
        past = _close_time_iso_at_lst_offset_from_today(-2)
        assert _determine_day_index(
            title="Will the high temperature be above 80?",
            market_data={"close_time": past},
            city_key="miami",
        ) is None

    def test_today_close_time_returns_zero(self):
        today = _close_time_iso_at_lst_offset_from_today(0)
        result = _determine_day_index(
            title="Will the high temperature be above 70?",
            market_data={"close_time": today},
            city_key="nyc",
        )
        assert result == 0

    def test_tomorrow_close_time_returns_one(self):
        tomorrow = _close_time_iso_at_lst_offset_from_today(1)
        result = _determine_day_index(
            title="Will the high temperature be above 70?",
            market_data={"close_time": tomorrow},
            city_key="nyc",
        )
        assert result == 1

    def test_eight_days_out_returns_none(self):
        far = _close_time_iso_at_lst_offset_from_today(8)
        assert _determine_day_index(
            title="Will the high temperature be above 70?",
            market_data={"close_time": far},
            city_key="nyc",
        ) is None

    def test_unparseable_close_time_falls_through_to_title(self):
        # Defensive: if close_time literally can't be parsed (corruption
        # or unexpected format), the legacy behaviour was to fall through
        # to title heuristics. This is the only path where title parsing
        # is allowed when an expiry field exists.
        result = _determine_day_index(
            title="Tomorrow's high in NYC",
            market_data={"close_time": "this is not a date"},
            city_key="nyc",
        )
        assert result == 1  # title says "tomorrow"

    def test_no_market_data_falls_through_to_title(self):
        # No expiry fields at all → trust title.
        result = _determine_day_index(
            title="Will tomorrow be above 70?",
            market_data=None,
            city_key="nyc",
        )
        assert result == 1

    def test_empty_market_data_falls_through_to_title(self):
        result = _determine_day_index(
            title="Will tomorrow be above 70?",
            market_data={},
            city_key="nyc",
        )
        assert result == 1

    def test_no_market_data_no_title_hint_defaults_today(self):
        # Legacy behaviour preserved: a totally undateable input still
        # falls back to 0. This was the original default and many test
        # fixtures rely on it.
        result = _determine_day_index(
            title="will it be hot",
            market_data=None,
            city_key="nyc",
        )
        assert result == 0
