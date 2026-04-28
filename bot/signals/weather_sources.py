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


# Order is irrelevant; treat as a set.
# 2026-04-26: TOMORROW removed (see constant note). Re-add only if the source
# is wired back into _collect_gaussians AND a TOS-clean storage path exists.
CANONICAL_WEATHER_SOURCES: frozenset[str] = frozenset({
    HRRR, NBM, NWS_POINT, WEATHER, METAR, MADIS, AFD,
})


# Subset that participates in the ``_collect_gaussians`` Gaussian combine.
# AFD is excluded because it's applied as a post-combine logit shift, not
# as an independent Gaussian (today). If/when AFD becomes a full Gaussian
# this set should expand and the comment should be updated.
GAUSSIAN_COMBINE_SOURCES: frozenset[str] = frozenset({
    HRRR, NBM, NWS_POINT, WEATHER, METAR, MADIS,
})


def is_canonical(source_name: str) -> bool:
    """True iff ``source_name`` is one of the registered canonical names."""
    return source_name in CANONICAL_WEATHER_SOURCES
