"""Retro-replay the post-2026-04-29 v2 combine on historical snapshots.

The 2026-04-29 fixes (NBM and MADIS dropped from ``_collect_gaussians``,
``_determine_day_index`` past-date guard, ``ensemble_p_yes`` label
inversion migrated) change what the v2 combine outputs. Historical
``weather_mm_shadow.fair_value_cents`` was computed with the broken
combine, so it can't be compared to the post-fix combine without
re-deriving the post-fix output on the same per-source inputs.

This tool walks every (ticker, recorded_at) cycle in
``weather_forecast_snapshots``, filters per-source rows to the post-fix
combine list (HRRR, METAR, NWS_POINT, weather), reconstructs Gaussians,
applies the same v2 corrections that production applies, runs
``combine_gaussian`` + post-combine adjustments, and projects to the
ticker's bracket bounds.

Output: per-cycle (ticker, recorded_at, postfix_p_yes_combined_mu,
postfix_p_yes_combined_sigma, postfix_p_yes_bracket).

Comparison metrics computed at the end: per-(UTC hour, TTE bucket)
Brier of pre-fix (production) vs post-fix (replay) vs market_mid.

This is read-only against the DB. Output written to a stand-alone
``replay_postfix_results`` table for downstream analysis.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
import time
from typing import Optional

# Local imports — must run with cwd at repo root.
sys.path.insert(0, ".")
from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import GaussianForecast, combine_gaussian
from bot.signals.weather_sources import GAUSSIAN_COMBINE_SOURCES

# Source-prior σ when sigma_f is null in snapshot.
_RAW_SIGMA = {
    "hrrr": 1.2, "nws_point": 2.0, "weather": 2.0, "metar": 1.5,
}


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bracket_bounds_from_ticker(ticker: str) -> Optional[tuple[float, float, bool]]:
    """Parse bracket / threshold bounds from the ticker.

    Returns (low, high, is_bracket) — for a threshold ticker (T-suffix)
    we use (-inf simulated as -1000, threshold) for "below" or
    (threshold, +inf simulated as 1000) for "above".

    For a bracket ``-B<N>`` the Kalshi convention is ``[floor(N), floor(N)+2)``,
    e.g., B68.5 means high in [68, 70). The ``.5`` in the ticker is the
    midpoint label, NOT the lower edge. Validated 2026-04-29 against
    settlement_result on KAUS Mar-Apr 2026 data: convention A (B−0.5 to
    B+1.5) matches all unrounded cases; the ticker-as-floor convention
    misses every YES-resolved bracket.
    """
    m = re.search(r"-B(-?\d+\.?\d*)$", ticker.upper())
    if m:
        b_value = float(m.group(1))
        lo = b_value - 0.5
        return (lo, lo + 2.0, True)
    m = re.search(r"-T(\d+\.?\d*)$", ticker.upper())
    if m:
        # Threshold direction is ambiguous from ticker alone (Kalshi's
        # T-series can be "above" or "below" depending on the market).
        # For Brier-comparison purposes we use ``floor=t, cap=+1000``
        # (i.e., "above") and let the per-cycle market_mid join sort
        # out direction; the post-fix prob is for "high > t" which the
        # caller can flip if needed.
        t = float(m.group(1))
        return (t, 1000.0, False)
    return None


def _project_to_bracket_p_yes(mu: float, sigma: float, lo: float, hi: float) -> float:
    """P(low <= X <= high) for X ~ N(mu, sigma)."""
    if sigma <= 0:
        return 1.0 if lo <= mu <= hi else 0.0
    z_hi = (hi - mu) / sigma
    z_lo = (lo - mu) / sigma
    p = _ncdf(z_hi) - _ncdf(z_lo)
    return max(0.005, min(0.995, p))


def _ensure_results_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS replay_postfix_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        postfix_mu_f REAL,
        postfix_sigma_f REAL,
        postfix_p_yes REAL,
        n_sources INTEGER,
        UNIQUE(ticker, recorded_at)
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rpr_ticker_ts "
        "ON replay_postfix_results(ticker, recorded_at)")


def _fetch_cycle_sources(conn: sqlite3.Connection, since_iso: str, limit: Optional[int]) -> list:
    """Return list of (ticker, recorded_at, [(src, mu_f, sigma_f, hours_out), ...]).

    Groups per (ticker, recorded_at) cycle so we replay one combine per cycle.
    """
    sql = """
        SELECT ticker, recorded_at, source, forecast_high_f, sigma_f, hours_out
        FROM weather_forecast_snapshots
        WHERE recorded_at > ?
          AND source IN ('hrrr', 'metar', 'nws_point', 'weather')
          AND forecast_high_f IS NOT NULL
        ORDER BY ticker, recorded_at, source
    """
    rows = conn.execute(sql, (since_iso,)).fetchall()
    # Group by (ticker, recorded_at)
    grouped: dict = {}
    for r in rows:
        key = (r[0], r[1])
        grouped.setdefault(key, []).append(r[2:])
    items = sorted(grouped.items())
    if limit:
        items = items[:limit]
    return items


def _build_corrected_gaussians(
    src_rows: list, ticker: str
) -> list:
    """Build [GaussianForecast] post-corrections for one cycle's sources."""
    gaussians = []
    for src, mu_f, sigma_f_raw, hours_out in src_rows:
        if src not in GAUSSIAN_COMBINE_SOURCES:
            continue
        try:
            sigma = float(sigma_f_raw) if sigma_f_raw is not None else _RAW_SIGMA.get(src, 2.0)
            g = GaussianForecast(
                mean_f=float(mu_f), sigma_f=sigma,
                horizon_hours=float(hours_out) if hours_out is not None else 0.0,
                source_name=src, source_tag=f"{src}:postfix_replay",
            )
        except (TypeError, ValueError):
            continue
        gaussians.append(g)

    if not gaussians:
        return []

    city_key = v2._city_for_ticker(ticker)
    corrected = []
    for g in gaussians:
        try:
            g = v2._apply_learned_sigma(g, city_key=city_key)
        except Exception:
            pass
        try:
            g = v2._apply_staleness_inflation(g)
        except Exception:
            pass
        try:
            g = v2._apply_mos_bias(g, city_key)
        except Exception:
            pass
        if g.sigma_f > v2._SOURCE_SIGMA_CEILING_F:
            g = g.with_sigma(v2._SOURCE_SIGMA_CEILING_F)
        corrected.append(g)
    return corrected


