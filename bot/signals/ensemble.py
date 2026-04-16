"""Ensemble router: collects all data sources and computes weighted probability estimate.

This is the core signal generation function. It calls all registered data sources,
handles pipeline health tracking, disagreement detection, correlated source counting,
and calibration correction.

Extracted from trade.py get_independent_estimate() (lines 2886-3100).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from bot.config import SOURCE_WEIGHTS, CORRELATED_GROUPS, OPENAI_API_KEY, SOURCE_MAX_HORIZON_DAYS

# Import all data sources
from bot.signals.sources.prediction_markets import get_polymarket_estimate, get_metaculus_estimate
from bot.signals.sources.crypto import get_crypto_estimate
from bot.signals.sources.weather import (
    get_weather_estimate,
    get_tomorrow_weather_estimate,
    get_noaa_alerts_for_market,
)
from bot.signals.sources.economics import get_fred_estimate, get_cleveland_fed_nowcast, get_bls_estimate
from bot.signals.sources.sports import get_sports_estimate
from bot.signals.sources.news import get_news_sentiment
from bot.signals.sources.company import get_company_kpi_estimate, get_sensortower_estimate
from bot.signals.sources.series import get_series_estimate
from bot.signals.sources.momentum import get_price_momentum
from bot.signals.sources.llm import get_llm_estimate
from bot.signals.sources.metar_observations import get_metar_observation_estimate
from bot.signals.sources.fedwatch import get_fedwatch_estimate


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline health tracking
# ══════════════════════════════════════════════════════════════════════════════

_PIPELINE_STATS: dict[str, dict] = {}


def pipeline_track_attempt(source: str) -> None:
    if source not in _PIPELINE_STATS:
        _PIPELINE_STATS[source] = {"attempted": 0, "returned": 0, "errors": 0, "latencies": []}
    _PIPELINE_STATS[source]["attempted"] += 1


def pipeline_track_result(source: str, success: bool, latency_ms: float = 0) -> None:
    if source not in _PIPELINE_STATS:
        _PIPELINE_STATS[source] = {"attempted": 0, "returned": 0, "errors": 0, "latencies": []}
    if success:
        _PIPELINE_STATS[source]["returned"] += 1
    else:
        _PIPELINE_STATS[source]["errors"] += 1
    if latency_ms > 0:
        _PIPELINE_STATS[source]["latencies"].append(latency_ms)


def record_pipeline_health(conn) -> None:
    """Record this run's pipeline health stats and detect degradations."""
    now_str = datetime.now(timezone.utc).isoformat()

    for source, stats in _PIPELINE_STATS.items():
        attempted = stats["attempted"]
        returned = stats["returned"]
        errors = stats["errors"]
        latencies = stats["latencies"]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0
        error_rate = errors / attempted if attempted > 0 else 0

        if attempted == 0:
            status = "idle"
        elif error_rate > 0.5:
            status = "degraded"
        elif returned == 0 and attempted > 0:
            status = "broken"
        else:
            status = "healthy"

        detail = ""
        prev = conn.execute("""
            SELECT markets_attempted, markets_returned, status
            FROM pipeline_health
            WHERE source = ? ORDER BY id DESC LIMIT 1
        """, (source,)).fetchone()

        if prev and prev[2] == "healthy" and status in ("degraded", "broken"):
            detail = f"ALERT: {source} degraded from healthy -> {status}"
            print(f"[pipeline] {detail}")
        elif prev and prev[1] and prev[1] > 5 and returned == 0:
            detail = f"ALERT: {source} returned 0 results (was {prev[1]} last run)"
            print(f"[pipeline] {detail}")

        conn.execute("""INSERT INTO pipeline_health
            (recorded_at, source, status, markets_attempted, markets_returned,
             avg_latency_ms, error_rate, detail)
            VALUES (?,?,?,?,?,?,?,?)""",
            (now_str, source, status, attempted, returned, avg_latency, error_rate, detail))

    conn.commit()
    _PIPELINE_STATS.clear()

    health_summary = conn.execute("""
        SELECT source, status, markets_returned
        FROM pipeline_health
        WHERE recorded_at = (SELECT MAX(recorded_at) FROM pipeline_health)
        ORDER BY source
    """).fetchall()
    if health_summary:
        print("[pipeline] Source health:")
        for src, status, returned in health_summary:
            icon = "+" if status == "healthy" else ("!" if status == "degraded" else "x")
            print(f"  {icon} {src}: {status} ({returned} results)")


