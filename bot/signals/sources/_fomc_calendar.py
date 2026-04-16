"""Single source of truth for FOMC meeting dates and Fed rate ranges.

Shared by fedwatch.py and zq_futures.py to avoid divergent calendars.
Dates are the announcement day (second day of 2-day meetings).
"""

from __future__ import annotations

from datetime import datetime, timezone

# ══════════════════════════════════════════════════════════════════════════════
# FOMC Meeting Schedule
# ══════════════════════════════════════════════════════════════════════════════

FOMC_MEETING_DATES: list[str] = [
    # 2025 (confirmed)
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-17",
    # 2026 (confirmed)
    "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
    # 2027 (projected — standard FOMC pattern: 8 meetings per year,
    # roughly Jan/Mar/May/Jun/Jul/Sep/Oct-Nov/Dec. Verify against the
    # official Fed calendar when published, expected Nov 2026.)
    "2027-01-27", "2027-03-17", "2027-05-05", "2027-06-16",
    "2027-07-28", "2027-09-22", "2027-10-27", "2027-12-15",
]

# Hard cutoff: do not trust model output beyond the last calendar entry.
# Any market expiring past this date gets no FedWatch/ZQ estimate.
FOMC_CALENDAR_CUTOFF = datetime(2027, 12, 31, tzinfo=timezone.utc)

# Month abbreviation map for ticker date parsing
MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Standard 25bp rate ranges the Fed targets
RATE_RANGES = [
    (0.00, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, 1.00),
    (1.00, 1.25), (1.25, 1.50), (1.50, 1.75), (1.75, 2.00),
    (2.00, 2.25), (2.25, 2.50), (2.50, 2.75), (2.75, 3.00),
    (3.00, 3.25), (3.25, 3.50), (3.50, 3.75), (3.75, 4.00),
    (4.00, 4.25), (4.25, 4.50), (4.50, 4.75), (4.75, 5.00),
    (5.00, 5.25), (5.25, 5.50), (5.50, 5.75), (5.75, 6.00),
]


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def parse_fomc_dates() -> list[datetime]:
    """Return FOMC meeting dates as sorted timezone-aware datetime objects."""
    out = []
    for ds in FOMC_MEETING_DATES:
        try:
            out.append(datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return sorted(out)


def next_meeting_after(dt: datetime) -> datetime | None:
    """Return the first FOMC meeting on or after *dt*, or None if past cutoff."""
    for m in parse_fomc_dates():
        if m >= dt:
            return m
    return None


def closest_meeting_to(dt: datetime) -> datetime | None:
    """Return the FOMC meeting closest to *dt* (before or after)."""
    meetings = parse_fomc_dates()
    if not meetings:
        return None
    return min(meetings, key=lambda m: abs((m - dt).total_seconds()))


def last_meeting_on_or_before(dt: datetime) -> datetime | None:
    """Return the most recent FOMC meeting on or before *dt*, or None."""
    candidates = [m for m in parse_fomc_dates() if m <= dt]
    return max(candidates) if candidates else None


def meetings_between(start: datetime, end: datetime) -> int:
    """Count FOMC meetings strictly between *start* and *end*."""
    return sum(1 for m in parse_fomc_dates() if start < m <= end)


def is_beyond_calendar(dt: datetime) -> bool:
    """True if *dt* is past the last known FOMC meeting date."""
    return dt > FOMC_CALENDAR_CUTOFF
