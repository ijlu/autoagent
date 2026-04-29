"""Source state machine — Phase B.2 dynamic source management.

Approved 2026-04-29 by Josh after debate of "auto-prune only" vs full
state machine. Conclusion: build the state machine now because it's
permanent architecture and the 2-4 day delta is small. Manual review
should never be the default for normal operation.

The four states::

    shadow         snapshotted, excluded from combine.
    probationary   included with σ inflated × 1.3 (capped weight).
    active         full weight.
    demoted        excluded from combine; auto-shadowed after cooldown.

Transitions are evaluated daily by ``evaluate_state_transitions`` against
``weather_source_state``. Per-cycle code in ``_collect_gaussians`` reads
the state via ``get_source_state`` to decide inclusion + weighting.

Transition thresholds + cooldowns are tuned to prevent flapping:
  - shadow → probationary requires n_settled ≥ 50 + reasonable skill
  - probationary → active requires another 50 settled rows + non-regression
  - active → demoted requires chronic degradation (5+ days) OR σ blow-up
  - demoted → shadow waits 7 days, then allows clean re-trial

All state changes append a ``last_state_change_iso`` and emit a daemon
log line so operators see the dynamic system at work.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


# ── States ────────────────────────────────────────────────────────────────
class SourceState:
    SHADOW = "shadow"
    PROBATIONARY = "probationary"
    ACTIVE = "active"
    DEMOTED = "demoted"


_VALID_STATES = frozenset({
    SourceState.SHADOW, SourceState.PROBATIONARY,
    SourceState.ACTIVE, SourceState.DEMOTED,
})


# ── Thresholds (tuned 2026-04-29) ────────────────────────────────────────
# Shadow → Probationary
SHADOW_TO_PROBATIONARY_MIN_N = 50
SHADOW_TO_PROBATIONARY_MAX_MAE_RATIO = 1.5  # vs baseline pooled MAE ~1.5°F
SHADOW_TO_PROBATIONARY_MAX_INDEP = 0.7  # too correlated → no value-add

# Probationary → Active
PROBATIONARY_TO_ACTIVE_MIN_N = 50  # additional rows beyond shadow
PROBATIONARY_TO_ACTIVE_MAX_BRIER_REGRESSION = 0.005

# Active → Demoted (chronic or catastrophic)
DEMOTION_SIGMA_BLOW_UP = 5.0  # σ this wide = effectively no signal
DEMOTION_BRIER_DEGRADATION_RATIO = 1.3  # 7d > 30d × 1.3
DEMOTION_DEGRADATION_DAYS = 5

# Probationary → Shadow (rolled back to safe state if performance bad)
# Same triggers as demotion but from probationary.

# Demoted → Shadow (cooldown then clean re-trial)
DEMOTION_COOLDOWN_DAYS = 7

# Combine inclusion
PROBATIONARY_SIGMA_INFLATION = 1.3  # cap weight while on trial


@dataclass
class SourceStateRow:
    source: str
    city: str
    state: str
    n_settled: int = 0
    mae_7d: Optional[float] = None
    mae_30d: Optional[float] = None
    brier_7d: Optional[float] = None
    brier_30d: Optional[float] = None
    sigma_fitted: Optional[float] = None
    bias_fitted: Optional[float] = None
    indep_vs_combine: Optional[float] = None
    last_state_change_iso: Optional[str] = None
    last_evaluated_iso: Optional[str] = None
    notes: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Read API ──────────────────────────────────────────────────────────────
def get_source_state(
    conn: sqlite3.Connection, source: str, city: str = "pooled"
) -> str:
    """Return the current state. Defaults to ``active`` for unknown
    (source, city) — sources without an explicit state row are
    assumed to be production-trusted (HRRR, METAR, NWS Point, weather
    pre-2026-04-29 — no state machine treatment yet).

    NEW sources should be added with an explicit PROBATIONARY row via
    the pre-seed script. SHADOW / DEMOTED states require an explicit row
    too — there is no "default to shadow" semantics.

    Falls back to ``pooled`` city if a per-city row isn't present.
    """
    row = conn.execute(
        "SELECT state FROM weather_source_state WHERE source=? AND city=?",
        (source, city),
    ).fetchone()
    if row is not None:
        return row[0]
    if city != "pooled":
        row = conn.execute(
            "SELECT state FROM weather_source_state WHERE source=? AND city='pooled'",
            (source,),
        ).fetchone()
        if row is not None:
            return row[0]
    return SourceState.ACTIVE


def get_full_row(
    conn: sqlite3.Connection, source: str, city: str = "pooled"
) -> Optional[SourceStateRow]:
    row = conn.execute(
        """SELECT source, city, state, n_settled, mae_7d, mae_30d,
                  brier_7d, brier_30d, sigma_fitted, bias_fitted,
                  indep_vs_combine, last_state_change_iso, last_evaluated_iso, notes
             FROM weather_source_state WHERE source=? AND city=?""",
        (source, city),
    ).fetchone()
    if row is None:
        return None
    return SourceStateRow(*row)


# ── Write API ─────────────────────────────────────────────────────────────
def upsert_state(
    conn: sqlite3.Connection,
    *,
    source: str,
    city: str,
    state: str,
    n_settled: Optional[int] = None,
    mae_7d: Optional[float] = None,
    mae_30d: Optional[float] = None,
    brier_7d: Optional[float] = None,
    brier_30d: Optional[float] = None,
    sigma_fitted: Optional[float] = None,
    bias_fitted: Optional[float] = None,
    indep_vs_combine: Optional[float] = None,
    notes: Optional[str] = None,
    state_changed: bool = False,
) -> None:
    """Upsert one source × city row. ``state_changed=True`` updates
    the ``last_state_change_iso`` timestamp; otherwise leaves it alone."""
    if state not in _VALID_STATES:
        raise ValueError(f"unknown state {state!r}")

    now = _now_iso()
    existing = conn.execute(
        "SELECT last_state_change_iso FROM weather_source_state "
        "WHERE source=? AND city=?",
        (source, city),
    ).fetchone()
    if existing is None:
        last_change = now if state_changed else None
        conn.execute(
            """INSERT INTO weather_source_state
               (source, city, state, n_settled, mae_7d, mae_30d,
                brier_7d, brier_30d, sigma_fitted, bias_fitted,
                indep_vs_combine, last_state_change_iso,
                last_evaluated_iso, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, city, state, n_settled, mae_7d, mae_30d,
             brier_7d, brier_30d, sigma_fitted, bias_fitted,
             indep_vs_combine, last_change, now, notes),
        )
    else:
        last_change = now if state_changed else existing[0]
        conn.execute(
            """UPDATE weather_source_state
                  SET state = ?, n_settled = COALESCE(?, n_settled),
                      mae_7d = ?, mae_30d = ?, brier_7d = ?, brier_30d = ?,
                      sigma_fitted = ?, bias_fitted = ?,
                      indep_vs_combine = ?, last_state_change_iso = ?,
                      last_evaluated_iso = ?, notes = COALESCE(?, notes)
                WHERE source=? AND city=?""",
            (state, n_settled, mae_7d, mae_30d, brier_7d, brier_30d,
             sigma_fitted, bias_fitted, indep_vs_combine, last_change,
             now, notes, source, city),
        )


