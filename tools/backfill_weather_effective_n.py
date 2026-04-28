"""A2.5a backfill harness — Open-Meteo + METAR historical.

Fetches historical forecasts from Open-Meteo's free historical-forecast-api
and observed daily-high temperatures from IEM's ASOS archive, replays them
into ``weather_gaussian_snapshots_backfill``, and fits per-source bias /
RMSE / sigma calibration for A3 skill-curve seeding and A5 MOS bias
correction.

A2.5a limitations (addressed by A2.5b+):

  * Only two sources — Open-Meteo (1 model) + METAR (1 obs). Each
    correlation group has n=1 member, so **within-group effective-N cannot
    be fit from this data alone**. HRRR + NBM (A2.5b) and AFD (A2.5c) land
    next to close that gap.
  * Lead-time resolution: Open-Meteo's historical-forecast-api returns
    one daily aggregate per past date. We tag those rows
    ``lead_hours=12`` as a nominal "morning-of" reference. Multi-lead
    stratification for A3 comes with HRRR/NBM which publish 4×/day init
    cycles.
  * No grib2 parsing. All sources backfilled here have JSON or CSV
    archive endpoints.

Usage::

    python3 tools/backfill_weather_effective_n.py \\
        --start 2026-01-01 --end 2026-04-20 \\
        --cities nyc,chicago,miami

The tool never writes anything production-critical (it writes only to
the backfill table). Safe to re-run — UNIQUE constraint on (source,
city, settlement_date, lead_hours) is ``INSERT OR REPLACE``'d.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests

from bot.daemon.stations import STATION_BY_CITY, WeatherStation
from bot.db import db_write, init_db


# ── Reference points for A2.5a ────────────────────────────────────────────

# Nominal lead time stamped on Open-Meteo + METAR backfill rows. Open-Meteo's
# historical-forecast-api returns one forecast per past date; we don't know
# the exact hour the forecast was issued, so we use 12h as a "morning-of"
# reference. A2.5b (HRRR/NBM via explicit init cycles) will stamp real
# lead_hours values.
_DEFAULT_LEAD_HOURS: int = 12

# Sigma schedules — must match the live signal modules' schedules exactly.
# Duplicated here so drift between live and backfill surfaces as a test
# failure in the sigma_schedule_drift_guard tests.
def open_meteo_sigma_for_day(day_idx: int) -> float:
    return 2.0 + day_idx * 0.6


def hrrr_sigma_for_day(day_idx: int) -> float:
    return 1.2 + day_idx * 0.5


def nbm_sigma_for_day(day_idx: int) -> float:
    return 1.8 + day_idx * 0.5


# METAR "sigma" when treated as a forecast-of-observed — for backfill
# purposes, a METAR daily-high reading at end of day has effectively zero
# forecast error (it IS the observation). We stamp a small epsilon so the
# row has a valid positive sigma the combiner would accept, but it's not
# used for fitting.
_METAR_OBS_SIGMA_EPSILON: float = 0.1


# Open-Meteo model name → (backfill source key, sigma function).
# Live sources (hrrr.py, ndfd_nbm.py, weather.py) already fetch via
# OM's forecast API with these exact ``models=`` params, so the backfill
# stays apples-to-apples with production.
# Source key here MUST be the canonical live name from
# bot.signals.weather_sources — drift-guard test pins this. Open-Meteo is
# called "weather" in live (see bot/signals/sources/weather.py) so the
# backfill writes that key, not "open_meteo".
_OM_MODELS: dict[Optional[str], tuple[str, "callable"]] = {
    None:             ("weather", open_meteo_sigma_for_day),  # default best_match (Open-Meteo)
    "gfs_hrrr":       ("hrrr",    hrrr_sigma_for_day),
    "gfs_seamless":   ("nbm",     nbm_sigma_for_day),
}


# ── HTTP fetchers ─────────────────────────────────────────────────────────

_OPEN_METEO_HIST_FORECAST_URL = (
    "https://historical-forecast-api.open-meteo.com/v1/forecast"
)

# IEM's ASOS archive. Returns CSV with 5-minute cadence METAR obs.
_IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

_USER_AGENT = "KalshiTradingBot-Backfill/1.0 (contact: joshlu@a16z.com)"


@dataclass(frozen=True)
class DailyRecord:
    """One (city, date) row with both forecast and observed daily high."""

    city: str
    settlement_date: str       # YYYY-MM-DD in the station's LST
    open_meteo_high_f: Optional[float]
    metar_high_f: Optional[float]


def _c_to_f(temp_c: float) -> float:
    return temp_c * 9.0 / 5.0 + 32.0


def fetch_om_model_daily_highs(
    station: WeatherStation, start_date: str, end_date: str,
    *, model: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> dict[str, float]:
    """Return {settlement_date_YYYY-MM-DD: forecast_high_f} from
    Open-Meteo's historical-forecast-api for one model.

    ``model=None`` uses OM's default ``best_match`` (the live
    ``get_weather_gaussian`` path). Pass ``"gfs_hrrr"`` or ``"gfs_seamless"``
    to match the live HRRR / NBM paths. One model per call — keeps the
    response shape unambiguous (single ``temperature_2m_max`` column
    regardless of model).

    Dates returned are in the station's local timezone (LST year-round —
    no DST drift).
    """
    sess = session or requests
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max",
        # Explicit LST offset so the returned dates align with Kalshi
        # settlement boundaries. Kalshi uses LST year-round; passing
        # "auto" would give local time with DST which is wrong for us.
        "timezone": f"GMT{station.lst_offset:+d}",
        "temperature_unit": "fahrenheit",
    }
    if model is not None:
        params["models"] = model

    r = sess.get(
        _OPEN_METEO_HIST_FORECAST_URL,
        params=params,
        timeout=30,
        headers={"User-Agent": _USER_AGENT},
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"open-meteo hist-forecast HTTP {r.status_code} "
            f"(model={model}): {r.text[:200]}"
        )
    payload = r.json()
    daily = payload.get("daily", {})
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    out: dict[str, float] = {}
    for d, h in zip(dates, highs):
        if h is None:
            continue
        out[d] = float(h)
    return out


# A2.5a back-compat alias. Kept so older scripts that imported the
# specific name still work; prefer ``fetch_om_model_daily_highs`` for
# new code.
def fetch_open_meteo_daily_highs(
    station: WeatherStation, start_date: str, end_date: str,
    *, session: Optional[requests.Session] = None,
) -> dict[str, float]:
    return fetch_om_model_daily_highs(
        station, start_date, end_date, model=None, session=session,
    )


def fetch_metar_daily_highs(
    station: WeatherStation, start_date: str, end_date: str,
    *, session: Optional[requests.Session] = None,
) -> dict[str, float]:
    """Return {settlement_date_YYYY-MM-DD: observed_high_f} from IEM ASOS archive.

    Daily high = max tmpf over the LST calendar day of the station. The IEM
    API returns UTC timestamps; we bucket by LST-local date (not UTC date).
    """
    sess = session or requests
    # Parse start/end to request a slightly widened UTC window so we
    # capture LST-day boundaries cleanly.
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    params = {
        "station": station.icao,
        "data": "tmpf",
        "year1": start_dt.year, "month1": start_dt.month, "day1": start_dt.day,
        # +1 day to capture LST end-of-day obs that spill into next UTC date
        "year2": end_dt.year, "month2": end_dt.month,
        "day2": end_dt.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "missing": "empty",
        "latlon": "no",
    }
    r = sess.get(
        _IEM_ASOS_URL,
        params=params,
        timeout=60,
        headers={"User-Agent": _USER_AGENT},
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"IEM asos HTTP {r.status_code}: {r.text[:200]}"
        )

    reader = csv.DictReader(io.StringIO(r.text))
    lst_tz = timezone(timedelta(hours=station.lst_offset))

    per_day: dict[str, float] = {}
    for row in reader:
        ts_raw = (row.get("valid") or "").strip()
        temp_raw = (row.get("tmpf") or "").strip()
        if not ts_raw or not temp_raw:
            continue
        try:
            temp_f = float(temp_raw)
        except ValueError:
            continue
        # METAR obs often include `-99.99` or values outside physical
        # plausible for daily high. Clamp bounds to avoid contaminating
        # the fit.
        if temp_f < -60.0 or temp_f > 140.0:
            continue
        try:
            dt_utc = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        dt_lst = dt_utc.astimezone(lst_tz)
        key = dt_lst.date().isoformat()
        if key not in per_day or temp_f > per_day[key]:
            per_day[key] = temp_f
    return per_day


# ── Replay + write ────────────────────────────────────────────────────────

def _sources_and_rows(
    city: str, date: str, *,
    forecasts_by_source: dict[str, float],
    metar: Optional[float],
    lead_hours: int,
) -> list[tuple]:
    """Build backfill row tuples for one (city, date).

    ``forecasts_by_source`` maps canonical source key (``"weather"``,
    ``"hrrr"``, ``"nbm"``, …) to that source's predicted daily-high °F.
    All rows carry ``observed_high_f = metar`` (or None if no METAR
    ground truth for that date) so the fitter doesn't need a join.

    Returns list of tuples matching the INSERT column order:
        (created_at, source, city, settlement_date, lead_hours,
         forecast_mean_f, forecast_sigma_f, observed_high_f)
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    obs = float(metar) if metar is not None else None
    rows: list[tuple] = []

    # Sigma function per source key — must match live signal sigma schedules.
    sigma_fn = {
        "weather": open_meteo_sigma_for_day,
        "hrrr": hrrr_sigma_for_day,
        "nbm": nbm_sigma_for_day,
    }

    for source, fcst in forecasts_by_source.items():
        if fcst is None or source not in sigma_fn:
            continue
        # lead_hours=12 ≈ "morning-of" forecast → treat as day_idx=0.
        sigma = sigma_fn[source](day_idx=0)
        rows.append((
            now_iso, source, city, date, lead_hours,
            float(fcst), sigma, obs,
        ))

    if metar is not None:
        rows.append((
            now_iso, "metar", city, date, 0,  # obs lead = 0
            float(metar),
            _METAR_OBS_SIGMA_EPSILON,
            float(metar),
        ))
    return rows


