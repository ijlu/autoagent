"""Methodology diagnostics for the weather ensemble v2.

Why: the directional-shadow backfill shows ensemble Brier 1.5–5.7 points
worse than market mid in 5 of 6 weather families, and the live combiner
doesn't beat the static 12h-lead backfill snapshot. That isolates the
gap to methodology (combine / σ / projection / per-family bias) rather
than data freshness.

Three checks, each cheap, run together:

  1. Per-source vs combined Brier. For every backfill row, project each
     source individually onto the same bracket and compute Brier vs
     ground truth and vs market mid. If any single source beats the
     combine, the combiner is destroying signal. If none beats market,
     the issue is upstream.

  2. Per-family residual histogram. From
     ``weather_gaussian_snapshots_backfill``, (forecast_mean − observed)
     per (source, family) — mean and stdev. Non-zero mean = a bias the
     MOS step isn't capturing. Stdev ≠ forecast σ = σ-calibration off.

  3. Projection sanity check. 1000 random (μ, σ, lo, hi) tuples: compare
     Φ-based bracket prob to a 50K-sample Monte Carlo. If they disagree
     materially, the closed-form projection has a bug. (Sanity, not a
     statistical test — pure math.)

Run on VPS where the alpha_backtest + backfill data lives:

    python -m tools.diagnose_methodology --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Optional

from bot.config import DB_PATH
from bot.db import init_db
from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import (
    GaussianForecast,
    probability_for_market,
)


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HTTP_PACE_S = 0.25
DECISION_LEAD_HOURS = 18.0  # matches backfill_directional_shadow.py

FAMILY_TO_CITY = {
    "KXHIGHNY":  "nyc",
    "KXHIGHCHI": "chicago",
    "KXHIGHMIA": "miami",
    "KXHIGHLAX": "los angeles",
    "KXHIGHAUS": "austin",
    "KXHIGHDEN": "denver",
}

# Sources we'll evaluate individually — must match what backfill loads.
TARGET_SOURCES = ("hrrr", "nbm", "weather")


# ── Kalshi market fetch (cached) ────────────────────────────────────────

_MARKET_CACHE: dict[str, Optional[dict]] = {}


def _http_get(url: str, retries: int = 3) -> dict:
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "kalshi-bot-diagnose/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception:
            if i == retries - 1:
                return {}
            time.sleep(0.5 * (2 ** i))
    return {}


def _fetch_market(ticker: str) -> Optional[dict]:
    if ticker in _MARKET_CACHE:
        return _MARKET_CACHE[ticker]
    url = f"{KALSHI_BASE}/markets/{urllib.parse.quote(ticker, safe='')}"
    data = _http_get(url)
    market = data.get("market")
    _MARKET_CACHE[ticker] = market
    time.sleep(HTTP_PACE_S)
    return market


# ── Diagnostic 1: per-source vs combined Brier ──────────────────────────

def _load_backfill_gaussians_raw(
    conn: sqlite3.Connection, city: str, settlement_date: str,
) -> dict[str, GaussianForecast]:
    """Same source filtering as backfill_directional_shadow but keyed by
    canonical source name so we can pull a single source out for the
    per-source projection."""
    rows = conn.execute(
        """SELECT source, forecast_mean_f, forecast_sigma_f, lead_hours
           FROM weather_gaussian_snapshots_backfill
           WHERE city = ? AND settlement_date = ?
             AND source IN ('hrrr','nbm','open_meteo','weather')
             AND forecast_mean_f IS NOT NULL AND forecast_sigma_f IS NOT NULL""",
        (city, settlement_date),
    ).fetchall()
    sources_present = {r[0] for r in rows}
    out: dict[str, GaussianForecast] = {}
    for source, mean_f, sigma_f, lead_h in rows:
        if source == "open_meteo" and "weather" in sources_present:
            continue
        name = "weather" if source == "open_meteo" else source
        try:
            g = GaussianForecast(
                mean_f=float(mean_f),
                sigma_f=float(sigma_f) if sigma_f and sigma_f > 0 else 2.0,
                horizon_hours=float(lead_h or 12.0),
                source_name=name,
                source_tag=f"{name}:{city}_{settlement_date}_diag",
            )
        except ValueError:
            continue
        out[name] = g
    return out


def _project_single(
    g: GaussianForecast, ticker: str, market_data: dict,
    city_key: Optional[str], apply_mos: bool, apply_skill_sigma: bool,
) -> Optional[float]:
    """Project a single Gaussian onto a market with the same per-source
    corrections v2 applies in the combine path."""
    projection = v2._parse_market_for_projection(ticker, market_data)
    if projection is None:
        return None
    is_bracket, threshold_f, is_above, lo_f, hi_f = projection

    g_corrected = g
    if apply_skill_sigma:
        try:
            g_corrected = v2._apply_learned_sigma(g_corrected)
        except Exception:
            pass
    if apply_mos and city_key is not None:
        try:
            g_corrected = v2._apply_mos_bias(g_corrected, city_key)
        except Exception:
            pass

    try:
        return probability_for_market(
            g_corrected,
            is_bracket=is_bracket, threshold_f=threshold_f, is_above=is_above,
            bracket_lo_f=lo_f, bracket_hi_f=hi_f,
        )
    except Exception:
        return None


def _project_combined(
    gaussians: list[GaussianForecast], ticker: str, market_data: dict,
    city_key: Optional[str] = None,
) -> Optional[float]:
    """Run the predict_v2 combine path on the supplied backfill gaussians.

    Pre-applies per-(source, [city,] horizon) skill σ + MOS bias so the
    output mirrors what the live combine would produce on this same data.
    Without that pre-application, the patched ``_collect_gaussians`` would
    feed predict_v2 the raw backfill σ values, dodging the calibration
    layer the live path always sees.
    """
    corrected: list[GaussianForecast] = []
    for g in gaussians:
        try:
            g2 = v2._apply_learned_sigma(g, city_key=city_key)
        except Exception:
            g2 = g
        try:
            g2 = v2._apply_mos_bias(g2, city_key)
        except Exception:
            pass
        corrected.append(g2)

    saved = v2._collect_gaussians
    v2._collect_gaussians = lambda *_a, **_kw: corrected
    try:
        prob, _tag = v2.predict_v2(ticker, market_data)
    except Exception:
        prob = None
    finally:
        v2._collect_gaussians = saved
    return prob


def diagnostic_1(conn: sqlite3.Connection) -> None:
    print()
    print("=" * 96)
    print("Diagnostic 1: per-source vs combined Brier (vs ground truth and vs market mid)")
    print("=" * 96)

    rows = conn.execute(
        """SELECT family, ticker, market_prob_yes, won_yes, ts_decision_unix
           FROM alpha_backtest
           WHERE decision_type = 'directional_shadow_backfill'
             AND won_yes IS NOT NULL
             AND market_prob_yes IS NOT NULL""",
    ).fetchall()
    print(f"  loaded {len(rows)} settled backfill rows; refetching markets...")

    # Aggregate Brier sums per (family, source) and a "mkt" pseudo-source.
    sums: dict[tuple[str, str], list[float]] = defaultdict(list)
    n_processed = 0
    n_skipped = 0
    t0 = time.time()
    last_print = t0

    # Cache backfill gaussians per (city, date) — same key for many tickers.
    gauss_cache: dict[tuple[str, str], dict[str, GaussianForecast]] = {}

    for family, ticker, market_p, won_yes, ts_dec in rows:
        if family not in FAMILY_TO_CITY:
            n_skipped += 1
            continue
        city = FAMILY_TO_CITY[family]
        # Settle date = the date in the event ticker (one day prior to close).
        # Easier: re-derive from ticker — KXHIGHNY-26APR23-...
        parts = ticker.split("-")
        if len(parts) < 2:
            n_skipped += 1
            continue
        date_suf = parts[1]  # e.g. 26APR23
        try:
            yy = int(date_suf[:2])
            mon = date_suf[2:5]
            dd = int(date_suf[5:7])
            months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
            m_idx = months.index(mon.upper()) + 1
            settle_date = f"20{yy:02d}-{m_idx:02d}-{dd:02d}"
        except (ValueError, IndexError):
            n_skipped += 1
            continue

        gkey = (city, settle_date)
        if gkey not in gauss_cache:
            gauss_cache[gkey] = _load_backfill_gaussians_raw(conn, city, settle_date)
        gmap = gauss_cache[gkey]
        if not gmap:
            n_skipped += 1
            continue

        market_data = _fetch_market(ticker)
        if not market_data:
            n_skipped += 1
            continue

        # MOS bias is keyed by v2._city_key (underscore-normalized).
        city_key = v2._city_key(city)

        # Per-source projection
        for src in TARGET_SOURCES:
            if src not in gmap:
                continue
            p = _project_single(
                gmap[src], ticker, market_data,
                city_key=city_key, apply_mos=True, apply_skill_sigma=True,
            )
            if p is None:
                continue
            sums[(family, src)].append((p - won_yes) ** 2)

        # Combined (full v2 path on these gaussians only, with per-city
        # skill σ + MOS bias pre-applied so the projection matches live).
        p_comb = _project_combined(
            list(gmap.values()), ticker, market_data, city_key=city_key,
        )
        if p_comb is not None:
            sums[(family, "combined")].append((p_comb - won_yes) ** 2)

        # Market mid baseline
        sums[(family, "mkt_mid")].append((market_p - won_yes) ** 2)

        n_processed += 1
        if time.time() - last_print > 10:
            elapsed = time.time() - t0
            rate = n_processed / max(elapsed, 1e-6)
            remaining = (len(rows) - n_processed) / max(rate, 1e-6)
            print(f"  ... {n_processed}/{len(rows)} ({rate:.1f}/s, ~{remaining:.0f}s left)")
            last_print = time.time()

    elapsed = time.time() - t0
    print(f"  done: processed={n_processed} skipped={n_skipped} in {elapsed:.0f}s")
    print()

    families = sorted({k[0] for k in sums.keys()})
    cols = ["mkt_mid", "combined", *TARGET_SOURCES]
    header = f"{'family':<11} " + " ".join(f"{c:>11}" for c in cols)
    print(header)
    print("-" * len(header))
    for fam in families:
        row = [fam.ljust(11)]
        for col in cols:
            vals = sums.get((fam, col), [])
            if not vals:
                row.append(f"{'-':>11}")
            else:
                brier = statistics.mean(vals)
                row.append(f"{brier:>10.4f}({len(vals)})")
        print(" ".join(row))

    # Per-source Brier averaged across families for the headline number.
    print()
    print("  Pooled across families:")
    for col in cols:
        vals = [v for (_f, c), lst in sums.items() if c == col for v in lst]
        if vals:
            print(f"    {col:<10} brier={statistics.mean(vals):.4f}  n={len(vals)}")


# ── Diagnostic 2: residual histogram per family/source ──────────────────

def diagnostic_2(conn: sqlite3.Connection) -> None:
    print()
    print("=" * 96)
    print("Diagnostic 2: per-(family, source) residual = forecast_mean − observed_high (°F)")
    print("=" * 96)
    rows = conn.execute(
        """SELECT b.city,
                  b.source,
                  b.forecast_mean_f,
                  b.forecast_sigma_f,
                  m.observed_high_f
           FROM weather_gaussian_snapshots_backfill b
           JOIN weather_gaussian_snapshots_backfill m
             ON m.city = b.city
            AND m.settlement_date = b.settlement_date
            AND m.source = 'metar'
           WHERE b.source IN ('hrrr','nbm','open_meteo','weather','metar')
             AND b.forecast_mean_f IS NOT NULL
             AND m.observed_high_f IS NOT NULL""",
    ).fetchall()
    print(f"  loaded {len(rows)} (forecast, observed) pairs")
    print()

    # (city, source) → list of residuals
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    sigmas: dict[tuple[str, str], list[float]] = defaultdict(list)
    for city, source, fmean, fsigma, obs in rows:
        if source == "metar":
            continue  # self-join would give 0 residual, skip
        residual = float(fmean) - float(obs)
        buckets[(city, source)].append(residual)
        if fsigma:
            sigmas[(city, source)].append(float(fsigma))

    print(f"  {'city':<12} {'source':<11} {'n':>5} {'mean':>8} {'std':>8} "
          f"{'fσ_mean':>8} {'std/fσ':>7} {'|t|_mean':>9}")
    print("  " + "-" * 80)
    cities = sorted({k[0] for k in buckets.keys()})
    sources = ["hrrr", "nbm", "weather", "open_meteo"]
    for city in cities:
        for source in sources:
            vals = buckets.get((city, source), [])
            if len(vals) < 5:
                continue
            mean = statistics.mean(vals)
            std = statistics.stdev(vals) if len(vals) >= 2 else 0.0
            fsig = statistics.mean(sigmas.get((city, source), [1.0]))
            ratio = std / fsig if fsig > 0 else float("nan")
            # Approximate |t| of the mean residual: |mean| / (std/sqrt(n))
            se = std / math.sqrt(len(vals)) if std > 0 and len(vals) > 0 else 0.0
            t_abs = abs(mean) / se if se > 0 else 0.0
            print(f"  {city:<12} {source:<11} {len(vals):>5} {mean:>+8.2f} "
                  f"{std:>8.2f} {fsig:>8.2f} {ratio:>7.2f} {t_abs:>9.1f}")
    print()
    print("  Reading guide:")
    print("    mean ≈ 0 ⇒ no systematic bias for that (city, source); MOS has nothing to fix.")
    print("    |t|_mean > 2 ⇒ bias is statistically real; MOS *should* be fixing it.")
    print("    std/fσ ≈ 1 ⇒ forecast σ matches realized error spread (well-calibrated).")
    print("    std/fσ > 1.3 ⇒ forecast σ too tight (overconfident); skill curve should widen.")
    print("    std/fσ < 0.7 ⇒ forecast σ too loose (underconfident).")


# ── Diagnostic 3: projection sanity check ───────────────────────────────

def diagnostic_3(seed: int = 17) -> None:
    print()
    print("=" * 96)
    print("Diagnostic 3: projection — Φ closed-form vs Monte Carlo (50K samples per tuple)")
    print("=" * 96)
    rng = random.Random(seed)
    samples = 50_000
    n_tuples = 200
    max_disagree = 0.0
    sum_abs_disagree = 0.0
    n_bad = 0
    n_clamp_hi = 0
    n_clamp_lo = 0
    for _ in range(n_tuples):
        mu = rng.uniform(40.0, 95.0)
        sigma = rng.uniform(0.8, 4.0)
        # Realistic bracket geometry: mostly 5°F brackets, some narrower / wider
        width = rng.choice([3.0, 5.0, 5.0, 5.0, 7.0])
        center = mu + rng.uniform(-2.0, 2.0)
        lo = center - width / 2
        hi = center + width / 2
        g = GaussianForecast(
            mean_f=mu, sigma_f=sigma, horizon_hours=12.0,
            source_name="diag", source_tag="diag",
        )
        # Closed-form projection
        p_phi = probability_for_market(
            g, is_bracket=True, threshold_f=None, is_above=True,
            bracket_lo_f=lo, bracket_hi_f=hi,
        )
        # Monte Carlo: count fraction of N(μ, σ) draws in [lo, hi]
        hits = 0
        for _ in range(samples):
            x = rng.gauss(mu, sigma)
            if lo <= x <= hi:
                hits += 1
        p_mc = hits / samples
        diff = abs(p_phi - p_mc)
        sum_abs_disagree += diff
        if diff > max_disagree:
            max_disagree = diff
        clamp_hit = abs(p_phi - 0.98) < 1e-4 or abs(p_phi - 0.02) < 1e-4
        if abs(p_phi - 0.98) < 1e-4:
            n_clamp_hi += 1
        if abs(p_phi - 0.02) < 1e-4:
            n_clamp_lo += 1
        # MC std error ≈ sqrt(p(1-p)/n) ≈ 0.0022 for p=0.5 → flag >0.01 (~5σ)
        if diff > 0.01 and not clamp_hit:
            n_bad += 1
            print(f"  ⚠ μ={mu:.1f} σ={sigma:.2f} bracket=[{lo:.1f},{hi:.1f}] "
                  f"Φ={p_phi:.4f} MC={p_mc:.4f} Δ={diff:+.4f}")

    print(f"  {n_tuples} random tuples, {samples} samples each:")
    print(f"    mean |Φ − MC|: {sum_abs_disagree / n_tuples:.5f}")
    print(f"    max  |Φ − MC|: {max_disagree:.5f}")
    print(f"    tuples hitting clamp 0.98: {n_clamp_hi}   hitting 0.02: {n_clamp_lo}")
    print(f"    tuples with Δ > 0.01 *not* explained by clamp: {n_bad}/{n_tuples}")
    if n_bad == 0:
        print("  ✓ closed-form Φ projection is consistent with Monte Carlo (no math bug).")
    else:
        print("  ⚠ Φ-vs-MC disagreement exceeds MC noise — projection bug suspected.")
    if n_clamp_hi + n_clamp_lo > 0:
        print(f"  ⚠ clamp [_DEFAULT_CLAMP=(0.02,0.98)] is biting: {n_clamp_hi+n_clamp_lo}/{n_tuples} "
              f"projections hit a boundary — for confident bracket forecasts, this caps")
        print(f"    Brier improvement from being right at very high confidence.")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--skip", default="", help="Comma list of diagnostics to skip: 1,2,3")
    args = p.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    conn = init_db(args.db)
    if "1" not in skip:
        diagnostic_1(conn)
    if "2" not in skip:
        diagnostic_2(conn)
    if "3" not in skip:
        diagnostic_3()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
