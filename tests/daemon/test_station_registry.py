"""T1.1 — canonical station registry regression guard.

Before T1.1 five separate files carried parallel station → LST / station
→ ICAO / city → basket tables, and they had drifted apart (KJFK in the
daemon, KNYC in the signal sources). This test freezes the invariants
of the canonical registry and makes sure every downstream consumer
reads the same values.

Failure means either:
- a new parallel map was added (consolidate into ``bot/daemon/stations.py``), or
- a consumer reached past the registry helper and hard-coded a value.
"""
from __future__ import annotations

import pytest

from bot.daemon.stations import (
    ALL_STATION_IDS,
    CITY_TO_STATION,
    SERIES_TO_STATION,
    STATION_BY_CITY,
    STATION_BY_SERIES,
    STATIONS,
    WeatherStation,
    lst_offset_for_station,
    station_for_city,
    station_for_ticker,
)


# ─── Registry shape ────────────────────────────────────────────────────────

def test_every_registry_entry_is_a_dataclass():
    for sid, s in STATIONS.items():
        assert isinstance(s, WeatherStation), f"{sid} is {type(s).__name__}"
        # Matches the primary ICAO key.
        assert s.icao == sid


def test_primary_icao_appears_in_its_own_madis_basket():
    """Ensures readers can iterate ``madis_basket`` without deduping."""
    for s in STATIONS.values():
        if s.madis_basket:
            assert s.icao in s.madis_basket, (
                f"{s.icao}: madis_basket {s.madis_basket} missing primary"
            )


def test_all_station_ids_contains_primaries_and_backups():
    for s in STATIONS.values():
        assert s.icao in ALL_STATION_IDS
        for bkup in s.backups:
            assert bkup in ALL_STATION_IDS


def test_no_duplicates_in_all_station_ids():
    assert len(ALL_STATION_IDS) == len(set(ALL_STATION_IDS))


def test_series_uniqueness():
    series = [s.series for s in STATIONS.values()]
    assert len(series) == len(set(series)), f"duplicate series in registry: {series}"


def test_dict_access_shim_matches_attribute_access():
    for s in STATIONS.values():
        assert s["icao"] == s.icao
        assert s["city"] == s.city
        assert s["series"] == s.series
        assert s["lst_offset"] == s.lst_offset
        assert s.get("lat") == s.lat
        assert s.get("nonexistent", "dflt") == "dflt"


# ─── NY settlement lock ────────────────────────────────────────────────────

def test_nyc_primary_is_knyc_not_kjfk():
    """NYC was intentionally moved from KJFK → KNYC in T1.1.

    Central Park (KNYC) is the NWS climatological station for NYC; Kalshi
    settlement tracks it. KJFK (coastal) runs 2-5°F cooler in summer —
    polling there would systematically bias shadow reads.

    KJFK remains as a backup so the poller still cross-references it.
    """
    ny = STATION_BY_SERIES["KXHIGHNY"]
    assert ny.icao == "KNYC", (
        "Do NOT flip this back to KJFK without re-running Phase 0 "
        "backtest against the new primary station. See stations.py header."
    )
    assert "KJFK" in ny.backups


# ─── Lookup helpers ────────────────────────────────────────────────────────

def test_station_for_ticker_matches_longest_series_prefix():
    ws = station_for_ticker("KXHIGHNY-26APR20-T75")
    assert ws is not None and ws.series == "KXHIGHNY"


def test_station_for_ticker_case_insensitive():
    assert station_for_ticker("kxhighny-26apr20-t75") is not None


def test_station_for_ticker_returns_none_on_unknown():
    assert station_for_ticker("KXBTC-26APR20-T75000") is None
    assert station_for_ticker("") is None


def test_station_for_ticker_handles_dead_markets_gracefully():
    """Houston/Phoenix/SF were removed — the lookup returns None rather
    than fabricating a city the daemon can't actually trade."""
    for dead_ticker in ("KXHIGHHOU-26APR20-T95", "KXHIGHPHX-26APR20-T105",
                        "KXHIGHSF-26APR20-T70"):
        assert station_for_ticker(dead_ticker) is None


def test_station_for_city_resolves_aliases():
    nyc = station_for_city("nyc")
    assert nyc is not None and nyc.icao == "KNYC"
    assert station_for_city("new york") is nyc          # alias
    assert station_for_city("New York") is nyc          # case-insensitive
    assert station_for_city("la") is station_for_city("los angeles")


def test_lst_offset_defaults_to_est_on_unknown():
    assert lst_offset_for_station("UNKNOWN_STATION") == -5


# ─── Consumer sync checks ───────────────────────────────────────────────────

def test_signal_source_weather_uses_registry():
    """weather.py's _TICKER_CITY_MAP / _CITY_STATION_MAP / _CITY_LST_OFFSET
    for tradeable cities must equal the registry values."""
    from bot.signals.sources import weather as weather_mod

    for series, station in STATION_BY_SERIES.items():
        assert weather_mod._TICKER_CITY_MAP[series] == station.city, series
        assert weather_mod._CITY_STATION_MAP[station.city] == station.icao, series
        assert weather_mod._CITY_LST_OFFSET[station.city] == station.lst_offset, series


def test_signal_source_madis_basket_matches_registry():
    from bot.signals.sources import madis as madis_mod

    for city, station in STATION_BY_CITY.items():
        if not station.madis_basket:
            continue
        assert madis_mod._CITY_STATION_BASKET[city] == list(station.madis_basket), city


def test_smart_gates_offset_matches_registry():
    """smart_gates._STATION_LST_OFFSET is derived from the registry."""
    from bot.daemon.smart_gates import _STATION_LST_OFFSET

    for sid, offset in _STATION_LST_OFFSET.items():
        assert offset == STATIONS[sid].lst_offset


def test_metar_observations_url_uses_registry_primaries():
    from bot.signals.sources import metar_observations as m

    for station in STATION_BY_SERIES.values():
        assert station.icao in m._METAR_URL, (
            f"{station.icao} missing from metar URL {m._METAR_URL}"
        )


# ─── No-parallel-map guard ─────────────────────────────────────────────────

@pytest.mark.parametrize("banned_pattern", [
    # These literal station-list definitions should no longer appear in
    # signal sources — they must derive from the canonical registry.
    ('bot/signals/sources/metar_observations.py', '_TICKER_STATION_MAP = {'),
    ('bot/signals/sources/metar_observations.py', '_METAR_STATION_MAP = {'),
    ('bot/signals/sources/metar_observations.py', '_STATION_LST_OFFSET = {'),
])
def test_no_parallel_station_dicts(banned_pattern):
    """Regression: the duplicate dicts that T1.1 deleted must not return."""
    from pathlib import Path
    rel_path, needle = banned_pattern
    path = Path(__file__).resolve().parent.parent.parent / rel_path
    text = path.read_text(encoding="utf-8")
    assert needle not in text, (
        f"{rel_path}: the pre-T1.1 dict `{needle}` has reappeared — "
        "use bot.daemon.stations instead."
    )
