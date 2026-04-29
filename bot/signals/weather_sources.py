"""Canonical weather-source name registry.

Single source of truth for the strings used to key per-source data —
``source_name`` on ``GaussianForecast``, ``source`` column on
``weather_gaussian_snapshots_backfill``, kv_cache keys for learned bias
and learned sigma, group routing, and tests.

Anything that writes or reads a weather-source string MUST use one of
these constants. Drift-guard tests assert that the live ``_collect_gaussians``
getters and the backfill tool only emit names from ``CANONICAL_WEATHER_SOURCES``.

Historical context: a silent drift between live (``"weather"``) and backfill
(``"open_meteo"``) for the Open-Meteo source meant the A5 MOS-bias fit
persisted keys the reader never queried. Calibration sat dark for weeks.
This module exists so that bug can't recur.
"""

from __future__ import annotations


HRRR: str = "hrrr"
NBM: str = "nbm"
NWS_POINT: str = "nws_point"
TOMORROW: str = "tomorrow"  # 2026-04-26: dropped from live ensemble (TOS storage clause + reanalysis-only history). Constant kept so any historical kv_cache row referencing it is still recognised by readers; not a member of CANONICAL_WEATHER_SOURCES.
WEATHER: str = "weather"  # Open-Meteo (kept "weather" to match live source_name)
METAR: str = "metar"
MADIS: str = "madis"
AFD: str = "afd"  # treated as a Gaussian via base + parsed_shift in v1
ICON: str = "icon"  # 2026-04-29: German DWD via Open-Meteo (Phase B.2)
UKMO: str = "ukmo"  # 2026-04-29: UK Met Office via Open-Meteo (Phase B.2)
IEM_1MIN: str = "iem_1min"  # 2026-04-29: IEM 1-min ASOS observations (Phase B.2)


# Order is irrelevant; treat as a set.
# 2026-04-26: TOMORROW removed (see constant note). Re-add only if the source
# is wired back into _collect_gaussians AND a TOS-clean storage path exists.
# 2026-04-29: ICON, UKMO, IEM_1MIN added (Phase B.2 dynamic ensemble).
CANONICAL_WEATHER_SOURCES: frozenset[str] = frozenset({
    HRRR, NBM, NWS_POINT, WEATHER, METAR, MADIS, AFD,
    ICON, UKMO, IEM_1MIN,
})


# Subset that participates in the ``_collect_gaussians`` Gaussian combine.
# AFD is excluded because it's applied as a post-combine logit shift, not
# as an independent Gaussian (today). If/when AFD becomes a full Gaussian
# this set should expand and the comment should be updated.
#
# 2026-04-29: NBM and MADIS dropped from the live combine.
#   * NBM was a misnamed Open-Meteo proxy — ``models=gfs_seamless`` returns
#     values identical to the default Open-Meteo blend (which is GFS for
#     US lat/lons), so it duplicated the WEATHER source. The constants
#     stay in CANONICAL_WEATHER_SOURCES because historical kv_cache rows
#     reference them. Real NBM (NOAA's NBM API) would re-enter this set
#     once wired up.
#   * MADIS's ``+8°F flat morning warming`` heuristic produced -8 to -17°F
#     bias in production snapshots. Replaced by metar_observations'
#     learned residual-σ which is strictly more skillful. Same back-compat
#     story.
GAUSSIAN_COMBINE_SOURCES: frozenset[str] = frozenset({
    HRRR, NWS_POINT, WEATHER, METAR,
    ICON, UKMO,  # Added 2026-04-29 (Phase B.2). PROBATIONARY
                 # state via pre-seed; promoted to ACTIVE by
                 # the daily evaluator after 50+ settled rows.
})

# IEM_1MIN deliberately NOT in GAUSSIAN_COMBINE_SOURCES even though
# the constant exists. Discovered 2026-04-29 post-deploy that IEM's
# `asos1min.py` endpoint has ~24h publication latency — useless for
# live observations of the current day's high. The constant + canonical
# membership stays so historical kv_cache rows keep working; the source
# module stays for potential future use as retrospective ground truth.


def is_canonical(source_name: str) -> bool:
    """True iff ``source_name`` is one of the registered canonical names."""
    return source_name in CANONICAL_WEATHER_SOURCES
