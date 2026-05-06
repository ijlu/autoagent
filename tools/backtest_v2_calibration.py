"""Backtest v2 calibration against the 91-day OM+METAR backfill.

Honest framing: the backfill table holds Open-Meteo + METAR data only.
hrrr/nbm rows duplicate open_meteo (a known A2.5a limitation — A2.5b
NOMADS GRIB2 backfill never landed). So this script tests *one*
question: does per-(source, city) EWMA MOS bias correction improve
predicted-high accuracy and threshold-projected Brier vs raw forecast?

Per-row workflow:

  1. Read every (city, settlement_date) where observed_high_f is set.
  2. Build a Gaussian from the open_meteo row (mean = forecast_mean_f,
     sigma = forecast_sigma_f or skill-fit σ if persisted).
  3. raw_pred  = forecast_mean_f
     cal_pred  = forecast_mean_f - mos_bias[(open_meteo, city)]
                 from kv_cache (already persisted by --persist-mos-bias).
  4. abs_err_raw = |raw_pred - observed|
     abs_err_cal = |cal_pred - observed|
  5. For threshold-projected Brier we also need the live ticker's
     threshold_f. We don't have those for backfill dates that the bot
     never traded. Approach: derive nominal thresholds from the
     observed daily-high (round to nearest 5°F) and treat that as the
     "T-bracket" the market would have priced. This is a proxy — it
     measures local probability quality near the truth, which is where
     real Kalshi T-tickers sit.

Outputs a per-family table comparing raw vs calibrated and prints the
NY/MIA cases the Apr 24 markout flagged.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

# Allow running as `python3 tools/backtest_v2_calibration.py` from repo root.
sys.path.insert(0, ".")

from bot.db import init_db, kv_get


# Map backfill city → display ticker family for grouping.
_CITY_TO_FAMILY: dict[str, str] = {
    "nyc": "KXHIGHNY",
    "chicago": "KXHIGHCHI",
    "miami": "KXHIGHMIA",
    "los angeles": "KXHIGHLAX",
    "austin": "KXHIGHAUS",
    "denver": "KXHIGHDEN",
}


def _city_key(raw: str) -> str:
    """Mirror weather_ensemble_v2._city_key — kv key normalisation."""
    return raw.strip().lower().replace(" ", "_")


def _phi(z: float) -> float:
    """Std-normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _prob_above(mu: float, sigma: float, threshold: float) -> float:
    """P(X > threshold) for X ~ N(mu, sigma)."""
    if sigma <= 0:
        return 1.0 if mu > threshold else 0.0
    return 1.0 - _phi((threshold - mu) / sigma)


@dataclass
class Row:
    city: str
    family: str
    settlement_date: str
    forecast_mean_f: float
    forecast_sigma_f: float
    observed_high_f: float
    bias_corrected_mean_f: float
    threshold_f: float          # synthetic — round(observed) ± 0.5°F to test sharp brackets
    actual_above: int           # 1 if observed > threshold else 0
    raw_p_above: float
    cal_p_above: float


def _read_bias(conn: sqlite3.Connection, source: str, city: str) -> Optional[float]:
    """Read EWMA bias from kv_cache."""
    payload = kv_get(conn, f"weather_mos_bias_{source}_{_city_key(city)}")
    if not isinstance(payload, dict):
        return None
    bias = payload.get("bias")
    return float(bias) if isinstance(bias, (int, float)) else None


def _read_skill_sigma(conn: sqlite3.Connection, source: str) -> Optional[float]:
    """Read learned σ from the 6-24h skill bucket (lead_hours=12 in backfill)."""
    payload = kv_get(conn, f"weather_skill_{source}_6_24")
    if not isinstance(payload, dict):
        return None
    sigma = payload.get("sigma")
    return float(sigma) if isinstance(sigma, (int, float)) else None


def build_rows(conn: sqlite3.Connection, source: str = "open_meteo") -> list[Row]:
    """Pull every backfill row with observed truth, apply bias + skill σ."""
    rows = conn.execute(
        """SELECT city, settlement_date, forecast_mean_f, forecast_sigma_f,
                  observed_high_f
             FROM weather_gaussian_snapshots_backfill
            WHERE source = ?
              AND observed_high_f IS NOT NULL
              AND forecast_mean_f IS NOT NULL""",
        (source,),
    ).fetchall()

    learned_sigma = _read_skill_sigma(conn, source)

    out: list[Row] = []
    for city, date, fcst, sigma, obs in rows:
        if city not in _CITY_TO_FAMILY:
            continue
        bias = _read_bias(conn, source, city) or 0.0
        # raw σ from row, or learned if persisted (still falls back to row).
        sigma_eff = float(learned_sigma) if learned_sigma is not None else float(sigma)
        cal_mean = float(fcst) - bias

        # Synthetic threshold: nearest integer °F below observed. Tests the
        # P(X > T) projection at a bracket close to the realized outcome,
        # which is where Kalshi T-brackets concentrate trading.
        threshold = math.floor(float(obs))  # int below obs → actual_above = 1 always
        actual_above = 1 if float(obs) > threshold else 0
        raw_p = _prob_above(float(fcst), float(sigma), threshold)
        cal_p = _prob_above(cal_mean, sigma_eff, threshold)

        out.append(Row(
            city=city,
            family=_CITY_TO_FAMILY[city],
            settlement_date=str(date),
            forecast_mean_f=float(fcst),
            forecast_sigma_f=float(sigma),
            observed_high_f=float(obs),
            bias_corrected_mean_f=cal_mean,
            threshold_f=float(threshold),
            actual_above=actual_above,
            raw_p_above=raw_p,
            cal_p_above=cal_p,
        ))
    return out


