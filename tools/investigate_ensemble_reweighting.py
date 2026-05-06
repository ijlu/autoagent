"""Per-source accuracy + ensemble reweighting investigation.

Goal: answer two questions on a SINGLE day's data —
  1. Which individual sources were closest to truth today?
  2. Would any reweighting scheme have produced a better combined μ
     than the current production combine?

Today's signal (truth) = NWS api.weather.gov peak temperature for each
city's settlement station, observed live as we run. This is the same
data ASOS sensors feed into Kalshi's CF6 settlement (modulo CF6's
1-3°F adjustment for above-METAR peaks).

For each city we compute:
  - per-source predicted μ (from `weather_forecast_snapshots` morning
    predictions, e.g. 12:00-16:00 UTC = pre-dawn LST)
  - actual peak (live from NWS)
  - residual = predicted - actual

Then we evaluate 4 reweighting scenarios:
  A. baseline: current production combine (model 1/n_model, obs 1/n_obs)
  B. obs-dominant: obs group weight × 4 (heavily anchor to METAR)
  C. metar-only-obs: METAR weight 1.0 (ignore nws_5min), all model sources at 1/n
  D. best-3-only: drop all but the 3 most-accurate sources (per today's
     per-source MAE), equal-weight precision combine

Usage:
  python3 tools/investigate_ensemble_reweighting.py --db /path/kalshi.db
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
import urllib.request
import urllib.error
import json
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Cities we care about + their NWS observation station.
CITIES: dict[str, dict] = {
    "KXHIGHNY":  {"station": "KNYC", "label": "nyc"},
    "KXHIGHMIA": {"station": "KMIA", "label": "miami"},
    "KXHIGHCHI": {"station": "KMDW", "label": "chicago"},
    "KXHIGHAUS": {"station": "KAUS", "label": "austin"},
    "KXHIGHDEN": {"station": "KDEN", "label": "denver"},
    "KXHIGHLAX": {"station": "KLAX", "label": "los_angeles"},
}


def fetch_nws_today_max(station: str, date_iso: str) -> Optional[float]:
    """Pull all NWS observations for ``station``; return today's max in °F.

    ``date_iso`` is ``YYYY-MM-DD``. Filters observations where the
    timestamp's date prefix matches.
    """
    url = f"https://api.weather.gov/stations/{station}/observations?limit=200"
    req = urllib.request.Request(
        url, headers={
            "User-Agent": "kalshi-bot reweighting probe",
            "Accept": "application/geo+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"  [{station}] fetch error: {e}", file=sys.stderr)
        return None
    features = body.get("features") or []
    temps_f = []
    for f in features:
        p = f.get("properties", {}) or {}
        ts = p.get("timestamp", "") or ""
        if not ts.startswith(date_iso):
            continue
        t = (p.get("temperature") or {}).get("value")
        if t is None:
            continue
        temps_f.append(float(t) * 9.0 / 5.0 + 32.0)
    if not temps_f:
        return None
    return max(temps_f)


def get_per_source_predictions(
    conn: sqlite3.Connection, since_iso: str, until_iso: str,
) -> dict[tuple[str, str], dict[str, float]]:
    """Return {(series, city): {source: avg_mu}} for forecast snapshots
    in the window. Average across the cycles in the window so transient
    noise washes out; we want the source's representative call for the day.
    """
    rows = conn.execute(
        """SELECT series, source, AVG(forecast_high_f) AS avg_mu, COUNT(*) AS n
             FROM weather_forecast_snapshots
            WHERE recorded_at BETWEEN ? AND ?
              AND source IN ('hrrr', 'nws_point', 'weather', 'icon', 'ukmo',
                             'gem', 'metno', 'ecmwf', 'metar', 'nws_5min',
                             'combined_v2')
              AND forecast_high_f IS NOT NULL
            GROUP BY series, source""",
        (since_iso, until_iso),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, float]] = {}
    for series, source, avg_mu, _n in rows:
        out.setdefault((series, ""), {})[source] = float(avg_mu)
    return out


def get_per_source_sigmas(
    conn: sqlite3.Connection, since_iso: str, until_iso: str,
) -> dict[tuple[str, str], dict[str, float]]:
    """Same shape but for σ — needed to run the precision-weighted
    combine for the reweighting scenarios."""
    rows = conn.execute(
        """SELECT series, source, AVG(sigma_f) AS avg_sigma
             FROM weather_forecast_snapshots
            WHERE recorded_at BETWEEN ? AND ?
              AND source IN ('hrrr', 'nws_point', 'weather', 'icon', 'ukmo',
                             'gem', 'metno', 'ecmwf', 'metar', 'nws_5min')
              AND sigma_f IS NOT NULL
            GROUP BY series, source""",
        (since_iso, until_iso),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, float]] = {}
    for series, source, avg_sigma in rows:
        out.setdefault((series, ""), {})[source] = float(avg_sigma)
    return out


_MODEL_SOURCES = frozenset({"hrrr", "nws_point", "weather", "icon", "ukmo",
                            "gem", "metno", "ecmwf"})
_OBS_SOURCES = frozenset({"metar", "nws_5min"})


def precision_combine(
    mus: dict[str, float], sigmas: dict[str, float],
    model_weight_fn=lambda n: 1.0 / max(1, n),
    obs_weight_fn=lambda n: 1.0 / max(1, n),
) -> Optional[float]:
    """Run the precision-weighted combine with custom weight functions
    per group. Returns combined μ (we don't care about combined σ here
    since the comparison is on μ accuracy)."""
    n_model = sum(1 for s in mus if s in _MODEL_SOURCES)
    n_obs = sum(1 for s in mus if s in _OBS_SOURCES)
    total_p = 0.0
    weighted_num = 0.0
    for source, mu in mus.items():
        sigma = sigmas.get(source)
        if sigma is None or sigma <= 0 or not math.isfinite(sigma):
            continue
        if source in _MODEL_SOURCES:
            w = model_weight_fn(n_model)
        elif source in _OBS_SOURCES:
            w = obs_weight_fn(n_obs)
        else:
            continue
        precision = w * (1.0 / (sigma * sigma))
        total_p += precision
        weighted_num += precision * mu
    if total_p <= 0:
        return None
    return weighted_num / total_p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/tmp/kalshi_trades.db")
    ap.add_argument("--date", default="2026-05-01",
                    help="LST settlement date in YYYY-MM-DD (default = today UTC)")
    args = ap.parse_args()

    # Pull source predictions from MORNING window (12-16 UTC = pre-dawn LST).
    # That's a "forward" call before the day's weather plays out; the most
    # honest test of forecast accuracy.
    morning_since = f"{args.date}T12:00:00Z"
    morning_until = f"{args.date}T16:00:00Z"

    print(f"[setup] db={args.db}  date={args.date}")
    print(f"[setup] morning window: {morning_since} → {morning_until}")
    conn = sqlite3.connect(args.db)

    mus_by_city = get_per_source_predictions(conn, morning_since, morning_until)
    sigmas_by_city = get_per_source_sigmas(conn, morning_since, morning_until)

    # Pull NWS actuals (live).
    print(f"[setup] fetching NWS actuals for {args.date}...")
    actuals = {}
    for series, cfg in CITIES.items():
        peak = fetch_nws_today_max(cfg["station"], args.date)
        if peak is not None:
            actuals[series] = peak
            print(f"  {cfg['station']:5s} actual today peak: {peak:.1f}°F")
        else:
            print(f"  {cfg['station']:5s} NO data")

    # ── (1) Per-source MAE ───────────────────────────────────────────
    print()
    print("=" * 100)
    print("PER-SOURCE PREDICTION RESIDUALS (predicted - actual; positive = warm)")
    print("=" * 100)
    src_residuals: dict[str, list[tuple[str, float]]] = {}  # source → [(series, residual)]
    print(f"{'series':12s} {'actual':>8s}  ", end="")
    sources = ["hrrr", "nws_point", "weather", "icon", "ukmo", "gem",
               "metno", "ecmwf", "metar", "nws_5min", "combined_v2"]
    for s in sources:
        print(f"{s:>10s}", end=" ")
    print()
    print("-" * 100)
    for series, actual in actuals.items():
        mus = mus_by_city.get((series, ""), {})
        print(f"{series:12s} {actual:>7.1f}°F  ", end="")
        for s in sources:
            mu = mus.get(s)
            if mu is None:
                print(f"{'—':>10s}", end=" ")
                continue
            residual = mu - actual
            src_residuals.setdefault(s, []).append((series, residual))
            print(f"{residual:>+9.1f}", end=" ")
        print()

    print()
    print("Per-source MAE (lower = more accurate):")
    print(f"{'source':14s} {'n_cities':>8s}  {'MAE':>7s}  {'mean_bias':>10s}")
    print("-" * 50)
    for s in sources:
        residuals = src_residuals.get(s, [])
        if not residuals:
            continue
        rs = [r for _, r in residuals]
        mae = sum(abs(r) for r in rs) / len(rs)
        bias = sum(rs) / len(rs)
        print(f"{s:14s} {len(rs):>8d}  {mae:>6.2f}°F  {bias:>+9.2f}°F")

    # ── (2) Reweighting scenarios ────────────────────────────────────
    print()
    print("=" * 100)
    print("REWEIGHTING SCENARIOS (combined μ MAE per scenario)")
    print("=" * 100)

    scenarios = [
        ("A_baseline_current", lambda n: 1.0 / max(1, n), lambda n: 1.0 / max(1, n)),
        ("B_obs_4x", lambda n: 1.0 / max(1, n), lambda n: 4.0 / max(1, n)),
        ("C_obs_8x", lambda n: 1.0 / max(1, n), lambda n: 8.0 / max(1, n)),
        ("D_metar_only_obs", lambda n: 1.0 / max(1, n), lambda n: 1.0),  # METAR weight 1.0
        ("E_models_half", lambda n: 0.5 / max(1, n), lambda n: 1.0 / max(1, n)),
        ("F_models_quarter", lambda n: 0.25 / max(1, n), lambda n: 1.0 / max(1, n)),
    ]

    print(f"{'scenario':22s} {'n':>4s}  {'MAE':>7s}  {'bias':>10s}")
    print("-" * 60)
    for name, mwf, owf in scenarios:
        residuals = []
        for series, actual in actuals.items():
            mus = mus_by_city.get((series, ""), {})
            sigmas = sigmas_by_city.get((series, ""), {})
            # Drop combined_v2 from inputs — it's the OLD output, not a source.
            mus_input = {k: v for k, v in mus.items() if k != "combined_v2"}
            combined = precision_combine(
                mus_input, sigmas,
                model_weight_fn=mwf, obs_weight_fn=owf,
            )
            if combined is not None:
                residuals.append(combined - actual)
        if not residuals:
            continue
        mae = sum(abs(r) for r in residuals) / len(residuals)
        bias = sum(residuals) / len(residuals)
        print(f"{name:22s} {len(residuals):>4d}  {mae:>6.2f}°F  {bias:>+9.2f}°F")

    # ── (3) Best-3-only scenario (data-driven) ───────────────────────
    print()
    print("=" * 100)
    print("(3) BEST-3-ONLY (top-3 sources by today's MAE; equal precision combine)")
    print("=" * 100)
    # Rank sources by MAE today (excluding combined_v2)
    src_maes = []
    for s in sources:
        if s == "combined_v2":
            continue
        rs = [r for _, r in src_residuals.get(s, [])]
        if not rs:
            continue
        src_maes.append((s, sum(abs(r) for r in rs) / len(rs)))
    src_maes.sort(key=lambda x: x[1])
    top3 = [s for s, _ in src_maes[:3]]
    print(f"top-3 sources today: {top3}")
    residuals = []
    for series, actual in actuals.items():
        mus = mus_by_city.get((series, ""), {})
        sigmas = sigmas_by_city.get((series, ""), {})
        # Equal precision combine on just top-3 sources.
        total_p = 0.0
        wnum = 0.0
        for s in top3:
            mu = mus.get(s)
            sigma = sigmas.get(s)
            if mu is None or sigma is None or sigma <= 0:
                continue
            p = 1.0 / (sigma * sigma)
            total_p += p
            wnum += p * mu
        if total_p > 0:
            combined = wnum / total_p
            residuals.append(combined - actual)
    if residuals:
        mae = sum(abs(r) for r in residuals) / len(residuals)
        bias = sum(residuals) / len(residuals)
        print(f"  best-3-only:        n={len(residuals):2d}  MAE={mae:.2f}°F  bias={bias:+.2f}°F")

    print()
    print("=" * 100)
    print("KEY: positive bias = ensemble is WARMER than actual. Today's pattern")
    print("is forecast-side warm bias; obs-anchored scenarios should win.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
