"""Per-city LST gate for cross-bracket entry, derived from data.

Cross-bracket should fire only during the **post-peak diurnal phase** of
the settlement city's day — when the daily high is essentially set and
the model's σ is genuinely tight. Previously gated by TTE (3-7h pre-
settle), but TTE conflates two regimes for cities with different peak
times. Per-city LST gate is the correct framing.

Per-city LST windows are derived empirically from
``tools/per_city_source_scorecard.py::empirical_phase_boundaries`` (peak
hour mode + first hour where running METAR ≥ 80% within 1°F of daily
high). Persisted to ``kv_cache`` under key
``cross_bracket_lst_gate:<series>``; daemon reads at decision time.

Falls back to a hardcoded per-series default if cache is empty (e.g.
on first deploy or after a kv_cache wipe).

Reference: reports/POSTFIX_REASSESSMENT_2026-05-05.md.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from bot.db import get_connection, kv_get, kv_set
from bot.daemon.stations import station_for_ticker


# Per-series defaults — empirically derived from per-city scorecards on
# 2026-05-05 (38 settled days of METAR backfill). Updated when the
# scheduled refresh runs against newer scorecard data.
#
# Format: series → (min_lst_hour_inclusive, max_lst_hour_inclusive)
# A decision can fire only when LST hour at the settlement-day evaluation
# time falls in [min, max]. settlement_day_lst_hour is computed from the
# cycle's UTC clock + the city's LST offset, on the LST date matching the
# ticker's settlement date.
#
# 2026-05-05 (Phase 3d): aligned LST gate min to METAR post-peak fast-path
# threshold (peak_hour + 2). Phase 3c counterfactual showed that cycles in
# the LST gate but BEFORE the fast-path threshold use the wide combined σ
# and continue losing on the same brackets. Aligning gate min = fast-path
# threshold means we only fire when METAR is being used.
DEFAULT_LST_GATE_BY_SERIES: dict[str, tuple[int, int]] = {
    "KXHIGHNY":  (15, 23),  # peak LST 13; +2h buffer
    "KXHIGHLAX": (13, 23),  # peak LST 11; +2h buffer
    "KXHIGHCHI": (16, 23),  # peak LST 14
    "KXHIGHAUS": (17, 23),  # peak LST 15
    "KXHIGHMIA": (14, 23),  # peak LST 12
    "KXHIGHDEN": (15, 23),  # peak LST 13
}

# Per-series default peak hour — also derived from scorecards. Used by
# the METAR post-peak fast-path in weather_ensemble_v2 to decide when
# the day's high is locked enough to override the combine with METAR's
# tight σ. See reports/PHASE_3C_COUNTERFACTUAL_2026-05-05.md.
DEFAULT_PEAK_HOUR_BY_SERIES: dict[str, int] = {
    "KXHIGHNY":  13,
    "KXHIGHLAX": 11,  # marine layer
    "KXHIGHCHI": 14,
    "KXHIGHAUS": 15,
    "KXHIGHMIA": 12,
    "KXHIGHDEN": 13,
}


# 2026-05-05 (Phase 3e stability detector): per-series rules for when the
# METAR post-peak fast-path can fire. Two parameters:
#   - always_arm_lst_hour: above this LST hour, fast-path always arms
#     regardless of how long running max has been stable. Reflects "sun
#     has decayed enough that no plausible spike remains."
#   - k_required_before: between LST [peak+1, always_arm_lst_hour),
#     require running max to have been stable for at least K hours.
#
# Derived from per-city METAR backfill analysis (~100 days/city) on
# 2026-05-05; see reports/PHASE_3E_STABILITY_VALIDATION_2026-05-05.md
# for the full per-city tables.
#
# CHI defaults match NY-like values because CHI METAR backfill only has
# 52 days (vs 100+ for others) — too small to fit per-city. Re-tune
# when CHI data accumulates.
POST_PEAK_RULE_BY_SERIES: dict[str, dict[str, int]] = {
    # 2026-05-05 cross-season validation (KLAX/KAUS/KMIA/KNYC summer 2024 +
    # KLAX Santa Ana fall 2024 from IEM ASOS archive) tightened three rules
    # vs the spring-only fit:
    #   - NY min_lst_for_k 14→15: K=3 at LST 14 has 8% summer risk (heat
    #     wave regime). K=3 at LST 15 stays 0%.
    #   - LAX k_required_before 1→2: K=1 at LST 13 has 7% Santa Ana risk.
    #     K=2 at LST 13 holds 0% across all seasons.
    #   - MIA k_required_before 2→3: K=2 at LST 15 has 8% summer risk
    #     (afternoon thunderstorm). K=3 at LST 15 holds 0%.
    # AUS, DEN, CHI rules unchanged: AUS validated in summer; DEN/CHI not
    # cross-validated due to data availability — flagged for re-tune.
    "KXHIGHNY":  {"always_arm_lst_hour": 17, "k_required_before": 3, "min_lst_for_k": 15},
    "KXHIGHLAX": {"always_arm_lst_hour": 14, "k_required_before": 2, "min_lst_for_k": 13},
    "KXHIGHAUS": {"always_arm_lst_hour": 16, "k_required_before": 1, "min_lst_for_k": 15},
    "KXHIGHMIA": {"always_arm_lst_hour": 16, "k_required_before": 3, "min_lst_for_k": 13},
    "KXHIGHDEN": {"always_arm_lst_hour": 16, "k_required_before": 1, "min_lst_for_k": 15},
    "KXHIGHCHI": {"always_arm_lst_hour": 17, "k_required_before": 2, "min_lst_for_k": 15},
}


def is_post_peak_safe(
    series: str, lst_hour: int, stability_hours: int,
) -> bool:
    """Return True when the METAR post-peak fast-path can fire safely.

    Encodes Josh's 2026-05-05 hypothesis (validated empirically): later
    LST hours need less stability proof because solar heating has
    decayed. Earlier LST hours need more stability hours to catch
    multi-modal warming days (convective afternoons in MIA, marine
    layer dissipation in LAX).
    """
    rule = POST_PEAK_RULE_BY_SERIES.get(series)
    if rule is None:
        # Unknown series — be conservative.
        return lst_hour >= 18
    if lst_hour >= rule["always_arm_lst_hour"]:
        return True
    if lst_hour >= rule["min_lst_for_k"]:
        return stability_hours >= rule["k_required_before"]
    return False


def get_running_high_state(
    station_icao: str,
    lst_date: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Read the METAR poller's persisted (running_high_f, last_increase_lst_hour)
    for a station + LST date from kv_cache.

    Returns ``None`` when no data is available (e.g., poller hasn't run
    today, or kv_cache key expired). Caller decides on fallback.

    The kv_cache key shape ``metar_daily_high_<STATION>_<YYYY-MM-DD>`` is
    written by ``bot.daemon.metar_poller.MetarPoller._persist_running_highs``
    every poll cycle (~30s).
    """
    if conn is None:
        try:
            conn = get_connection()
        except Exception:
            return None
    key = f"metar_daily_high_{station_icao}_{lst_date}"
    record = kv_get(conn, key)
    if isinstance(record, dict) and "high_f" in record:
        return {
            "high_f": float(record["high_f"]),
            "last_increase_lst_hour": int(
                record.get("last_increase_lst_hour", -1)
            ),
        }
    return None

