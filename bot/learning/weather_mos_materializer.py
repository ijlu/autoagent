"""Live-snapshot → MOS-bias backfill materializer.

For each settled weather ticker we have ``weather_forecast_snapshots`` rows
for, write one row per canonical source into
``weather_gaussian_snapshots_backfill`` carrying the morning-of forecast and
the realized observed daily high. The same EWMA fitter
(``tools.backfill_weather_effective_n.fit_mos_bias``) then trains over both
historical (Open-Meteo archive) and live-materialized rows uniformly.

Why this lives next to ``settlement_backfill.py`` rather than inside
``record_settlements``:
``record_settlements`` only fires for tickers we held a position on. Weather
MM is shadow-only today (``WEATHER_MM_LIVE=false``), so the portfolio
settlement loop sees zero weather tickers in steady state. Driving off the
catalog (settlement_date < today_utc) sees every settled market regardless
of whether we traded it — matching the architecture of
``bot/learning/settlement_backfill.py``.

Idempotency: ``weather_gaussian_snapshots_backfill`` has
``UNIQUE(source, city, settlement_date, lead_hours)``. We always write
``lead_hours=12`` (matching the existing Open-Meteo historical convention)
so re-runs collapse to ``INSERT OR IGNORE`` no-ops.
"""
from __future__ import annotations

import logging
import math
import re
import time
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.daemon.stations import STATION_BY_SERIES, WeatherStation
from bot.db import db_write_ctx, kv_set
from bot.signals.sources._fomc_calendar import MONTH_ABBR
from bot.signals.weather_ensemble_v2 import _city_key, _series_from_ticker
from bot.signals.weather_sources import GAUSSIAN_COMBINE_SOURCES

logger = logging.getLogger(__name__)

# ── MOS bias fitting constants ────────────────────────────────────────────────
# These mirror tools/backfill_weather_effective_n.py. Keep in sync.
_MOS_BIAS_KEY_PREFIX: str = "weather_mos_bias_"
_MOS_BIAS_TTL_SEC: int = 45 * 86400
_MOS_BIAS_MIN_SAMPLES: int = 8
_MOS_BIAS_MAX_ABS_F: float = 5.0
_MOS_BIAS_EWMA_HALF_LIFE_DAYS: float = 14.0


@dataclass
class MOSBiasFit:
    source: str
    city: str
    n: int
    bias_f: float
    eff_n: float


def _ewma_weight(date_iso: str, ref_date_iso: str, half_life_days: float) -> float:
    try:
        d = datetime.strptime(date_iso[:10], "%Y-%m-%d")
        ref = datetime.strptime(ref_date_iso[:10], "%Y-%m-%d")
    except (ValueError, IndexError):
        return 0.0
    age_days = max(0.0, (ref - d).total_seconds() / 86400.0)
    return 2.0 ** (-age_days / half_life_days)


def fit_and_persist_mos_bias(
    conn: sqlite3.Connection,
) -> dict:
    """Fit EWMA per-(source, city) forecast bias from ``weather_gaussian_snapshots_backfill``
    and write correction values to ``kv_cache`` so ``_apply_mos_bias`` in
    ``weather_ensemble_v2`` can de-bias Gaussian forecasts at quote time.

    Idempotent — re-running overwrites kv_cache with the latest fit.
    Returns a stats dict with ``cells_fitted``, ``keys_written``, ``cells_thin``.
    """
    ref_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = conn.execute(
            """SELECT source, city, settlement_date, forecast_mean_f, observed_high_f
                 FROM weather_gaussian_snapshots_backfill
                WHERE observed_high_f IS NOT NULL
                  AND forecast_mean_f IS NOT NULL
                  AND source != 'metar'"""
        ).fetchall()
    except Exception as exc:
        logger.warning("[mos_fitter] backfill query failed: %s", exc)
        return {"cells_fitted": 0, "keys_written": 0, "cells_thin": 0, "error": str(exc)}

    per_cell: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for src, city, date_iso, fcst, obs in rows:
        w = _ewma_weight(str(date_iso), ref_date, _MOS_BIAS_EWMA_HALF_LIFE_DAYS)
        if w <= 0:
            continue
        per_cell.setdefault((str(src), str(city)), []).append((w, float(fcst) - float(obs)))

    fits: list[MOSBiasFit] = []
    for (src, city), pairs in per_cell.items():
        sum_w = sum(w for w, _ in pairs)
        if sum_w <= 0:
            continue
        sum_we = sum(w * e for w, e in pairs)
        sum_w2 = sum(w * w for w, _ in pairs)
        eff_n = (sum_w * sum_w) / sum_w2 if sum_w2 > 0 else 0.0
        fits.append(MOSBiasFit(src, city, len(pairs), sum_we / sum_w, eff_n))

    keys_written = 0
    cells_thin = 0
    with db_write_ctx(conn):
        for f in fits:
            if f.eff_n < _MOS_BIAS_MIN_SAMPLES:
                cells_thin += 1
                continue
            if abs(f.bias_f) > _MOS_BIAS_MAX_ABS_F:
                continue
            key = f"{_MOS_BIAS_KEY_PREFIX}{f.source}_{_city_key(f.city)}"
            kv_set(conn, key, {
                "bias": f.bias_f, "n": f.n, "eff_n": f.eff_n,
                "fit_at": datetime.now(timezone.utc).isoformat(),
            }, _MOS_BIAS_TTL_SEC)
            keys_written += 1

    return {"cells_fitted": len(fits), "keys_written": keys_written, "cells_thin": cells_thin}


