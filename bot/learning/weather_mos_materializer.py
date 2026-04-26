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
