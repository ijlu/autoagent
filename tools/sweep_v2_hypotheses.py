"""Single-pass sweep of v2 weather ensemble hypotheses.

Runs the same dataset of settled tickers through ``predict_v2`` multiple
times, each with one knob altered, and tabulates pooled Brier vs market
mid + a per-family breakdown. The heavy work — refetching Kalshi market
metadata for ~1500 tickers — happens once on the first experiment;
subsequent runs hit the in-process ``_MARKET_CACHE`` from
``backtest_v2_replay``.

Hypotheses tested (in order):

    baseline           — v2 as shipped (σ_floor=0.5, AFD on, truncation on,
                         learned ρ from kv_cache).
    sigma_floor=0.3    \\
    sigma_floor=0.75    | sweep _COMBINED_SIGMA_FLOOR_F. H1: late-day floor
    sigma_floor=1.0     | too tight is the leading suspect — market acts as
    sigma_floor=1.25    | if its σ_eff ≈ 1.0°F, our 0.5°F floor over-
    sigma_floor=1.5    /  concentrates mass on the centred bracket.
    afd_off            — disable AFD shift entirely. H6: AFD's pooled-Brier
                         "win" of 0.013 may not be 2σ from zero.
    trunc_off          — drop the running-max truncation floor in projection.
                         H3: late-day, truncation re-amplifies in-bracket
                         prob by ~2× when forecast μ ≈ observed max.
    rho_force_1        — force group correlation ρ=1.0 (full discount).
                         H4: persisted weather_group_corr_* may have been
                         fit on the pre-fix Open-Meteo-triple-counted data
                         and now under-discounts correlated forecasts.

Output: pooled comparison table + per-family table. Δ_vs_baseline > 0
means the knob improved Brier; the magnitude relative to the 0.04 gap
tells us how much of it that knob explains.

Run on the VPS:

    python -m tools.sweep_v2_hypotheses --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import statistics
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from bot.config import DB_PATH
from bot.db import init_db
from bot.signals import weather_ensemble_v2 as v2

from tools.backtest_v2_replay import _fetch_market, _replay_predict_v2


# ── Patch helpers ─────────────────────────────────────────────────────
#
# Each helper is a context manager that swaps a v2 internal at __enter__
# and restores it at __exit__. Multiple knobs are not stacked — every
# experiment runs against the as-shipped baseline state.

@contextmanager
def patched_sigma_floor(value: float) -> Iterator[None]:
    saved = v2._COMBINED_SIGMA_FLOOR_F
    v2._COMBINED_SIGMA_FLOOR_F = value
    try:
        yield
    finally:
        v2._COMBINED_SIGMA_FLOOR_F = saved


@contextmanager
def patched_afd_off() -> Iterator[None]:
    """Force ``get_afd_bias`` to return (None, None, None) so predict_v2
    skips the AFD shift step. Same mechanism backtest_v2_replay uses for
    its ``--disable-afd`` flag."""
    import bot.signals.sources.afd as afd_mod
    saved = afd_mod.get_afd_bias
    afd_mod.get_afd_bias = lambda *a, **kw: (None, None, None)
    try:
        yield
    finally:
        afd_mod.get_afd_bias = saved


@contextmanager
def patched_truncation_off() -> Iterator[None]:
    """Strip ``truncation_floor_f`` from every projection call inside v2.

    ``predict_v2`` imports ``probability_for_market`` into its module
    namespace, so we patch the local binding (``v2.probability_for_market``)
    rather than the original in ``weather_forecast``.
    """
    saved = v2.probability_for_market

    def _no_trunc(forecast, **kw):
        kw["truncation_floor_f"] = None
        return saved(forecast, **kw)

    v2.probability_for_market = _no_trunc
    try:
        yield
    finally:
        v2.probability_for_market = saved


@contextmanager
def patched_rho_force_1() -> Iterator[None]:
    """Force ``_get_group_rho`` to return 1.0 — equivalent to the MVP
    full-discount fallback. Nullifies any persisted weather_group_corr_*
    fit. Doesn't touch the kv row, so the daemon is unaffected after
    the experiment ends.
    """
    saved = v2._get_group_rho
    v2._get_group_rho = lambda *a, **kw: 1.0
    try:
        yield
    finally:
        v2._get_group_rho = saved


@contextmanager
def noop_ctx() -> Iterator[None]:
    yield


# ── Experiment runner ─────────────────────────────────────────────────

def _settled_rows(conn: sqlite3.Connection) -> list:
    """One row per settled ticker. Same query as backtest_v2_replay.run()."""
    return conn.execute(
        """SELECT ticker, series,
                  MAX(ts_unix) AS ts,
                  MAX(fair_value_cents) AS live_fair,
                  MAX(market_mid) AS market_mid,
                  MAX(ticker_settled_yes) AS settled
             FROM weather_mm_shadow
            WHERE ticker_settled_yes IS NOT NULL
              AND fair_value_cents IS NOT NULL
              AND market_mid IS NOT NULL
         GROUP BY ticker
         ORDER BY ts ASC""",
    ).fetchall()


def run_experiment(
    conn: sqlite3.Connection,
    rows: list,
    label: str,
    patch_ctx,
    progress_every: int = 200,
) -> dict[str, list[tuple[float, float, float]]]:
    """Replay every settled ticker under ``patch_ctx``.

    Returns ``{series: [(live_b, mkt_b, replay_b), ...]}``.
    """
    series_brier: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    skipped = {"no_market": 0, "no_replay": 0}
    n_processed = 0
    t0 = time.time()

    with patch_ctx:
        for ticker, series, _ts, live_fair, market_mid, settled in rows:
            market = _fetch_market(ticker)
            if not market:
                skipped["no_market"] += 1
                continue
            try:
                replay = _replay_predict_v2(ticker, market, conn)
            except Exception:
                replay = None
            if replay is None or replay[0] is None:
                skipped["no_replay"] += 1
                continue

            prob, _tag = replay
            won_yes = float(settled)
            live_b = (float(live_fair) / 100.0 - won_yes) ** 2
            mkt_b = (float(market_mid) / 100.0 - won_yes) ** 2
            replay_b = (float(prob) - won_yes) ** 2
            series_brier[series].append((live_b, mkt_b, replay_b))
            n_processed += 1

            if progress_every and n_processed % progress_every == 0:
                elapsed = time.time() - t0
                rate = n_processed / max(elapsed, 1e-6)
                print(f"  [{label}] {n_processed}/{len(rows)} "
                      f"({rate:.1f}/s)")

    elapsed = time.time() - t0
    print(f"  [{label}] processed={n_processed} "
          f"skipped={dict(skipped)} ({elapsed:.0f}s)")
    return series_brier


# ── Reporting ─────────────────────────────────────────────────────────

def _pooled_metrics(
    brier: dict[str, list[tuple[float, float, float]]],
) -> tuple[int, float, float, float, float]:
    """Pooled (n, live_brier, mkt_brier, replay_brier, edge_vs_mkt)."""
    flat = [t for v in brier.values() for t in v]
    n = len(flat)
    if not n:
        return (0, 0.0, 0.0, 0.0, 0.0)
    live = statistics.mean(t[0] for t in flat)
    mkt = statistics.mean(t[1] for t in flat)
    replay = statistics.mean(t[2] for t in flat)
    return (n, live, mkt, replay, mkt - replay)


def print_pooled_table(results: dict[str, dict]) -> None:
    print()
    print("=" * 100)
    print("v2 hypothesis sweep — pooled Brier across all settled tickers")
    print("=" * 100)
    print(f"  {'experiment':<22} {'n':>5} {'v1_live':>9} {'v2_replay':>10} "
          f"{'mkt_mid':>9} {'edge_vs_mkt':>12} {'Δ_vs_baseline':>15}")
    print("  " + "-" * 90)

    baseline_replay: Optional[float] = None
    for label, brier in results.items():
        n, live, mkt, replay, edge = _pooled_metrics(brier)
        if label == "baseline":
            baseline_replay = replay
            delta_str = "         --"
        else:
            if baseline_replay is None:
                delta_str = "          ?"
            else:
                delta = baseline_replay - replay
                delta_str = f"{delta:>+15.4f}"
        print(f"  {label:<22} {n:>5} {live:>9.4f} {replay:>10.4f} "
              f"{mkt:>9.4f} {edge:>+12.4f} {delta_str}")

    print()
    print("Reading guide:")
    print("  v1_live    = Brier of fair_value_cents recorded in shadow at decision time")
    print("               (mostly v1 outputs — pre-overhaul Platt-curve era).")
    print("  v2_replay  = today's predict_v2 evaluated against historical snapshots.")
    print("  mkt_mid    = market consensus at the same decision time.")
    print("  edge_vs_mkt > 0    → v2 beats market mid (alpha).")
    print("  Δ_vs_baseline > 0  → this knob *improved* Brier vs the shipped v2.")
    print("                       Magnitude = how much of the gap that knob explains.")


def print_per_family_table(results: dict[str, dict]) -> None:
    families = sorted({s for r in results.values() for s in r.keys()})
    if not families:
        return
    print()
    print("=" * 92)
    print("Per-family replay Brier (lower is better)")
    print("=" * 92)
    print(f"  {'family':<11}", end="")
    for label in results:
        print(f"{label:>16}", end="")
    print()
    print("  " + "-" * (11 + 16 * len(results)))

    for fam in families:
        print(f"  {fam:<11}", end="")
        for label in results:
            samples = results[label].get(fam, [])
            if samples:
                replay_b = statistics.mean(t[2] for t in samples)
                print(f"{replay_b:>16.4f}", end="")
            else:
                print(f"{'-':>16}", end="")
        print()


# ── Main ──────────────────────────────────────────────────────────────

def build_experiments() -> list[tuple[str, object]]:
    """Order matters — baseline first so the market cache primes for the
    rest, and so Δ_vs_baseline reads correctly."""
    return [
        ("baseline",          noop_ctx()),
        ("sigma_floor=0.3",   patched_sigma_floor(0.3)),
        ("sigma_floor=0.75",  patched_sigma_floor(0.75)),
        ("sigma_floor=1.0",   patched_sigma_floor(1.0)),
        ("sigma_floor=1.25",  patched_sigma_floor(1.25)),
        ("sigma_floor=1.5",   patched_sigma_floor(1.5)),
        ("afd_off",           patched_afd_off()),
        ("trunc_off",         patched_truncation_off()),
        ("rho_force_1",       patched_rho_force_1()),
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--limit", type=int, default=None,
                   help="Limit settled tickers (debug).")
    args = p.parse_args()

    conn = init_db(args.db)
    rows = _settled_rows(conn)
    if args.limit:
        rows = rows[:args.limit]
    print(f"[sweep] {len(rows)} settled tickers loaded")

    # Suppress snapshot writes for the duration of the sweep. predict_v2
    # writes one row per call to weather_forecast_snapshots; at 9
    # experiments × ~1500 tickers, we'd add ~13.5K rows that downstream
    # audit tools (audit_source_residuals, audit_source_accuracy_by_horizon)
    # would read as if they were real production snapshots.
    v2._write_snapshots = lambda rows: None

    experiments = build_experiments()
    results: dict[str, dict] = {}

    for label, patch_ctx in experiments:
        print()
        print(f"── running: {label} ────────────────────────────────")
        results[label] = run_experiment(conn, rows, label, patch_ctx)

    print_pooled_table(results)
    print_per_family_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