# ── Transition rules ──────────────────────────────────────────────────────
def _decide_next_state(
    current: SourceStateRow,
    *,
    baseline_mae: float = 1.5,
) -> tuple[str, str]:
    """Pure function: given a row's metrics, return (new_state, reason).

    Pure-function shape so the test suite can pin every transition with
    fixture data alone, no DB needed.
    """
    state = current.state
    n = current.n_settled or 0
    sigma = current.sigma_fitted
    mae_30d = current.mae_30d
    mae_7d = current.mae_7d
    brier_7d = current.brier_7d
    brier_30d = current.brier_30d
    indep = current.indep_vs_combine

    # ── Catastrophic blow-up: any state → DEMOTED ────────────────────────
    if sigma is not None and sigma > DEMOTION_SIGMA_BLOW_UP and state != SourceState.SHADOW:
        return (SourceState.DEMOTED, f"sigma={sigma:.2f} > {DEMOTION_SIGMA_BLOW_UP}")

    # ── ACTIVE → DEMOTED on chronic degradation ──────────────────────────
    if state == SourceState.ACTIVE:
        if (mae_7d is not None and mae_30d is not None
                and mae_7d > mae_30d * DEMOTION_BRIER_DEGRADATION_RATIO):
            return (SourceState.DEMOTED,
                    f"mae_7d={mae_7d:.2f} > mae_30d={mae_30d:.2f} × "
                    f"{DEMOTION_BRIER_DEGRADATION_RATIO}")
        if (brier_7d is not None and brier_30d is not None
                and brier_7d > brier_30d * DEMOTION_BRIER_DEGRADATION_RATIO):
            return (SourceState.DEMOTED,
                    f"brier_7d={brier_7d:.3f} > brier_30d={brier_30d:.3f} × "
                    f"{DEMOTION_BRIER_DEGRADATION_RATIO}")

    # ── PROBATIONARY → DEMOTED on same triggers (rolled back, not just shadow) ──
    if state == SourceState.PROBATIONARY:
        if (mae_7d is not None and mae_30d is not None
                and mae_7d > mae_30d * DEMOTION_BRIER_DEGRADATION_RATIO):
            return (SourceState.SHADOW,  # softer than active — back to shadow not demoted
                    f"probationary mae_7d={mae_7d:.2f} regressed; back to shadow")

    # ── DEMOTED → SHADOW after cooldown ──────────────────────────────────
    if state == SourceState.DEMOTED:
        if current.last_state_change_iso:
            try:
                changed = datetime.fromisoformat(
                    current.last_state_change_iso.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - changed).total_seconds() / 86400
                if age_days >= DEMOTION_COOLDOWN_DAYS:
                    return (SourceState.SHADOW,
                            f"demoted cooldown {age_days:.1f}d ≥ "
                            f"{DEMOTION_COOLDOWN_DAYS}d; re-trial")
            except (ValueError, TypeError):
                pass
        return (state, "demoted cooldown not elapsed")

    # ── SHADOW → PROBATIONARY ─────────────────────────────────────────────
    if state == SourceState.SHADOW:
        if (n >= SHADOW_TO_PROBATIONARY_MIN_N
                and mae_30d is not None
                and mae_30d <= baseline_mae * SHADOW_TO_PROBATIONARY_MAX_MAE_RATIO
                and (indep is None
                     or abs(indep) <= SHADOW_TO_PROBATIONARY_MAX_INDEP)):
            return (SourceState.PROBATIONARY,
                    f"shadow→probationary n={n} mae_30d={mae_30d:.2f} indep={indep}")
        return (state, "shadow gate not met")

    # ── PROBATIONARY → ACTIVE ─────────────────────────────────────────────
    if state == SourceState.PROBATIONARY:
        # Need 100 total = 50 shadow + 50 probationary
        if (n >= SHADOW_TO_PROBATIONARY_MIN_N + PROBATIONARY_TO_ACTIVE_MIN_N
                and brier_30d is not None
                and (brier_7d is None
                     or brier_7d <= brier_30d
                     + PROBATIONARY_TO_ACTIVE_MAX_BRIER_REGRESSION)):
            return (SourceState.ACTIVE,
                    f"probationary→active n={n} brier_30d={brier_30d:.3f}")
        return (state, "probationary gate not met")

    return (state, "no transition")