# kv_cache key prefix
_KV_KEY_PREFIX = "cross_bracket_lst_gate:"
_KV_PEAK_KEY_PREFIX = "cross_bracket_peak_hour:"

# TTL on kv_cache writes (24h). Forces nightly refresh from the daily
# scorecard task; if that task fails we fall back to defaults rather
# than to stale numbers.
_KV_TTL_SECONDS: int = 24 * 3600


@dataclass(frozen=True)
class LstGate:
    series: str
    min_lst_hour: int  # inclusive
    max_lst_hour: int  # inclusive
    source: str        # "kv_cache" | "default"

    def includes(self, lst_hour: int) -> bool:
        return self.min_lst_hour <= lst_hour <= self.max_lst_hour


def get_lst_gate(series: str, conn: Optional[sqlite3.Connection] = None) -> LstGate:
    """Return the active LST gate for a Kalshi series (e.g. KXHIGHNY).

    Reads from kv_cache; falls back to ``DEFAULT_LST_GATE_BY_SERIES`` if
    cache is empty or contains a malformed value. ``conn=None`` opens
    the daemon's shared connection.
    """
    if conn is None:
        conn = get_connection()
    payload = kv_get(conn, _KV_KEY_PREFIX + series)
    if isinstance(payload, dict):
        try:
            return LstGate(
                series=series,
                min_lst_hour=int(payload["min_lst_hour"]),
                max_lst_hour=int(payload["max_lst_hour"]),
                source="kv_cache",
            )
        except (ValueError, KeyError, TypeError):
            pass
    default = DEFAULT_LST_GATE_BY_SERIES.get(series)
    if default is None:
        return LstGate(series=series, min_lst_hour=18, max_lst_hour=23,
                       source="default_unknown_series")
    return LstGate(
        series=series, min_lst_hour=default[0], max_lst_hour=default[1],
        source="default",
    )


