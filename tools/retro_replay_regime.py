"""Phase A.6 — retro-replay validation: would regime-conditional METAR
residual σ have moved Brier on the existing 143 settled tickers?

This is the killer validation for the regime hypothesis. The earlier
stratification analysis showed σ-reduction of 14-40% in late-day cells
across most cities. But σ-reduction is academic until we measure whether
it would have moved actual probabilities far enough to change Brier on
the cases the model loses today.

Method:
  1. Load 30-day regime CSVs (already pulled by regime_features_pull).
  2. Fit pooled and regime-conditional residual σ per (station, lst_hour
     [, regime]). Both fits come from the SAME data — controls for any
     production-fitter idiosyncrasy.
  3. For each settled ticker in the diagnostic, identify the regime at
     the snapshot's recorded_at time from the CSV.
  4. Replay predict_v2 twice with patched ``_sigma_for_hours``:
       (a) baseline = CSV-pooled σ (control — replaces production kv)
       (b) treatment = CSV-regime σ with hierarchical fallback
  5. Compute Brier(baseline) vs Brier(treatment) vs Brier(market).
  6. Slice by horizon-bucket and bracket-edge-distance to localize lift.

The hierarchical fallback walks (city, hour, regime) → (city, regime) →
(city, hour) → pooled — same as the proposed production design. Cells
with n < ``_MIN_FIT_N`` fall through to the next tier.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import pstdev
from typing import Optional

# Reuse stratifier helpers + production stations + replay infrastructure.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import DB_PATH  # noqa: E402
from bot.db import init_db  # noqa: E402
from bot.daemon.stations import STATION_BY_SERIES  # noqa: E402
from bot.signals import weather_ensemble_v2 as v2  # noqa: E402
from bot.signals.sources import metar_observations as metar  # noqa: E402
from tools.diagnose_v2_gap import _settle_unix_from_ticker  # noqa: E402
from tools.backtest_v2_replay import _fetch_market, _replay_predict_v2  # noqa: E402
from tools.regime_stratify_residuals import (  # noqa: E402
    _wind_bucket, _sky_bucket, _dewpoint_bucket, _load_csv,
)


_MIN_FIT_N = 5  # min cell samples to fit σ; fallback otherwise
# σ floor — sensor noise + mid-hour cadence puts a physical lower bound.
# Don't let the regime fit produce wildly tight σ that breaks the projection.
_SIGMA_FLOOR_F = 0.3


@dataclass
class _RegimeFit:
    """All regime σ tiers for one (station, hour). Caller picks the
    deepest tier with n >= _MIN_FIT_N at lookup time."""
    pooled_sigma: float                   # (station, hour)
    pooled_n: int
    by_regime: dict[str, tuple[float, int]]    # (station, hour, regime) → (σ, n)


def _ddep_of(row: dict) -> Optional[float]:
    return row.get("ddep")


def _regime_label(row: dict, taxonomy: str) -> str:
    """Compute the regime bucket for the given taxonomy.

    Pinned axes are ``wind``, ``sky``, ``ddep``, and pairs.
    Returns ``"unknown"`` when any required field is missing — caller
    treats unknown as "no regime detected, fall back".
    """
    if taxonomy == "wind":
        return _wind_bucket(row.get("drct"))
    if taxonomy == "sky":
        return _sky_bucket(row.get("skyc1") or "")
    if taxonomy == "ddep":
        return _dewpoint_bucket(_ddep_of(row))
    if taxonomy == "wind+sky":
        w = _wind_bucket(row.get("drct"))
        s = _sky_bucket(row.get("skyc1") or "")
        if w == "unknown" or s == "unknown":
            return "unknown"
        return f"{w}|{s}"
    if taxonomy == "wind+ddep":
        w = _wind_bucket(row.get("drct"))
        d = _dewpoint_bucket(_ddep_of(row))
        if w == "unknown" or d == "unknown":
            return "unknown"
        return f"{w}|{d}"
    raise ValueError(f"unknown taxonomy: {taxonomy}")


# Per-city taxonomy — picked from regime_feasibility's late-day winner.
# KLAX is left as "wind+sky" but the σ-reduction is small; the fit will
# usually fall back to (city, hour) pooled, which is the right behavior.
_CITY_TAXONOMY: dict[str, str] = {
    "KAUS": "wind+ddep",
    "KDEN": "wind+sky",
    "KLAX": "wind+sky",
    "KMDW": "wind+sky",
    "KMIA": "wind+sky",
    "KNYC": "wind",
}


def _fit_regime(
    rows: list[dict], station: str,
) -> dict[int, _RegimeFit]:
    """For one station, fit pooled + per-regime σ per LST hour.

    Returns ``{lst_hour: _RegimeFit}``. Hours with fewer than
    ``_MIN_FIT_N`` total samples are still emitted with pooled_sigma but
    no regime cells (the call site falls back).
    """
    taxonomy = _CITY_TAXONOMY.get(station, "wind+sky")
    by_hour: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_hour[r["lst_hour"]].append(r)

    out: dict[int, _RegimeFit] = {}
    for hr, cell in by_hour.items():
        # Skip rows without ground-truth residual (today's day, CF6 not
        # published yet) — those are kept for regime lookup but not
        # for fitting σ.
        cell_fittable = [r for r in cell if r["residual_peak_f"] is not None]
        residuals = [r["residual_peak_f"] for r in cell_fittable]
        pooled = max(_SIGMA_FLOOR_F, pstdev(residuals)) if len(residuals) >= 2 else float("nan")
        by_regime: dict[str, tuple[float, int]] = {}
        regime_buckets: dict[str, list[float]] = defaultdict(list)
        for r in cell_fittable:
            label = _regime_label(r, taxonomy)
            if label == "unknown":
                continue
            regime_buckets[label].append(r["residual_peak_f"])
        for label, vals in regime_buckets.items():
            if len(vals) < _MIN_FIT_N:
                continue
            sig = max(_SIGMA_FLOOR_F, pstdev(vals))
            by_regime[label] = (sig, len(vals))
        out[hr] = _RegimeFit(
            pooled_sigma=pooled, pooled_n=len(residuals),
            by_regime=by_regime,
        )
    return out


def _build_csv_rows_index(
    csv_dir: Path, stations: list[str],
) -> dict[str, dict[tuple[str, int], dict]]:
    """For each station, load all rows and index by (lst_date, lst_hour)
    so we can look up the regime at a specific snapshot time.
    """
    out: dict[str, dict[tuple[str, int], dict]] = {}
    for st in stations:
        path = csv_dir / f"{st}.csv"
        if not path.exists():
            print(f"[warn] {path} missing — {st} will fall back to pooled")
            out[st] = {}
            continue
        rows = _load_csv(path)
        idx: dict[tuple[str, int], dict] = {}
        for r in rows:
            idx[(r["lst_date"], r["lst_hour"])] = r
        out[st] = idx
    return out


def _lst_hour_at(
    station_icao: str, recorded_at_iso: str,
) -> Optional[tuple[str, int]]:
    """Convert a UTC recorded_at to the station's (lst_date, lst_hour).
    Returns None if parse fails.
    """
    ws = None
    for key, w in STATION_BY_SERIES.items():  # noqa: B007
        if w.icao == station_icao:
            ws = w
            break
    if ws is None:
        return None
    try:
        # ISO strings from the snapshot writer have ±00:00 offset
        dt_utc = datetime.fromisoformat(recorded_at_iso)
    except ValueError:
        return None
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    from datetime import timedelta as _td
    lst = dt_utc.astimezone(timezone(_td(hours=ws.lst_offset)))
    return (lst.date().isoformat(), lst.hour)


def _station_for_ticker(ticker: str) -> Optional[str]:
    series = ticker.split("-", 1)[0].upper()
    ws = STATION_BY_SERIES.get(series)
    return ws.icao if ws else None


def _resolve_sigma(
    fits: dict[int, _RegimeFit],
    lst_hour: int,
    regime_label: Optional[str],
    *,
    use_regime: bool,
) -> tuple[float, str]:
    """Hierarchical lookup for the σ to use, returning (σ, tier_used).
    """
    fit = fits.get(lst_hour)
    if fit is None:
        return (1.0, "no_fit")
    if use_regime and regime_label and regime_label != "unknown":
        cell = fit.by_regime.get(regime_label)
        if cell is not None and cell[1] >= _MIN_FIT_N:
            return (cell[0], "regime_hour")
    if not math.isnan(fit.pooled_sigma) and fit.pooled_n >= _MIN_FIT_N:
        return (fit.pooled_sigma, "pooled_hour")
    # Fallback to a coarse default — production schedule kicks in as
    # last resort. _sigma_for_hours's no-station path uses a horizon-
    # based schedule; pick a wide-but-not-extreme σ so the projection
    # doesn't blow up.
    return (1.0, "default")


def _bucket_horizon(h_out: float) -> str:
    if h_out < 0:
        return "post_settle"
    if h_out < 6:
        return "0-6h"
    if h_out < 12:
        return "6-12h"
    if h_out < 24:
        return "12-24h"
    return "24h+"


def _project_brackets(
    market: dict, mu: float, sigma: float,
) -> Optional[float]:
    """Project (μ, σ) to a probability for the ticker's outcome side.

    Reuses ``v2._parse_market_for_projection`` and computes the
    Gaussian-CDF-based projection that ``predict_v2`` does internally.
    """
    proj = v2._parse_market_for_projection(market.get("ticker", ""), market)
    if proj is None:
        return None
    is_bracket, threshold_f, is_above, lo_f, hi_f = proj
    if sigma <= 0:
        sigma = _SIGMA_FLOOR_F

    def _ncdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))

    if is_bracket:
        if lo_f is None or hi_f is None:
            return None
        return max(0.02, min(0.98, _ncdf(hi_f) - _ncdf(lo_f)))
    # Threshold (T-suffix): probability of being ABOVE
    if threshold_f is None:
        return None
    p_above = 1.0 - _ncdf(threshold_f)
    return max(0.02, min(0.98, p_above if is_above else 1.0 - p_above))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="/home/kalshi/autoagent/reports/regime_features")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap rows for fast iteration (0 = all)")
    args = ap.parse_args(argv)

    csv_dir = Path(args.csv_dir)

    # ── Fit per-station σ tables from CSVs ─────────────────────────────
    print(f"[fit] loading CSVs from {csv_dir}")
    stations = ["KAUS", "KDEN", "KLAX", "KMDW", "KMIA", "KNYC"]
    fits: dict[str, dict[int, _RegimeFit]] = {}
    for st in stations:
        path = csv_dir / f"{st}.csv"
        if not path.exists():
            print(f"  {st}: CSV missing, skipping")
            continue
        rows = _load_csv(path)
        fits[st] = _fit_regime(rows, st)
        n_fit = sum(1 for hr in fits[st] if fits[st][hr].pooled_n >= _MIN_FIT_N)
        n_regime = sum(len(fits[st][hr].by_regime) for hr in fits[st])
        tax = _CITY_TAXONOMY.get(st, "?")
        print(f"  {st} ({tax:9s}): {n_fit} hours pooled-fit, "
              f"{n_regime} hour×regime cells")

    # Regime-features index for snapshot-time lookup
    csv_index = _build_csv_rows_index(csv_dir, stations)

    # ── Iterate diagnostic candidates (same query as diagnose_v2_gap) ──
    # Connect directly — bypasses the daemon-shared init_db() flow which
    # uses a relative DB_PATH that breaks under `sudo -u kalshi python3 -m`
    # invocation. Replay is read-only, so no WAL setup needed.
    conn = sqlite3.connect(args.db)
    candidates = conn.execute(
        """SELECT s.ticker, s.series,
                  MAX(s.ts_unix) AS ts,
                  MAX(s.fair_value_cents) AS live_fair,
                  MAX(s.market_mid) AS market_mid,
                  MAX(s.ticker_settled_yes) AS settled,
                  MAX(ab.ts_settle_unix) AS ab_settle_unix
             FROM weather_mm_shadow s
             LEFT JOIN alpha_backtest ab ON ab.ticker = s.ticker
            WHERE s.ticker_settled_yes IS NOT NULL
              AND s.fair_value_cents IS NOT NULL
              AND s.market_mid IS NOT NULL
         GROUP BY s.ticker
         ORDER BY ts ASC""",
    ).fetchall()
    if args.limit:
        candidates = candidates[:args.limit]
    print(f"[replay] {len(candidates)} candidate tickers")

    # ── Replay both modes: pooled (CSV) and regime (CSV) ───────────────
    # We wrap _apply_learned_sigma so the METAR Gaussian's σ gets
    # overridden with our CSV fit (in either mode), not the production
    # kv. That's the apples-to-apples control — both modes use the SAME
    # data source for σ; only the conditioning differs.

    # We need to know the regime per ticker at snapshot time. Pre-compute
    # it once (snapshots are static historical rows).
    ticker_regime: dict[str, tuple[Optional[str], Optional[int]]] = {}
    # For each ticker, find the snapshot CLOSEST TO BUT BEFORE settle —
    # that's the prediction state at peak relevance for bracket-edge
    # calls (typically a few hours before the day's high is locked in).
    # Using MAX(recorded_at) instead picks today's predictions, which
    # are made at LST hour 8-10 when σ is structurally large and any
    # regime effect is bounded by the early-morning uncertainty.
    settle_by_ticker: dict[str, float] = {}
    for tup in candidates:
        ticker = tup[0]
        ts_unix = tup[2]
        ab_set = tup[6]
        settle_unix = (
            float(ab_set) if ab_set is not None
            else _settle_unix_from_ticker(ticker)
        )
        if settle_unix is not None:
            settle_by_ticker[ticker] = settle_unix
    snap_time_by_ticker: dict[str, str] = {}
    for ticker, settle_unix in settle_by_ticker.items():
        # Pick the latest combined_v2 snapshot whose recorded_at falls
        # before settle. SQLite stores ISO strings; compare lexically.
        from datetime import timezone as _tz
        cutoff_iso = datetime.fromtimestamp(
            settle_unix, tz=_tz.utc,
        ).isoformat()
        row = conn.execute(
            """SELECT MAX(recorded_at) FROM weather_forecast_snapshots
                WHERE ticker = ? AND source = 'combined_v2'
                  AND recorded_at < ?""",
            (ticker, cutoff_iso),
        ).fetchone()
        if row and row[0]:
            snap_time_by_ticker[ticker] = row[0]

    fail_reasons: dict[str, int] = defaultdict(int)
    debug_examples: list[str] = []
    for tup in candidates:
        ticker = tup[0]
        recorded_at = snap_time_by_ticker.get(ticker)
        st = _station_for_ticker(ticker)
        if not recorded_at:
            fail_reasons["no_recorded_at"] += 1
            ticker_regime[ticker] = (None, None)
            continue
        if not st:
            fail_reasons["no_station"] += 1
            ticker_regime[ticker] = (None, None)
            continue
        d_h = _lst_hour_at(st, recorded_at)
        if d_h is None:
            fail_reasons["lst_parse_fail"] += 1
            ticker_regime[ticker] = (None, None)
            continue
        lst_date, lst_hour = d_h
        regime_row = csv_index.get(st, {}).get((lst_date, lst_hour))
        if regime_row is None:
            fail_reasons["csv_miss"] += 1
            if len(debug_examples) < 5:
                csv_keys = sorted(csv_index.get(st, {}).keys())[:3]
                debug_examples.append(
                    f"  miss: {ticker} st={st} recorded={recorded_at} "
                    f"→ ({lst_date},{lst_hour}) | csv has {len(csv_index.get(st, {}))} cells, e.g. {csv_keys}"
                )
            ticker_regime[ticker] = (None, lst_hour)
            continue
        tax = _CITY_TAXONOMY.get(st, "wind+sky")
        label = _regime_label(regime_row, tax)
        ticker_regime[ticker] = (label, lst_hour)
    print(f"[debug] ticker_regime fail reasons: {dict(fail_reasons)}")
    for ex in debug_examples:
        print(ex)

    # Debug: distribution of regime labels and CSV-cell coverage
    label_dist: dict[tuple[Optional[str], Optional[str]], int] = defaultdict(int)
    cell_status: dict[str, int] = defaultdict(int)
    for ticker, (label, hr) in ticker_regime.items():
        st = _station_for_ticker(ticker)
        label_dist[(st, label)] += 1
        if label is None:
            cell_status["no_label"] += 1
            continue
        f = fits.get(st)
        if f is None or hr is None:
            cell_status["no_fit"] += 1
            continue
        fit = f.get(hr)
        if fit is None:
            cell_status["no_hour_fit"] += 1
            continue
        cell = fit.by_regime.get(label)
        if cell is None:
            cell_status["regime_cell_missing"] += 1
        elif cell[1] < _MIN_FIT_N:
            cell_status[f"regime_cell_thin_n={cell[1]}"] += 1
        else:
            cell_status["regime_cell_ok"] += 1
    print(f"[debug] cell-resolution status: {dict(cell_status)}")
    top_labels = sorted(label_dist.items(), key=lambda t: -t[1])[:15]
    print(f"[debug] top (station, label) pairs:")
    for (st, lab), n in top_labels:
        print(f"    {st} {lab!r}  n={n}")

    sigma_dist: dict[str, list[tuple[float, str]]] = {
        "pooled": [], "regime": [],
    }

    # The replay reads METAR σ from a flat _RAW_SOURCE_SIGMA prior and
    # then runs _apply_learned_sigma, which only overrides for one
    # (city, bucket) combo (nyc 6_24). To make σ swap stick, we wrap
    # _apply_learned_sigma so that for the METAR source only, our CSV-
    # derived σ replaces whatever the wrapped fn produced. The other
    # sources are untouched. The current ticker context is stashed on a
    # module attribute so the wrapper can look up (station, lst_hour,
    # regime).
    saved_apply_learned = v2._apply_learned_sigma

    def make_metar_override(use_regime: bool):
        mode = "regime" if use_regime else "pooled"

        def _wrapped(g, city_key=None):
            base = saved_apply_learned(g, city_key=city_key)
            if g.source_name != "metar":
                return base
            ctx = getattr(v2, "_replay_metar_ctx", None)
            if ctx is None:
                return base
            station, lst_hour, label = ctx
            f = fits.get(station)
            if f is None:
                return base
            sigma, tier = _resolve_sigma(
                f, int(lst_hour), label, use_regime=use_regime,
            )
            sigma_dist[mode].append((sigma, tier))
            # Side-channel: track σ_before / σ_after for the spot-check
            shift_log = getattr(v2, "_replay_sigma_log", None)
            if shift_log is not None:
                shift_log.append({
                    "mode": mode, "tier": tier,
                    "before": float(base.sigma_f), "after": float(sigma),
                    "label": label, "station": station, "hour": lst_hour,
                })
            return base.with_sigma(sigma)
        return _wrapped

    rows_pooled: list[dict] = []
    rows_regime: list[dict] = []
    skipped = {"no_market": 0, "no_replay": 0, "no_settle": 0}

    # Capture the combined.μ/σ that predict_v2 lands on PER MODE — so we
    # can confirm regime mode actually produces a different combined σ.
    combined_by_ticker_mode: dict[tuple[str, str], dict] = {}

    def _make_capture(mode_name):
        def _c(snapshot_rows):
            for r in snapshot_rows:
                if r[3] == "combined_v2":
                    combined_by_ticker_mode[(r[2], mode_name)] = {
                        "mean_f": r[5], "sigma_f": r[6], "prob": r[4],
                    }
        return _c

    sigma_log_pooled: list[dict] = []
    sigma_log_regime: list[dict] = []

    for run_label, mode_use_regime, out_rows in (
        ("pooled", False, rows_pooled),
        ("regime", True, rows_regime),
    ):
        v2._apply_learned_sigma = make_metar_override(mode_use_regime)
        v2._replay_sigma_log = (
            sigma_log_pooled if run_label == "pooled" else sigma_log_regime
        )
        # Stash a snapshot capture so we can verify combined.μ/σ shifts.
        saved_writer_for_mode = v2._write_snapshots
        v2._write_snapshots = _make_capture(run_label)
        sigma_dist[run_label].clear()
        for tup in candidates:
            ticker = tup[0]
            ts_unix = tup[2]
            settled = tup[5]
            ab_set = tup[6]
            market_mid = tup[4]
            settle_unix = (
                float(ab_set) if ab_set is not None
                else _settle_unix_from_ticker(ticker)
            )
            if settle_unix is None:
                if run_label == "pooled":
                    skipped["no_settle"] += 1
                continue
            market = _fetch_market(ticker)
            if not market:
                if run_label == "pooled":
                    skipped["no_market"] += 1
                continue
            # Stash (station, lst_hour, regime_label) for the patched
            # _apply_learned_sigma to read when it sees the METAR source.
            label, hr = ticker_regime.get(ticker, (None, None))
            station_for_ticker = _station_for_ticker(ticker)
            v2._replay_metar_ctx = (
                station_for_ticker, hr if hr is not None else 0, label,
            )

            try:
                replay = _replay_predict_v2(ticker, market, conn)
            except Exception:
                replay = None
            if replay is None or replay[0] is None:
                if run_label == "pooled":
                    skipped["no_replay"] += 1
                continue

            replay_prob = float(replay[0])
            mkt_prob = float(market_mid) / 100.0
            won = float(settled)
            h_out = (settle_unix - float(ts_unix)) / 3600.0
            out_rows.append({
                "ticker": ticker,
                "won": won,
                "replay_prob": replay_prob,
                "mkt_prob": mkt_prob,
                "h_out": h_out,
                "regime_label": label,
            })
        v2._write_snapshots = saved_writer_for_mode

    # Restore originals
    v2._apply_learned_sigma = saved_apply_learned
    if hasattr(v2, "_replay_metar_ctx"):
        delattr(v2, "_replay_metar_ctx")

    # ── Compare ────────────────────────────────────────────────────────
    print(f"\n[results] pooled={len(rows_pooled)} regime={len(rows_regime)} "
          f"skipped={skipped}")
    if not rows_pooled or not rows_regime:
        print("[results] no rows; aborting")
        return 1

    by_t_pool = {r["ticker"]: r for r in rows_pooled}
    by_t_reg = {r["ticker"]: r for r in rows_regime}
    common = sorted(set(by_t_pool) & set(by_t_reg))
    print(f"[results] common tickers between modes: {len(common)}")

    def _brier(prob, won): return (prob - won) ** 2

    print(f"\n{'='*88}")
    print(f"  Aggregate Brier comparison (pooled-σ vs regime-σ vs market)")
    print(f"{'='*88}")
    pool_brier = sum(_brier(by_t_pool[t]["replay_prob"], by_t_pool[t]["won"])
                     for t in common) / len(common)
    reg_brier = sum(_brier(by_t_reg[t]["replay_prob"], by_t_reg[t]["won"])
                    for t in common) / len(common)
    mkt_brier = sum(_brier(by_t_pool[t]["mkt_prob"], by_t_pool[t]["won"])
                    for t in common) / len(common)
    print(f"  pooled (CSV)   B={pool_brier:.4f}")
    print(f"  regime (CSV)   B={reg_brier:.4f}")
    print(f"  market         B={mkt_brier:.4f}")
    print(f"  Δ(regime−pool) = {reg_brier - pool_brier:+.4f}")
    print(f"  Δ(regime−mkt)  = {reg_brier - mkt_brier:+.4f}  (we want this near 0 or negative)")
    print(f"  Δ(pool−mkt)    = {pool_brier - mkt_brier:+.4f}  (status quo gap)")

    print(f"\n  Brier sliced by horizon-bucket")
    by_bucket: dict[str, list[str]] = defaultdict(list)
    for t in common:
        by_bucket[_bucket_horizon(by_t_pool[t]["h_out"])].append(t)
    print(f"  {'bucket':12s}  {'n':>4s}  {'pool':>7s}  {'regime':>7s}  "
          f"{'market':>7s}  {'Δ pool→reg':>11s}")
    for bk in ("0-6h", "6-12h", "12-24h", "24h+", "post_settle"):
        ts = by_bucket.get(bk, [])
        if not ts:
            continue
        bp = sum(_brier(by_t_pool[t]["replay_prob"], by_t_pool[t]["won"]) for t in ts) / len(ts)
        br = sum(_brier(by_t_reg[t]["replay_prob"], by_t_reg[t]["won"]) for t in ts) / len(ts)
        bm = sum(_brier(by_t_pool[t]["mkt_prob"], by_t_pool[t]["won"]) for t in ts) / len(ts)
        print(f"  {bk:12s}  {len(ts):4d}  {bp:7.4f}  {br:7.4f}  "
              f"{bm:7.4f}  {br-bp:+11.4f}")

    print(f"\n  Brier sliced by μ-bracket-distance (close-edge bucket spotlight)")
    # Recompute the captured combined.μ→edge distance bucket to match diagnose_v2_gap
    captured: dict[str, dict] = {}

    def _capture(snapshot_rows):
        for r in snapshot_rows:
            if r[3] == "combined_v2":
                captured[r[2]] = {"mean_f": r[5], "sigma_f": r[6]}

    saved_writer = v2._write_snapshots
    v2._write_snapshots = _capture
    v2._apply_learned_sigma = make_metar_override(False)  # pooled for capture
    for t in common:
        label, hr = ticker_regime.get(t, (None, None))
        v2._replay_metar_ctx = (
            _station_for_ticker(t), hr if hr is not None else 0, label,
        )
        market = _fetch_market(t)
        if market:
            try:
                _replay_predict_v2(t, market, conn)
            except Exception:
                pass
    v2._write_snapshots = saved_writer
    v2._apply_learned_sigma = saved_apply_learned
    if hasattr(v2, "_replay_metar_ctx"):
        delattr(v2, "_replay_metar_ctx")

    by_dist: dict[str, list[str]] = defaultdict(list)
    for t in common:
        market = _fetch_market(t)
        cap = captured.get(t)
        if not market or not cap:
            continue
        proj = v2._parse_market_for_projection(t, market)
        if proj is None:
            continue
        is_bracket, threshold_f, _is_above, lo_f, hi_f = proj
        mu = cap["mean_f"]
        if is_bracket and lo_f is not None and hi_f is not None:
            inner = min(abs(mu - lo_f), abs(mu - hi_f))
            sign = +1 if (lo_f <= mu <= hi_f) else -1
            d = sign * inner
        elif threshold_f is not None:
            d = mu - threshold_f
        else:
            continue
        if d < -2.0:
            bk = "deep_out (<-2°F)"
        elif d < -0.5:
            bk = "out (-2..-0.5)"
        elif d < 0.5:
            bk = "edge (-0.5..0.5)"
        elif d < 2.0:
            bk = "in (0.5..2)"
        else:
            bk = "deep_in (>2°F)"
        by_dist[bk].append(t)
    print(f"  {'bucket':22s}  {'n':>4s}  {'pool':>7s}  {'regime':>7s}  "
          f"{'market':>7s}  {'Δ pool→reg':>11s}")
    for bk in ("deep_out (<-2°F)", "out (-2..-0.5)", "edge (-0.5..0.5)",
               "in (0.5..2)", "deep_in (>2°F)"):
        ts = by_dist.get(bk, [])
        if not ts:
            continue
        bp = sum(_brier(by_t_pool[t]["replay_prob"], by_t_pool[t]["won"]) for t in ts) / len(ts)
        br = sum(_brier(by_t_reg[t]["replay_prob"], by_t_reg[t]["won"]) for t in ts) / len(ts)
        bm = sum(_brier(by_t_pool[t]["mkt_prob"], by_t_pool[t]["won"]) for t in ts) / len(ts)
        print(f"  {bk:22s}  {len(ts):4d}  {bp:7.4f}  {br:7.4f}  "
              f"{bm:7.4f}  {br-bp:+11.4f}")

    print(f"\n  Tier usage breakdown:")
    for mode in ("pooled", "regime"):
        tiers: dict[str, int] = defaultdict(int)
        for _sig, tier in sigma_dist[mode]:
            tiers[tier] += 1
        total = sum(tiers.values())
        if total == 0:
            continue
        print(f"    {mode:7s}: " + ", ".join(
            f"{t}={n}({n/total*100:.0f}%)" for t, n in sorted(tiers.items())
        ))

    # Combined σ shift per ticker — confirms whether regime mode actually
    # produces a different combined σ at the projection step.
    print(f"\n  Combined.σ per-ticker comparison (top-10 |Δσ|):")
    sigma_pairs = []
    for t in common:
        cp = combined_by_ticker_mode.get((t, "pooled"))
        cr = combined_by_ticker_mode.get((t, "regime"))
        if cp is None or cr is None:
            continue
        d_sig = cr["sigma_f"] - cp["sigma_f"]
        d_p = cr["prob"] - cp["prob"]
        sigma_pairs.append((t, cp["sigma_f"], cr["sigma_f"], cp["mean_f"],
                            cr["mean_f"], d_sig, d_p))
    sigma_pairs.sort(key=lambda r: -abs(r[5]))
    print(f"    {'ticker':38s}  pool_σ  reg_σ    pool_μ  reg_μ    Δσ      Δp")
    for ti, ps, rs, pm, rm, ds, dp in sigma_pairs[:10]:
        print(f"    {ti:38s}  {ps:5.2f}   {rs:5.2f}   {pm:5.1f}   "
              f"{rm:5.1f}   {ds:+5.2f}  {dp:+.4f}")

    # Per-ticker Δp distribution — does the σ-shift translate to actual
    # probability changes at the projection?
    print(f"\n  Per-ticker Δp distribution (regime − pooled), close-edge bucket:")
    edge_tickers = [t for t in common
                    if t in by_t_pool and t in by_t_reg
                    and t in {tt for ts in by_dist.get("edge (-0.5..0.5)", []) for tt in [ts]}]
    deltas = []
    for t in common:
        dp = by_t_reg[t]["replay_prob"] - by_t_pool[t]["replay_prob"]
        deltas.append(dp)
    nonzero = [d for d in deltas if abs(d) > 1e-6]
    if nonzero:
        from statistics import mean as _m, median as _md
        print(f"    nonzero shifts: {len(nonzero)} / {len(deltas)} tickers")
        print(f"    |Δp| mean={_m([abs(d) for d in nonzero]):.4f}  "
              f"max={max(abs(d) for d in nonzero):.4f}")
        # Show ticker-level top shifts (where regime mattered most)
        ticker_shifts = sorted(
            [(t, by_t_reg[t]["replay_prob"], by_t_pool[t]["replay_prob"],
              by_t_pool[t]["mkt_prob"], by_t_pool[t]["won"])
             for t in common if abs(by_t_reg[t]["replay_prob"] - by_t_pool[t]["replay_prob"]) > 1e-6],
            key=lambda r: -abs(r[1] - r[2]),
        )[:10]
        print(f"\n    Top-10 |Δp| tickers:")
        print(f"      {'ticker':38s}  pool_p  reg_p   mkt_p   won  Δp")
        for ti, rp, pp, mp, w in ticker_shifts:
            print(f"      {ti:38s}  {pp:.3f}   {rp:.3f}   {mp:.3f}  {int(w)}    {rp-pp:+.3f}")
    else:
        print(f"    no probability shifts — regime override not flowing through")

    # σ-shift distribution to confirm the override is meaningfully changing
    # METAR's σ in regime mode vs pooled mode.
    print(f"\n  METAR σ comparison (pooled mode vs regime mode):")
    p_sigmas = [s["after"] for s in sigma_log_pooled]
    r_sigmas = [s["after"] for s in sigma_log_regime]
    if p_sigmas and r_sigmas:
        from statistics import mean as _mean, median as _median
        print(f"    pooled σ:  mean={_mean(p_sigmas):.2f}  "
              f"median={_median(p_sigmas):.2f}  "
              f"min={min(p_sigmas):.2f}  max={max(p_sigmas):.2f}")
        print(f"    regime σ:  mean={_mean(r_sigmas):.2f}  "
              f"median={_median(r_sigmas):.2f}  "
              f"min={min(r_sigmas):.2f}  max={max(r_sigmas):.2f}")
        # Per-tier σ in regime mode
        by_tier: dict[str, list[float]] = defaultdict(list)
        for s in sigma_log_regime:
            by_tier[s["tier"]].append(s["after"])
        print(f"\n  Regime mode σ by tier:")
        for tier, vals in sorted(by_tier.items()):
            print(f"    {tier:15s}  n={len(vals):4d}  "
                  f"mean={_mean(vals):.2f}  median={_median(vals):.2f}  "
                  f"min={min(vals):.2f}  max={max(vals):.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