def fit_and_persist_skill_curves(conn: sqlite3.Connection) -> dict:
    """Refit per-(source, [city,] horizon-bucket) skill σ from
    ``weather_gaussian_snapshots_backfill`` and persist to ``kv_cache``.

    Wraps the canonical fitters in ``tools.backfill_weather_effective_n``
    so the daemon can refresh skill σ overrides on the same scheduler
    cadence as the MOS-bias fit. Always asks for ``per_city=True`` —
    diagnostics show error spread varies 0.9-2.0°F across cities for the
    same source, so a single pooled σ is wrong everywhere. Cells with
    fewer than ``_SKILL_MIN_SAMPLES`` samples are skipped on persist;
    consumers (``weather_ensemble_v2._get_learned_sigma``) fall through
    to the pooled key when a city cell is absent.

    Returns ``{"buckets_fitted": int, "keys_written": int,
    "buckets_thin": int, "city_keys_written": int}``.
    """
    from tools.backfill_weather_effective_n import (
        fit_skill_curves,
        persist_skill_fit,
        _SKILL_MIN_SAMPLES,
    )

    try:
        # winsorize_pct=0.02 clips top+bottom 2% of residuals before computing
        # RMSE. On a 115-sample (city, source) cell that's ~2 samples each
        # tail — enough to absorb a single freak weather day without
        # flattening the realistic σ estimate.
        fits = fit_skill_curves(conn, per_city=True, winsorize_pct=0.02)
    except Exception as exc:
        logger.warning("[skill_fitter] fit failed: %s", exc)
        return {"buckets_fitted": 0, "keys_written": 0, "buckets_thin": 0,
                "city_keys_written": 0, "error": str(exc)}

    keys_written = 0
    city_keys_written = 0
    buckets_thin = 0
    with db_write_ctx(conn):
        for fit in fits:
            if fit.n < _SKILL_MIN_SAMPLES:
                buckets_thin += 1
                continue
            try:
                persist_skill_fit(conn, fit)
                keys_written += 1
                if fit.city:
                    city_keys_written += 1
            except Exception as exc:
                logger.warning(
                    "[skill_fitter] persist failed %s/%s%s: %s",
                    fit.source, fit.bucket,
                    f"/{fit.city}" if fit.city else "", exc,
                )

    return {"buckets_fitted": len(fits), "keys_written": keys_written,
            "buckets_thin": buckets_thin,
            "city_keys_written": city_keys_written}


# ── Snapshots-based skill σ + MOS bias fitter (Options A + C) ──────────────
#
# The original skill σ fitter (``fit_and_persist_skill_curves``) reads from
# ``weather_gaussian_snapshots_backfill``, which has only ~12 rows for
# NWS Point / MADIS / Tomorrow because those sources weren't in the original
# Open-Meteo-only backfill. Fitter skips them as "thin", they fall back to a
# pooled-across-cities σ that is 3+°F vs the canonical sources' 1°F, and
# they get effectively excluded from the live combine.
#
# This fitter reads from ``weather_forecast_snapshots`` instead — the table
# the live ensemble writes to on every quote. By joining settled-day
# observations from ``weather_metar_hourly_backfill``, we get hundreds of
# rows per (source, city) and can fit per-city skill σ + MOS bias for
# every source we actually use, not just the four with deep backfill.
#
# Discovery: 2026-04-27 audit showed NWS Point quoted 94°F for Austin while
# HRRR/NBM said 89.5°F. NWS Point's contribution to the combine was 4% of
# total precision because its σ was 3.67°F (pooled fallback). Live trades
# cleared at YES≤9¢ — market believed NWS Point's higher number. We were
# throwing away exactly the source that was right.

