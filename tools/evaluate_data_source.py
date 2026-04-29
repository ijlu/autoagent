"""Quick-eval framework for weather data sources.

Asked by Josh on 2026-04-29: before fully integrating any new source,
fetch sample historical data, compare to actuals, compute independence
vs existing sources. Decide whether it's worth shadow-logging or fully
adding to ``_collect_gaussians`` based on actual marginal value.

Inputs:
  - source name + fetch function ``(station, lst_date) -> (mu_f, sigma_f)``
  - station list (defaults to our 6 production stations)
  - date range (defaults to last 30 days of weather_metar_hourly_backfill)

Joins each fetched μ to ``weather_metar_hourly_backfill.daily_high_f``
(post-CF6-fix ground truth — already audited 2026-04-29).

Reports:
  - MAE, RMSE, bias by station + pooled
  - p50/p90/p99 of |error|
  - Residual correlation vs HRRR and METAR (independence check —
    high correlation = redundant; low correlation = adds signal)
  - Recommendation: integrate / shadow-log / skip

Independence is the load-bearing metric. The 2026-04-29 NBM/OM
investigation showed two "different" sources can be 100% correlated
(same Open-Meteo endpoint with one parameter). We always check.

This tool is read-only against our DB; it just calls outbound APIs.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional


# Production station catalog; mirrors STATION_BY_SERIES.
PROD_STATIONS = [
    ("KNYC", 40.78, -73.97, "America/New_York"),
    ("KMDW", 41.78, -87.75, "America/Chicago"),
    ("KMIA", 25.79, -80.32, "America/New_York"),  # Miami uses ET
    ("KAUS", 30.18, -97.68, "America/Chicago"),
    ("KLAX", 33.94, -118.41, "America/Los_Angeles"),
    ("KDEN", 39.84, -104.66, "America/Denver"),
]


@dataclass
class EvalRow:
    station: str
    lst_date: str
    actual_high_f: float
    pred_mu_f: float
    pred_sigma_f: Optional[float]
    error_f: float        # pred - actual
    abs_error_f: float    # |pred - actual|


@dataclass
class SourceEval:
    name: str
    n: int
    mae: float
    rmse: float
    bias: float           # mean(pred - actual); negative = cold
    p50_abs: float
    p90_abs: float
    p99_abs: float
    max_abs: float
    pct_within_1F: float
    pct_within_2F: float
    per_station: dict[str, dict] = field(default_factory=dict)
    indep_vs_hrrr: Optional[float] = None  # Pearson r of residuals; lower = more independent
    indep_vs_metar: Optional[float] = None
    rows: list[EvalRow] = field(default_factory=list)


def _ground_truth(conn: sqlite3.Connection, since_iso: str) -> dict[tuple[str, str], float]:
    """{(station, lst_date): actual_high_f} from the audited backfill table."""
    rows = conn.execute(
        """SELECT station, lst_date, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE daily_high_f IS NOT NULL AND lst_date >= ?
            GROUP BY station, lst_date""",
        (since_iso,),
    ).fetchall()
    return {(s, d): float(h) for s, d, h in rows}


def _existing_predictions(
    conn: sqlite3.Connection, source: str, since_iso: str
) -> dict[tuple[str, str], float]:
    """For ``source``, return the day-ahead μ for each (station, lst_date).

    Selection rule: take the snapshot with hours_out closest to 18h before
    settle (≈ "morning-before forecast"). Skips days without a snapshot in
    the [12, 30] hour window — we want a stable horizon for the residual
    correlation, not a mix of 1h and 5d forecasts.
    """
    rows = conn.execute(
        """SELECT s.ticker, s.recorded_at, s.forecast_high_f, s.hours_out
             FROM weather_forecast_snapshots s
            WHERE s.source = ? AND s.forecast_high_f IS NOT NULL
              AND s.hours_out BETWEEN 12 AND 30
              AND s.recorded_at >= ?""",
        (source, since_iso),
    ).fetchall()

    # Pick the snapshot per (ticker) closest to hours_out=18.
    best: dict[str, tuple[float, float]] = {}  # ticker → (forecast, |h_out-18|)
    for ticker, _ts, mu, hout in rows:
        score = abs(float(hout) - 18.0)
        cur = best.get(ticker)
        if cur is None or score < cur[1]:
            best[ticker] = (float(mu), score)

    # Map ticker → (station, lst_date) using ticker decode.
    out: dict[tuple[str, str], float] = {}
    for ticker, (mu, _score) in best.items():
        meta = _ticker_meta(ticker)
        if meta is None:
            continue
        station, lst_date = meta
        out[(station, lst_date)] = mu
    return out


_TICKER_TO_STATION = {
    "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA",
    "KXHIGHAUS": "KAUS", "KXHIGHLAX": "KLAX", "KXHIGHDEN": "KDEN",
}
_MONTH = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05",
          "JUN": "06", "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10",
          "NOV": "11", "DEC": "12"}


def _ticker_meta(ticker: str) -> Optional[tuple[str, str]]:
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    station = _TICKER_TO_STATION.get(parts[0])
    if not station:
        return None
    suf = parts[1]
    if len(suf) < 7:
        return None
    try:
        yy = int(suf[:2])
        mon = _MONTH.get(suf[2:5].upper())
        dd = int(suf[5:7])
        if not mon:
            return None
        return (station, f"20{yy:02d}-{mon}-{dd:02d}")
    except (ValueError, IndexError):
        return None


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 5 or len(xs) != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(len(s) * pct / 100)
    return s[min(k, len(s) - 1)]


def evaluate_source(
    conn: sqlite3.Connection,
    name: str,
    fetch_fn: Callable[[str, str, float, float], Optional[tuple[float, Optional[float]]]],
    *,
    days_back: int = 30,
    stations: list[tuple] = None,
    skip_if_no_truth: bool = True,
) -> SourceEval:
    """Run the eval. ``fetch_fn(station, lst_date, lat, lon) -> (mu, sigma) | None``."""
    stations = stations or PROD_STATIONS
    today = datetime.now().date()
    since_iso = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")

    truth = _ground_truth(conn, since_iso)
    print(f"[eval] ground-truth rows: {len(truth)}")

    rows: list[EvalRow] = []
    n_calls, n_misses = 0, 0
    t0 = time.time()
    for icao, lat, lon, tz in stations:
        for offset in range(days_back):
            lst_date = (today - timedelta(days=offset + 1)).strftime("%Y-%m-%d")
            actual = truth.get((icao, lst_date))
            if actual is None:
                if skip_if_no_truth:
                    continue
                actual = float("nan")
            try:
                pred = fetch_fn(icao, lst_date, lat, lon, tz=tz)
            except Exception as e:
                print(f"[eval] {name} {icao} {lst_date}: {type(e).__name__}: {e}")
                pred = None
            n_calls += 1
            if pred is None:
                n_misses += 1
                continue
            mu, sigma = pred
            err = float(mu) - float(actual)
            rows.append(EvalRow(
                station=icao, lst_date=lst_date,
                actual_high_f=float(actual), pred_mu_f=float(mu),
                pred_sigma_f=float(sigma) if sigma is not None else None,
                error_f=err, abs_error_f=abs(err),
            ))
    elapsed = time.time() - t0
    print(f"[eval] {name}: {len(rows)} samples ({n_calls} calls, "
          f"{n_misses} misses) in {elapsed:.1f}s")

    if not rows:
        return SourceEval(name=name, n=0, mae=0, rmse=0, bias=0,
                          p50_abs=0, p90_abs=0, p99_abs=0, max_abs=0,
                          pct_within_1F=0, pct_within_2F=0)

    abs_errs = [r.abs_error_f for r in rows]
    signed = [r.error_f for r in rows]
    n = len(rows)
    ev = SourceEval(
        name=name, n=n,
        mae=round(sum(abs_errs) / n, 3),
        rmse=round(math.sqrt(sum(e * e for e in abs_errs) / n), 3),
        bias=round(sum(signed) / n, 3),
        p50_abs=round(_percentile(abs_errs, 50), 2),
        p90_abs=round(_percentile(abs_errs, 90), 2),
        p99_abs=round(_percentile(abs_errs, 99), 2),
        max_abs=round(max(abs_errs), 2),
        pct_within_1F=round(sum(1 for e in abs_errs if e <= 1.0) / n, 3),
        pct_within_2F=round(sum(1 for e in abs_errs if e <= 2.0) / n, 3),
        rows=rows,
    )

    # Per-station breakdown.
    by_station: dict[str, list[EvalRow]] = {}
    for r in rows:
        by_station.setdefault(r.station, []).append(r)
    for s, srows in by_station.items():
        ev.per_station[s] = {
            "n": len(srows),
            "mae": round(sum(r.abs_error_f for r in srows) / len(srows), 2),
            "bias": round(sum(r.error_f for r in srows) / len(srows), 2),
            "p90": round(_percentile([r.abs_error_f for r in srows], 90), 2),
        }

    # Independence: residual correlation vs HRRR + METAR's day-ahead snapshots.
    hrrr_pred = _existing_predictions(conn, "hrrr", since_iso)
    metar_pred = _existing_predictions(conn, "metar", since_iso)

    matched_hrrr_x, matched_hrrr_y = [], []
    matched_metar_x, matched_metar_y = [], []
    for r in rows:
        key = (r.station, r.lst_date)
        actual = r.actual_high_f
        if key in hrrr_pred:
            matched_hrrr_x.append(r.error_f)
            matched_hrrr_y.append(hrrr_pred[key] - actual)
        if key in metar_pred:
            matched_metar_x.append(r.error_f)
            matched_metar_y.append(metar_pred[key] - actual)

    ev.indep_vs_hrrr = (
        round(_pearson(matched_hrrr_x, matched_hrrr_y), 3)
        if _pearson(matched_hrrr_x, matched_hrrr_y) is not None else None
    )
    ev.indep_vs_metar = (
        round(_pearson(matched_metar_x, matched_metar_y), 3)
        if _pearson(matched_metar_x, matched_metar_y) is not None else None
    )

    return ev


def format_report(evs: list[SourceEval]) -> str:
    """Summary table sortable by your-preferred-metric."""
    lines = ["# Source quick-eval results", ""]
    lines.append(f"_Generated_: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")

    lines.append("## Headline ranking")
    lines.append("")
    lines.append(
        "| source | n | MAE °F | RMSE | bias | p90 |err| | within 1°F | "
        "indep vs HRRR | indep vs METAR |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    sorted_evs = sorted(evs, key=lambda e: e.mae if e.n > 0 else 999)
    for e in sorted_evs:
        if e.n == 0:
            lines.append(f"| {e.name} | 0 | — | — | — | — | — | — | — |")
            continue
        ihrrr = f"{e.indep_vs_hrrr:+.2f}" if e.indep_vs_hrrr is not None else "—"
        imetar = f"{e.indep_vs_metar:+.2f}" if e.indep_vs_metar is not None else "—"
        lines.append(
            f"| {e.name} | {e.n} | {e.mae} | {e.rmse} | {e.bias:+.2f} | "
            f"{e.p90_abs} | {e.pct_within_1F*100:.0f}% | {ihrrr} | {imetar} |"
        )
    lines.append("")
    lines.append(
        "**Independence interpretation**: residual correlation. 0 = totally "
        "independent (adds full signal). 0.5 = correlated. 1.0 = redundant "
        "with that source. <0.5 is what we want."
    )
    lines.append("")

    for e in sorted_evs:
        if e.n == 0:
            continue
        lines.append(f"## {e.name} per-station breakdown")
        lines.append("")
        lines.append("| station | n | MAE | bias | p90 |err| |")
        lines.append("|---|---:|---:|---:|---:|")
        for s in sorted(e.per_station):
            d = e.per_station[s]
            lines.append(
                f"| {s} | {d['n']} | {d['mae']} | {d['bias']:+.2f} | {d['p90']} |"
            )
        lines.append("")

    return "\n".join(lines)


# ── Recommendation logic ───────────────────────────────────────────────
def classify(e: SourceEval, baseline_mae: float = 1.5) -> str:
    """Heuristic verdict given the eval results."""
    if e.n < 30:
        return "INSUFFICIENT_DATA"
    if e.mae > baseline_mae * 1.5:
        return "SKIP_low_skill"
    if e.indep_vs_hrrr is not None and abs(e.indep_vs_hrrr) > 0.85 and \
       e.indep_vs_metar is not None and abs(e.indep_vs_metar) > 0.85:
        return "SKIP_redundant"
    if e.mae <= baseline_mae and e.indep_vs_hrrr is not None and abs(e.indep_vs_hrrr) < 0.7:
        return "INTEGRATE"
    return "SHADOW_LOG"


# ── CLI ──────────────────────────────────────────────────────────────────
# Each registered source is (name, fetch_fn). fetch_fn signature:
#   fetch_fn(station_icao, lst_date, lat, lon) -> Optional[(mu_f, sigma_f)]
SOURCE_REGISTRY: dict[str, Callable] = {}


def register(name: str):
    def _wrap(fn):
        SOURCE_REGISTRY[name] = fn
        return fn
    return _wrap


# Default registrations live in tools/evaluate_sources/*.py.
# Importing those modules registers their fetch functions here.
def _import_default_sources():
    try:
        import tools.evaluate_sources  # noqa: F401  (registers its members)
    except ImportError as e:
        print(f"[eval] WARN: failed to import tools.evaluate_sources: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=30, help="lookback in days")
    ap.add_argument("--sources", nargs="+", default=None,
                    help="source names to eval (default: all registered)")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    _import_default_sources()

    conn = sqlite3.connect(args.db)
    sources = args.sources or list(SOURCE_REGISTRY)
    print(f"[eval] running {len(sources)} source(s): {sources}")

    evs: list[SourceEval] = []
    for name in sources:
        fn = SOURCE_REGISTRY.get(name)
        if fn is None:
            print(f"[eval] WARN: source {name!r} not registered; skipping")
            continue
        ev = evaluate_source(conn, name, fn, days_back=args.days)
        evs.append(ev)

    report = format_report(evs)
    print()
    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"[eval] report written to {args.output}")
    else:
        print(report)

    # Verdicts
    print("\n## Recommendations")
    for e in evs:
        verdict = classify(e)
        print(f"- {e.name}: {verdict} (n={e.n}, mae={e.mae}, "
              f"indep_hrrr={e.indep_vs_hrrr})")


if __name__ == "__main__":
    main()
