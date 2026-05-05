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

from typing import Optional


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
GEM: str = "gem"  # 2026-04-30: Canadian CMC GEM via Open-Meteo
METNO: str = "metno"  # 2026-04-30: MET Norway via Open-Meteo
ECMWF: str = "ecmwf"  # 2026-04-30: ECMWF HRES (IFS 0.25°) via Open-Meteo
NWS_5MIN: str = "nws_5min"  # 2026-04-30: NWS api.weather.gov 5-min ASOS observations
NWS_5MIN_DIURNAL: str = "nws_5min_diurnal"  # 2026-05-02: METAR diurnal fit + 5-min input
NWS_5MIN_ANALOG: str = "nws_5min_analog"  # 2026-05-02: today's curve fit + historical analog


# Order is irrelevant; treat as a set.
# 2026-04-26: TOMORROW removed (see constant note). Re-add only if the source
# is wired back into _collect_gaussians AND a TOS-clean storage path exists.
# 2026-04-29: ICON, UKMO, IEM_1MIN added (Phase B.2 dynamic ensemble).
CANONICAL_WEATHER_SOURCES: frozenset[str] = frozenset({
    HRRR, NBM, NWS_POINT, WEATHER, METAR, MADIS, AFD,
    ICON, UKMO, IEM_1MIN,
    GEM, METNO, ECMWF, NWS_5MIN,  # 2026-04-30
    NWS_5MIN_DIURNAL, NWS_5MIN_ANALOG,  # 2026-05-02
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
    HRRR, NWS_POINT, METAR,
    # 2026-05-05: WEATHER dropped from combine. Per-city scorecard analysis
    # (reports/PER_SOURCE_INVESTIGATION_2026-05-05.md + POSTFIX_REASSESSMENT_
    # 2026-05-05.md) showed corr(hrrr, weather) = 0.994 (NY) / 1.000 (LAX)
    # at peak window — both pull from Open-Meteo (gfs_hrrr vs default
    # blend; default blend IS gfs at US lat/lons). Duplicated the HRRR
    # signal in the combine, halving effective weight of every other
    # source. Constant kept in CANONICAL_WEATHER_SOURCES so historical
    # kv_cache rows + back-fill snapshot rows referencing `weather` are
    # still recognized by readers; getter no longer wired into
    # weather_ensemble_v2._collect_gaussians.
    # WEATHER,
    ICON, UKMO,  # Added 2026-04-29 (Phase B.2). PROBATIONARY
                 # state via pre-seed; promoted to ACTIVE by
                 # the daily evaluator after 50+ settled rows.
    # 2026-04-30: validated via tools/investigate_new_forecast_sources.py.
    # GEM cuts pooled MAE 12% (best single addition); MetNo 4%; ECMWF
    # HRES 3% but most independent of current sources (ρ=0.34 vs ICON).
    # All start PROBATIONARY pending live shadow validation.
    GEM, METNO, ECMWF,
    # 2026-04-30 (later): NWS 5-min ASOS observations wired in.
    # Sub-hourly observation channel paralleling METAR. Same physical
    # sensor family — sits in _OBS_GROUP for correlation discount.
    # Verified live across all 6 city stations; 15-25 min publication
    # lag handled by `issued_at` + `_staleness_inflation_factor`.
    NWS_5MIN,
    # 2026-05-02: NWS_5MIN_DIURNAL feeds the freshest 5-min reading
    # through METAR's existing per-(station, lst_hour) regression —
    # gives us a forecast that updates 12× faster than METAR's hourly
    # application of the same fit. Sits in _OBS_GROUP alongside METAR
    # + NWS_5MIN; group correlation discount handles the overlap.
    NWS_5MIN_DIURNAL,
    # 2026-05-02 (later): NWS_5MIN_ANALOG demoted to SHADOW after
    # post-deploy probe revealed two issues that need data + a
    # vintage-strategy refactor before it's combine-worthy:
    #   1. Feature-vintage bug: the `forecast_hrrr/weather` features
    #      come from `AVG(forecast_high_f)` across the ticker's full
    #      snapshot lifetime. For a 7-day-aged market, the average
    #      collapses to the long-range outlook (~70°F for KMIA) and
    #      doesn't reflect the morning-of consensus (~95°F). Today's
    #      and historical days' averages converge to *stale* features
    #      so neighbors are picked on the wrong axis. Needs a
    #      latest-snapshot-per-source strategy.
    #   2. Regime coverage: only 35 historical days, peaks 78-88°F.
    #      Today's market is 93-95°F. Even with a perfect matcher the
    #      top-10-peak average is bounded above by 88°F — 5°F below
    #      truth on hot days. ~4-6 weeks of additional history needed
    #      before the matcher can span both regimes.
    # Stays in CANONICAL_WEATHER_SOURCES (registry membership for
    # historical kv_cache rows + back-fill snapshot rows). Removed
    # from `_collect_gaussians` until both the vintage strategy is
    # refactored AND we have ~60+ days of historical coverage.
    # NWS_5MIN_ANALOG,
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


# ─── Per-city source exclusions ───────────────────────────────────────
# 2026-05-04: introduced after the postmortem-driven regression revealed
# that several sources have *city-specific* biases of 4-10°F that the
# MOS-bias clamp (max ±8°F) can't fully correct. Rather than running
# every source for every city and trusting bias correction to handle
# everything, we exclude sources from cities where the regression shows
# they're structurally unreliable.
#
# Each entry maps a normalized city key (lower_with_underscores) to a
# frozenset of source names that should NOT contribute to the combine
# for that city. The check fires in `weather_ensemble_v2._collect_gaussians`
# right after each source returns its Gaussian.
#
# The registry is intentionally small to start. Add entries only with
# evidence (regression bias > 3°F, n >= 30, multiple-day persistence).
# Removing entries also requires evidence that the underlying source
# behavior changed.
EXCLUDED_SOURCES_BY_CITY: dict[str, frozenset[str]] = {
    # KNYC: nws_point cold-biased -5.86°F, n=5588 (regression 2026-05-04).
    # nws_5min and nws_5min_diurnal already filtered earlier via
    # PRIMARY_5MIN_STATION_BY_CITY (KLGA proxy ran +5°F warm vs KNYC).
    "nyc": frozenset({"nws_point"}),
    # KMDW: nws_point -5.79°F, nws_5min -7.35°F. The 5-min source's
    # "running max so far" interpretation is consistently below CF6
    # peak truth at Midway specifically.
    "chicago": frozenset({"nws_point", "nws_5min"}),
    # KMIA: nws_point -3.07°F, nws_5min -4.21°F.
    "miami": frozenset({"nws_point", "nws_5min"}),
    # KLAX: marine layer issue. metno and gem are 9-10°F warm-biased
    # (large outliers); ukmo/ecmwf/hrrr/icon/weather are also warm but
    # MOS-bias-correctable. Exclude the two worst until per-source bias
    # corrections ratchet up.
    "los_angeles": frozenset({"metno", "gem"}),
    # KAUS, KDEN: no exclusions — biases within ±3°F across all sources.
}


def is_excluded_for_city(source_name: str, city_key: Optional[str]) -> bool:
    """True iff ``source_name`` should be excluded from the combine for
    the given ``city_key``. Returns False if either argument is None or
    if the city has no entry in EXCLUDED_SOURCES_BY_CITY."""
    if city_key is None:
        return False
    excluded = EXCLUDED_SOURCES_BY_CITY.get(city_key)
    if excluded is None:
        return False
    return source_name in excluded