# Stations by city — same set used everywhere.
_STATION_BY_CITY_KEY: dict[str, str] = {
    "nyc": "KNYC", "chicago": "KMDW", "miami": "KMIA",
    "los_angeles": "KLAX", "austin": "KAUS", "denver": "KDEN",
}

# Bucket boundaries match weather_ensemble_v2._SKILL_BUCKET_EDGES exactly.
_SNAPSHOT_BUCKET_EDGES: tuple[int, ...] = (0, 6, 24, 48, 168)


def _bucket_for_hours_out(hours_out: float) -> Optional[str]:
    if hours_out is None or hours_out < 0:
        return None
    for lo, hi in zip(_SNAPSHOT_BUCKET_EDGES[:-1], _SNAPSHOT_BUCKET_EDGES[1:]):
        if lo <= hours_out < hi:
            return f"{lo}_{hi}"
    return None


def _city_from_series(series: str) -> Optional[str]:
    """Map ``KXHIGHNY`` → ``nyc``, etc."""
    s = (series or "").upper()
    if s == "KXHIGHNY":  return "nyc"
    if s == "KXHIGHCHI": return "chicago"
    if s == "KXHIGHMIA": return "miami"
    if s == "KXHIGHLAX": return "los_angeles"
    if s == "KXHIGHAUS": return "austin"
    if s == "KXHIGHDEN": return "denver"
    return None


def _settlement_date_from_ticker_simple(ticker: str) -> Optional[str]:
    """Extract YYYY-MM-DD from KXHIGH<CITY>-26APR27-... → 2026-04-27."""
    parts = (ticker or "").split("-")
    if len(parts) < 2:
        return None
    suf = parts[1]
    if len(suf) < 7:
        return None
    months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    try:
        yy = int(suf[:2])
        mon = suf[2:5].upper()
        dd = int(suf[5:7])
        m_idx = months.index(mon) + 1
        return f"20{yy:02d}-{m_idx:02d}-{dd:02d}"
    except (ValueError, IndexError):
        return None