def report_per_family(rows: list[Row]) -> None:
    """Per-family MAE, RMSE on predicted high; Brier on threshold projection.

    Compares raw forecast vs bias-corrected. Brier is computed against a
    synthetic threshold (floor(observed)) so actual_above is always 1 —
    not a fair Brier test. We also report mean(p_above): a calibrated
    forecast at threshold-just-below-truth should have mean p ≈ 1.0.
    """
    by_family: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_family[r.family].append(r)

    print(f"\n{'family':<14} {'n':>4} "
          f"{'MAE_raw':>8} {'MAE_cal':>8}   "
          f"{'RMSE_raw':>9} {'RMSE_cal':>9}   "
          f"{'p>T_raw':>8} {'p>T_cal':>8}   "
          f"{'Δ MAE':>7}")
    print("-" * 100)
    for family, fam_rows in sorted(by_family.items()):
        n = len(fam_rows)
        mae_raw = sum(abs(r.forecast_mean_f - r.observed_high_f) for r in fam_rows) / n
        mae_cal = sum(abs(r.bias_corrected_mean_f - r.observed_high_f) for r in fam_rows) / n
        rmse_raw = math.sqrt(sum((r.forecast_mean_f - r.observed_high_f) ** 2 for r in fam_rows) / n)
        rmse_cal = math.sqrt(sum((r.bias_corrected_mean_f - r.observed_high_f) ** 2 for r in fam_rows) / n)
        p_raw = sum(r.raw_p_above for r in fam_rows) / n
        p_cal = sum(r.cal_p_above for r in fam_rows) / n
        delta = mae_cal - mae_raw
        print(f"{family:<14} {n:>4} "
              f"{mae_raw:>8.3f} {mae_cal:>8.3f}   "
              f"{rmse_raw:>9.3f} {rmse_cal:>9.3f}   "
              f"{p_raw:>8.3f} {p_cal:>8.3f}   "
              f"{delta:>+7.3f}")

    # Pooled
    n = len(rows)
    mae_raw = sum(abs(r.forecast_mean_f - r.observed_high_f) for r in rows) / n
    mae_cal = sum(abs(r.bias_corrected_mean_f - r.observed_high_f) for r in rows) / n
    rmse_raw = math.sqrt(sum((r.forecast_mean_f - r.observed_high_f) ** 2 for r in rows) / n)
    rmse_cal = math.sqrt(sum((r.bias_corrected_mean_f - r.observed_high_f) ** 2 for r in rows) / n)
    print("-" * 100)
    print(f"{'POOLED':<14} {n:>4} "
          f"{mae_raw:>8.3f} {mae_cal:>8.3f}   "
          f"{rmse_raw:>9.3f} {rmse_cal:>9.3f}")


def report_known_bad_cases(rows: list[Row]) -> None:
    """Inspect specific NY/MIA dates the Apr 24 markout flagged.

    The Apr 24 report cited NY T67 and MIA B84.5 as catastrophic v2
    failures. We look for any backfill row in those families where
    forecast was significantly off and check whether bias correction
    would have moved it toward truth.
    """
    print("\nKnown-bad case investigation (Apr 24 markout):")
    print("-" * 100)
    for family in ("KXHIGHNY", "KXHIGHMIA"):
        fam_rows = [r for r in rows if r.family == family]
        if not fam_rows:
            continue
        # Find rows where raw forecast erred ≥ 3°F.
        bad = sorted(
            [r for r in fam_rows
             if abs(r.forecast_mean_f - r.observed_high_f) >= 3.0],
            key=lambda r: -abs(r.forecast_mean_f - r.observed_high_f),
        )
        print(f"\n{family} — top miss days (|forecast − observed| ≥ 3°F):")
        if not bad:
            print("  (none — forecast tracked observed within 3°F all 89 days)")
            continue
        print(f"  {'date':<12} {'fcst':>6} {'obs':>5} "
              f"{'err_raw':>8} {'err_cal':>8} {'improved?':>10}")
        for r in bad[:8]:
            err_raw = r.forecast_mean_f - r.observed_high_f
            err_cal = r.bias_corrected_mean_f - r.observed_high_f
            improved = "yes" if abs(err_cal) < abs(err_raw) else "no"
            print(f"  {r.settlement_date:<12} {r.forecast_mean_f:>6.1f} "
                  f"{r.observed_high_f:>5.1f} "
                  f"{err_raw:>+8.2f} {err_cal:>+8.2f} {improved:>10}")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="SQLite DB path")
    p.add_argument("--source", default="open_meteo",
                   help="Backfill source to evaluate (default open_meteo)")
    args = p.parse_args(argv)

    conn = init_db(args.db)
    rows = build_rows(conn, source=args.source)
    print(f"Backfill rows scored: {len(rows)} from source='{args.source}'")
    print(f"Coverage: {min(r.settlement_date for r in rows)} → "
          f"{max(r.settlement_date for r in rows)}")
    print(f"Cities: {sorted({r.city for r in rows})}")

    report_per_family(rows)
    report_known_bad_cases(rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