def replay_and_write(
    conn: sqlite3.Connection,
    station: WeatherStation,
    start_date: str,
    end_date: str,
    *,
    lead_hours: int = _DEFAULT_LEAD_HOURS,
    session: Optional[requests.Session] = None,
    models: Optional[list[Optional[str]]] = None,
) -> int:
    """Fetch OM models + METAR for the station's date range, write rows.
    Returns the number of rows written.

    ``models`` is a list of Open-Meteo ``models=`` parameter values (or
    ``None`` for the default best_match). Defaults to the full set of
    live model sources: ``[None, "gfs_hrrr", "gfs_seamless"]`` mirroring
    ``bot.signals.sources.{weather, hrrr, ndfd_nbm}`` respectively.
    """
    if models is None:
        models = [None, "gfs_hrrr", "gfs_seamless"]

    # Fetch each model separately. Each call is O(30s) max and OM caches
    # aggressively — total wall time stays tractable for ~90-day windows.
    per_model: dict[str, dict[str, float]] = {}
    for m in models:
        source_key, _ = _OM_MODELS[m]
        per_model[source_key] = fetch_om_model_daily_highs(
            station, start_date, end_date, model=m, session=session,
        )

    metar = fetch_metar_daily_highs(
        station, start_date, end_date, session=session,
    )

    # Union over all source date keys — a date shows up if ANY source has it.
    all_dates: set[str] = set(metar)
    for dates in per_model.values():
        all_dates.update(dates)

    all_rows: list[tuple] = []
    for d in sorted(all_dates):
        forecasts_here = {k: v.get(d) for k, v in per_model.items()}
        all_rows.extend(_sources_and_rows(
            station.city, d,
            forecasts_by_source=forecasts_here,
            metar=metar.get(d),
            lead_hours=lead_hours,
        ))

    if not all_rows:
        return 0

    def _write(c):
        c.executemany(
            """INSERT OR REPLACE INTO weather_gaussian_snapshots_backfill
               (created_at, source, city, settlement_date, lead_hours,
                forecast_mean_f, forecast_sigma_f, observed_high_f)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            all_rows,
        )
    db_write(_write, conn=conn)
    return len(all_rows)


# ── Fit + report ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceFit:
    source: str
    n: int
    mean_bias_f: float       # mean(forecast - observed) — signed
    rmse_f: float            # sqrt(mean((forecast - observed)^2))
    reported_sigma_f: float  # what the source was stamping per row
    sigma_ratio: float       # rmse_f / reported_sigma_f — >1 under-confident, <1 over-confident


def fit_per_source(conn: sqlite3.Connection) -> list[SourceFit]:
    """Compute bias/RMSE/sigma-calibration per (source, city, lead_hours).

    We exclude rows where observed_high_f is NULL (no METAR ground truth
    for that date — OM-only rows). METAR rows are skipped for the fit
    (forecast == observed by construction); they're stored for
    downstream joins.
    """
    rows = conn.execute(
        """SELECT source, forecast_mean_f, forecast_sigma_f, observed_high_f
           FROM weather_gaussian_snapshots_backfill
           WHERE observed_high_f IS NOT NULL
             AND source != 'metar'"""
    ).fetchall()
    per_source: dict[str, list[tuple[float, float, float]]] = {}
    for source, fcst, sigma, obs in rows:
        if fcst is None or sigma is None:
            continue
        per_source.setdefault(source, []).append((float(fcst), float(sigma), float(obs)))

    out: list[SourceFit] = []
    for source, vals in sorted(per_source.items()):
        n = len(vals)
        if n == 0:
            continue
        errs = [f - o for f, _, o in vals]
        sigmas = [s for _, s, _ in vals]
        mean_bias = sum(errs) / n
        rmse = math.sqrt(sum(e * e for e in errs) / n)
        # Use the mode-ish reported sigma (all rows from the same source
        # at the same lead typically have one σ).
        reported_sigma = sum(sigmas) / n
        sigma_ratio = rmse / reported_sigma if reported_sigma > 0 else float("nan")
        out.append(SourceFit(
            source=source, n=n,
            mean_bias_f=mean_bias, rmse_f=rmse,
            reported_sigma_f=reported_sigma,
            sigma_ratio=sigma_ratio,
        ))
    return out


# ── Effective-N per correlation group (A2.5c) ─────────────────────────────
#
# The MVP combine in ``weather_ensemble_v2`` uses weight = 1/n_group
# (equivalent to assuming ρ=1.0 within the group — the safest assumption
# when we had no data). With HRRR + NBM + Open-Meteo backfilled, we can
# measure the realized pairwise correlation among the MODEL group's
# forecast errors and fit a proper effective-N = n / (1 + (n−1)ρ).
#
# OBS group has only METAR in the backfill right now; n_eff fitting needs
# ≥2 sources per group so OBS falls through to the MVP discount until
# MADIS backfill lands.

_MODEL_GROUP_SOURCES: tuple[str, ...] = ("weather", "hrrr", "nbm")


@dataclass(frozen=True)
class GroupFit:
    group: str
    sources: tuple[str, ...]
    n_pairs: int                  # (city, date, lead) rows where ALL members fired
    rho: float                    # mean pairwise Pearson correlation of errors
    n_sources_present: int
    n_eff: float                  # n / (1 + (n-1) * rho)


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation. Returns None if undefined (too few samples or
    zero variance in either series)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx2 = sum((x - mx) ** 2 for x in xs)
    dy2 = sum((y - my) ** 2 for y in ys)
    if dx2 <= 0 or dy2 <= 0:
        return None
    return num / math.sqrt(dx2 * dy2)


def fit_group_correlation(
    conn: sqlite3.Connection,
    group_sources: Iterable[str] = _MODEL_GROUP_SOURCES,
    *, group_name: str = "model",
) -> Optional[GroupFit]:
    """Fit mean pairwise forecast-error correlation among members of a
    correlation group and return the effective-N to use in the combine.

    Errors ``e_i = forecast_i - observed_i``. Only (city, date, lead) rows
    where every listed source fired and ``observed_high_f`` is present
    contribute. Returns ``None`` if fewer than 2 sources / <2 joint
    samples — caller should fall back to the MVP 1/n discount.
    """
    sources = tuple(group_sources)
    if len(sources) < 2:
        return None
    placeholders = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"""SELECT source, city, settlement_date, lead_hours,
                   forecast_mean_f, observed_high_f
              FROM weather_gaussian_snapshots_backfill
             WHERE source IN ({placeholders})
               AND observed_high_f IS NOT NULL
               AND forecast_mean_f IS NOT NULL""",
        sources,
    ).fetchall()

    pivot: dict[tuple, dict[str, float]] = {}
    for src, city, date, lead, fcst, obs in rows:
        key = (city, date, lead)
        pivot.setdefault(key, {})[src] = float(fcst) - float(obs)

    joint = {k: v for k, v in pivot.items() if all(s in v for s in sources)}
    n_pairs = len(joint)
    if n_pairs < 2:
        return None

    corrs: list[float] = []
    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            xs = [v[sources[i]] for v in joint.values()]
            ys = [v[sources[j]] for v in joint.values()]
            r = _pearson(xs, ys)
            if r is not None:
                corrs.append(r)
    if not corrs:
        return None

    rho = sum(corrs) / len(corrs)
    n = len(sources)
    # Guard against pathological anti-correlation producing a divide-by-zero.
    denom = 1.0 + (n - 1) * rho
    if denom <= 0:
        # Anti-correlated to the point where the formula blows up; cap at
        # "completely independent" (n_eff = n). This is mostly defensive —
        # real-world models never anti-correlate this strongly.
        n_eff = float(n)
    else:
        n_eff = n / denom
    return GroupFit(
        group=group_name, sources=sources, n_pairs=n_pairs,
        rho=rho, n_sources_present=n, n_eff=n_eff,
    )


# kv_cache key prefix must match the reader in
# ``bot.signals.weather_ensemble_v2._GROUP_RHO_KEY_PREFIX``.
_GROUP_RHO_KEY_PREFIX: str = "weather_group_corr_"
_GROUP_RHO_TTL_SEC: int = 90 * 86400


# ── A3: horizon-stratified skill curves ───────────────────────────────────
#
# Each Gaussian-capable source carries a self-reported ``sigma_f`` derived
# from a hardcoded day-offset schedule (e.g. OM: 2.0 + 0.6·day_idx). Those
# priors are rough. With a ≥10-sample backfill we replace them with the
# realized RMSE at that lead time, bucketed because sample counts per exact
# hour are too thin but plenty per 0-6h, 6-24h, etc.

# Bucket edges: [0, 6, 24, 48, 168] hours. Chosen to isolate
#   nowcast (0-6h) — METAR-quality real-time observation window
#   6-24h — the prime Kalshi weather-MM zone (post morning, settle PM)
#   24-48h — next-day forecast
#   48-168h — 2-7 day outlook
_SKILL_BUCKET_EDGES: tuple[int, ...] = (0, 6, 24, 48, 168)

# Minimum samples in a bucket before we trust the fit enough to persist.
# Below this the fit is printed in the report but flagged as "thin" and
# skipped on persist to avoid whiplashing live σ off sparse data.
_SKILL_MIN_SAMPLES: int = 10

# Must match the reader in ``weather_ensemble_v2._SKILL_KEY_PREFIX``.
_SKILL_KEY_PREFIX: str = "weather_skill_"
_SKILL_TTL_SEC: int = 30 * 86400


def _bucket_for(horizon_hours: float) -> Optional[str]:
    """Return the bucket label ("0_6", "6_24", …) for a horizon in hours.

    Half-open intervals: ``[lo, hi)``. Horizons ≥ the top edge (168h) or
    < 0 return None — we don't try to fit outside the Kalshi
    weather-market horizon.
    """
    if horizon_hours is None or horizon_hours < 0:
        return None
    for lo, hi in zip(_SKILL_BUCKET_EDGES[:-1], _SKILL_BUCKET_EDGES[1:]):
        if lo <= horizon_hours < hi:
            return f"{lo}_{hi}"
    return None


@dataclass(frozen=True)
class SkillFit:
    source: str
    bucket: str               # e.g. "6_24"
    n: int
    bias_f: float             # mean(forecast - observed)
    rmse_f: float             # sqrt(mean((forecast - observed)^2))
    prior_sigma_f: float      # average self-reported σ across the rows (for drift diagnostics)
    city: Optional[str] = None  # None = pooled across all cities


def fit_skill_curves(
    conn: sqlite3.Connection, *, per_city: bool = False,
    winsorize_pct: float = 0.0,
) -> list[SkillFit]:
    """Fit per-(source, horizon bucket) bias + RMSE from the backfill table.

    RMSE is returned (not std); treating σ=RMSE is the right conservative
    call before A5's MOS bias correction zeroes the ``bias_f`` column.
    After A5, σ will converge on std(e) = sqrt(RMSE² − bias²).

    METAR rows are skipped (obs of ground truth; forecast == observed by
    construction — σ there is the epsilon stamped during replay, not a
    real forecast error).

    When ``per_city=True`` also fits per-(source, city, bucket) cells in
    addition to the pooled fits. Diagnostics show the actual error std
    varies 0.9–2.0°F across cities for the same source — a single pooled
    σ is wrong for everyone. Per-city σ feeds back through
    ``weather_ensemble_v2._get_learned_sigma`` with pooled fallback when
    a city cell is too thin.
    """
    # lead_hours in the backfill is nominal (A2.5 defaults to 12h). We
    # bucket on it so the fit already lines up with the buckets the live
    # combiner queries.
    rows = conn.execute(
        """SELECT source, city, lead_hours, forecast_mean_f, forecast_sigma_f,
                  observed_high_f
             FROM weather_gaussian_snapshots_backfill
            WHERE observed_high_f IS NOT NULL
              AND forecast_mean_f IS NOT NULL
              AND source != 'metar'"""
    ).fetchall()

    pooled: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    per_city_buckets: dict[
        tuple[str, str, str], list[tuple[float, float, float]]
    ] = {}
    for src, city, lead, fcst, sigma, obs in rows:
        if lead is None:
            continue
        bucket = _bucket_for(float(lead))
        if bucket is None:
            continue
        sample = (float(fcst), float(sigma or 0.0), float(obs))
        pooled.setdefault((src, bucket), []).append(sample)
        if per_city and city:
            per_city_buckets.setdefault((src, str(city), bucket), []).append(sample)

    def _fit_one(samples, src, bucket, city=None) -> SkillFit:
        n = len(samples)
        errs = [f - o for f, _, o in samples]
        sigs = [s for _, s, _ in samples]
        # Winsorize: clip residuals outside [p, 1-p] back to those quantiles
        # so a single 5°F miss in a 90-day sample doesn't double the σ
        # estimate. With ``winsorize_pct=0`` this is a no-op (current behavior).
        if winsorize_pct > 0 and n >= 20:
            sorted_errs = sorted(errs)
            lo_idx = int(n * winsorize_pct)
            hi_idx = n - 1 - lo_idx
            lo_val = sorted_errs[lo_idx]
            hi_val = sorted_errs[hi_idx]
            errs = [max(lo_val, min(hi_val, e)) for e in errs]
        bias = sum(errs) / n
        rmse = math.sqrt(sum(e * e for e in errs) / n)
        prior = sum(sigs) / n if sigs else 0.0
        return SkillFit(
            source=src, bucket=bucket, n=n,
            bias_f=bias, rmse_f=rmse,
            prior_sigma_f=prior, city=city,
        )

    out: list[SkillFit] = []
    for (src, bucket), samples in sorted(pooled.items()):
        if samples:
            out.append(_fit_one(samples, src, bucket, city=None))
    for (src, city, bucket), samples in sorted(per_city_buckets.items()):
        if samples:
            out.append(_fit_one(samples, src, bucket, city=city))
    return out


def persist_skill_fit(
    conn: sqlite3.Connection, fit: SkillFit,
    *, ttl_seconds: int = _SKILL_TTL_SEC,
) -> str:
    """Write a fitted skill curve into kv_cache. Returns the kv key written.

    Pooled fits land at ``weather_skill_<source>_<bucket>``; per-city fits
    land at ``weather_skill_<source>_<city>_<bucket>``.
    ``weather_ensemble_v2._get_learned_sigma`` reads per-city first, falls
    back to pooled, falls back to the source's own prior.
    """
    from bot.db import kv_set

    if fit.city:
        # City names from the backfill table can include spaces ("los angeles");
        # normalize to underscore form for kv key safety + consumer alignment.
        city_key = fit.city.strip().lower().replace(" ", "_")
        key = f"{_SKILL_KEY_PREFIX}{fit.source}_{city_key}_{fit.bucket}"
    else:
        key = f"{_SKILL_KEY_PREFIX}{fit.source}_{fit.bucket}"
    payload = {
        "sigma": fit.rmse_f,
        "bias": fit.bias_f,
        "n": fit.n,
        "prior_sigma": fit.prior_sigma_f,
        "fit_at": datetime.now(timezone.utc).isoformat(),
    }
    if fit.city:
        payload["city"] = fit.city
    kv_set(conn, key, payload, ttl_seconds)
    return key


def report_skill_curves(fits: list[SkillFit]) -> str:
    if not fits:
        return "No skill fits — run backfill first."
    lines = [
        "Per-(source, horizon) skill curves",
        "-" * 78,
        f"{'source':<12} {'bucket':<10} {'n':>4} {'bias°F':>8} "
        f"{'RMSE°F':>8} {'prior°F':>8} {'note':<20}",
        "-" * 78,
    ]
    for f in fits:
        note = "" if f.n >= _SKILL_MIN_SAMPLES else f"thin (<{_SKILL_MIN_SAMPLES})"
        lines.append(
            f"{f.source:<12} {f.bucket:<10} {f.n:>4d} "
            f"{f.bias_f:>+8.2f} {f.rmse_f:>8.2f} "
            f"{f.prior_sigma_f:>8.2f} {note:<20}"
        )
    lines.extend([
        "-" * 78,
        "Legend:",
        "  bias°F   : mean(forecast − observed). A5 MOS correction will zero this.",
        "  RMSE°F   : realized forecast error per horizon. Persists as learned σ.",
        "  prior°F  : current hardcoded σ for that bucket — drift from RMSE = "
        "signal the hardcode is stale.",
        f"  Persist skips buckets with n < {_SKILL_MIN_SAMPLES} to avoid "
        "whiplashing live σ.",
    ])
    return "\n".join(lines)


# ── A5: per-(source, city) EWMA MOS bias ─────────────────────────────────
#
# After A3's skill-curve σ tuning, the strongest residual in live forecasts
# is a *systematic mean error* — HRRR runs warm in Miami, NWS cold-biases
# Denver, etc. Fit EWMA mean(forecast - observed) per (source, city) and
# shift the live Gaussian by -bias in the combiner.
#
# Granularity: 2-tuple (source, city). Earlier 4-tuple (source, city,
# season, bucket) starved every cell at 30 days of backfill depth. Pool
# until any (source, city) cell carries ≥ 200 EWMA-weight; re-stratify
# then if seasons/buckets show distinct biases.
#
# Decay: EWMA with half-life H_DAYS — observations N days old count
# 2^(-N/H) as much as today's. H=14 is the production default; H=∞
# degenerates to a flat mean (regression-tested).
#
# Reader lives in bot.signals.weather_ensemble_v2 — key prefix is pinned
# by drift-guard tests.

_MOS_BIAS_KEY_PREFIX: str = "weather_mos_bias_"
_MOS_BIAS_TTL_SEC: int = 45 * 86400
_MOS_BIAS_MIN_SAMPLES: int = 8
# Don't persist a bias whose |value| exceeds this — likely a corrupt cell
# rather than a genuine systematic bias. Matches the reader's clamp.
_MOS_BIAS_MAX_ABS_F: float = 5.0
# Half-life in days for EWMA weight decay. 14d means a 14-day-old reading
# counts half as much as today's; 28-day-old counts a quarter. Drift in a
# forecast model gets fully tracked in ~2 weeks.
_MOS_BIAS_EWMA_HALF_LIFE_DAYS: float = 14.0


def _city_key(raw: str) -> str:
    """Normalize city value ('Los Angeles') for kv key use. Matches
    weather_ensemble_v2._city_key exactly — drift-guard test pins both."""
    return raw.strip().lower().replace(" ", "_")


@dataclass(frozen=True)
class MOSBiasFit:
    source: str
    city: str            # as-stored in backfill table (e.g., "nyc", "los angeles")
    n: int               # raw row count contributing
    bias_f: float        # EWMA-weighted mean(forecast - observed)
    eff_n: float         # effective sample size: Σw² / Σw² normalized


def _ewma_weight(date_iso: str, ref_date_iso: str, half_life_days: float) -> float:
    """Weight for a row dated ``date_iso``, given EWMA reference
    ``ref_date_iso`` (typically wall-clock today) and half-life in days.

    Returns 1.0 when ``half_life_days`` is +∞ (so EWMA degenerates to a
    flat mean — used by the equivalence regression test). Returns 0.0 if
    the date is malformed; caller will simply skip.
    """
    if half_life_days == float("inf"):
        return 1.0
    try:
        d = datetime.strptime(date_iso[:10], "%Y-%m-%d")
        ref = datetime.strptime(ref_date_iso[:10], "%Y-%m-%d")
    except (ValueError, IndexError):
        return 0.0
    age_days = (ref - d).total_seconds() / 86400.0
    if age_days < 0:
        # A future-dated row shouldn't out-weigh "today". Cap weight at 1.0.
        age_days = 0.0
    return 2.0 ** (-age_days / half_life_days)


def fit_mos_bias(
    conn: sqlite3.Connection,
    *,
    half_life_days: float = _MOS_BIAS_EWMA_HALF_LIFE_DAYS,
    ref_date_iso: Optional[str] = None,
) -> list[MOSBiasFit]:
    """Fit per-(source, city) EWMA mean forecast-minus-observed from the
    Gaussian backfill table.

    ``half_life_days`` controls EWMA decay; pass ``float("inf")`` for a
    flat mean (regression test). ``ref_date_iso`` overrides the
    EWMA reference date — defaults to today (UTC). Useful for tests so
    deterministic fixtures don't drift with wall clock.

    Only rows with ``observed_high_f IS NOT NULL`` contribute. METAR rows
    are dropped (observed == forecast by construction for the backfill
    entry, so error is identically 0).
    """
    rows = conn.execute(
        """SELECT source, city, settlement_date,
                  forecast_mean_f, observed_high_f
             FROM weather_gaussian_snapshots_backfill
            WHERE observed_high_f IS NOT NULL
              AND forecast_mean_f IS NOT NULL
              AND source != 'metar'"""
    ).fetchall()

    if ref_date_iso is None:
        ref_date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # per_cell maps (source, city) → list of (weight, error)
    per_cell: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for src, city, date_iso, fcst, obs in rows:
        w = _ewma_weight(str(date_iso), ref_date_iso, half_life_days)
        if w <= 0:
            continue
        err = float(fcst) - float(obs)
        per_cell.setdefault((str(src), str(city)), []).append((w, err))

    out: list[MOSBiasFit] = []
    for (src, city), pairs in sorted(per_cell.items()):
        sum_w = sum(w for w, _ in pairs)
        if sum_w <= 0:
            continue
        sum_we = sum(w * e for w, e in pairs)
        sum_w2 = sum(w * w for w, _ in pairs)
        bias = sum_we / sum_w
        # Kish effective sample size: (Σw)² / Σw². With equal weights this
        # equals n; with skewed weights it shrinks toward the count of the
        # heaviest rows. Anchors the min-samples gate to information content
        # rather than raw row count when EWMA discounts old rows hard.
        eff_n = (sum_w * sum_w) / sum_w2 if sum_w2 > 0 else 0.0
        out.append(MOSBiasFit(
            source=src, city=city, n=len(pairs),
            bias_f=bias, eff_n=eff_n,
        ))
    return out


def persist_mos_bias(
    conn: sqlite3.Connection, fits: list[MOSBiasFit],
    *, ttl_seconds: int = _MOS_BIAS_TTL_SEC,
    min_samples: int = _MOS_BIAS_MIN_SAMPLES,
) -> list[str]:
    """Write fitted MOS biases to kv_cache.

    One kv row per (source, city) cell that clears the effective-sample
    and magnitude guards. Key shape: ``weather_mos_bias_{source}_{city}``.
    Returns the keys written.

    Effective sample size (``eff_n``) is checked against ``min_samples``
    rather than raw ``n`` — a cell with 30 rows but EWMA weight
    concentrated on 3 recent observations carries roughly 3 rows of
    information, and we shouldn't pretend otherwise.
    """
    from bot.db import kv_set

    keys: list[str] = []
    for f in fits:
        if f.eff_n < min_samples:
            continue
        if abs(f.bias_f) > _MOS_BIAS_MAX_ABS_F:
            continue
        city_key = _city_key(f.city)
        key = f"{_MOS_BIAS_KEY_PREFIX}{f.source}_{city_key}"
        payload = {
            "bias": f.bias_f,
            "n": f.n,
            "eff_n": f.eff_n,
            "fit_at": datetime.now(timezone.utc).isoformat(),
        }
        kv_set(conn, key, payload, ttl_seconds)
        keys.append(key)
    return keys


def report_mos_bias(fits: list[MOSBiasFit]) -> str:
    if not fits:
        return "No MOS bias fits — run backfill first."
    lines = [
        "Per-(source, city) EWMA MOS bias: weighted mean(forecast − observed)",
        "-" * 70,
        f"{'source':<12} {'city':<14} {'n':>4} {'eff_n':>7} "
        f"{'bias°F':>8} {'note':<14}",
        "-" * 70,
    ]
    for f in fits:
        note = ""
        if f.eff_n < _MOS_BIAS_MIN_SAMPLES:
            note = f"thin (<{_MOS_BIAS_MIN_SAMPLES})"
        elif abs(f.bias_f) > _MOS_BIAS_MAX_ABS_F:
            note = "|bias| > cap"
        lines.append(
            f"{f.source:<12} {f.city:<14} {f.n:>4d} {f.eff_n:>7.1f} "
            f"{f.bias_f:>+8.2f} {note:<14}"
        )
    lines.extend([
        "-" * 70,
        f"Persist requires eff_n ≥ {_MOS_BIAS_MIN_SAMPLES} and "
        f"|bias| ≤ {_MOS_BIAS_MAX_ABS_F}°F.",
    ])
    return "\n".join(lines)


# ── A4: per-(station, LST hour) METAR diurnal fits ────────────────────────
#
# The v1 METAR Gaussian in bot/signals/sources/metar_observations.py uses a
# naive hours_left-only blend of forecast + running_high. Real stations
# have per-hour diurnal persistence: an unusually warm KDEN morning usually
# ends with an unusually warm afternoon, but KMIA tracks differently. Fit
# station-specific α_h + β_h·T(h) from historical hourly METAR, and
# replace the v1 mean/sigma with μ = max(α_h + β_h·T_obs, running_high),
# σ = rmse_h at runtime.
#
# Runtime reader lives in bot.signals.sources.metar_observations — key
# prefix constant must match exactly (drift-guard test pins both).

_DIURNAL_KEY_PREFIX: str = "weather_metar_diurnal_"
_DIURNAL_TTL_SEC: int = 60 * 86400
_DIURNAL_MIN_SAMPLES: int = 10
# Hard bounds on fitted σ — anti-degeneracy + anti-runaway. A station with
# near-zero observed error on 10 samples is almost certainly over-fit, and
# a >15°F RMSE should fall back to the prior instead of blessing garbage.
_DIURNAL_SIGMA_FLOOR_F: float = 0.3
_DIURNAL_SIGMA_CEIL_F: float = 12.0


def fetch_metar_hourly(
    station: WeatherStation, start_date: str, end_date: str,
    *, session: Optional[requests.Session] = None,
) -> list[tuple[str, int, float, float]]:
    """Fetch hourly METAR observations from IEM ASOS archive.

    Returns a list of ``(lst_date_iso, lst_hour, temp_f, daily_high_f)``
    tuples, one per (LST date, LST hour) cell. Temp is the LAST reading
    whose timestamp falls in that LST hour — that's the "most recent
    observation at hour h" that a runtime call would have had available.
    ``daily_high_f`` is the max tmpf over the whole LST calendar day the
    cell belongs to; identical across cells of the same date.
    """
    sess = session or requests
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    params = {
        "station": station.icao,
        "data": "tmpf",
        "year1": start_dt.year, "month1": start_dt.month, "day1": start_dt.day,
        "year2": end_dt.year, "month2": end_dt.month, "day2": end_dt.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "missing": "empty",
        "latlon": "no",
    }
    r = sess.get(
        _IEM_ASOS_URL,
        params=params,
        timeout=60,
        headers={"User-Agent": _USER_AGENT},
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"IEM asos HTTP {r.status_code}: {r.text[:200]}"
        )

    reader = csv.DictReader(io.StringIO(r.text))
    lst_tz = timezone(timedelta(hours=station.lst_offset))

    # (lst_date, lst_hour) → (last_temp_f, last_utc_dt)
    per_cell: dict[tuple[str, int], tuple[float, datetime]] = {}
    # lst_date → running max
    per_day_max: dict[str, float] = {}

    for row in reader:
        ts_raw = (row.get("valid") or "").strip()
        temp_raw = (row.get("tmpf") or "").strip()
        if not ts_raw or not temp_raw:
            continue
        try:
            temp_f = float(temp_raw)
        except ValueError:
            continue
        if temp_f < -60.0 or temp_f > 140.0:
            continue
        try:
            dt_utc = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        dt_lst = dt_utc.astimezone(lst_tz)
        lst_date = dt_lst.date().isoformat()
        lst_hour = dt_lst.hour
        cell = (lst_date, lst_hour)

        # Keep the LAST reading in each cell (largest UTC timestamp).
        prev = per_cell.get(cell)
        if prev is None or dt_utc > prev[1]:
            per_cell[cell] = (temp_f, dt_utc)

        # Daily high across all readings.
        if temp_f > per_day_max.get(lst_date, -1e9):
            per_day_max[lst_date] = temp_f

    out: list[tuple[str, int, float, float]] = []
    for (lst_date, lst_hour), (temp_f, _) in sorted(per_cell.items()):
        daily_high = per_day_max.get(lst_date)
        if daily_high is None:
            continue
        out.append((lst_date, lst_hour, temp_f, daily_high))
    return out


# ── CF6 (NWS Climatological Daily Report) ────────────────────────────
#
# Kalshi settles every weather market on the NWS Daily Climatological
# Report (CF6 form), not raw METAR tmpf max — confirmed 2026-04-28 via
# tools/validate_cf6_hypothesis.py against the 4 catastrophic Miami cases.
# CF6's TMAX field captures inter-observation peaks (continuous ASOS
# tracking), which can be 1-3°F above the max-of-hourly-tmpf we used to
# derive daily_high_f. Training the ensemble on tmpf max produced a
# systematic cold bias matching the live-shadow Brier gap.
#
# CF6 products are issued daily by the NWS WFO that owns each ASOS
# station. The product PIL is "CF6" + the 3-letter station ID (e.g.,
# CF6MIA for Miami International). One product covers the whole month;
# fetching the latest issue of any month gives the full month's TMAX.

import re as _cf6_re

_AFOS_URL: str = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"

# Station ICAO → CF6 PIL. Naming is ICAO-suffix + "CF6" prefix for most
# stations, with the New York exception (Central Park is KNYC; product
# is CF6NYC, not CF6CP).
_CF6_PIL_BY_STATION: dict[str, str] = {
    "KNYC": "CF6NYC",
    "KMDW": "CF6MDW",
    "KMIA": "CF6MIA",
    "KLAX": "CF6LAX",
    "KAUS": "CF6AUS",
    "KDEN": "CF6DEN",
}


def _parse_cf6_daily_max(body: str) -> dict[int, int]:
    """Extract ``{day_of_month: tmax_F}`` from a CF6 product body.

    The CF6 daily-rows table starts after a header line containing
    ``DY MAX MIN``. Data rows are leading-whitespace + 1-2 digit day
    + 1-3 digit max temp. Summary rows ('SM', 'AV') and end-of-page
    boilerplate don't match the day-row regex and are silently skipped.
    """
    out: dict[int, int] = {}
    in_table = False
    for line in body.splitlines():
        stripped = line.strip()
        if not in_table:
            if (stripped.startswith("DY ") and "MAX" in stripped
                    and "MIN" in stripped):
                in_table = True
            continue
        m = _cf6_re.match(r"^\s*(\d{1,2})\s+(\d{1,3}|M|-)\s+", line)
        if not m:
            if out and stripped.startswith((
                "AVERAGE MONTHLY", "DPTR FM NORMAL", "HIGHEST", "LOWEST",
                "TOTAL FOR MONTH", "[TEMPERATURE", "[PRESSURE",
            )):
                break
            continue
        try:
            day = int(m.group(1))
            if not (1 <= day <= 31):
                continue
            tmax_raw = m.group(2)
            if tmax_raw in ("M", "-"):
                continue
            tmax = int(tmax_raw)
            if -60 <= tmax <= 140:
                out[day] = tmax
        except ValueError:
            continue
    return out


def fetch_cf6_tmax(
    station_icao: str, year: int, month: int,
    *, session: Optional[requests.Session] = None,
) -> dict[int, int]:
    """Fetch CF6 daily TMAX values from IEM's AFOS archive for the given
    (station, year, month). Returns ``{day_of_month: tmax_F}``.

    Empty dict on missing PIL mapping, missing product, or unparseable
    response — caller decides whether absence is fatal.
    """
    pil = _CF6_PIL_BY_STATION.get(station_icao.upper())
    if pil is None:
        return {}

    # Query a date a few days into the next month so the response covers
    # all days of the target month (CF6 is issued daily; the latest issue
    # before our `e` cutoff has the full month-to-date table).
    cutoff = datetime(year, month, 28, tzinfo=timezone.utc) + timedelta(days=10)
    end_iso = cutoff.strftime("%Y-%m-%dT%H:%MZ")

    sess = session or requests
    params = {"pil": pil, "limit": "1", "e": end_iso}
    try:
        r = sess.get(
            _AFOS_URL, params=params, timeout=30,
            headers={"User-Agent": _USER_AGENT},
        )
    except requests.RequestException:
        return {}
    if r.status_code != 200 or not r.text:
        return {}
    return _parse_cf6_daily_max(r.text)


def update_daily_high_from_cf6(
    conn: sqlite3.Connection,
    station_icao: str,
    year: int,
    month: int,
    *,
    session: Optional[requests.Session] = None,
) -> int:
    """Fetch CF6 TMAX for (station, year, month) and update every
    weather_metar_hourly_backfill row of that (station, lst_date) with
    the correct daily_high_f. Returns the number of distinct days
    overwritten (= number of CF6 day-rows applied).

    No-op when CF6 returns nothing for the period (network failure,
    out-of-archive month). Idempotent — running daily writes the same
    value when the CF6 doesn't change.
    """
    daily_max = fetch_cf6_tmax(station_icao, year, month, session=session)
    if not daily_max:
        return 0

    rows = [
        (float(tmax), station_icao, f"{year:04d}-{month:02d}-{day:02d}")
        for day, tmax in daily_max.items()
    ]

    def _do(c):
        c.executemany(
            "UPDATE weather_metar_hourly_backfill "
            "SET daily_high_f = ? "
            "WHERE station = ? AND lst_date = ?",
            rows,
        )

    db_write(_do, conn=conn)
    return len(rows)


def replay_hourly_and_write(
    conn: sqlite3.Connection,
    station: WeatherStation,
    start_date: str,
    end_date: str,
    *,
    session: Optional[requests.Session] = None,
) -> int:
    """Fetch hourly METAR for the station and write to
    ``weather_metar_hourly_backfill``. Returns rows written.

    Idempotent via UNIQUE(station, lst_date, lst_hour) + INSERT OR REPLACE.
    """
    records = fetch_metar_hourly(station, start_date, end_date, session=session)
    if not records:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (now_iso, station.icao, lst_date, lst_hour, temp_f, daily_high)
        for (lst_date, lst_hour, temp_f, daily_high) in records
    ]

    def _write(c):
        c.executemany(
            """INSERT OR REPLACE INTO weather_metar_hourly_backfill
               (created_at, station, lst_date, lst_hour, temp_f, daily_high_f)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
    db_write(_write, conn=conn)
    return len(rows)


@dataclass(frozen=True)
class DiurnalFit:
    station: str
    lst_hour: int
    n: int
    alpha: float        # intercept
    beta: float         # slope of daily_high on T(h)
    rmse: float         # sqrt(mean(resid²)) after fit


def _ols_fit(xs: list[float], ys: list[float]) -> Optional[tuple[float, float, float]]:
    """Return (alpha, beta, rmse) for y = alpha + beta·x. None if
    undefined (n < 2 or zero variance in x)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    beta = sxy / sxx
    alpha = my - beta * mx
    resids = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
    rmse = math.sqrt(sum(r * r for r in resids) / n)
    return alpha, beta, rmse


def fit_metar_diurnal(conn: sqlite3.Connection) -> list[DiurnalFit]:
    """Fit α + β·T(h) → daily_high per (station, lst_hour).

    Excludes the cell at lst_hour that falls *at or after* the daily max —
    including those rows biases β toward 1.0 tautologically (T(h) == high).
    We drop cells where the temp equals the day's recorded high so the
    regression learns morning→afternoon persistence, not trivial equality.
    """
    rows = conn.execute(
        """SELECT station, lst_hour, temp_f, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE daily_high_f IS NOT NULL"""
    ).fetchall()

    per_cell: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for station, lst_hour, temp_f, high_f in rows:
        # Drop rows where the reading IS the day's high — prevents
        # tautological β=1.0 fits in the afternoon hours.
        if temp_f is None or high_f is None:
            continue
        if abs(float(temp_f) - float(high_f)) < 1e-6:
            continue
        per_cell.setdefault((station, int(lst_hour)), []).append(
            (float(temp_f), float(high_f))
        )

    out: list[DiurnalFit] = []
    for (station, lst_hour), samples in sorted(per_cell.items()):
        fit = _ols_fit([x for x, _ in samples], [y for _, y in samples])
        if fit is None:
            continue
        alpha, beta, rmse = fit
        out.append(DiurnalFit(
            station=station, lst_hour=lst_hour,
            n=len(samples), alpha=alpha, beta=beta, rmse=rmse,
        ))
    return out


def persist_diurnal_fit(
    conn: sqlite3.Connection,
    fits: list[DiurnalFit],
    *, ttl_seconds: int = _DIURNAL_TTL_SEC,
    min_samples: int = _DIURNAL_MIN_SAMPLES,
) -> list[str]:
    """Persist per-station diurnal fits to kv_cache.

    One kv_cache row per station, keyed ``weather_metar_diurnal_<station>``,
    carrying a dict ``{"<hour>": {"alpha", "beta", "rmse", "n"}, ...}``.
    Only cells with ``n >= min_samples`` and a σ inside the guard band
    are persisted.

    Returns the list of kv keys written (one per station).
    """
    from bot.db import kv_set

    per_station: dict[str, dict[str, dict]] = {}
    for f in fits:
        if f.n < min_samples:
            continue
        if f.rmse < _DIURNAL_SIGMA_FLOOR_F or f.rmse > _DIURNAL_SIGMA_CEIL_F:
            continue
        per_station.setdefault(f.station, {})[str(f.lst_hour)] = {
            "alpha": f.alpha,
            "beta": f.beta,
            "rmse": f.rmse,
            "n": f.n,
        }

    keys: list[str] = []
    for station, hour_map in sorted(per_station.items()):
        if not hour_map:
            continue
        key = f"{_DIURNAL_KEY_PREFIX}{station}"
        payload = {
            "hours": hour_map,
            "fit_at": datetime.now(timezone.utc).isoformat(),
        }
        kv_set(conn, key, payload, ttl_seconds)
        keys.append(key)
    return keys


def report_diurnal_fits(fits: list[DiurnalFit]) -> str:
    if not fits:
        return "No diurnal fits — run hourly backfill first."
    lines = [
        "Per-(station, LST hour) diurnal fits: daily_high = α + β·T(h)",
        "-" * 72,
        f"{'station':<8} {'hour':>4} {'n':>4} {'α°F':>8} {'β':>7} "
        f"{'RMSE°F':>8} {'note':<18}",
        "-" * 72,
    ]
    for f in fits:
        note = "" if f.n >= _DIURNAL_MIN_SAMPLES else f"thin (<{_DIURNAL_MIN_SAMPLES})"
        if f.rmse < _DIURNAL_SIGMA_FLOOR_F or f.rmse > _DIURNAL_SIGMA_CEIL_F:
            note = (note + " outlier-σ").strip()
        lines.append(
            f"{f.station:<8} {f.lst_hour:>4d} {f.n:>4d} "
            f"{f.alpha:>+8.2f} {f.beta:>+7.3f} "
            f"{f.rmse:>8.2f} {note:<18}"
        )
    lines.extend([
        "-" * 72,
        "Legend:",
        "  α, β    : daily_high ≈ α + β·T(h) from historical METAR obs.",
        "  RMSE°F  : residual σ after fit — persisted as the live σ.",
        f"  Persist requires n ≥ {_DIURNAL_MIN_SAMPLES} and "
        f"{_DIURNAL_SIGMA_FLOOR_F} ≤ σ ≤ {_DIURNAL_SIGMA_CEIL_F}°F.",
    ])
    return "\n".join(lines)


def persist_group_fit(
    conn: sqlite3.Connection, fit: GroupFit,
    *, ttl_seconds: int = _GROUP_RHO_TTL_SEC,
) -> str:
    """Write the fitted rho to kv_cache under
    ``weather_group_corr_<group>`` so ``weather_ensemble_v2`` picks it
    up on the next cycle. Returns the kv key that was written.

    Payload includes extras (n_eff, n_pairs, sources, fit_at) for
    operator debugging — the reader only uses ``rho``.
    """
    from bot.db import kv_set

    key = f"{_GROUP_RHO_KEY_PREFIX}{fit.group}"
    payload = {
        "rho": fit.rho,
        "n_eff": fit.n_eff,
        "n_sources": fit.n_sources_present,
        "n_pairs": fit.n_pairs,
        "sources": list(fit.sources),
        "fit_at": datetime.now(timezone.utc).isoformat(),
    }
    kv_set(conn, key, payload, ttl_seconds)
    return key


def report_groups(fits: list[GroupFit]) -> str:
    if not fits:
        return (
            "No group fits — need ≥2 sources with ≥2 joint samples. "
            "Run the backfill first or wait for MADIS (OBS group)."
        )
    lines = [
        "Effective-N per correlation group",
        "-" * 72,
        f"{'group':<8} {'sources':<34} {'n_joint':>8} {'rho':>8} {'n_eff':>8}",
        "-" * 72,
    ]
    for f in fits:
        src_str = ",".join(f.sources)
        lines.append(
            f"{f.group:<8} {src_str:<34} {f.n_pairs:>8d} "
            f"{f.rho:>+8.3f} {f.n_eff:>8.2f}"
        )
    lines.extend([
        "-" * 72,
        "Legend:",
        "  rho    : mean pairwise Pearson corr(error_i, error_j) over joint rows.",
        "  n_eff  : n / (1 + (n-1)*rho) — feed as 1/n_eff into combine_gaussian.",
        "           MVP in weather_ensemble_v2 currently uses rho=1.0 (n_eff=1).",
        "           When rho < 1, sources get more weight than the MVP gives them.",
    ])
    return "\n".join(lines)


def report(fits: list[SourceFit]) -> str:
    """Human-readable summary for operator console."""
    if not fits:
        return "No fits — run backfill first."
    lines = [
        "A2.5a per-source fit summary",
        "-" * 70,
        f"{'source':<15} {'n':>4} {'bias°F':>8} {'RMSE°F':>8} "
        f"{'σ_rep°F':>8} {'σ_ratio':>9}",
        "-" * 70,
    ]
    for f in fits:
        lines.append(
            f"{f.source:<15} {f.n:>4d} {f.mean_bias_f:>+8.2f} "
            f"{f.rmse_f:>8.2f} {f.reported_sigma_f:>8.2f} "
            f"{f.sigma_ratio:>9.2f}"
        )
    lines.extend([
        "-" * 70,
        "Legend:",
        "  bias°F   : mean(forecast − observed). Positive = source runs warm.",
        "  RMSE°F   : realized forecast error. Compare to reported σ.",
        "  σ_ratio  : RMSE / reported σ. >1 = under-confident, <1 = over-confident.",
        "             MOS bias correction (A5) will zero the bias column.",
        "             Skill curves (A3) will drive σ_ratio → 1.0.",
    ])
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────

def _parse_cities(raw: str) -> list[WeatherStation]:
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    missing = [k for k in keys if k not in STATION_BY_CITY]
    if missing:
        raise SystemExit(
            f"Unknown cities: {missing}. "
            f"Known: {sorted(STATION_BY_CITY)}"
        )
    # Deduplicate while preserving order (aliases like 'la' → same station).
    seen = set()
    stations: list[WeatherStation] = []
    for k in keys:
        s = STATION_BY_CITY[k]
        if s.icao in seen:
            continue
        seen.add(s.icao)
        stations.append(s)
    return stations


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (LST)")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (LST)")
    p.add_argument(
        "--cities", default="nyc,chicago,miami,los angeles,austin,denver",
        help="Comma-separated city keys (see bot.daemon.stations)",
    )
    p.add_argument(
        "--db", default=None,
        help="SQLite DB path. Defaults to bot.config.DB_PATH.",
    )
    p.add_argument(
        "--lead-hours", type=int, default=_DEFAULT_LEAD_HOURS,
        help="Nominal lead time to stamp on OM rows",
    )
    p.add_argument(
        "--report-only", action="store_true",
        help="Skip network fetches; just print the fit from existing rows",
    )
    p.add_argument(
        "--persist-effective-n", action="store_true",
        help=(
            "After fitting per-group rho, write it to kv_cache so "
            "weather_ensemble_v2 uses the learned n_eff on the next "
            "cycle. Without this flag the fitter is read-only."
        ),
    )
    p.add_argument(
        "--persist-skill-curves", action="store_true",
        help=(
            "After fitting per-(source, horizon) RMSE, write each bucket "
            "with n >= {min} samples to kv_cache so weather_ensemble_v2 "
            "replaces the hardcoded σ schedule with the learned σ."
            .format(min=_SKILL_MIN_SAMPLES)
        ),
    )
    p.add_argument(
        "--hourly-metar", action="store_true",
        help=(
            "Fetch hourly METAR observations into "
            "weather_metar_hourly_backfill for A4 diurnal fitting. "
            "Independent of the daily backfill — runs alongside."
        ),
    )
    p.add_argument(
        "--persist-diurnal", action="store_true",
        help=(
            "After fitting per-(station, hour) α + β·T(h), persist to "
            "kv_cache so metar_observations.get_metar_gaussian replaces "
            "its naive time-blend with the learned predictor."
        ),
    )
    p.add_argument(
        "--persist-mos-bias", action="store_true",
        help=(
            "After fitting per-(source, city, season, horizon) "
            "mean(forecast − observed), persist to kv_cache so "
            "weather_ensemble_v2 subtracts the bias before combine."
        ),
    )
    args = p.parse_args(argv)

    conn = init_db(args.db)
    stations = _parse_cities(args.cities)

    if not args.report_only:
        session = requests.Session()
        total = 0
        for station in stations:
            print(f"[backfill] {station.city} ({station.icao}) "
                  f"{args.start} → {args.end}")
            try:
                n = replay_and_write(
                    conn, station, args.start, args.end,
                    lead_hours=args.lead_hours, session=session,
                )
                print(f"  wrote {n} rows")
                total += n
            except Exception as e:
                print(f"  error: {type(e).__name__}: {e}", file=sys.stderr)
            # Gentle rate-limit between cities — IEM asks politely.
            time.sleep(1.0)
        print(f"[backfill] wrote {total} rows total")

    fits = fit_per_source(conn)
    print()
    print(report(fits))

    print()
    model_fit = fit_group_correlation(
        conn, _MODEL_GROUP_SOURCES, group_name="model",
    )
    group_fits = [f for f in [model_fit] if f is not None]
    print(report_groups(group_fits))

    if args.persist_effective_n:
        if not group_fits:
            print(
                "\n[persist] skipped — no fits to persist. "
                "Run the backfill first."
            )
        else:
            print("\n[persist] writing learned n_eff to kv_cache …")
            for f in group_fits:
                key = persist_group_fit(conn, f)
                print(f"  {key}  rho={f.rho:+.3f}  n_eff={f.n_eff:.2f}")
            print(
                "[persist] done. weather_ensemble_v2 will pick this up "
                "on the next cycle."
            )

    print()
    skill_fits = fit_skill_curves(conn)
    print(report_skill_curves(skill_fits))

    # A4: hourly METAR backfill + diurnal fit
    if args.hourly_metar and not args.report_only:
        session = session if 'session' in locals() else requests.Session()
        total_hourly = 0
        for station in stations:
            print(
                f"[hourly-metar] {station.city} ({station.icao}) "
                f"{args.start} → {args.end}"
            )
            try:
                n_h = replay_hourly_and_write(
                    conn, station, args.start, args.end, session=session,
                )
                print(f"  wrote {n_h} hourly rows")
                total_hourly += n_h
            except Exception as e:
                print(f"  error: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(1.0)
        print(f"[hourly-metar] wrote {total_hourly} rows total")

    print()
    diurnal_fits = fit_metar_diurnal(conn)
    print(report_diurnal_fits(diurnal_fits))

    if args.persist_diurnal:
        keys = persist_diurnal_fit(conn, diurnal_fits)
        if not keys:
            print(
                f"\n[persist] diurnal skipped — no (station, hour) cells "
                f"with n ≥ {_DIURNAL_MIN_SAMPLES} inside σ bounds."
            )
        else:
            print("\n[persist] writing learned diurnal fits to kv_cache …")
            for k in keys:
                print(f"  {k}")
            print(
                "[persist] done. metar_observations.get_metar_gaussian "
                "will use learned α+β·T on the next cycle."
            )

    # A5: EWMA MOS bias per (source, city)
    print()
    mos_fits = fit_mos_bias(conn)
    print(report_mos_bias(mos_fits))

    if args.persist_mos_bias:
        keys = persist_mos_bias(conn, mos_fits)
        if not keys:
            print(
                f"\n[persist] MOS bias skipped — no cells with "
                f"eff_n ≥ {_MOS_BIAS_MIN_SAMPLES} inside |bias| ≤ "
                f"{_MOS_BIAS_MAX_ABS_F}°F bounds."
            )
        else:
            print("\n[persist] writing learned MOS biases to kv_cache …")
            for k in keys:
                print(f"  {k}")
            print(
                "[persist] done. weather_ensemble_v2 will shift by "
                "-bias on the next cycle."
            )

    if args.persist_skill_curves:
        persistable = [f for f in skill_fits if f.n >= _SKILL_MIN_SAMPLES]
        if not persistable:
            print(
                f"\n[persist] skipped — no (source, bucket) pairs with "
                f"n ≥ {_SKILL_MIN_SAMPLES} samples yet."
            )
        else:
            print("\n[persist] writing learned skill curves to kv_cache …")
            for f in persistable:
                key = persist_skill_fit(conn, f)
                print(
                    f"  {key}  σ={f.rmse_f:.2f}°F  bias={f.bias_f:+.2f}°F  "
                    f"n={f.n}"
                )
            print(
                "[persist] done. weather_ensemble_v2 will use learned σ "
                "for those buckets on the next cycle."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
