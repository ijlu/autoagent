"""NWS 5-min temperature → daily-high forecast via diurnal fit.

Reuses METAR's existing per-(station, lst_hour) diurnal regression
(``weather_metar_diurnal_<station>`` in kv_cache, format documented in
``bot.signals.sources.metar_observations._get_diurnal_fit``) but feeds
it the latest 5-min NWS observation rather than METAR's hourly reading.

The fit is ``daily_high = α + β · current_temp_at_hour_h``, with σ
from the fit RMSE. Fitted on 90+ days of historical METAR + CF6 data
by ``tools.backfill_weather_effective_n.fit_metar_diurnal``.

Why this is interesting on top of METAR's own application of the same
fit: NWS 5-min cadence updates ~12× faster than hourly METAR. When a
front passes through, the temperature drops within minutes; METAR's
hourly reading lags up to 60 minutes. Feeding the fresher 5-min
observation to the same fit produces a forecast that responds to
intra-hour temperature changes.

This is a NEW source — it does not replace ``nws_5min`` (running max).
Both contribute to the obs group with their own σ; the precision-
weighted combine handles the overlap automatically (correlation
discount via _OBS_GROUP).

Reuses the gate from ``bot.signals.sources.nws_5min``: returns ``None``
before ``_MIN_LST_HOUR_TO_FIRE`` because the diurnal fit is fit on
post-noon hours and produces nonsense at night.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.daemon.stations import (
    lst_offset_for_station, station_for_ticker,
)
from bot.signals.sources.metar_observations import _get_diurnal_fit
from bot.signals.sources.nws_5min import (
    PRIMARY_5MIN_STATION_BY_CITY, _MAX_OBS_AGE_S,
    _MIN_LST_HOUR_TO_FIRE, fetch_recent_observations,
)
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


_SOURCE_NAME = "nws_5min_diurnal"


def _latest_5min_temp(icao: str) -> Optional[tuple[float, datetime]]:
    """Return ``(temp_f, obs_time_utc)`` for the most recent NWS 5-min
    reading. Distinct from ``nws_5min.get_recent_max_temp_f`` (which
    returns the MAX over the last hour) — for the diurnal fit we want
    the freshest single reading, not a rolling max, because the fit's
    ``β · current_temp`` term wants the latest temperature.
    """
    obs = fetch_recent_observations(icao)
    if not obs:
        return None
    # fetch_recent_observations returns features ordered newest-first
    latest = obs[0]
    return (latest["temp_f"], latest["obs_time_utc"])


def get_nws_5min_diurnal_gaussian(
    ticker: str, market_data: dict,
) -> Optional[GaussianForecast]:
    """Predict today's daily high from the diurnal fit applied to the
    latest 5-min NWS observation.

    Returns ``None`` when:
      - Ticker isn't a weather market for one of our stations
      - LST hour < _MIN_LST_HOUR_TO_FIRE (gate inherited from nws_5min)
      - No fresh NWS observation available (>15min stale or absent)
      - No diurnal fit persisted for this (station, lst_hour) — the
        fitter only writes cells with ≥10 historical days of data.
        Cold-start cells fall back to None; the obs group still has
        METAR + nws_5min running-max.
    """
    ws = station_for_ticker((ticker or "").upper())
    if ws is None:
        return None

    city = ws.city.lower().replace(" ", "_")
    # NYC is intentionally absent from PRIMARY_5MIN_STATION_BY_CITY
    # because KLGA proxy ran 3-5°F warm vs KNYC. See the source map's
    # header in nws_5min.py for the postmortem.
    poll_station = PRIMARY_5MIN_STATION_BY_CITY.get(city)
    if poll_station is None:
        return None
    lst_offset = lst_offset_for_station(ws.icao)

    # LST gate — same threshold as nws_5min source. Pre-noon the
    # diurnal fit's input is "morning temperature" which has poor
    # explanatory power for the eventual peak. The fit RMSE rises
    # sharply at low LST hours; better to suppress.
    lst_now = datetime.now(timezone.utc) + timedelta(hours=lst_offset)
    if lst_now.hour < _MIN_LST_HOUR_TO_FIRE:
        return None

    # METAR's diurnal fits are keyed on the GROUND-TRUTH station
    # (KNYC / KMDW / KMIA / KAUS / KDEN / KLAX), NOT the 5-min poll
    # station (which differs only for NY where we use KLGA as proxy).
    # Read fits using the ground-truth station so we get the right
    # α/β regression for THIS market's actual settlement station.
    fit = _get_diurnal_fit(ws.icao, lst_now.hour)
    if fit is None:
        return None

    pair = _latest_5min_temp(poll_station)
    if pair is None:
        return None
    temp_f, obs_time = pair

    obs_age_s = (datetime.now(timezone.utc) - obs_time).total_seconds()
    if obs_age_s > _MAX_OBS_AGE_S:
        return None

    alpha, beta, rmse = fit
    predicted_high = alpha + beta * temp_f

    # Sanity guard: a diurnal fit can produce wild values when the
    # input temp is far from the historical training range. Cap the
    # prediction to a 50°F window around the input — reasonable peak
    # warming above current temp is +20°F max in CONUS spring.
    max_warming_f = 30.0
    predicted_high = max(temp_f - 5.0, min(temp_f + max_warming_f, predicted_high))

    if not math.isfinite(predicted_high):
        return None

    horizon_hours = hours_until_settlement_end(lst_offset, day_idx=0)

    return GaussianForecast(
        mean_f=float(predicted_high),
        sigma_f=float(rmse),
        horizon_hours=horizon_hours,
        source_name=_SOURCE_NAME,
        source_tag=f"{_SOURCE_NAME}:{poll_station}_lst{lst_now.hour:02d}",
        issued_at=obs_time.timestamp(),
    )
