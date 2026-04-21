"""Bayesian combiner for weather sources.

Given 8 potentially-correlated weather sources (metar observations, Open-Meteo,
Tomorrow.io, NWS hourly, NBM, HRRR, MADIS basket, AFD forecaster discussion),
combine them into a single probability estimate that beats any individual
source on historical Brier.

Strategy:
  1. Collect (prob, source_tag) from every weather source that has an opinion
  2. Look up learned per-source weight from weather_source_weights (falls back
     to hand-set priors)
  3. Shrink toward the weighted mean with precision = Σ(weight)
  4. Log every component + the combined estimate to weather_forecast_snapshots
     for post-hoc calibration.

This source registers with the main ensemble as "weather_ensemble" for
KXHIGH* / KXHMONTHRANGE* / KXHURR* tickers. The main ensemble then treats it
as a single source (rather than 8 correlated ones), which is both more honest
about effective sample size AND lets the weather sub-ensemble learn its own
internal weights without polluting the main ensemble's adaptive learning.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from bot.db import get_connection, db_write


# Hand-set priors — used until the weather_source_weights table has learned values.
# METAR (observations) is the gold standard for settlement-day predictions.
# HRRR > NBM > Open-Meteo > Tomorrow.io for near-term forecasts.
# AFD adds forecaster judgement, MADIS adds sensor-drift protection.
DEFAULT_WEATHER_PRIORS = {
    "metar":     1.00,
    "hrrr":      0.90,
    "nbm":       0.85,
    "nws_point": 0.75,
    "tomorrow":  0.70,
    "weather":   0.65,  # Open-Meteo default model
    "madis":     0.55,
    "afd":       0.40,
    "noaa":      0.50,  # NOAA alerts (still a weather signal)
}


def _get_learned_weights(series: str) -> dict[str, float]:
    """Load learned weights from DB. Falls back to DEFAULT_WEATHER_PRIORS."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT source, weight FROM weather_source_weights WHERE series = ?",
            (series,),
        ).fetchall()
        learned = {src: float(w) for src, w in rows} if rows else {}
    except Exception:
        learned = {}
    # Merge — learned overrides defaults, defaults fill gaps
    merged = dict(DEFAULT_WEATHER_PRIORS)
    merged.update(learned)
    return merged


def _source_family_key(source_tag: str) -> str:
    """Return the short source family key from a source tag like 'nbm:nyc_2026-04-17'."""
    if not source_tag:
        return ""
    return source_tag.split(":", 1)[0].strip().lower()


def _series_from_ticker(ticker: str) -> str:
    """Extract the series prefix from a ticker (e.g. 'KXHIGHNY' from 'KXHIGHNY-26APR17-T75')."""
    if not ticker:
        return "UNKNOWN"
    return ticker.split("-", 1)[0].upper()


def _snapshot_component(
    conn, series: str, ticker: str, source: str, prob: float, baseline_high_f: Optional[float]
) -> None:
    """Record a single source estimate for post-hoc calibration."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO weather_forecast_snapshots
               (recorded_at, series, ticker, source, forecast_prob, forecast_high_f)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (now_iso, series, ticker, source, prob, baseline_high_f),
        )
    except Exception as e:
        print(f"[weather_ensemble] snapshot error: {type(e).__name__}: {e}")


def predict(
    ticker: str, market_data: dict, yes_ask: Optional[float] = None
) -> tuple[Optional[float], Optional[str]]:
    """Combine all weather sources into a single probability estimate.

    Returns (prob, source_tag) or (None, None) if no weather source has an
    opinion. Called by the main ensemble when it sees a weather-family ticker.
    """
    # Import lazily to avoid circular imports (weather_ensemble ⊂ ensemble at module level)
    from bot.signals.sources.metar_observations import get_metar_observation_estimate
    from bot.signals.sources.weather import (
        get_weather_estimate,
        get_tomorrow_weather_estimate,
        get_noaa_alerts_for_market,
    )
    from bot.signals.sources.nws_point import get_nws_point_estimate
    from bot.signals.sources.ndfd_nbm import get_nbm_estimate
    from bot.signals.sources.hrrr import get_hrrr_estimate
    from bot.signals.sources.madis import get_madis_estimate
    from bot.signals.sources.afd import get_afd_estimate

    ticker = ticker or ""
    ticker_upper = ticker.upper()
    # Only fire for weather-family tickers
    is_weather_ticker = (
        ticker_upper.startswith("KXHIGH")
        or ticker_upper.startswith("KXHMONTHRANGE")
        or ticker_upper.startswith("KXHURR")
    )
    if not is_weather_ticker:
        return None, None

    series = _series_from_ticker(ticker)
    weights_map = _get_learned_weights(series)

    # Collect estimates. Every source either returns (prob, tag) or (None, None).
    sources: list[tuple[str, callable]] = [
        ("metar",     get_metar_observation_estimate),
        ("hrrr",      get_hrrr_estimate),
        ("nbm",       get_nbm_estimate),
        ("nws_point", get_nws_point_estimate),
        ("tomorrow",  get_tomorrow_weather_estimate),
        ("weather",   get_weather_estimate),
        ("madis",     get_madis_estimate),
        ("afd",       get_afd_estimate),
        ("noaa",      get_noaa_alerts_for_market),
    ]

    estimates: list[tuple[float, float, str]] = []  # (prob, weight, label)
    for name, fn in sources:
        try:
            prob, tag = fn(ticker, market_data)
        except Exception as e:
            print(f"[weather_ensemble] {name} raised {type(e).__name__}: {e}")
            continue
        if prob is None:
            continue
        w = weights_map.get(name, DEFAULT_WEATHER_PRIORS.get(name, 0.3))
        estimates.append((prob, w, name))

    if not estimates:
        return None, None

    # Weighted average
    total_w = sum(w for _, w, _ in estimates)
    if total_w <= 0:
        return None, None
    combined = sum(p * w for p, w, _ in estimates) / total_w
    combined = max(0.02, min(0.98, combined))

    # Record component snapshots (best-effort, doesn't block the estimate)
    try:
        def _write(c):
            for p, _, name in estimates:
                _snapshot_component(c, series, ticker, name, p, None)
            _snapshot_component(c, series, ticker, "combined", combined, None)
        db_write(_write)
    except Exception as e:
        print(f"[weather_ensemble] snapshot batch error: {type(e).__name__}: {e}")

    labels = "+".join(name for _, _, name in estimates)
    print(
        f"[weather_ensemble] {series} n={len(estimates)} combined={combined:.3f} "
        f"({', '.join(f'{n}={p:.2f}' for p, _, n in estimates)})"
    )
    return combined, f"weather_ensemble:{labels}"
