"""Pin WEATHER_CITIES tradeable-city lat/lon to settlement station coords.

2026-05-04 postmortem: every forecast source (HRRR, ICON, UKMO, GEM,
MetNo, ECMWF, weather, nws_point) reads its lat/lon from WEATHER_CITIES.
The original design used "downtown" coordinates, claiming forecast grids
work better at city center. But Kalshi settles on the AIRPORT METAR
(KLAX, KMIA, KMDW, KAUS, KDEN) or KNYC Central Park — not downtown.

The mismatch was up to 14 miles for LAX (Downtown LA → Vernon, CA via
NWS /points). That distance crossed marine-layer boundaries and biased
7+ sources +5 to +10°F warm at KLAX in regression. NYC's "Lower
Manhattan" lat/lon resolved to Hoboken NJ — different microclimate from
Central Park.

This test makes any drift of the tradeable-city lat/lon away from the
settlement station an immediate failure. Cities NOT in the daemon's
STATIONS registry (phoenix, seattle, boston, atlanta, …) keep downtown
coords because they're not Kalshi-tradeable and settlement-alignment
doesn't apply.

Tolerance is tight: 0.05° ≈ 3.5 miles. Beyond that we're in a different
NWS grid cell and a meaningfully different micro-climate.
"""
from __future__ import annotations

import pytest

from bot.daemon.stations import STATIONS
from bot.signals.sources.weather import WEATHER_CITIES


# 0.05° ≈ 3.5 miles ≈ within the same 2.5km NWS grid cell
LATLON_TOL_DEG = 0.05


@pytest.mark.parametrize("station", STATIONS.values())
def test_weather_city_aligns_with_station(station):
    """Every tradeable city's WEATHER_CITIES lat/lon must be within
    0.05° of the settlement station — otherwise forecast sources query
    the wrong NWS grid cell."""
    city_key = station.city
    assert city_key in WEATHER_CITIES, (
        f"settlement station {station.icao} (city={city_key!r}) is "
        f"missing from WEATHER_CITIES"
    )
    fc_lat = WEATHER_CITIES[city_key]["lat"]
    fc_lon = WEATHER_CITIES[city_key]["lon"]
    dlat = abs(fc_lat - station.lat)
    dlon = abs(fc_lon - station.lon)
    assert dlat <= LATLON_TOL_DEG, (
        f"{station.icao}: WEATHER_CITIES lat={fc_lat} drifts {dlat:.4f}° "
        f"from settlement station lat={station.lat}. Forecast queries "
        f"will land in a different NWS grid cell — see test docstring."
    )
    assert dlon <= LATLON_TOL_DEG, (
        f"{station.icao}: WEATHER_CITIES lon={fc_lon} drifts {dlon:.4f}° "
        f"from settlement station lon={station.lon}. Forecast queries "
        f"will land in a different NWS grid cell — see test docstring."
    )


def test_aliases_match_canonical():
    """`new york` and `la` aliases must match their canonical entries.
    Drift here means one source uses settlement coords and another
    uses downtown — silently producing different forecasts."""
    assert WEATHER_CITIES["nyc"]["lat"] == WEATHER_CITIES["new york"]["lat"]
    assert WEATHER_CITIES["nyc"]["lon"] == WEATHER_CITIES["new york"]["lon"]
    assert WEATHER_CITIES["los angeles"]["lat"] == WEATHER_CITIES["la"]["lat"]
    assert WEATHER_CITIES["los angeles"]["lon"] == WEATHER_CITIES["la"]["lon"]


def test_non_tradeable_cities_unchanged():
    """Sanity: non-tradeable directional cities keep their downtown
    coords. Settlement-alignment only applies to Kalshi markets."""
    # Phoenix, Seattle, Boston, Atlanta, etc. are NOT in STATIONS.
    tradeable_cities = {st.city for st in STATIONS.values()}
    for city in ("phoenix", "seattle", "boston", "atlanta",
                 "san francisco", "dc", "minneapolis"):
        assert city in WEATHER_CITIES, (
            f"non-tradeable city {city!r} missing — directional signals "
            f"depend on it"
        )
        assert city not in tradeable_cities, (
            f"{city} is now tradeable; add to STATIONS and the alignment "
            f"test will validate. Right now this test is just guarding "
            f"against accidental removal."
        )
