"""Tests for KXFED ticker date parsing and FOMC horizon guard.

Covers:
- Old ticker format (DD+MMM+YY): KXFED-27APR25-T4.625
- Live ticker format (YY+MMM):   KXFED-26JUL-T3.25
- Bracket variants for both formats
- Horizon guard: beyond-cutoff tickers return None
- FOMC meeting resolution from mid-month anchors
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from bot.signals.sources._fomc_calendar import (
    FOMC_MEETING_DATES,
    FOMC_CALENDAR_CUTOFF,
    parse_fomc_dates,
    next_meeting_after,
    closest_meeting_to,
    last_meeting_on_or_before,
    meetings_between,
    is_beyond_calendar,
    MONTH_ABBR,
)
from bot.signals.sources.fedwatch import (
    _parse_ticker_date,
    _parse_ticker_threshold,
    _parse_ticker_bracket,
)


# ══════════════════════════════════════════════════════════════════════════════
# _parse_ticker_date — old format (DD+MMM+YY)
# ══════════════════════════════════════════════════════════════════════════════

class TestParseTickerDateOldFormat:
    def test_standard_old_format(self):
        dt = _parse_ticker_date("KXFED-27APR25-T4.625")
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 4
        assert dt.day == 27

    def test_old_format_may(self):
        dt = _parse_ticker_date("KXFED-07MAY26-T4.375")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 7

    def test_old_format_bracket(self):
        dt = _parse_ticker_date("KXFED-07MAY26-B4.25-4.50")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 7


# ══════════════════════════════════════════════════════════════════════════════
# _parse_ticker_date — live format (YY+MMM)
# ══════════════════════════════════════════════════════════════════════════════

class TestParseTickerDateLiveFormat:
    def test_live_format_jul_2026(self):
        """KXFED-26JUL-T3.25 should resolve to the Jul 2026 FOMC meeting."""
        dt = _parse_ticker_date("KXFED-26JUL-T3.25")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 7
        # Should resolve to the FOMC meeting date, not the 15th
        assert dt.day == 29  # 2026-07-29 FOMC meeting

    def test_live_format_apr_2027(self):
        """KXFED-27APR-T2.50 should resolve to the closest Apr 2027 FOMC meeting."""
        dt = _parse_ticker_date("KXFED-27APR-T2.50")
        assert dt is not None
        assert dt.year == 2027
        assert dt.month == 4
        # Should resolve to ~2027-04 meeting (projected date)

    def test_live_format_sep_2026(self):
        dt = _parse_ticker_date("KXFED-26SEP-T2.75")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 9
        assert dt.day == 16  # 2026-09-16 FOMC meeting

    def test_live_format_oct_2026(self):
        dt = _parse_ticker_date("KXFED-26OCT-T3.75")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 10
        assert dt.day == 28  # 2026-10-28 FOMC meeting

    def test_live_format_dec_2026(self):
        dt = _parse_ticker_date("KXFED-26DEC-T4.25")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 12
        assert dt.day == 16  # 2026-12-16 FOMC meeting

    def test_live_format_bracket(self):
        dt = _parse_ticker_date("KXFED-26MAY-B4.25-4.50")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 6  # 2026-05-06 FOMC meeting

    def test_live_format_lowercase(self):
        """Ticker parsing should be case-insensitive."""
        dt = _parse_ticker_date("kxfed-26jul-t3.25")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 7

    def test_non_kxfed_ticker_returns_none(self):
        """Non-KXFED tickers should not match the live format."""
        dt = _parse_ticker_date("KXHIGH-26JUL-T85")
        assert dt is None

    def test_garbage_returns_none(self):
        dt = _parse_ticker_date("KXFED-GARBAGE")
        assert dt is None

    def test_empty_string_returns_none(self):
        dt = _parse_ticker_date("")
        assert dt is None


# ══════════════════════════════════════════════════════════════════════════════
# _parse_ticker_threshold
# ══════════════════════════════════════════════════════════════════════════════

class TestParseTickerThreshold:
    def test_simple_threshold(self):
        assert _parse_ticker_threshold("KXFED-26JUL-T3.25") == 3.25

    def test_decimal_threshold(self):
        assert _parse_ticker_threshold("KXFED-27APR-T2.50") == 2.50

    def test_old_format_threshold(self):
        assert _parse_ticker_threshold("KXFED-27APR25-T4.625") == 4.625

    def test_bracket_returns_none(self):
        assert _parse_ticker_threshold("KXFED-26MAY-B4.25-4.50") is None

    def test_no_threshold(self):
        assert _parse_ticker_threshold("KXFED-26JUL") is None


# ══════════════════════════════════════════════════════════════════════════════
# _parse_ticker_bracket
# ══════════════════════════════════════════════════════════════════════════════

class TestParseTickerBracket:
    def test_simple_bracket(self):
        result = _parse_ticker_bracket("KXFED-26MAY-B4.25-4.50")
        assert result is not None
        assert result == (4.25, 4.50)

    def test_old_format_bracket(self):
        result = _parse_ticker_bracket("KXFED-07MAY26-B4.25-4.50")
        assert result is not None
        assert result == (4.25, 4.50)

    def test_threshold_returns_none(self):
        assert _parse_ticker_bracket("KXFED-26JUL-T3.25") is None


# ══════════════════════════════════════════════════════════════════════════════
# FOMC calendar helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestFOMCCalendar:
    def test_parse_fomc_dates_sorted(self):
        dates = parse_fomc_dates()
        assert len(dates) >= 16  # at least 2025+2026
        # Verify sorted
        for i in range(1, len(dates)):
            assert dates[i] > dates[i - 1]

    def test_includes_2027(self):
        dates = parse_fomc_dates()
        years = {d.year for d in dates}
        assert 2027 in years

    def test_next_meeting_after_mid_2026(self):
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        meeting = next_meeting_after(dt)
        assert meeting is not None
        assert meeting == datetime(2026, 6, 17, tzinfo=timezone.utc)

    def test_next_meeting_past_all_returns_none(self):
        dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        assert next_meeting_after(dt) is None

    def test_closest_meeting_mid_july_2026(self):
        dt = datetime(2026, 7, 15, tzinfo=timezone.utc)
        meeting = closest_meeting_to(dt)
        assert meeting is not None
        assert meeting == datetime(2026, 7, 29, tzinfo=timezone.utc)

    def test_last_meeting_on_or_before(self):
        dt = datetime(2026, 8, 1, tzinfo=timezone.utc)
        meeting = last_meeting_on_or_before(dt)
        assert meeting is not None
        assert meeting == datetime(2026, 7, 29, tzinfo=timezone.utc)

    def test_meetings_between_2026(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 12, 31, tzinfo=timezone.utc)
        assert meetings_between(start, end) == 8


# ══════════════════════════════════════════════════════════════════════════════
# Horizon guard
# ══════════════════════════════════════════════════════════════════════════════

class TestHorizonGuard:
    def test_within_calendar(self):
        dt = datetime(2026, 7, 15, tzinfo=timezone.utc)
        assert not is_beyond_calendar(dt)

    def test_at_cutoff_boundary(self):
        assert not is_beyond_calendar(FOMC_CALENDAR_CUTOFF)

    def test_beyond_cutoff(self):
        dt = datetime(2028, 1, 1, tzinfo=timezone.utc)
        assert is_beyond_calendar(dt)

    def test_far_future(self):
        dt = datetime(2030, 6, 15, tzinfo=timezone.utc)
        assert is_beyond_calendar(dt)
