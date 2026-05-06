"""LST alignment helpers for diurnal-phase analysis.

Single source of truth for converting (UTC timestamp, station/city/ticker)
to local-solar-time hour and the corresponding diurnal phase. Used by
``per_city_source_scorecard`` and the ensemble redesign work.

Why these matter
----------------

Cross-bracket investigation (2026-05-05) showed that the v2 ensemble's σ
is dramatically under-calibrated *before* the day's diurnal peak — the
model claims σ=1.37°F at 38h pre-settlement when realized error is ~9°F.
The correct organizing axis is **time-of-day relative to the diurnal
peak**, not raw hours-to-settlement (TTE). A 12h-TTE decision in NY at
07:00 LST (pre-peak, day hasn't started) is a fundamentally different
state from a 12h-TTE decision in AUS at 19:00 LST (post-peak, high is
set). These helpers make that distinction first-class.

LST is **fixed** — Kalshi defines settlement against local-standard-time
year-round, ignoring DST. ``WeatherStation.lst_offset`` is the canonical
offset.

Phase boundaries (defaults; per-city overrides expected from Phase 2f)
----------------------------------------------------------------------

- ``pre_peak``    LST 06:00–11:59 — day hasn't happened, NWP dominates
- ``peak_window`` LST 12:00–17:59 — high is being realized
- ``post_peak``   LST 18:00–23:59 — high is essentially set, METAR ~truth
- ``overnight``   LST 00:00–05:59 — pre-dawn, between settlement and next day
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bot.daemon.stations import (
    WeatherStation,
    station_for_city,
    station_for_ticker,
    lst_offset_for_station,
)


# Default phase boundaries. Per-city overrides will be derived empirically
# from Phase 2f (per-city diurnal-peak detection) and stored as a dict
# keyed by station ICAO when that work lands.
DEFAULT_PHASE_BOUNDARIES: tuple[tuple[str, int, int], ...] = (
    ("overnight",   0,  6),
    ("pre_peak",    6,  12),
    ("peak_window", 12, 18),
    ("post_peak",   18, 24),
)


def lst_hour(utc_ts: float | int | datetime, *, lst_offset: int) -> int:
    """Return the LST hour (0–23) for a UTC timestamp, given the city's
    fixed LST offset (e.g. ``-5`` for EST).

    Accepts unix-seconds (int/float) or a tz-aware ``datetime``.
    """
    if isinstance(utc_ts, datetime):
        if utc_ts.tzinfo is None:
            raise ValueError("datetime must be tz-aware (UTC)")
        ts = utc_ts.astimezone(timezone.utc).timestamp()
    else:
        ts = float(utc_ts)
    # Hour-of-day in LST = (UTC hour + lst_offset) mod 24
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (utc_dt.hour + lst_offset) % 24


def lst_date(utc_ts: float | int | datetime, *, lst_offset: int) -> str:
    """Return the LST calendar date (YYYY-MM-DD) for a UTC timestamp.

    Used to align forecast snapshots to the settlement *day* under the
    same LST convention Kalshi uses.
    """
    if isinstance(utc_ts, datetime):
        if utc_ts.tzinfo is None:
            raise ValueError("datetime must be tz-aware (UTC)")
        ts = utc_ts.astimezone(timezone.utc).timestamp()
    else:
        ts = float(utc_ts)
    # Shift the UTC timestamp by the LST offset, then take the date part.
    # lst_offset is hours; convert to seconds.
    lst_ts = ts + lst_offset * 3600
    return datetime.fromtimestamp(lst_ts, tz=timezone.utc).strftime("%Y-%m-%d")


def diurnal_phase(
    hour: int,
    *,
    boundaries: tuple[tuple[str, int, int], ...] = DEFAULT_PHASE_BOUNDARIES,
) -> str:
    """Bucket an LST hour into a diurnal phase string.

    Boundaries are half-open ``[lo, hi)`` and must cover [0, 24). The
    default boundaries can be overridden per-city when Phase 2f has
    measured the empirical peak time.
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour out of range: {hour}")
    for name, lo, hi in boundaries:
        if lo <= hour < hi:
            return name
    raise RuntimeError(f"phase boundaries do not cover hour {hour}")


def phase_for(utc_ts, *, lst_offset: int) -> tuple[int, str]:
    """Convenience: return ``(lst_hour, phase)`` for a UTC timestamp."""
    h = lst_hour(utc_ts, lst_offset=lst_offset)
    return h, diurnal_phase(h)


def offset_for_ticker(ticker: str) -> int:
    """LST offset for a Kalshi ticker, defaulting to -5 if unknown."""
    s = station_for_ticker(ticker)
    return s.lst_offset if s is not None else -5


def offset_for_city(city: str) -> Optional[int]:
    """LST offset for a city key (canonical or alias), or None."""
    s = station_for_city(city)
    return s.lst_offset if s is not None else None