def fit_and_persist_skill_from_snapshots(
    conn: sqlite3.Connection,
    *,
    sources: tuple[str, ...] = ("hrrr", "nbm", "weather", "open_meteo",
                                 "nws_point", "madis", "metar"),
    min_samples: int = 15,
    winsorize_pct: float = 0.02,
) -> dict:
    """Fit per-(source, city, horizon-bucket) skill σ + MOS bias from
    ``weather_forecast_snapshots`` joined to observed daily highs in
    ``weather_metar_hourly_backfill``.

    Persists each fit under
    ``weather_skill_<source>_<city>_<bucket>`` (σ payload) and
    ``weather_mos_bias_<source>_<city>`` (bias payload). Same key shapes
    that the live combine reads.

    Skips cells with fewer than ``min_samples`` samples to avoid
    whiplashing live σ off sparse data. Returns counts:
    ``{"skill_keys_written": int, "skill_cells_thin": int,
       "mos_keys_written": int, "mos_cells_thin": int}``.
    """
    # Pull DISTINCT (source, ticker) — one snapshot per ticker per source.
    # Multiple snapshots are recorded as the day progresses (every quote
    # cycle), but they share the same observed daily high. Counting each
    # row would over-state the sample size and pollute σ with within-event
    # correlation. We take the latest snapshot per (source, ticker) — the
    # one closest to settlement, hence most accurate post-hoc.
    rows = conn.execute(
        """SELECT s.source, s.series, s.ticker, s.forecast_high_f,
                  s.sigma_f, s.hours_out
             FROM weather_forecast_snapshots s
             JOIN (
                 SELECT source, ticker, MAX(id) AS max_id
                   FROM weather_forecast_snapshots
                  WHERE forecast_high_f IS NOT NULL
                    AND source != 'combined_v2'
                    AND source != 'afd_bias'
                    AND hours_out IS NOT NULL
                  GROUP BY source, ticker
             ) latest ON latest.max_id = s.id""",
    ).fetchall()

    # Build (station, lst_date) → daily_high lookup (one query, in-memory).
    obs_rows = conn.execute(
        """SELECT DISTINCT station, lst_date, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE daily_high_f IS NOT NULL""",
    ).fetchall()
    obs_lookup: dict[tuple[str, str], float] = {
        (str(s), str(d)): float(h) for s, d, h in obs_rows
    }

    # Cell key: (source, city, bucket). Each cell collects forecast_high − observed.
    skill_cells: dict[tuple[str, str, str], list[float]] = {}
    bias_cells: dict[tuple[str, str], list[float]] = {}

    for src, series, ticker, fcst, _sigma, hours_out in rows:
        if src not in sources:
            continue
        city = _city_from_series(series)
        if city is None or city not in _STATION_BY_CITY_KEY:
            continue
        settle_date = _settlement_date_from_ticker_simple(ticker)
        if settle_date is None:
            continue
        station = _STATION_BY_CITY_KEY[city]
        observed = obs_lookup.get((station, settle_date))
        if observed is None:
            continue
        residual = float(fcst) - float(observed)
        bucket = _bucket_for_hours_out(float(hours_out))
        if bucket is None:
            continue
        skill_cells.setdefault((src, city, bucket), []).append(residual)
        bias_cells.setdefault((src, city), []).append(residual)

    skill_keys_written = 0
    skill_cells_thin = 0
    mos_keys_written = 0
    mos_cells_thin = 0

    with db_write_ctx(conn):
        # Skill σ: one kv key per (source, city, bucket). Winsorize residuals.
        for (src, city, bucket), residuals in sorted(skill_cells.items()):
            n = len(residuals)
            if n < min_samples:
                skill_cells_thin += 1
                continue
            if winsorize_pct > 0:
                sr = sorted(residuals)
                lo = sr[int(n * winsorize_pct)]
                hi = sr[n - 1 - int(n * winsorize_pct)]
                residuals = [max(lo, min(hi, r)) for r in residuals]
            mean_r = sum(residuals) / n
            var_r = sum((r - mean_r) ** 2 for r in residuals) / n
            rmse = math.sqrt(max(0.0, var_r + mean_r * mean_r))  # RMSE = sqrt(σ² + bias²)
            sigma_clamped = max(0.3, min(8.0, rmse))
            payload = {
                "sigma": sigma_clamped,
                "bias": mean_r,
                "n": n,
                "fit_at": datetime.now(timezone.utc).isoformat(),
                "source_table": "weather_forecast_snapshots",
            }
            key = f"weather_skill_{src}_{city}_{bucket}"
            try:
                kv_set(conn, key, payload, 30 * 86400)
                skill_keys_written += 1
            except Exception as exc:
                logger.warning("[snapshot_skill] persist %s failed: %s", key, exc)

        # MOS bias: one kv key per (source, city). EWMA-equivalent: just the
        # mean over all observed residuals (winsorized).
        for (src, city), residuals in sorted(bias_cells.items()):
            n = len(residuals)
            if n < min_samples:
                mos_cells_thin += 1
                continue
            if winsorize_pct > 0:
                sr = sorted(residuals)
                lo = sr[int(n * winsorize_pct)]
                hi = sr[n - 1 - int(n * winsorize_pct)]
                residuals = [max(lo, min(hi, r)) for r in residuals]
            mean_r = sum(residuals) / n
            bias_clamped = max(-5.0, min(5.0, mean_r))
            payload = {
                "bias": bias_clamped,
                "n": n,
                "eff_n": float(n),
                "fit_at": datetime.now(timezone.utc).isoformat(),
                "source_table": "weather_forecast_snapshots",
            }
            key = f"weather_mos_bias_{src}_{city}"
            try:
                kv_set(conn, key, payload, 45 * 86400)
                mos_keys_written += 1
            except Exception as exc:
                logger.warning("[snapshot_mos] persist %s failed: %s", key, exc)

    return {
        "skill_keys_written": skill_keys_written,
        "skill_cells_thin": skill_cells_thin,
        "mos_keys_written": mos_keys_written,
        "mos_cells_thin": mos_cells_thin,
    }


