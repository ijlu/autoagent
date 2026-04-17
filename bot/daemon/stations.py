"""Station configuration for the weather daemon.

Each station maps to a Kalshi weather series (KXHIGHNY -> KJFK, etc).
Includes timezone offsets, multi-station backups, and city metadata.
"""
from __future__ import annotations

# Primary stations -- these are what Kalshi uses for settlement
STATIONS: dict[str, dict] = {
    "KJFK": {
        "city": "nyc",
        "series": "KXHIGHNY",
        "lst_offset": -5,  # EST always (settlement uses LST, not daylight)
        "lat": 40.64,
        "lon": -73.78,
        "backups": ["KLGA", "KEWR"],  # secondary stations for cross-reference
    },
    "KMDW": {
        "city": "chicago",
        "series": "KXHIGHCHI",
        "lst_offset": -6,
        "lat": 41.79,
        "lon": -87.75,
        "backups": ["KORD"],
    },
    "KLAX": {
        "city": "los angeles",
        "series": "KXHIGHLAX",
        "lst_offset": -8,
        "lat": 33.94,
        "lon": -118.41,
        "backups": ["KBUR", "KSNA"],
    },
    "KAUS": {
        "city": "austin",
        "series": "KXHIGHAUS",
        "lst_offset": -6,
        "lat": 30.19,
        "lon": -97.67,
        "backups": [],
    },
    "KMIA": {
        "city": "miami",
        "series": "KXHIGHMIA",
        "lst_offset": -5,
        "lat": 25.79,
        "lon": -80.29,
        "backups": ["KFLL"],
    },
    "KDEN": {
        "city": "denver",
        "series": "KXHIGHDEN",
        "lst_offset": -7,
        "lat": 39.86,
        "lon": -104.67,
        "backups": [],
    },
    # Houston/Phoenix/SF removed 2026-04-16 — Kalshi has no open KXHIGHHOU/PHX/SF
    # markets. Leaving them in caused ~7 no-market errors/hour. Re-add if Kalshi
    # launches these series.
}

# Reverse lookups
SERIES_TO_STATION: dict[str, str] = {v["series"]: k for k, v in STATIONS.items()}
CITY_TO_STATION: dict[str, str] = {v["city"]: k for k, v in STATIONS.items()}

# All station IDs for the METAR API call (primary + backups)
ALL_STATION_IDS: list[str] = []
for _station_id, _cfg in STATIONS.items():
    ALL_STATION_IDS.append(_station_id)
    ALL_STATION_IDS.extend(_cfg.get("backups", []))
