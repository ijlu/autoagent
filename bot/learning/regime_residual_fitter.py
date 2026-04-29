"""Stage 1 — regime-conditional residual σ fitter.

Companion to ``bot.learning.weather_mos_materializer.fit_and_persist_metar_residual_sigma``,
which fits one σ per (station, lst_hour) by pooling across all weather days.
This fitter additionally stratifies by *regime* — wind direction + sky cover
(or wind+dewpoint, per-city) — so the production METAR Gaussian can use a
tighter σ when the prevailing regime is detectable.

Per-city taxonomy is fixed at ship time based on the regime feasibility study
(Phase A.3 in reports/WEATHER_REGIME_INVESTIGATION_2026-04-28.md). Cells with
n < ``_MIN_FIT_N`` are emitted as tier-2 (station, regime) pooled — the
predict-time lookup walks the hierarchy:

    Tier 1: (station, hour, regime)  — most specific
    Tier 2: (station, regime)        — pool across hours when tier 1 thin
    Tier 3: (station, hour)          — current pooled fitter
    Tier 4: schedule fallback        — _sigma_for_hours

This module only writes tier-1 and tier-2 keys. Tier-3 is the existing
fitter's domain (unchanged). The lookup function in metar_observations
walks all 4 tiers.

Run from the scheduler at the same cadence as the existing fitter (1h). Both
write to kv_cache idempotently — re-fitting just refreshes the values.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from bot.db import db_write_ctx, kv_set


logger = logging.getLogger(__name__)


# Per-city taxonomy: which regime axes to stratify by, per the Phase A
# feasibility study. KLAX is "wind+sky" but the late-day σ-reduction was
# only −6.9% — most cells will naturally fall back to tier 3 (existing
# pooled). That's intended: the fitter records what data is there, the
# lookup function decides whether to use it.
_CITY_TAXONOMY: dict[str, str] = {
    "KAUS": "wind+ddep",
    "KDEN": "wind+sky",
    "KLAX": "wind+sky",
    "KMDW": "wind+sky",
    "KMIA": "wind+sky",
    "KNYC": "wind",
}


# Minimum cell size to fit σ. 30-day backfill × ~3 (split by regime
# bucket × ~3 weeks of fit-relevant data) → most cells will be at the
# 5-10 sample range. Below 5 produces unstable σ estimates that spook
# the projection — fall back to a coarser tier instead.
_MIN_FIT_N: int = 5

# Sigma floor — same physical-noise rationale as the pooled fitter.
_SIGMA_FLOOR_F: float = 0.3
_SIGMA_CEIL_F: float = 12.0

# Winsorize the top/bottom 2% of residuals before computing σ. Same
# convention as the pooled fitter — absorbs single freak weather days
# without a fitter-level outlier handler.
_WINSORIZE_PCT: float = 0.02

# kv keys
TIER1_KEY_PREFIX: str = "weather_metar_residual_sigma_regime_"
TIER2_KEY_PREFIX: str = "weather_metar_residual_sigma_station_regime_"

# Refit cadence — 24h is enough; the input data only changes once per day
# when the daily backfill task fires. TTL set to 14 days so a fitter
# outage doesn't immediately flush keys; the lookup falls back gracefully.
_KV_TTL_S: int = 14 * 86400


# ── Regime label helpers (mirror tools/regime_stratify_residuals.py) ──

_WIND_BUCKETS = (
    ("N", 315.0, 45.0),
    ("E", 45.0, 135.0),
    ("S", 135.0, 225.0),
    ("W", 225.0, 315.0),
)


def _wind_bucket(deg: Optional[float]) -> str:
    if deg is None:
        return "unknown"
    deg = float(deg) % 360.0
    for name, lo, hi in _WIND_BUCKETS:
        if lo <= hi:
            if lo <= deg < hi:
                return name
        else:
            if deg >= lo or deg < hi:
                return name
    return "unknown"


def _sky_bucket(skyc1: Optional[str]) -> str:
    s = (skyc1 or "").strip().upper()
    if s in ("CLR", "FEW"):
        return "clear"
    if s in ("SCT", "BKN"):
        return "partly"
    if s in ("OVC", "VV"):
        return "overcast"
    return "unknown"


def _ddep_bucket(tmpf: Optional[float], dwpf: Optional[float]) -> str:
    if tmpf is None or dwpf is None:
        return "unknown"
    ddep = float(tmpf) - float(dwpf)
    if ddep <= 5.0:
        return "humid"
    if ddep <= 15.0:
        return "moderate"
    return "dry"


def _regime_label(
    station: str, taxonomy: str,
    *, drct: Optional[float], skyc1: Optional[str],
    tmpf: Optional[float], dwpf: Optional[float],
) -> str:
    """Return the regime bucket label for the configured taxonomy.
    Returns ``"unknown"`` when any required axis is missing.
    """
    if taxonomy == "wind":
        return _wind_bucket(drct)
    if taxonomy == "sky":
        return _sky_bucket(skyc1)
    if taxonomy == "ddep":
        return _ddep_bucket(tmpf, dwpf)
    if taxonomy == "wind+sky":
        w = _wind_bucket(drct)
        s = _sky_bucket(skyc1)
        if w == "unknown" or s == "unknown":
            return "unknown"
        return f"{w}|{s}"
    if taxonomy == "wind+ddep":
        w = _wind_bucket(drct)
        d = _ddep_bucket(tmpf, dwpf)
        if w == "unknown" or d == "unknown":
            return "unknown"
        return f"{w}|{d}"
    return "unknown"


# ── Fitter ────────────────────────────────────────────────────────────

def _fit_sigma(values: list[float]) -> tuple[float, float]:
    """Returns (σ, mean_residual) with winsorization."""
    n = len(values)
    if n < 2:
        return 0.0, sum(values) / n if n else 0.0
    sorted_v = sorted(values)
    lo_idx = int(n * _WINSORIZE_PCT)
    hi_idx = n - 1 - lo_idx
    lo_v = sorted_v[lo_idx]
    hi_v = sorted_v[hi_idx]
    clipped = [max(lo_v, min(hi_v, r)) for r in values]
    mean_r = sum(clipped) / n
    var_r = sum((r - mean_r) ** 2 for r in clipped) / n
    sigma = math.sqrt(max(0.0, var_r))
    sigma = max(_SIGMA_FLOOR_F, min(_SIGMA_CEIL_F, sigma))
    return sigma, mean_r


def fit_and_persist_regime_residual_sigma(
    conn: sqlite3.Connection,
) -> dict:
    """Refit per-(station, hour, regime) and per-(station, regime) METAR
    residual σ from the regime sibling table joined to the temperature
    backfill, persisting to kv_cache.

    Walks ``weather_metar_hourly_backfill`` (for temp_f + daily_high_f
    via self-join → running_max_at_hour) joined to
    ``weather_metar_hourly_regime`` (for the regime axes). For each
    (station, lst_date, lst_hour) triple where both tables have a row,
    derives the residual = daily_high − running_max_at_hour and assigns
    the regime label per the per-station taxonomy.

    Returns ``{"tier1_keys_written": int, "tier2_keys_written": int,
                "tier1_thin": int, "tier2_thin": int}``.
    """
    # Pull joined per-(station, lst_date, lst_hour) rows. running_max comes
    # from a correlated self-join so we get the max temp across all hours
    # ≤ this hour on the same date.
    rows = conn.execute(
        """
        SELECT a.station, a.lst_date, a.lst_hour AS h,
               MAX(b.temp_f) AS running_max,
               a.temp_f AS temp_f_at_hour,
               a.daily_high_f,
               r.dwpf, r.drct, r.skyc1
          FROM weather_metar_hourly_backfill a
          JOIN weather_metar_hourly_backfill b
            ON b.station = a.station
           AND b.lst_date = a.lst_date
           AND b.lst_hour <= a.lst_hour
          LEFT JOIN weather_metar_hourly_regime r
            ON r.station = a.station
           AND r.lst_date = a.lst_date
           AND r.lst_hour = a.lst_hour
         WHERE a.daily_high_f IS NOT NULL
           AND b.temp_f IS NOT NULL
         GROUP BY a.station, a.lst_date, a.lst_hour
        """
    ).fetchall()

    # Group residuals by (station, hour, regime) and (station, regime).
    tier1_cells: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    tier2_cells: dict[tuple[str, str], list[float]] = defaultdict(list)

    for (station, _date, h, running_max, _temp_at, daily_high,
         dwpf, drct, skyc1) in rows:
        if running_max is None or daily_high is None:
            continue
        residual = float(daily_high) - float(running_max)
        # daily_high < running_max would mean the running max is already
        # the day's high — residual ≈ 0. Drop tiny-negative noise from
        # bad backfill rows; the existing pooled fitter does the same.
        if residual < -0.5:
            continue
        taxonomy = _CITY_TAXONOMY.get(str(station))
        if taxonomy is None:
            continue
        label = _regime_label(
            str(station), taxonomy,
            drct=drct, skyc1=skyc1,
            tmpf=None, dwpf=dwpf,  # ddep needs current tmpf — skip if missing
        )
        # ddep taxonomy needs current-hour tmpf; pull it from the running
        # row's temp_f_at_hour (joined column). Easier than re-querying.
        # Recompute the label only for ddep-using taxonomies.
        if taxonomy in ("wind+ddep", "ddep"):
            label = _regime_label(
                str(station), taxonomy,
                drct=drct, skyc1=skyc1,
                tmpf=_temp_at, dwpf=dwpf,
            )
        if label == "unknown":
            continue
        tier1_cells[(str(station), int(h), label)].append(residual)
        tier2_cells[(str(station), label)].append(residual)

    # Persist
    now_iso = datetime.now(timezone.utc).isoformat()
    tier1_written = tier1_thin = 0
    tier2_written = tier2_thin = 0

    with db_write_ctx(conn):
        # Tier 1: (station, hour, regime)
        for (station, hour, label), vals in sorted(tier1_cells.items()):
            n = len(vals)
            if n < _MIN_FIT_N:
                tier1_thin += 1
                continue
            sigma, mean_r = _fit_sigma(vals)
            payload = {
                "sigma": sigma, "n": n, "mean_residual": mean_r,
                "tier": "regime_hour",
                "taxonomy": _CITY_TAXONOMY[station],
                "fit_at": now_iso,
            }
            key = f"{TIER1_KEY_PREFIX}{station}_{hour}_{label}"
            try:
                kv_set(conn, key, payload, _KV_TTL_S)
                tier1_written += 1
            except Exception as exc:
                logger.warning(
                    "[regime_residual_fitter] tier1 persist %s/%d/%s: %s",
                    station, hour, label, exc,
                )

        # Tier 2: (station, regime) — pools across hours
        for (station, label), vals in sorted(tier2_cells.items()):
            n = len(vals)
            if n < _MIN_FIT_N:
                tier2_thin += 1
                continue
            sigma, mean_r = _fit_sigma(vals)
            payload = {
                "sigma": sigma, "n": n, "mean_residual": mean_r,
                "tier": "station_regime",
                "taxonomy": _CITY_TAXONOMY[station],
                "fit_at": now_iso,
            }
            key = f"{TIER2_KEY_PREFIX}{station}_{label}"
            try:
                kv_set(conn, key, payload, _KV_TTL_S)
                tier2_written += 1
            except Exception as exc:
                logger.warning(
                    "[regime_residual_fitter] tier2 persist %s/%s: %s",
                    station, label, exc,
                )

    return {
        "tier1_keys_written": tier1_written,
        "tier1_thin": tier1_thin,
        "tier2_keys_written": tier2_written,
        "tier2_thin": tier2_thin,
    }


def get_regime_sigma(
    conn: sqlite3.Connection,
    station: str,
    lst_hour: int,
    regime_label: Optional[str],
) -> tuple[Optional[float], str]:
    """Return ``(sigma_or_none, tier_used)`` walking the hierarchy.

    Caller is responsible for tier-3 / tier-4 fallback (the existing
    pooled fitter / schedule). This function only owns the regime tiers.

    ``tier_used`` is one of: ``"regime_hour"``, ``"station_regime"``,
    ``"none"`` (no regime fit available). Tracking this in the snapshot
    columns powers the Stage-2 promotion gate.
    """
    from bot.db import kv_get

    if regime_label and regime_label != "unknown":
        # Tier 1
        key1 = f"{TIER1_KEY_PREFIX}{station}_{int(lst_hour)}_{regime_label}"
        try:
            payload = kv_get(conn, key1)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            sig = payload.get("sigma")
            if isinstance(sig, (int, float)) and 0.1 <= float(sig) <= 15.0:
                return float(sig), "regime_hour"
        # Tier 2
        key2 = f"{TIER2_KEY_PREFIX}{station}_{regime_label}"
        try:
            payload = kv_get(conn, key2)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            sig = payload.get("sigma")
            if isinstance(sig, (int, float)) and 0.1 <= float(sig) <= 15.0:
                return float(sig), "station_regime"

    return None, "none"