# ── METAR residual σ fitter (per-station, per-LST-hour) ─────────────────────
#
# Replaces the hardcoded ``_sigma_for_hours`` schedule in
# ``bot.signals.sources.metar_observations``. For each (station, lst_hour)
# cell we compute, across every backfilled day, the standard deviation of
# (eventual_daily_high − running_max_at_that_hour). That number is the
# right σ to put on METAR's Gaussian when we're at that station + that hour.
#
# Pre-fix: ``_sigma_for_hours(hours_left=4)`` returned 5.0°F regardless of
# city or hour, so METAR's contribution to the precision-weighted combine
# was a 4% rounding error. With per-(station, lst_hour) residual σ measured
# from the hourly backfill, late-day σ collapses to ~0.3-1.0°F and METAR
# dominates the combine — matching the physics that "running max is
# observed; only the residual peak is uncertain."
_METAR_RESIDUAL_SIGMA_KEY_PREFIX: str = "weather_metar_residual_sigma_"
_METAR_RESIDUAL_SIGMA_TTL_SEC: int = 30 * 86400
_METAR_RESIDUAL_SIGMA_MIN_SAMPLES: int = 15
_METAR_RESIDUAL_SIGMA_FLOOR_F: float = 0.2  # don't go below METAR sensor noise
_METAR_RESIDUAL_SIGMA_CEIL_F: float = 12.0  # paranoia ceiling
_METAR_RESIDUAL_WINSORIZE_PCT: float = 0.02


def fit_and_persist_metar_residual_sigma(conn: sqlite3.Connection) -> dict:
    """Refit per-(station, LST hour) METAR residual σ from
    ``weather_metar_hourly_backfill`` and persist to ``kv_cache``.

    For each (station, hour h), measures std of
    ``daily_high_f − running_max_at_h`` across every backfill date with a
    populated daily_high. Winsorizes residuals at 2% tails to absorb
    freak weather days, floors at 0.2°F (sensor noise), caps at 12°F.

    Persists each cell as
    ``weather_metar_residual_sigma_<station>_<lst_hour>``. The METAR
    Gaussian getter consults this kv first, falls back to the hardcoded
    schedule when a cell hasn't been fit yet.

    Returns ``{"cells_fitted": int, "keys_written": int, "cells_thin": int}``.
    """
    # Pull per-day max-up-to-hour using a self-join; keep daily_high_f along.
    rows = conn.execute(
        """
        SELECT a.station, a.lst_date, a.lst_hour AS h,
               MAX(b.temp_f) AS running_max,
               a.daily_high_f
          FROM weather_metar_hourly_backfill a
          JOIN weather_metar_hourly_backfill b
            ON b.station = a.station
           AND b.lst_date = a.lst_date
           AND b.lst_hour <= a.lst_hour
         WHERE a.daily_high_f IS NOT NULL
           AND b.temp_f IS NOT NULL
         GROUP BY a.station, a.lst_date, a.lst_hour
        """
    ).fetchall()

    # (station, hour) -> list of residuals (daily_high - running_max_at_hour)
    cells: dict[tuple[str, int], list[float]] = {}
    for station, _date, h, running_max, daily_high in rows:
        if running_max is None or daily_high is None:
            continue
        residual = float(daily_high) - float(running_max)
        # daily_high < running_max is impossible by construction (daily_high ≥
        # any temp during the day) but guard against bad backfill rows.
        if residual < -0.5:
            continue
        cells.setdefault((str(station), int(h)), []).append(residual)

    keys_written = 0
    cells_thin = 0
    with db_write_ctx(conn):
        for (station, hour), residuals in sorted(cells.items()):
            n = len(residuals)
            if n < _METAR_RESIDUAL_SIGMA_MIN_SAMPLES:
                cells_thin += 1
                continue
            # Winsorize tails so a single freak day doesn't double σ.
            residuals_sorted = sorted(residuals)
            lo_idx = int(n * _METAR_RESIDUAL_WINSORIZE_PCT)
            hi_idx = n - 1 - lo_idx
            lo_v = residuals_sorted[lo_idx]
            hi_v = residuals_sorted[hi_idx]
            clipped = [max(lo_v, min(hi_v, r)) for r in residuals]
            mean_r = sum(clipped) / n
            var_r = sum((r - mean_r) ** 2 for r in clipped) / n
            sigma_f = math.sqrt(max(0.0, var_r))
            sigma_f = max(_METAR_RESIDUAL_SIGMA_FLOOR_F,
                          min(_METAR_RESIDUAL_SIGMA_CEIL_F, sigma_f))
            payload = {
                "sigma": sigma_f,
                "n": n,
                "mean_residual": mean_r,
                "fit_at": datetime.now(timezone.utc).isoformat(),
            }
            key = f"{_METAR_RESIDUAL_SIGMA_KEY_PREFIX}{station}_{hour}"
            try:
                kv_set(conn, key, payload, _METAR_RESIDUAL_SIGMA_TTL_SEC)
                keys_written += 1
            except Exception as exc:
                logger.warning(
                    "[metar_residual_fitter] persist failed %s/%d: %s",
                    station, hour, exc,
                )

    return {"cells_fitted": len(cells), "keys_written": keys_written,
            "cells_thin": cells_thin}


