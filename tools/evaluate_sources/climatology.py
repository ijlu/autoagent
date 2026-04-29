"""Climatology baseline: per-(station, day-of-year) historical average.

Computed leave-one-out from our own ``weather_metar_hourly_backfill``:
for each (station, target_date), average the daily_high_f from the same
day-of-year across all OTHER years/dates we have, but since our backfill
is only ~3 months, we use a windowed average: same station, ±N days
across all our observed dates EXCLUDING the target itself.

This is the dumbest possible "forecast" — what's the typical high for
this station, ignoring weather entirely. Useful as:

  1. Sanity floor — any real forecast should beat climatology
  2. Independence — climatology has zero correlation with weather
     models (it's not modeling weather, it's modeling the calendar)
  3. Catch-all when other sources are unavailable

We use a ±14-day window centered on the target's day-of-year, leave-
one-out (drop the target row itself).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from tools.evaluate_data_source import register


_CLIM_CACHE: dict = {}


def _get_conn() -> sqlite3.Connection:
    """Open a fresh connection per call. Avoids stale-connection issues
    when the EVAL_DB_PATH changes between runs."""
    import os
    path = os.environ.get("EVAL_DB_PATH", "/tmp/kalshi_trades_postmig.db")
    return sqlite3.connect(path)


@register("climatology_loo_14d")
def fetch(station: str, lst_date: str, lat: float, lon: float, *, tz: str = "America/New_York") -> Optional[tuple[float, Optional[float]]]:
    """Leave-one-out 14-day windowed climatology."""
    cache_key = (station, lst_date)
    if cache_key in _CLIM_CACHE:
        return _CLIM_CACHE[cache_key]

    target = datetime.strptime(lst_date, "%Y-%m-%d").date()
    target_doy = target.timetuple().tm_yday

    conn = _get_conn()
    rows = conn.execute(
        """SELECT lst_date, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE station = ? AND daily_high_f IS NOT NULL
              AND lst_date != ?
            GROUP BY lst_date""",
        (station, lst_date),
    ).fetchall()

    in_window = []
    for d_str, high in rows:
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
            doy = d.timetuple().tm_yday
            # Distance, wrapping around year boundary
            delta = min(abs(doy - target_doy), 365 - abs(doy - target_doy))
            if delta <= 14:
                in_window.append(float(high))
        except (ValueError, TypeError):
            continue

    if len(in_window) < 5:
        _CLIM_CACHE[cache_key] = None
        return None

    mu = sum(in_window) / len(in_window)
    # Climatology variance — empirical std of the window. Floor at 4°F
    # (climatology is wide on purpose).
    mean = mu
    variance = sum((x - mean) ** 2 for x in in_window) / len(in_window)
    sigma = max(4.0, variance ** 0.5)
    out = (round(mu, 2), round(sigma, 2))
    _CLIM_CACHE[cache_key] = out
    return out