# ── Metric refresh ────────────────────────────────────────────────────────
# Daily task pulls fresh σ + bias from kv_cache (where the existing
# fitters already wrote them) and computes 7d/30d MAE from
# weather_forecast_snapshots × weather_metar_hourly_backfill. Runs right
# before the transition evaluator so transitions act on fresh data.

_ICAO_TO_CITY = {
    "KNYC": "nyc", "KMDW": "chicago", "KMIA": "miami",
    "KAUS": "austin", "KLAX": "los_angeles", "KDEN": "denver",
}


def _read_kv_cache_value(
    conn: sqlite3.Connection, key: str, field: str
) -> Optional[float]:
    """Read a numeric field from a kv_cache row's JSON value."""
    import json
    row = conn.execute(
        "SELECT value FROM kv_cache WHERE key = ?", (key,)
    ).fetchone()
    if row is None or not row[0]:
        return None
    try:
        d = json.loads(row[0])
        v = d.get(field) if isinstance(d, dict) else None
        return float(v) if isinstance(v, (int, float)) else None
    except (ValueError, TypeError):
        return None


def _compute_mae_window(
    conn: sqlite3.Connection, source: str, city: str, days: int,
) -> tuple[Optional[float], int]:
    """MAE of (source.forecast - daily_high) over the last ``days`` days.

    Joins ``weather_forecast_snapshots`` (per-cycle source predictions) to
    ``weather_metar_hourly_backfill.daily_high_f`` (ground truth, CF6
    TMAX). Returns (mae_f, n) — n=0 means insufficient data.

    For per-city queries we map city → station via ICAO_TO_CITY (reverse
    of the production registry). For ``city='pooled'`` we average across
    all stations.
    """
    icao_map = {v: k for k, v in _ICAO_TO_CITY.items()}
    if city == "pooled":
        # Aggregate across all stations for this source
        rows = conn.execute(
            f"""SELECT s.forecast_high_f, b.daily_high_f
                  FROM weather_forecast_snapshots s
                  JOIN weather_metar_hourly_backfill b
                    ON b.station IN ({",".join(["?"] * len(icao_map))})
                   AND b.lst_date = SUBSTR(s.recorded_at, 1, 10)
                   AND b.daily_high_f IS NOT NULL
                 WHERE s.source = ?
                   AND s.forecast_high_f IS NOT NULL
                   AND s.recorded_at > datetime('now', ? || ' days')
                 GROUP BY s.id""",
            tuple(icao_map.values()) + (source, f"-{days}"),
        ).fetchall()
    else:
        icao = icao_map.get(city)
        if icao is None:
            return (None, 0)
        rows = conn.execute(
            """SELECT s.forecast_high_f, b.daily_high_f
                  FROM weather_forecast_snapshots s
                  JOIN weather_metar_hourly_backfill b
                    ON b.station = ?
                   AND b.lst_date = SUBSTR(s.recorded_at, 1, 10)
                   AND b.daily_high_f IS NOT NULL
                 WHERE s.source = ?
                   AND s.forecast_high_f IS NOT NULL
                   AND s.recorded_at > datetime('now', ? || ' days')""",
            (icao, source, f"-{days}"),
        ).fetchall()

    if not rows:
        return (None, 0)
    errs = [abs(float(f) - float(a)) for f, a in rows
            if f is not None and a is not None]
    if not errs:
        return (None, 0)
    return (sum(errs) / len(errs), len(errs))