def fit_and_persist_group_correlation(conn: sqlite3.Connection) -> dict:
    """Refit the model-group pairwise error correlation and persist
    n_eff to ``kv_cache`` so ``weather_ensemble_v2`` weights correlated
    sources correctly.

    Returns ``{"persisted": bool, "rho": float|None, "n_eff": float|None,
    "n_pairs": int|None}``.
    """
    from tools.backfill_weather_effective_n import (
        fit_group_correlation,
        persist_group_fit,
        _MODEL_GROUP_SOURCES,
    )

    try:
        fit = fit_group_correlation(
            conn, _MODEL_GROUP_SOURCES, group_name="model",
        )
    except Exception as exc:
        logger.warning("[group_rho_fitter] fit failed: %s", exc)
        return {"persisted": False, "rho": None, "n_eff": None,
                "n_pairs": None, "error": str(exc)}

    if fit is None:
        return {"persisted": False, "rho": None, "n_eff": None, "n_pairs": None}

    with db_write_ctx(conn):
        try:
            persist_group_fit(conn, fit)
            return {"persisted": True, "rho": fit.rho, "n_eff": fit.n_eff,
                    "n_pairs": fit.n_pairs}
        except Exception as exc:
            logger.warning("[group_rho_fitter] persist failed: %s", exc)
            return {"persisted": False, "rho": fit.rho, "n_eff": fit.n_eff,
                    "n_pairs": fit.n_pairs, "error": str(exc)}


# Static lead bucket for live-materialized rows. Matches the Open-Meteo
# historical backfill (``_OM_MODELS`` writes lead_hours=12). Keeping this
# static — rather than echoing the snapshot's actual hours_out — gives the
# UNIQUE constraint a stable partition: re-runs collapse to no-ops, and the
# EWMA fitter (which pools across all leads) sees one row per (source,
# city, date) instead of one per cycle cadence.
_MATERIALIZED_LEAD_HOURS: int = 12

# How many days of past settlement_date to scan per pass. EWMA half-life is
# 14d, so anything older than this contributes <50% weight relative to a
# fresh row — not worth a special pass. Bounds the cost of a single tick.
_DEFAULT_MAX_BACK_DAYS: int = 14

# Canonical source names that participate in the Gaussian combine. The
# combined_v2 row and afd_bias row in ``weather_forecast_snapshots`` are
# excluded — they aren't bias-correctable Gaussian forecasters.
_MATERIALIZED_SOURCES: frozenset[str] = GAUSSIAN_COMBINE_SOURCES - {"metar"}
# METAR is excluded: it's the observation source, not a forecast. The
# ``forecast_mean`` value on a METAR snapshot is effectively the current
# observation, not a forward prediction — fitting bias against itself
# would be circular. (The historical backfill applies the same exclusion
# in ``fit_mos_bias`` via ``WHERE source != 'metar'``.)


def _settlement_date_from_ticker(ticker: str) -> Optional[str]:
    """Extract LST settlement date from a Kalshi weather ticker.

    Format: ``KX{HIGH|LOW}{CITY}-{YY}{MMM}{DD}-{T|B}{strike}``.
    Examples::

        KXHIGHNY-26APR24-T67   -> "2026-04-24"
        KXHIGHMIA-26APR18-B84.5 -> "2026-04-18"

    Returns ``None`` if the ticker shape doesn't match — caller skips.
    """
    if not ticker:
        return None
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker.upper())
    if m is None:
        return None
    yy = int(m.group(1))
    month = MONTH_ABBR.get(m.group(2))
    dd = int(m.group(3))
    if month is None:
        return None
    try:
        d = datetime(2000 + yy, month, dd)
    except ValueError:
        return None
    return d.strftime("%Y-%m-%d")