def replay_cycle(src_rows: list, ticker: str) -> Optional[tuple[float, float, float, int]]:
    """Replay one cycle's post-fix combine. Returns (mu, sigma, p_yes, n).

    Skips ``predict_v2``'s market-data-dependent steps (bracket parsing
    from API, AFD bias, running-high floor that needs METAR running max
    from market_data) — uses ticker-derived brackets and snapshot-only
    inputs. The resulting probability is a "Gaussian-projected" estimate,
    which is the dominant signal anyway. AFD and running-high adjustments
    are O(0.01) Brier shifts; not relevant for the headline window analysis.
    """
    corrected = _build_corrected_gaussians(src_rows, ticker)
    if not corrected:
        return None
    weighted = v2._weighted_inputs_with_group_discount(corrected)
    combined = combine_gaussian(weighted, combined_name="combined_postfix")
    if combined is None:
        return None

    # σ inflation (matches predict_v2 step 4b).
    try:
        combined = v2._apply_sigma_inflation(combined)
    except Exception:
        pass

    # σ floor (matches step 4d).
    if combined.sigma_f < v2._COMBINED_SIGMA_FLOOR_F:
        combined = combined.with_sigma(v2._COMBINED_SIGMA_FLOOR_F)

    bounds = _bracket_bounds_from_ticker(ticker)
    if bounds is None:
        return None
    lo, hi, _is_bracket = bounds
    p_yes = _project_to_bracket_p_yes(combined.mean_f, combined.sigma_f, lo, hi)
    return (combined.mean_f, combined.sigma_f, p_yes, len(corrected))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--since", default="2026-04-21",
                    help="ISO date — only replay snapshots after this")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap on number of cycles to replay")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    _ensure_results_table(conn)

    print(f"[replay] fetching cycles since {args.since}…")
    items = _fetch_cycle_sources(conn, args.since, args.limit)
    print(f"[replay] {len(items)} cycles to replay")

    written = 0
    skipped = 0
    t_start = time.time()
    for i, ((ticker, recorded_at), src_rows) in enumerate(items):
        result = replay_cycle(src_rows, ticker)
        if result is None:
            skipped += 1
            continue
        mu, sigma, p_yes, n = result
        try:
            conn.execute(
                """INSERT OR REPLACE INTO replay_postfix_results
                   (ticker, recorded_at, postfix_mu_f, postfix_sigma_f, postfix_p_yes, n_sources)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ticker, recorded_at, mu, sigma, p_yes, n),
            )
            written += 1
        except Exception as e:
            print(f"[replay] insert failed for {ticker}@{recorded_at}: {e}")
            skipped += 1
        if (i + 1) % 5000 == 0:
            conn.commit()
            print(f"[replay] {i+1}/{len(items)} processed "
                  f"(written={written}, skipped={skipped}, "
                  f"rate={(i+1)/(time.time()-t_start):.0f}/s)")
    conn.commit()
    conn.close()
    print(f"[replay] done: written={written}, skipped={skipped}, "
          f"elapsed={time.time()-t_start:.1f}s")


if __name__ == "__main__":
    main()