def set_lst_gate(
    series: str,
    min_lst_hour: int,
    max_lst_hour: int,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Persist a derived LST gate to kv_cache. Called by the daily
    scorecard refresh task; not by trading code."""
    if not 0 <= min_lst_hour <= 23 or not 0 <= max_lst_hour <= 23:
        raise ValueError(f"LST hours must be in [0, 23]: got ({min_lst_hour}, {max_lst_hour})")
    if min_lst_hour > max_lst_hour:
        raise ValueError(f"min_lst_hour > max_lst_hour: ({min_lst_hour}, {max_lst_hour})")
    if conn is None:
        conn = get_connection()
    kv_set(conn, _KV_KEY_PREFIX + series, {
        "min_lst_hour": int(min_lst_hour),
        "max_lst_hour": int(max_lst_hour),
        "updated_at": int(time.time()),
    }, ttl_seconds=_KV_TTL_SECONDS)


def gate_for_ticker(ticker: str, conn: Optional[sqlite3.Connection] = None) -> Optional[LstGate]:
    """Resolve a ticker to its LST gate via the city's series."""
    station = station_for_ticker(ticker)
    if station is None:
        return None
    return get_lst_gate(station.series, conn=conn)


# ─── Peak hour accessors (used by METAR post-peak fast-path) ────────────────


def get_peak_hour(series: str, conn: Optional[sqlite3.Connection] = None) -> int:
    """Return the empirical LST peak hour for a series, falling back to
    ``DEFAULT_PEAK_HOUR_BY_SERIES``. Returns 13 (typical mid-afternoon)
    for unknown series — conservative for cross-bracket purposes since
    a later-than-actual peak just delays the fast-path firing.

    If no DB is available (e.g. tests, or kv_cache unreachable), silently
    falls back to the hardcoded default. Production callers always have
    a DB; this is purely for resilience.
    """
    if conn is None:
        try:
            conn = get_connection()
        except Exception:
            return DEFAULT_PEAK_HOUR_BY_SERIES.get(series, 13)
    cached = kv_get(conn, _KV_PEAK_KEY_PREFIX + series)
    if isinstance(cached, dict) and "peak_hour" in cached:
        try:
            ph = int(cached["peak_hour"])
            if 0 <= ph <= 23:
                return ph
        except (ValueError, TypeError):
            pass
    return DEFAULT_PEAK_HOUR_BY_SERIES.get(series, 13)


def set_peak_hour(
    series: str,
    peak_hour: int,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Persist a derived peak hour to kv_cache. Called by the daily
    refresh task."""
    if not 0 <= peak_hour <= 23:
        raise ValueError(f"peak_hour out of range: {peak_hour}")
    if conn is None:
        conn = get_connection()
    kv_set(conn, _KV_PEAK_KEY_PREFIX + series, {
        "peak_hour": int(peak_hour),
        "updated_at": int(time.time()),
    }, ttl_seconds=_KV_TTL_SECONDS)


# ─── Empirical-refresh path ─────────────────────────────────────────────────


def refresh_all_from_metar(
    conn: Optional[sqlite3.Connection] = None,
    *,
    safety_buffer_hours: int = 2,
    max_lst_hour: int = 23,
    min_days_required: int = 14,
) -> dict[str, dict]:
    """For every series in ``DEFAULT_LST_GATE_BY_SERIES``, derive an
    empirical LST gate from ``weather_metar_hourly_backfill`` and persist
    to kv_cache.

    The gate's lower bound is: ``first_post_peak_hour + safety_buffer_hours``,
    where ``first_post_peak_hour`` is the smallest LST hour at which the
    METAR running max is within 1°F of the daily-high field on ≥80% of
    backfilled days (see ``tools.per_city_source_scorecard``).

    Falls back to the hardcoded default if any of:
      - fewer than ``min_days_required`` days of METAR backfill
      - empirical post-peak hour can't be detected (e.g., daily_high_f
        consistently exceeds hourly METAR by >1°F at all hours, as on NY)

    Returns a per-series dict of {empirical_post_peak, applied_min, source}.
    Caller responsible for logging.
    """
    if conn is None:
        conn = get_connection()

    # Local imports to avoid forcing tools.* dep on bot/learning package users.
    from tools.per_city_source_scorecard import (
        empirical_phase_boundaries, load_metar_observations,
    )

    out: dict[str, dict] = {}
    for series, default_gate in DEFAULT_LST_GATE_BY_SERIES.items():
        # Resolve series → station via a synthetic ticker
        station = station_for_ticker(f"{series}-stub-Bstub")
        if station is None:
            out[series] = {"source": "skip_unknown_station"}
            continue

        metar_rows = load_metar_observations(conn, station.icao)
        n_days = len({r["lst_date"] for r in metar_rows})
        if n_days < min_days_required:
            out[series] = {
                "source": "default_insufficient_data",
                "n_days": n_days,
                "applied_min": default_gate[0],
                "applied_max": default_gate[1],
            }
            continue

        boundaries = empirical_phase_boundaries(metar_rows)
        post_peak = boundaries.get("first_post_peak_hour", -1)
        peak_hour = boundaries.get("peak_hour_median")

        # Always persist peak hour if we got one — used by the METAR
        # post-peak fast-path independent of LST gate quality.
        if isinstance(peak_hour, int) and 0 <= peak_hour <= 23:
            set_peak_hour(series, peak_hour, conn=conn)

        if post_peak is None or post_peak < 0:
            out[series] = {
                "source": "default_no_post_peak_signal",
                "n_days": n_days,
                "peak_hour": peak_hour,
                "applied_min": default_gate[0],
                "applied_max": default_gate[1],
            }
            continue

        derived_min = max(0, min(23, post_peak + safety_buffer_hours))
        derived_max = max_lst_hour
        if derived_min > derived_max:
            # Edge case — fall back rather than persist garbage.
            out[series] = {
                "source": "default_derived_min_too_high",
                "n_days": n_days,
                "post_peak": post_peak,
                "derived_min": derived_min,
                "peak_hour": peak_hour,
                "applied_min": default_gate[0],
                "applied_max": default_gate[1],
            }
            continue

        set_lst_gate(series, derived_min, derived_max, conn=conn)
        out[series] = {
            "source": "empirical",
            "n_days": n_days,
            "post_peak": post_peak,
            "peak_hour": peak_hour,
            "applied_min": derived_min,
            "applied_max": derived_max,
        }

    return out