def _compute_sigma_window(
    conn: sqlite3.Connection, source: str, city: str, days: int,
) -> Optional[float]:
    """Std-dev of (forecast - actual) residuals over the last ``days``
    days. This is what the state machine wants for ``sigma_fitted`` —
    it tracks the source's actual error spread, independent of the kv_cache
    pre-seed (which may not be in the canonical key shape we'd need to
    query without enumerating buckets).
    """
    icao_map = {v: k for k, v in _ICAO_TO_CITY.items()}
    if city == "pooled":
        rows = conn.execute(
            f"""SELECT s.forecast_high_f, b.daily_high_f
                  FROM weather_forecast_snapshots s
                  JOIN weather_metar_hourly_backfill b
                    ON b.station IN ({",".join(["?"] * len(icao_map))})
                   AND b.lst_date = SUBSTR(s.recorded_at, 1, 10)
                   AND b.daily_high_f IS NOT NULL
                 WHERE s.source = ?
                   AND s.forecast_high_f IS NOT NULL
                   AND s.recorded_at > datetime('now', ? || ' days')
                 GROUP BY s.id""",
            tuple(icao_map.values()) + (source, f"-{days}"),
        ).fetchall()
    else:
        icao = icao_map.get(city)
        if icao is None:
            return None
        rows = conn.execute(
            """SELECT s.forecast_high_f, b.daily_high_f
                  FROM weather_forecast_snapshots s
                  JOIN weather_metar_hourly_backfill b
                    ON b.station = ?
                   AND b.lst_date = SUBSTR(s.recorded_at, 1, 10)
                   AND b.daily_high_f IS NOT NULL
                 WHERE s.source = ?
                   AND s.forecast_high_f IS NOT NULL
                   AND s.recorded_at > datetime('now', ? || ' days')""",
            (icao, source, f"-{days}"),
        ).fetchall()
    residuals = [float(f) - float(a) for f, a in rows
                 if f is not None and a is not None]
    if len(residuals) < 5:
        return None
    mean = sum(residuals) / len(residuals)
    var = sum((r - mean) ** 2 for r in residuals) / len(residuals)
    return var ** 0.5


