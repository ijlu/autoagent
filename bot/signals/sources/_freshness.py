"""Schedule-derived latest-cycle timestamps for forecast sources.

Each numerical weather prediction model has a deterministic publish schedule.
HRRR runs every hour at H:00 UTC; NBM runs at 01z/07z/13z/19z; NWS Point
updates hourly. Given current time + the schedule, we can compute the most
recent COMPLETED cycle's start time without parsing API responses or
trusting any source's claimed `issuedAt`.

This timestamp is what feeds ``GaussianForecast.issued_at`` and drives the
staleness inflation in ``weather_ensemble_v2._apply_staleness_inflation``.
At the moment a forecast is consumed, ``staleness_h = now - issued_at`` is
the actual age of the model run that produced the data — much more accurate
than a constant per-source "average staleness" assumption.

Why per-source completion lag matters: a model that runs at H:00 doesn't
finish computing until ~H:30 (HRRR) or ~H:60 (NBM, which has heavier
post-processing). Until completion, the published cycle is still the
PREVIOUS one. ``completion_lag_minutes`` captures this so we don't briefly
flip ``issued_at`` to a cycle that hasn't actually published yet.

Bounded by design: even if this returns a wildly wrong cycle (e.g. parser
bug, NOAA outage), the consuming staleness inflation is clamped to
[1.0, 2.0]× — σ can at most double. So a wrong ``issued_at`` can't blow
up the ensemble.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Sequence


def latest_cycle_issued_at(
    cycle_hours_utc: Sequence[int],
    *,
    completion_lag_minutes: int = 30,
    now_utc: "datetime | None" = None,
) -> float:
    """Return the unix timestamp of the most recent completed model cycle.

    Parameters
    ----------
    cycle_hours_utc
        UTC hours at which the model publishes runs. ``range(24)`` for
        hourly models like HRRR; ``[1, 7, 13, 19]`` for NBM; etc.
    completion_lag_minutes
        Minutes after cycle start before the run is considered "completed"
        and available to API consumers. HRRR ~30, NBM ~60, NWS Point ~15.
    now_utc
        Override for testing. Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    Unix timestamp (float, seconds) of the latest completed cycle. If no
    cycle has completed in the last 36 hours (deeply unusual — would
    indicate a NOAA outage or a misconfigured schedule), returns
    ``now − 24h`` so staleness inflation maxes out at the clamp ceiling.
    """
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if not cycle_hours_utc:
        return now.timestamp() - 86400.0

    # Search today and the previous two days — covers the "we're 5 minutes
    # past midnight UTC and the only completed cycle is yesterday's last
    # one" case without relying on heuristic offsets.
    for offset_days in (0, 1, 2):
        anchor_date = (now - timedelta(days=offset_days)).date()
        for h in sorted(cycle_hours_utc, reverse=True):
            cycle = datetime(
                anchor_date.year, anchor_date.month, anchor_date.day,
                h, 0, 0, tzinfo=timezone.utc,
            )
            if cycle + timedelta(minutes=completion_lag_minutes) <= now:
                return cycle.timestamp()

    # Fallback: nothing completed in 3 days — return 24h ago so consumers
    # treat data as maximally stale (clamp ceiling kicks in).
    return now.timestamp() - 86400.0


# ── Per-source schedules ──────────────────────────────────────────────────
#
# These are the schedules NOAA / NWS / Open-Meteo publish. If they change
# (e.g. NBM moves to 4-hourly), update here and bump the schedule lag if
# completion times change too.

_HRRR_CYCLE_HOURS_UTC: tuple[int, ...] = tuple(range(24))   # hourly
_HRRR_COMPLETION_LAG_MIN: int = 30

_NBM_CYCLE_HOURS_UTC: tuple[int, ...] = (1, 7, 13, 19)      # 6-hourly
_NBM_COMPLETION_LAG_MIN: int = 60

_NWS_POINT_CYCLE_HOURS_UTC: tuple[int, ...] = tuple(range(24))  # hourly
_NWS_POINT_COMPLETION_LAG_MIN: int = 15

# Open-Meteo aggregates several models. For temperature highs in the 0-2 day
# horizon it primarily serves HRRR (when ``&models=hrrr`` is used) and
# ECMWF/IFS otherwise. Use the HRRR schedule as a conservative upper bound
# on freshness — Open-Meteo's caching layer can lag by 5-15 minutes on top.
_OPEN_METEO_CYCLE_HOURS_UTC: tuple[int, ...] = tuple(range(24))
_OPEN_METEO_COMPLETION_LAG_MIN: int = 45


def hrrr_latest_issued_at(now_utc: "datetime | None" = None) -> float:
    return latest_cycle_issued_at(
        _HRRR_CYCLE_HOURS_UTC,
        completion_lag_minutes=_HRRR_COMPLETION_LAG_MIN,
        now_utc=now_utc,
    )


def nbm_latest_issued_at(now_utc: "datetime | None" = None) -> float:
    return latest_cycle_issued_at(
        _NBM_CYCLE_HOURS_UTC,
        completion_lag_minutes=_NBM_COMPLETION_LAG_MIN,
        now_utc=now_utc,
    )


def nws_point_latest_issued_at(now_utc: "datetime | None" = None) -> float:
    return latest_cycle_issued_at(
        _NWS_POINT_CYCLE_HOURS_UTC,
        completion_lag_minutes=_NWS_POINT_COMPLETION_LAG_MIN,
        now_utc=now_utc,
    )


def open_meteo_latest_issued_at(now_utc: "datetime | None" = None) -> float:
    return latest_cycle_issued_at(
        _OPEN_METEO_CYCLE_HOURS_UTC,
        completion_lag_minutes=_OPEN_METEO_COMPLETION_LAG_MIN,
        now_utc=now_utc,
    )