# ══════════════════════════════════════════════════════════════════════════════
# Calibration correction
# ══════════════════════════════════════════════════════════════════════════════

def apply_calibration_correction(prob: float, corrections: dict) -> float:
    """Apply learned calibration correction to an ensemble probability.

    corrections: dict of bucket -> {bias, n, avg_estimate, actual_rate}
    """
    if not corrections:
        return prob
    bucket = f"{int(prob * 10) / 10:.1f}-{int(prob * 10) / 10 + 0.1:.1f}"
    if bucket in corrections:
        bias = corrections[bucket].get("bias", 0)
        return max(0.02, min(0.98, prob - bias))
    return prob


# ══════════════════════════════════════════════════════════════════════════════
# Main ensemble function
# ══════════════════════════════════════════════════════════════════════════════

def get_independent_estimate(
    ticker: str,
    market_data: dict,
    yes_ask: float,
    volume: float,
    adaptive_weights: Optional[dict] = None,
    calibration_corrections: Optional[dict] = None,
    disabled_sources: Optional[set] = None,
) -> tuple[Optional[float], Optional[str], int]:
    """Collect ALL available data sources and compute a weighted ensemble average.

    Returns:
        (ensemble_probability, source_description, num_effective_sources)
        Returns (None, None, 0) if no sources have estimates.
    """
    weights = adaptive_weights if adaptive_weights else SOURCE_WEIGHTS
    estimates = []
    _disabled = disabled_sources or set()

    # ── Compute days to resolution for source horizon filtering ──
    _days_to_resolution = None
    if market_data:
        _close_time_str = market_data.get("close_time") or market_data.get("expiration_time")
        if _close_time_str:
            try:
                if isinstance(_close_time_str, str):
                    _ct = datetime.fromisoformat(_close_time_str.replace("Z", "+00:00"))
                else:
                    _ct = _close_time_str
                _delta = _ct - datetime.now(timezone.utc)
                _days_to_resolution = max(0.1, _delta.total_seconds() / 86400)
            except Exception:
                pass

    def _tracked_call(source_name, func, *args, **kwargs):
        """Call a data source with pipeline health tracking."""
        if source_name in _disabled:
            return (None, None)
        pipeline_track_attempt(source_name)
        t0 = time.time()
        try:
            result = func(*args, **kwargs)
            latency = (time.time() - t0) * 1000
            if result is not None and result[0] is not None:
                pipeline_track_result(source_name, True, latency)
            elif latency < 100:
                _PIPELINE_STATS[source_name]["attempted"] -= 1
            else:
                pipeline_track_result(source_name, False, latency)
            return result
        except Exception:
            latency = (time.time() - t0) * 1000
            pipeline_track_result(source_name, False, latency)
            return (None, None)

    # ── Call all sources ──
    poly_prob, poly_src = _tracked_call("polymarket", get_polymarket_estimate, ticker, market_data)
    if poly_prob is not None:
        estimates.append((poly_prob, weights.get("polymarket", 0.75), poly_src))

    crypto_prob, crypto_src = _tracked_call("crypto", get_crypto_estimate, ticker, market_data)
    if crypto_prob is not None:
        estimates.append((crypto_prob, weights.get("crypto", 0.65), crypto_src))

    weather_prob, weather_src = _tracked_call("weather", get_weather_estimate, ticker, market_data)
    if weather_prob is not None:
        estimates.append((weather_prob, weights.get("weather", 0.80), weather_src))

    tmrw_prob, tmrw_src = _tracked_call("tomorrow", get_tomorrow_weather_estimate, ticker, market_data)
    if tmrw_prob is not None:
        estimates.append((tmrw_prob, weights.get("tomorrow", 0.82), tmrw_src))

    noaa_prob, noaa_src = _tracked_call("noaa", get_noaa_alerts_for_market, ticker, market_data)
    if noaa_prob is not None:
        estimates.append((noaa_prob, weights.get("noaa", 0.70), noaa_src))

    metar_prob, metar_src = _tracked_call("metar", get_metar_observation_estimate, ticker, market_data)
    if metar_prob is not None:
        estimates.append((metar_prob, weights.get("metar", 0.90), metar_src))

    fred_prob, fred_src = _tracked_call("fred", get_fred_estimate, ticker, market_data)
    if fred_prob is not None:
        estimates.append((fred_prob, weights.get("fred", 0.50), fred_src))

    clevfed_prob, clevfed_src = _tracked_call("clevfed", get_cleveland_fed_nowcast, ticker, market_data)
    if clevfed_prob is not None:
        estimates.append((clevfed_prob, weights.get("clevfed", 0.72), clevfed_src))

    bls_prob, bls_src = _tracked_call("bls", get_bls_estimate, ticker, market_data)
    if bls_prob is not None:
        estimates.append((bls_prob, weights.get("bls", 0.50), bls_src))

    fedwatch_prob, fedwatch_src = _tracked_call("fedwatch", get_fedwatch_estimate, ticker, market_data)
    if fedwatch_prob is not None:
        estimates.append((fedwatch_prob, weights.get("fedwatch", 0.80), fedwatch_src))

    sports_prob, sports_src = _tracked_call("odds", get_sports_estimate, ticker, market_data)
    if sports_prob is not None:
        estimates.append((sports_prob, weights.get("odds", 0.85), sports_src))

    meta_prob, meta_src = _tracked_call("metaculus", get_metaculus_estimate, ticker, market_data)
    if meta_prob is not None:
        estimates.append((meta_prob, weights.get("metaculus", 0.70), meta_src))

    news_prob, news_src = _tracked_call("finnhub", get_news_sentiment, ticker, market_data)
    if news_prob is not None:
        estimates.append((news_prob, weights.get("finnhub", 0.30), news_src))

    kpi_prob, kpi_src = _tracked_call("company_kpi", get_company_kpi_estimate, ticker, market_data)
    if kpi_prob is not None:
        estimates.append((kpi_prob, weights.get("company_kpi", 0.65), kpi_src))

    st_prob, st_src = _tracked_call("sensortower", get_sensortower_estimate, ticker, market_data)
    if st_prob is not None:
        estimates.append((st_prob, weights.get("sensortower", 0.55), st_src))

    series_prob, series_src = _tracked_call("series", get_series_estimate, ticker, market_data)
    if series_prob is not None:
        estimates.append((series_prob, weights.get("series", 0.75), series_src))

    # Momentum — GATED: only if no other estimates (avoids hundreds of API calls)
    if not estimates:
        momentum = get_price_momentum(ticker)
        if momentum and abs(momentum["momentum"]) > 0.02:
            adj = momentum["momentum"] * 0.5
            mom_est = max(0.02, min(0.98, yes_ask + adj))
            if abs(mom_est - yes_ask) > 0.02:
                estimates.append((mom_est, weights.get("momentum", 0.15), f"momentum_adj={adj:+.2f}"))

    # LLM — LAST RESORT for markets no regex source can parse
    _LLM_SKIP_CATEGORIES = {"crypto", "weather", "sports"}
    category = market_data.get("category", "").lower() if market_data else ""
    llm_category_ok = not any(cat in category for cat in _LLM_SKIP_CATEGORIES)
    if llm_category_ok:
        title_check = (market_data.get("title", "") or "").lower()
        if any(kw in title_check for kw in [
            "bitcoin", "btc", "ethereum", "eth", "solana",
            "temperature", "degrees", "nba", "nfl", "mlb", "nhl", "ncaa"
        ]):
            llm_category_ok = False
    if not estimates and OPENAI_API_KEY and volume >= 200 and llm_category_ok:
        llm_prob, llm_src = get_llm_estimate(ticker, market_data)
        if llm_prob is not None:
            estimates.append((llm_prob, weights.get("llm", 0.15), llm_src))

    # ── Source horizon filtering ──
    # Decay weights for sources whose max forecast horizon is shorter than the
    # market's time-to-resolution.  A 7-day weather forecast applied to a 90-day
    # market is mostly noise — downweight (min 0.2x) rather than hard-skip so
    # the signal still contributes at reduced strength.
    if _days_to_resolution is not None and estimates:
        _horizon_adjusted = []
        _decayed_any = False
        for prob, weight, src in estimates:
            # Infer source key from description: try full prefix before ':',
            # fall back to prefix before '_' (handles "company_kpi:..." and
            # "momentum_adj=+0.05" patterns)
            _src_key = src.split(":")[0].strip().lower()
            if _src_key not in SOURCE_MAX_HORIZON_DAYS:
                _src_key = _src_key.split("_")[0]
            _max_h = SOURCE_MAX_HORIZON_DAYS.get(_src_key)
            if _max_h is not None and _days_to_resolution > _max_h:
                _decay = max(0.2, _max_h / _days_to_resolution)
                _horizon_adjusted.append((prob, weight * _decay, src))
                _decayed_any = True
            else:
                _horizon_adjusted.append((prob, weight, src))
        if _decayed_any:
            print(f"[ensemble] Horizon filter: {_days_to_resolution:.1f}d to resolution, "
                  f"decayed {sum(1 for a, b in zip(estimates, _horizon_adjusted) if a[1] != b[1])} sources")
        estimates = _horizon_adjusted

    if not estimates:
        if volume > 10000:
            return None, "high_vol_efficient", 0
        return None, None, 0

    # ── Disagreement detection ──
    if len(estimates) >= 2:
        probs_only = [p for p, _, _ in estimates]
        max_spread = max(probs_only) - min(probs_only)
        if max_spread >= 0.20:
            sources_str = ", ".join(f"{s}={p:.2f}" for p, _, s in estimates)
            print(f"[ensemble] SKIP: source disagreement {max_spread:.2f} > 0.20 "
                  f"({sources_str})")
            return None, f"disagreement:{max_spread:.2f}", 0

    # ── Weighted ensemble average ──
    total_weight = sum(w for _, w, _ in estimates)
    ensemble_prob = sum(p * w for p, w, _ in estimates) / total_weight
    sources = "+".join(s.split(":")[0] if ":" in s else s[:10] for _, _, s in estimates)

    # ── Effective independent source count ──
    source_names = set()
    for _, _, s in estimates:
        base = s.split(":")[0] if ":" in s else s.split("_")[0]
        source_names.add(base.lower())

    claimed_by_group = set()
    n_effective = 0.0
    for group_name, group_members in CORRELATED_GROUPS.items():
        overlap = source_names & group_members
        if len(overlap) >= 2:
            n_effective += 1.0
            claimed_by_group |= overlap
        elif len(overlap) == 1:
            n_effective += 1.0
            claimed_by_group |= overlap
    ungrouped = source_names - claimed_by_group
    n_effective += len(ungrouped)
    n_sources = max(1, round(n_effective))

    # ── Calibration correction ──
    raw_prob = ensemble_prob
    if calibration_corrections:
        ensemble_prob = apply_calibration_correction(ensemble_prob, calibration_corrections)
        if abs(ensemble_prob - raw_prob) > 0.001:
            print(f"[calibration] Corrected {raw_prob:.3f} -> {ensemble_prob:.3f} "
                  f"(correction={ensemble_prob - raw_prob:+.3f})")

    print(f"[ensemble] {n_sources} sources -> {ensemble_prob:.3f} "
          f"({', '.join(f'{s}={p:.2f}' for p, _, s in estimates)})")

    # Safety clamp
    ensemble_prob = max(0.02, min(0.98, ensemble_prob))
    return ensemble_prob, f"ensemble({sources})", n_sources