def refresh_metrics(conn: sqlite3.Connection) -> int:
    """For each source × city in weather_source_state, refresh:
      - sigma_fitted: std-dev of (forecast - actual) residuals over 30d
      - bias_fitted: from kv_cache (what `_apply_mos_bias` uses)
      - mae_7d / mae_30d / n_settled: computed from settlements
    Returns number of rows updated.

    σ is computed directly here (not pulled from kv_cache) because:
    1. The kv_cache key shape is per-bucket; the state machine only
       cares about a pooled-across-bucket value
    2. Computing it from the same data we already JOIN for MAE is
       cheaper than enumerating bucket keys + pooling
    3. Decoupled — the state machine works even before learned σ has
       been written to kv_cache for this source
    """
    rows = conn.execute(
        "SELECT source, city, state FROM weather_source_state"
    ).fetchall()
    updated = 0
    for source, city, state in rows:
        # Bias: per-(source, city) key — kv_cache is the canonical home
        bias = _read_kv_cache_value(
            conn, f"weather_mos_bias_{source}_{city}", "bias")
        # σ: from residuals over 30d
        sigma = _compute_sigma_window(conn, source, city, 30)
        mae_7d, n_7 = _compute_mae_window(conn, source, city, 7)
        mae_30d, n_30 = _compute_mae_window(conn, source, city, 30)
        upsert_state(
            conn, source=source, city=city, state=state,
            n_settled=n_30,
            mae_7d=mae_7d, mae_30d=mae_30d,
            sigma_fitted=sigma, bias_fitted=bias,
            state_changed=False,
        )
        updated += 1
    return updated


# ── Daily evaluator ───────────────────────────────────────────────────────
def evaluate_state_transitions(
    conn: sqlite3.Connection, *, baseline_mae: float = 1.5
) -> list[tuple[str, str, str, str, str]]:
    """Walk every source × city row, compute metrics, apply transition
    rules. Returns the list of transitions that fired:
    (source, city, old_state, new_state, reason).

    Caller is the daemon's daily ``source_state_evaluator`` task. It is
    responsible for refreshing the metrics columns BEFORE calling this
    (e.g., recomputing mae_7d / brier_30d from fresh settlements). This
    function is the pure transition decision; metric update is separate.
    """
    rows = conn.execute(
        """SELECT source, city, state, n_settled, mae_7d, mae_30d,
                  brier_7d, brier_30d, sigma_fitted, bias_fitted,
                  indep_vs_combine, last_state_change_iso,
                  last_evaluated_iso, notes
             FROM weather_source_state"""
    ).fetchall()

    transitions = []
    for r in rows:
        cur = SourceStateRow(*r)
        new_state, reason = _decide_next_state(cur, baseline_mae=baseline_mae)
        if new_state != cur.state:
            upsert_state(
                conn,
                source=cur.source, city=cur.city,
                state=new_state, n_settled=cur.n_settled,
                mae_7d=cur.mae_7d, mae_30d=cur.mae_30d,
                brier_7d=cur.brier_7d, brier_30d=cur.brier_30d,
                sigma_fitted=cur.sigma_fitted, bias_fitted=cur.bias_fitted,
                indep_vs_combine=cur.indep_vs_combine,
                state_changed=True,
                notes=f"transition: {reason}",
            )
            transitions.append(
                (cur.source, cur.city, cur.state, new_state, reason))
    return transitions


# ── Combine integration helper ───────────────────────────────────────────
def is_source_in_combine(state: str) -> bool:
    """True iff the source should contribute to the precision-weighted combine.

    ACTIVE and PROBATIONARY are in the combine (probationary with capped
    weight via PROBATIONARY_SIGMA_INFLATION). SHADOW + DEMOTED are out.
    """
    return state in (SourceState.ACTIVE, SourceState.PROBATIONARY)


def sigma_inflation_for_state(state: str) -> float:
    """Multiplier applied to a probationary source's σ (caps its weight).

    1.0 for active/shadow/demoted (no inflation; shadow/demoted are
    excluded from combine anyway). 1.3 for probationary — limits the
    influence of a source still on trial."""
    if state == SourceState.PROBATIONARY:
        return PROBATIONARY_SIGMA_INFLATION
    return 1.0
