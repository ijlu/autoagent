"""Comprehensive station-mapping verification.

For every source × city, find:
  1. What lat/lon (or station ICAO) the source actually queries
  2. What that resolves to (via NWS /points)
  3. The bias at SETTLEMENT (latest hours_out, when truth is known)
  4. Flag any (source, city) pair with |bias| > 2°F at settlement —
     everything should converge to actuals as TTE → 0
"""
import math
import requests
from collections import defaultdict
from bot.db import init_db
from bot.daemon.stations import STATIONS
from bot.signals.sources.weather import WEATHER_CITIES
from bot.signals.sources.nws_5min import PRIMARY_5MIN_STATION_BY_CITY


UA = "kalshi-bot diagnostic (joshlu@a16z.com)"

# 1) STATION-ID-BASED SOURCES (use ICAO directly, no lat/lon)
ICAO_SOURCES = {
    "metar":     "ws.icao",                  # bot.daemon.stations
    "nws_5min":  "PRIMARY_5MIN_STATION_BY_CITY",
    "nws_5min_diurnal": "PRIMARY_5MIN_STATION_BY_CITY",
    "madis":     "ws.madis_basket",
}

# 2) LAT/LON-BASED SOURCES (read WEATHER_CITIES)
LATLON_SOURCES = ["hrrr", "weather", "icon", "ukmo", "gem", "metno",
                  "ecmwf", "nws_point"]

# 3) AFD is text-based. No station mapping; uses NWS WFO office (which
# is keyed by lat/lon → forecast office mapping).

print("=== Per-city source-to-station resolution ===\n")
print(f"{'city':<14} {'source':<20} {'mapping':<35} {'resolves to':<25}")
print("-" * 100)

for st in STATIONS.values():
    settle_icao = st.icao
    city = st.city
    print(f"{city:<14} {'(SETTLEMENT)':<20} {settle_icao:<35} "
          f"{st.icao} ({st.lat}, {st.lon})")

    # ICAO-based sources
    print(f"{city:<14} {'metar':<20} ws.icao = {settle_icao:<26} "
          f"{settle_icao} ✓")

    nws5min_st = PRIMARY_5MIN_STATION_BY_CITY.get(city, "(SKIPPED — see exclusion)")
    print(f"{city:<14} {'nws_5min':<20} "
          f"PRIMARY_5MIN_BY_CITY = {nws5min_st}")
    print(f"{city:<14} {'nws_5min_diurnal':<20} "
          f"(same as nws_5min)")

    # Lat/lon sources
    fc_lat = WEATHER_CITIES.get(city, {}).get("lat")
    fc_lon = WEATHER_CITIES.get(city, {}).get("lon")
    if fc_lat is None:
        print(f"{city:<14} (lat/lon sources)  WEATHER_CITIES MISSING")
        continue

    # Probe NWS /points to see what the lat/lon resolves to
    url = f"https://api.weather.gov/points/{fc_lat:.4f},{fc_lon:.4f}"
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/geo+json"}, timeout=8)
        if r.status_code == 200:
            p = r.json().get("properties", {})
            office = p.get("gridId", "?")
            grid_x = p.get("gridX", "?")
            grid_y = p.get("gridY", "?")
            relative = p.get("relativeLocation", {}).get("properties", {})
            city_name = relative.get("city", "?")
            state = relative.get("state", "?")
            for src in LATLON_SOURCES:
                print(f"{city:<14} {src:<20} "
                      f"WEATHER_CITIES = ({fc_lat}, {fc_lon}):<35"
                      f" {office} {grid_x},{grid_y} → {city_name}, {state}")
                break  # all lat/lon sources use the same coords
        else:
            print(f"{city:<14} (lat/lon sources)  HTTP {r.status_code}")
    except Exception as e:
        print(f"{city:<14} (lat/lon sources)  err: {e}")

    print()


# ===== Per-source bias at SETTLEMENT (last hours_out, last 14 days) =====
print()
print("=== Per-source bias at SETTLEMENT (last hours_out, n>=10 days) ===")
print(f"{'source':<14} {'city':>8} {'n':>5} {'mean_bias':>10} {'rmse':>7}  flag")

conn = init_db("kalshi_trades.db")

# Truth (last 21 days for safety margin)
truth = {}
for st_icao, dt, hi in conn.execute("""
    SELECT station, lst_date, MAX(daily_high_f)
    FROM weather_metar_hourly_backfill
    WHERE daily_high_f IS NOT NULL AND lst_date >= date('now','-21 days')
    GROUP BY station, lst_date
"""):
    truth[(st_icao, dt)] = float(hi)

SERIES_TO_STATION = {st.series: st.icao for st in STATIONS.values()}

# Latest hours_out per (settle date, source, ticker) — closest to settlement
rows = conn.execute("""
    SELECT source, series, ticker, forecast_high_f, hours_out, recorded_at
    FROM weather_forecast_snapshots
    WHERE recorded_at >= datetime('now','-14 days')
      AND hours_out IS NOT NULL
      AND hours_out >= 0 AND hours_out <= 3
      AND forecast_high_f IS NOT NULL
""").fetchall()

# Bucket by (source, station)
def lst_date_from_ticker(ticker):
    months = {"JAN":1, "FEB":2, "MAR":3, "APR":4, "MAY":5, "JUN":6,
              "JUL":7, "AUG":8, "SEP":9, "OCT":10, "NOV":11, "DEC":12}
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    yymmmdd = parts[1]
    if len(yymmmdd) != 7:
        return None
    yy, mmm, dd = yymmmdd[:2], yymmmdd[2:5], yymmmdd[5:7]
    if mmm not in months:
        return None
    return f"20{yy}-{months[mmm]:02d}-{dd}"

buckets = defaultdict(list)
for src, series, ticker, mu, ho, recorded in rows:
    station = SERIES_TO_STATION.get(series)
    if not station:
        continue
    lst_date = lst_date_from_ticker(ticker)
    if not lst_date or (station, lst_date) not in truth:
        continue
    buckets[(src, station)].append(float(mu) - truth[(station, lst_date)])

flagged = []
for (src, st_icao), biases in sorted(buckets.items()):
    n = len(biases)
    if n < 10:
        continue
    mb = sum(biases) / n
    rmse = math.sqrt(sum(b*b for b in biases) / n)
    flag = ""
    if abs(mb) > 2.0:
        flag = "⚠ |bias|>2°F at settlement"
        flagged.append((src, st_icao, mb, rmse, n))
    elif rmse > 4.0:
        flag = "⚠ rmse>4°F at settlement"
    print(f"  {src:<14} {st_icao:>8} {n:>5} {mb:>+10.2f} {rmse:>7.2f}  {flag}")

print()
print("=== FLAGGED (|bias| > 2°F at settlement, n >= 10) ===")
if flagged:
    for src, st, mb, rmse, n in sorted(flagged, key=lambda x: -abs(x[2])):
        print(f"  {src:<14} {st:>8} bias={mb:+.2f}°F rmse={rmse:.2f}°F n={n}")
else:
    print("  none — all sources converge to within 2°F of truth at settlement")
