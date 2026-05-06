"""Full backtest v2: re-run with per-city HRRR σ priors carve-out.

Same logic as full_post_fix_backtest.py but uses the per-city σ prior
table from bot.signals.sources.hrrr to test whether the DEN regression
is fixed.
"""
import math
from collections import defaultdict
from bot.db import init_db
from bot.signals.weather_sources import EXCLUDED_SOURCES_BY_CITY
from bot.signals.sources.hrrr import _HRRR_SIGMA_PRIOR_BY_CITY, _HRRR_SIGMA_PRIOR_DEFAULT


SERIES_TO_STATION = {
    "KXHIGHMIA": "KMIA", "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW",
    "KXHIGHLAX": "KLAX", "KXHIGHAUS": "KAUS", "KXHIGHDEN": "KDEN",
}
SERIES_TO_CITY = {
    "KXHIGHMIA": "miami", "KXHIGHNY": "nyc", "KXHIGHCHI": "chicago",
    "KXHIGHLAX": "los_angeles", "KXHIGHAUS": "austin", "KXHIGHDEN": "denver",
}
PER_FAMILY_FACTOR = {
    "KXHIGHAUS": 4.0, "KXHIGHCHI": 4.0, "KXHIGHDEN": 1.0,
    "KXHIGHLAX": 4.0, "KXHIGHMIA": 3.0, "KXHIGHNY": 3.0,
}


def normal_cdf(x, mu, sigma):
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / sigma / math.sqrt(2.0)))


def decay_factor(base, tte_h):
    if tte_h is None or tte_h >= 8.0:
        return base
    if tte_h <= 2.0:
        return 1.0
    return 1.0 + (base - 1.0) * (tte_h - 2.0) / 6.0


def precision_combine(gauss):
    sw = sm = 0.0
    for mu, sigma in gauss:
        if sigma <= 0:
            continue
        w = 1.0 / (sigma * sigma)
        sw += w
        sm += mu * w
    if sw == 0:
        return None
    return sm / sw, math.sqrt(1.0 / sw)


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


def parse_bracket(ticker):
    last = ticker.split("-")[-1]
    if last.startswith("B"):
        try:
            c = float(last[1:])
            return ("bracket", c - 0.5, c + 0.5)
        except ValueError:
            return None
    elif last.startswith("T"):
        try:
            return ("threshold", float(last[1:]), None)
        except ValueError:
            return None
    return None


conn = init_db("kalshi_trades.db")

truth = {}
for st, dt, hi in conn.execute("""
    SELECT station, lst_date, MAX(daily_high_f)
    FROM weather_metar_hourly_backfill
    WHERE daily_high_f IS NOT NULL AND lst_date >= date('now','-21 days')
    GROUP BY station, lst_date
"""):
    truth[(st, dt)] = float(hi)

rows = conn.execute("""
    SELECT recorded_at, ticker, series, source, forecast_high_f, sigma_f, hours_out
    FROM weather_forecast_snapshots
    WHERE recorded_at >= datetime('now','-14 days')
      AND hours_out BETWEEN 0 AND 18
      AND forecast_high_f IS NOT NULL
      AND sigma_f IS NOT NULL
""").fetchall()

groups = defaultdict(dict)
for ts, ticker, series, src, mu, sigma, ho in rows:
    groups[(ts, ticker, series)][src] = (float(mu), float(sigma), ho)

fam_old_brier = defaultdict(list)
fam_new_brier = defaultdict(list)

for (ts, ticker, series), srcs in groups.items():
    if "combined_v2" not in srcs:
        continue
    station = SERIES_TO_STATION.get(series)
    city = SERIES_TO_CITY.get(series)
    if not station or not city:
        continue
    lst_date = lst_date_from_ticker(ticker)
    if not lst_date or (station, lst_date) not in truth:
        continue
    actual_high = truth[(station, lst_date)]
    parsed = parse_bracket(ticker)
    if parsed is None:
        continue
    kind = parsed[0]

    old_mu, old_sigma, ho = srcs["combined_v2"]
    if kind == "bracket":
        lo, hi = parsed[1], parsed[2]
        old_p = max(0.02, min(0.98, normal_cdf(hi, old_mu, old_sigma) - normal_cdf(lo, old_mu, old_sigma)))
        outcome = 1 if (lo <= actual_high < hi) else 0
    else:
        thresh = parsed[1]
        old_p = max(0.02, min(0.98, 1.0 - normal_cdf(thresh, old_mu, old_sigma)))
        outcome = 1 if actual_high > thresh else 0

    excluded = EXCLUDED_SOURCES_BY_CITY.get(city, frozenset())
    inputs = []
    for src_name, (mu, sigma, _ho) in srcs.items():
        if src_name == "combined_v2":
            continue
        if src_name == "afd":
            continue
        if src_name in excluded:
            continue
        # Per-city HRRR σ floor
        if src_name == "hrrr":
            city_floor = _HRRR_SIGMA_PRIOR_BY_CITY.get(city, _HRRR_SIGMA_PRIOR_DEFAULT)
            if sigma < city_floor:
                sigma = city_floor
        inputs.append((mu, sigma))

    combined = precision_combine(inputs)
    if combined is None:
        continue
    new_mu, new_sigma_pre = combined

    base = PER_FAMILY_FACTOR.get(series, 1.0)
    factor = decay_factor(base, ho)
    new_sigma = new_sigma_pre * factor

    if kind == "bracket":
        new_p = max(0.02, min(0.98, normal_cdf(hi, new_mu, new_sigma) - normal_cdf(lo, new_mu, new_sigma)))
    else:
        new_p = max(0.02, min(0.98, 1.0 - normal_cdf(thresh, new_mu, new_sigma)))

    fam_old_brier[series].append((old_p - outcome) ** 2)
    fam_new_brier[series].append((new_p - outcome) ** 2)


print(f"{'family':<11} {'n':>6} {'OLD Brier':>10} {'NEW Brier':>10} {'delta':>9} {'%':>7}")
total_old, total_new, total_n = 0.0, 0.0, 0
for series in sorted(fam_old_brier):
    old_list = fam_old_brier[series]
    new_list = fam_new_brier[series]
    n = len(old_list)
    if n == 0:
        continue
    old_b = sum(old_list) / n
    new_b = sum(new_list) / n
    delta = new_b - old_b
    pct = -100 * delta / old_b if old_b else 0
    total_old += sum(old_list)
    total_new += sum(new_list)
    total_n += n
    print(f"  {series:<10} {n:>6} {old_b:>10.4f} {new_b:>10.4f} "
          f"{delta:>+9.4f} {pct:>+6.1f}%")

if total_n:
    pooled_old = total_old / total_n
    pooled_new = total_new / total_n
    delta = pooled_new - pooled_old
    pct = -100 * delta / pooled_old if pooled_old else 0
    print()
    print(f"  {'POOLED':<10} {total_n:>6} {pooled_old:>10.4f} {pooled_new:>10.4f} "
          f"{delta:>+9.4f} {pct:>+6.1f}%")
