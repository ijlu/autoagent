"""Regime-conditional MOS bias fitter for the v2 weather ensemble.

Companion to ``regime_residual_fitter.py`` (which conditions METAR's
residual σ on regime). This module does the analogous thing for
non-METAR sources: refit per-(source, city, regime) MOS bias from the
Gaussian snapshots backfill joined to the per-day regime label, and
persist to ``kv_cache``.

Why this exists: pooling MOS bias across regimes hides real physical
structure. HRRR may run +0.5°F warm on clear days but +1.8°F warm on
overcast days; a single pooled "+1.15°F" number is wrong for both. On
2026-04-30 the bracket-winners stratification showed a residual −0.88°F
cool bias on our μ predictions even after pooled MOS subtraction —
likely a regime-mixing artifact. Regime-conditional bias fixes the
average while widening the per-regime fit signal.

Read path: ``bot.signals.weather_ensemble_v2._get_mos_bias`` tries the
regime-conditional key first when the live regime label is known
(via ``_current_regime_label_for_city``), then falls back to the
pooled (source, city) key. So writing these keys is purely additive —
absent or empty cells just fall through to existing behavior.

Operational note: this fitter is intended to run nightly alongside
``tools.backfill_weather_effective_n.fit_mos_bias`` (the pooled
fitter) — call ``fit_and_persist_mos_bias_by_regime(conn)`` after the
nightly backfill cycle.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from bot.db import db_write_ctx, kv_set
# Reuse the regime label generator + city taxonomy from the existing
# residual-σ fitter so both fitters use the SAME regime semantics.
from bot.learning.regime_residual_fitter import (
    _CITY_TAXONOMY, _regime_label,
)

logger = logging.getLogger(__name__)


# Per-city primary station (matches bot/daemon/stations.py registry).
_CITY_TO_STATION: dict[str, str] = {
    "austin":      "KAUS",
    "denver":      "KDEN",
    "los_angeles": "KLAX",
    "chicago":     "KMDW",
    "miami":       "KMIA",
    "nyc":         "KNYC",
}

# Hours we'll pull regime labels at — we want the regime at the
# expected peak heating window. Order matters: try 14 first (2pm LST,
# typical peak), then 15, then 13. The first non-"unknown" label wins.
_PEAK_HOUR_PROBES: tuple[int, ...] = (14, 15, 13)

# Minimum cell size to persist. Below this, bias estimates are too
# noisy and we'd rather fall back to pooled.
#
# 2026-05-01: lowered from 30 → 15 after verifying that the snapshot
# backfill table has 90-120 days for HRRR/weather but only 1-7 days
# for the newer combine sources (ICON/UKMO/GEM/MetNo/ECMWF). At n=30,
# even HRRR's cells were sparse once split across 3-4 regime buckets
# per city (~25 samples/cell on average). At n=15, well-aged sources
# produce keys immediately while new sources still need ~2 weeks to
# accumulate. The bias-variance trade-off at n=15 is acceptable —
# standard error of the bias estimate is σ/√15 ≈ 0.4°F for a typical
# 1.5°F per-source σ, well below the ±5°F clamp.
_MIN_FIT_N: int = 15

# Magnitude clamp on the persisted bias. Same as the pooled fitter's
# `_MOS_BIAS_MAX_ABS_F` ceiling: a single outlier cell shouldn't move
# the Gaussian by more than 5°F.
_BIAS_MAX_ABS_F: float = 5.0

_KEY_PREFIX: str = "weather_mos_bias_"
# 14-day TTL — same as the pooled MOS bias fitter. A fitter outage
# shouldn't immediately flush keys; lookup gracefully falls back.
_KV_TTL_S: int = 14 * 86400


def _resolve_regime_for_day(
    conn: sqlite3.Connection, station: str, lst_date: str,
) -> Optional[str]:
    """Pull the regime axes for ``(station, lst_date)`` at the peak
    heating window, returning a single regime label.

    Walks ``_PEAK_HOUR_PROBES`` in order; first non-"unknown" label
    wins. Returns None when no probed hour produced a usable label.
    """
    taxonomy = _CITY_TAXONOMY.get(station)
    if taxonomy is None:
        return None

    for hour in _PEAK_HOUR_PROBES:
        row = conn.execute(
            """SELECT dwpf, drct, skyc1
                 FROM weather_metar_hourly_regime
                WHERE station = ? AND lst_date = ? AND lst_hour = ?""",
            (station, lst_date, hour),
        ).fetchone()
        if row is None:
            continue
        dwpf, drct, skyc1 = row
        # ddep taxonomies need tmpf — pull from the temperature backfill.
        tmpf = None
        if taxonomy in ("ddep", "wind+ddep"):
            t_row = conn.execute(
                """SELECT temp_f FROM weather_metar_hourly_backfill
                    WHERE station = ? AND lst_date = ? AND lst_hour = ?""",
                (station, lst_date, hour),
            ).fetchone()
            if t_row is not None:
                tmpf = t_row[0]
        label = _regime_label(
            station, taxonomy,
            drct=drct, skyc1=skyc1, tmpf=tmpf, dwpf=dwpf,
        )
        if label != "unknown":
            return label
    return None


def fit_and_persist_mos_bias_by_regime(
    conn: sqlite3.Connection,
) -> dict:
    """Refit per-(source, city, regime) MOS bias from the gaussian
    backfill and persist to kv_cache.

    Mirrors the pooled fitter in ``backfill_weather_effective_n.fit_mos_bias``
    but groups on regime in addition to (source, city). Restricted to
    sources currently in the live combine — same reasoning as the
    pooled fitter.

    Returns ``{"keys_written": int, "cells_thin": int, "rows_processed": int}``.
    """
    # Live source restriction (don't fit retired sources).
    from bot.signals.weather_sources import GAUSSIAN_COMBINE_SOURCES as _LIVE
    live_sources = tuple(_LIVE - {"metar"})  # METAR error is identically 0
    if not live_sources:
        return {"keys_written": 0, "cells_thin": 0, "rows_processed": 0}

    placeholders = ", ".join("?" for _ in live_sources)
    rows = conn.execute(
        f"""SELECT source, city, settlement_date,
                   forecast_mean_f, observed_high_f
              FROM weather_gaussian_snapshots_backfill
             WHERE observed_high_f IS NOT NULL
               AND forecast_mean_f IS NOT NULL
               AND source IN ({placeholders})""",
        live_sources,
    ).fetchall()

    # Cache regime labels per (city, lst_date) so we don't re-query for
    # every source's row on the same day.
    regime_cache: dict[tuple[str, str], Optional[str]] = {}

    # Group residuals by (source, city, regime).
    cells: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    rows_processed = 0
    for src, city, date_iso, fcst, obs in rows:
        rows_processed += 1
        try:
            err = float(fcst) - float(obs)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(err):
            continue
        station = _CITY_TO_STATION.get(str(city))
        if station is None:
            continue
        ck = (str(city), str(date_iso))
        if ck not in regime_cache:
            regime_cache[ck] = _resolve_regime_for_day(conn, station, str(date_iso))
        regime = regime_cache[ck]
        if regime is None:
            continue
        cells[(str(src), str(city), regime)].append(err)

    # Persist.
    now_iso = datetime.now(timezone.utc).isoformat()
    keys_written = 0
    cells_thin = 0
    with db_write_ctx(conn):
        for (src, city, regime), errs in sorted(cells.items()):
            if len(errs) < _MIN_FIT_N:
                cells_thin += 1
                continue
            bias = sum(errs) / len(errs)
            # Magnitude clamp (sanity, same as pooled fitter).
            bias = max(-_BIAS_MAX_ABS_F, min(_BIAS_MAX_ABS_F, bias))
            payload = {
                "bias": bias,
                "n": len(errs),
                "regime": regime,
                "fit_at": now_iso,
                "source_table": "weather_gaussian_snapshots_backfill",
            }
            key = f"{_KEY_PREFIX}{src}_{city}_{regime}"
            try:
                kv_set(conn, key, payload, _KV_TTL_S)
                keys_written += 1
            except Exception as exc:
                logger.warning(
                    "[mos_bias_regime_fitter] persist %s: %s", key, exc
                )

    return {
        "keys_written": keys_written,
        "cells_thin": cells_thin,
        "rows_processed": rows_processed,
    }