def _city_for_ticker(ticker: str) -> Optional[tuple[str, WeatherStation]]:
    """Return ``(city_key, station)`` for a weather ticker, or None."""
    series = _series_from_ticker(ticker)
    ws = STATION_BY_SERIES.get(series)
    if ws is None:
        return None
    return _city_key(ws.city), ws


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _eligible_tickers(
    conn: sqlite3.Connection,
    *,
    today_iso: str,
    max_back_days: int,
) -> list[tuple[str, str]]:
    """Return ``[(ticker, settlement_date), ...]`` we should attempt to
    materialize.

    Eligibility:
      * The ticker has at least one row in ``weather_forecast_snapshots``
        for a canonical Gaussian source (we have a forecast to log).
      * Its settlement_date (parsed from the ticker string) lies in
        ``[today - max_back_days, today)`` — past LST days are locked,
        same-day is racy with the LST window close.

    The caller dedupes (city, date) pairs across IEM fetches.
    """
    # We can't filter on settlement_date in SQL — it's encoded in the
    # ticker string, not a column — so we filter in Python. Distinct ticker
    # already collapses dozens of cycle rows per market to one tuple.
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM weather_forecast_snapshots
            WHERE source IN ({src_placeholders})""".format(
            src_placeholders=",".join("?" * len(_MATERIALIZED_SOURCES)),
        ),
        tuple(sorted(_MATERIALIZED_SOURCES)),
    ).fetchall()

    today = datetime.strptime(today_iso, "%Y-%m-%d").date()
    earliest = today - timedelta(days=max_back_days)

    out: list[tuple[str, str]] = []
    for (ticker,) in rows:
        date_iso = _settlement_date_from_ticker(ticker)
        if date_iso is None:
            continue
        d = datetime.strptime(date_iso, "%Y-%m-%d").date()
        if d >= today or d < earliest:
            continue
        out.append((ticker, date_iso))
    return out


def _morning_of_per_source(
    conn: sqlite3.Connection, ticker: str,
) -> dict[str, tuple[float, float]]:
    """Return ``{source: (forecast_mean_f, sigma_f)}`` — one row per
    canonical Gaussian source for this ticker, picking the snapshot whose
    ``hours_out`` is closest to 12 (ties broken by latest ``recorded_at``).

    Sources with no rows for this ticker are absent from the output.
    Snapshots with NULL ``forecast_high_f`` are skipped.
    """
    placeholders = ",".join("?" * len(_MATERIALIZED_SOURCES))
    rows = conn.execute(
        f"""SELECT source, forecast_high_f, sigma_f, hours_out, recorded_at
              FROM weather_forecast_snapshots
             WHERE ticker = ?
               AND source IN ({placeholders})
               AND forecast_high_f IS NOT NULL""",
        (ticker, *sorted(_MATERIALIZED_SOURCES)),
    ).fetchall()

    # Pick min(|hours_out - 12|, -recorded_at) per source.
    best: dict[str, tuple[float, str, float, float]] = {}
    # value tuple: (distance_to_12, recorded_at_neg_for_recency_tiebreak, mean, sigma)
    for source, mean_f, sigma_f, hours_out, recorded_at in rows:
        if mean_f is None:
            continue
        h = float(hours_out) if hours_out is not None else 0.0
        dist = abs(h - 12.0)
        # negate recorded_at via lexicographic invert: later string sorts
        # later, so use (dist, "~~~~" - recorded_at) trick? simpler — store
        # recorded_at as-is and break ties by "max" rather than "min".
        prior = best.get(source)
        if prior is None:
            best[source] = (dist, recorded_at or "", float(mean_f),
                             float(sigma_f) if sigma_f is not None else 0.0)
            continue
        prior_dist, prior_ts, _, _ = prior
        if dist < prior_dist or (dist == prior_dist and (recorded_at or "") > prior_ts):
            best[source] = (dist, recorded_at or "", float(mean_f),
                             float(sigma_f) if sigma_f is not None else 0.0)

    return {src: (mean, sigma) for src, (_, _, mean, sigma) in best.items()}


def _fetch_iem_high(
    station: WeatherStation, date_iso: str, *, http_session=None,
) -> Optional[float]:
    """Wrapper around the historical backfill tool's IEM fetcher.

    Single-day fetch: pass start=end=date_iso. Returns None on missing /
    malformed data; caller skips materialization for that (city, date).
    """
    from tools.backfill_weather_effective_n import fetch_metar_daily_highs

    try:
        # IEM endpoint truncates the inclusive window: when start==end, the
        # response is sometimes empty. Widen by ±1 day and pick out date_iso.
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        start = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        end = (d + timedelta(days=1)).strftime("%Y-%m-%d")
        per_day = fetch_metar_daily_highs(
            station, start, end, session=http_session,
        )
    except Exception as exc:
        logger.warning(
            "[mos_materializer] IEM fetch failed for %s %s: %s",
            station.icao, date_iso, exc,
        )
        return None
    return per_day.get(date_iso)


def materialize_due(
    conn: sqlite3.Connection,
    *,
    max_back_days: int = _DEFAULT_MAX_BACK_DAYS,
    today_iso: Optional[str] = None,
    http_session=None,
) -> dict[str, int]:
    """Main entrypoint. Walks eligible tickers, materializes one row per
    canonical Gaussian source per (city, date) into the backfill table.

    Idempotent: ``INSERT OR IGNORE`` against
    ``UNIQUE(source, city, settlement_date, lead_hours)`` so re-runs are
    no-ops once a (city, date) has been materialized.

    Returns a stats dict — consumed by the daemon scheduler wrapper for
    structured logging.
    """
    stats = {
        "tickers_eligible": 0,
        "city_dates_eligible": 0,
        "iem_calls": 0,
        "iem_misses": 0,
        "rows_written": 0,
        "tickers_no_snapshots": 0,
        "tickers_unresolved_city": 0,
    }

    if today_iso is None:
        today_iso = _today_utc_iso()

    eligible = _eligible_tickers(
        conn, today_iso=today_iso, max_back_days=max_back_days,
    )
    stats["tickers_eligible"] = len(eligible)
    if not eligible:
        return stats

    # Group eligible tickers by (city_key, station, date) so we fetch IEM
    # once per (city, date) pair regardless of how many tickers settled on
    # that day.
    grouped: dict[tuple[str, str], tuple[WeatherStation, list[str]]] = {}
    for ticker, date_iso in eligible:
        resolved = _city_for_ticker(ticker)
        if resolved is None:
            stats["tickers_unresolved_city"] += 1
            continue
        city_key, station = resolved
        # Backfill table convention: store the as-typed station.city
        # (e.g. "los angeles"), not the normalized key. The fitter
        # normalizes on read via ``_city_key``. Drift-guard tests pin both.
        key = (station.city, date_iso)
        if key not in grouped:
            grouped[key] = (station, [])
        grouped[key][1].append(ticker)

    stats["city_dates_eligible"] = len(grouped)
    now_iso = datetime.now(timezone.utc).isoformat()

    for i, ((city, date_iso), (station, tickers)) in enumerate(grouped.items()):
        if i > 0:
            time.sleep(2)
        observed_high_f = _fetch_iem_high(
            station, date_iso, http_session=http_session,
        )
        stats["iem_calls"] += 1
        if observed_high_f is None:
            stats["iem_misses"] += 1
            continue

        # Aggregate morning-of snapshots across every ticker that fired on
        # this (city, date). Pick best per source across all tickers — the
        # forecast for "NYC daily high on Apr 24" is the same regardless
        # of which bracket we were quoting. Take the absolute closest-to-12
        # snapshot across siblings.
        merged: dict[str, tuple[float, float]] = {}
        for ticker in tickers:
            per_source = _morning_of_per_source(conn, ticker)
            if not per_source:
                continue
            for src, (mean, sigma) in per_source.items():
                if src not in merged:
                    merged[src] = (mean, sigma)
                # No tie-break needed across siblings: forecasts for the
                # same (city, date) are identical regardless of ticker.
                # First-write wins is fine.

        if not merged:
            stats["tickers_no_snapshots"] += len(tickers)
            continue

        with db_write_ctx(conn):
            for source, (mean_f, sigma_f) in merged.items():
                try:
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO weather_gaussian_snapshots_backfill
                              (created_at, source, city, settlement_date,
                               lead_hours, forecast_mean_f, forecast_sigma_f,
                               observed_high_f)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (now_iso, source, city, date_iso,
                         _MATERIALIZED_LEAD_HOURS,
                         float(mean_f),
                         float(sigma_f) if sigma_f else None,
                         float(observed_high_f)),
                    )
                    stats["rows_written"] += int(cur.rowcount or 0)
                except Exception as exc:
                    logger.warning(
                        "[mos_materializer] insert failed %s/%s/%s: %s",
                        source, city, date_iso, exc,
                    )

    return stats
