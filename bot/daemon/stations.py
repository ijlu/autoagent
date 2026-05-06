"""Canonical weather-station registry (T1.1).

**Single source of truth** for every weather-related component:

- Daemon METAR poller reads from here (``ALL_STATION_IDS`` drives the URL).
- ``WeatherQuoter`` / ``smart_gates`` pull LST offsets from here.
- Signal sources (``metar_observations``, ``weather``, ``madis``,
  ``hrrr``, ``ndfd_nbm``, ``nws_point``, ``afd``) import the same
  registry — no more parallel station maps drifting out of sync.

Before T1.1 there were five separate registries
(``stations.py``, ``metar_observations.py``, ``weather.py``,
``madis.py``, plus a derived copy in ``smart_gates.py``) that disagreed
on small but important details — **NYC resolved to KJFK in the daemon
but KNYC in the signal sources**, a 2–5°F systematic gap depending on
season.

We resolved that conflict in favor of **KNYC** (Central Park) because:

1. Kalshi's ``KXHIGHNY`` markets track the NWS climatological record
   for NYC, which has used Central Park since the 19th century. KJFK
   is coastal and runs 2–5°F cooler in summer — the daemon polling
   KJFK would have been mis-aligned with settlement.
2. Phase 0 backtest validation (Brier 0.09–0.21 on weather families,
   +4.7¢ markout) was computed with signal sources reading KNYC.
   Switching them to KJFK would have invalidated that evidence.
3. Weather MM is currently blocked (``WEATHER_MM_LIVE=false``), so
   re-homing the daemon primary from KJFK to KNYC has zero trading
   impact today — it just aligns the shadow-mode readings with
   settlement.

KJFK, KLGA, and KEWR remain in the NY ``backups`` tuple so the
poller still cross-references them and the MADIS basket still
spatially averages them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# Dataclass
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WeatherStation:
    """Everything a weather component needs to know about one tradable series.

    Attributes
    ----------
    icao : str
        Primary METAR station ID (e.g. ``"KNYC"``). This is what the
        daemon poller fetches and what ``WeatherQuoter`` uses for
        settlement-aligned observations.
    city : str
        Canonical city key (lowercase, no abbreviation — e.g.
        ``"nyc"``, not ``"new york"``).
    series : str
        Kalshi ticker prefix (e.g. ``"KXHIGHNY"``).
    lst_offset : int
        Local-Standard-Time UTC offset (hours). **Fixed** — does NOT
        apply DST. Kalshi settlement windows are defined against LST.
    lat : float
        Station latitude (decimal degrees, positive north).
    lon : float
        Station longitude (decimal degrees, negative west).
    backups : tuple[str, ...]
        Additional METAR stations within ~15 miles. Used by the
        METAR poller for backfill when the primary is silent.
    madis_basket : tuple[str, ...]
        50-mile-radius station basket for MADIS-style spatial
        consistency. Always *includes* the primary — iterating this
        gives the full query set with no deduping needed.
    aliases : tuple[str, ...]
        Alternative city keys that should resolve to this station
        (e.g. ``"new york"`` for ``"nyc"``, ``"la"`` for
        ``"los angeles"``).
    """

    icao: str
    city: str
    series: str
    lst_offset: int
    lat: float
    lon: float
    backups: tuple[str, ...] = ()
    madis_basket: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    # ---------------------------------------------------------------
    # Dict-style access for backwards-compat with pre-T1.1 call sites
    # that still index with ``cfg["lst_offset"]`` etc.
    #
    # Prefer attribute access (``cfg.lst_offset``) in new code. These
    # shims exist so the refactor can migrate call sites incrementally
    # without breaking tests on every commit.
    # ---------------------------------------------------------------

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


# ═══════════════════════════════════════════════════════════════════════════════
# Registry — tradeable weather series
# ═══════════════════════════════════════════════════════════════════════════════

# Order matters: iteration order equals daemon poll order. Kept stable so
# log diffs stay readable across deploys.
_REGISTRY: tuple[WeatherStation, ...] = (
    WeatherStation(
        icao="KNYC",
        city="nyc",
        series="KXHIGHNY",
        lst_offset=-5,           # EST, year-round
        lat=40.78,
        lon=-73.97,
        backups=("KJFK", "KLGA", "KEWR"),
        madis_basket=("KNYC", "KLGA", "KJFK", "KEWR", "KTEB"),
        aliases=("new york",),
    ),
    WeatherStation(
        icao="KMDW",
        city="chicago",
        series="KXHIGHCHI",
        lst_offset=-6,
        lat=41.79,
        lon=-87.75,
        backups=("KORD",),
        madis_basket=("KMDW", "KORD", "KPWK", "KGYY"),
    ),
    WeatherStation(
        icao="KLAX",
        city="los angeles",
        series="KXHIGHLAX",
        lst_offset=-8,
        lat=33.94,
        lon=-118.41,
        backups=("KBUR", "KSNA"),
        madis_basket=("KLAX", "KBUR", "KSMO", "KLGB"),
        aliases=("la",),
    ),
    WeatherStation(
        icao="KAUS",
        city="austin",
        series="KXHIGHAUS",
        lst_offset=-6,
        lat=30.19,
        lon=-97.67,
        backups=(),
        madis_basket=("KAUS", "KBAZ", "KGTU", "KEDC"),
    ),
    WeatherStation(
        icao="KMIA",
        city="miami",
        series="KXHIGHMIA",
        lst_offset=-5,
        lat=25.79,
        lon=-80.29,
        backups=("KFLL",),
        madis_basket=("KMIA", "KFLL", "KTMB", "KHWO"),
    ),
    WeatherStation(
        icao="KDEN",
        city="denver",
        series="KXHIGHDEN",
        lst_offset=-7,
        lat=39.86,
        lon=-104.67,
        backups=(),
        madis_basket=("KDEN", "KBJC", "KAPA", "KCFO"),
    ),
    # Houston/Phoenix/SF removed 2026-04-16 — Kalshi has no open
    # KXHIGHHOU/PHX/SF markets. Leaving them in caused ~7 no-market
    # errors/hour. Re-add when/if Kalshi launches these series.
)


# ═══════════════════════════════════════════════════════════════════════════════
# Derived lookup tables
# ═══════════════════════════════════════════════════════════════════════════════

# Keyed by primary ICAO — what the daemon has iterated since day one.
STATIONS: dict[str, WeatherStation] = {s.icao: s for s in _REGISTRY}

# Series prefix → canonical station.
STATION_BY_SERIES: dict[str, WeatherStation] = {s.series: s for s in _REGISTRY}

# City key (including aliases) → canonical station.
STATION_BY_CITY: dict[str, WeatherStation] = {}
for _s in _REGISTRY:
    STATION_BY_CITY[_s.city] = _s
    for _alias in _s.aliases:
        STATION_BY_CITY[_alias] = _s

# Legacy string→string maps kept for callers that just need the ICAO.
SERIES_TO_STATION: dict[str, str] = {s.series: s.icao for s in _REGISTRY}
CITY_TO_STATION: dict[str, str] = {
    city: s.icao for city, s in STATION_BY_CITY.items()
}

# All station IDs to request from the METAR API (primary + backups, no dups).
_all_ids: list[str] = []
_seen: set[str] = set()
for _s in _REGISTRY:
    for _sid in (_s.icao, *_s.backups):
        if _sid not in _seen:
            _all_ids.append(_sid)
            _seen.add(_sid)
ALL_STATION_IDS: list[str] = _all_ids
del _all_ids, _seen, _s


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers — prefer these over manual dict indexing
# ═══════════════════════════════════════════════════════════════════════════════

def station_for_ticker(ticker: str) -> Optional[WeatherStation]:
    """Resolve a Kalshi ticker to its WeatherStation, or None.

    Matches the series prefix (``KXHIGHNY-26APR20-T75`` → KXHIGHNY → KNYC).
    Case-insensitive.
    """
    if not ticker:
        return None
    t = ticker.upper()
    # Longest-prefix match so KXHIGHNY doesn't accidentally match KXHIGH.
    for series in sorted(STATION_BY_SERIES, key=len, reverse=True):
        if t.startswith(series):
            return STATION_BY_SERIES[series]
    return None


def station_for_city(city: str) -> Optional[WeatherStation]:
    """Resolve a city key (canonical or alias) to its WeatherStation."""
    if not city:
        return None
    return STATION_BY_CITY.get(city.lower())


def lst_offset_for_station(icao: str) -> int:
    """Return the fixed LST offset for a station, defaulting to -5 (EST)."""
    s = STATIONS.get(icao)
    return s.lst_offset if s is not None else -5
