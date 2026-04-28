"""Gaussian-first weather ensemble combiner (A2).

v1 (``weather_ensemble.predict``) combined per-source *probabilities* via a
weighted average. That's information-lossy: every source internally computes
a (mean, sigma) over the predicted daily high, projects it onto the
ticker's threshold with a logistic, and only then does v1 see the scalar.
Any threshold-agnostic information (mean shift, horizon, sigma shape) is
discarded at the projection step.

v2 combines the distributions. Each source exposes ``get_<source>_gaussian()``
returning a ``GaussianForecast(mean_f, sigma_f, horizon_hours, ...)``. v2:

  1. Collects Gaussians from the 7 Gaussian-capable sources (HRRR, NBM,
     NWS Point, Open-Meteo, Tomorrow.io, METAR, MADIS).
  2. Groups correlated sources ({hrrr, nbm, nws_point, tomorrow, weather}
     as models; {metar, madis} as station observations). Within a group of
     size *n* each source's precision contribution is scaled by 1/n so
     correlated inputs don't multi-count. This is the MVP correlation
     discount; A2.5 will fit per-group effective_n empirically from
     archived forecasts + settlements.
  3. Precision-weighted Bayesian combine via
     ``weather_forecast.combine_gaussian`` → single combined Gaussian.
  4. Applies AFD as a *bias* shift (``combined.shifted(afd_bias_f)``)
     rather than a probability-space vote. AFD's internal representation
     is a signed °F adjustment on NBM baseline — treating it as a bias
     preserves threshold-invariance (a forecaster saying "+2°F warmer
     than guidance" shifts every bracket in the series identically).
  5. Projects onto the market's threshold / bracket via
     ``probability_for_market``.
  6. Blends NOAA severe-weather alert probability (rare signal) in
     logit-space as a weak secondary vote.
  7. Logs per-source snapshots to ``weather_forecast_snapshots`` with
     sigma_f + hours_out populated so A3 can fit horizon-stratified
     skill curves from the logged stream.

Feature-flag gated via ``WEATHER_ENSEMBLE_V2``. v1 remains the default
until backtest / shadow evidence clears the bar.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Optional

from bot.db import db_write, get_connection, kv_get
from bot.signals.weather_forecast import (
    GaussianForecast,
    WeightedForecast,
    combine_gaussian,
    probability_for_market,
)


# ── Correlation groups ─────────────────────────────────────────────────
# Model forecasts share training data; station obs share the same
# atmosphere — both groups need a within-group effective-N discount so
# correlated sources don't multi-count in the precision-weighted combine.
#
# Per-source weight = n_eff / n, where
#   n_eff = n / (1 + (n-1) * rho)
# and ``rho`` is the learned mean pairwise error correlation for the
# group. With rho=1.0 (MVP fallback when no fit is persisted), n_eff=1
# and weight=1/n — byte-identical to the original "full discount" MVP.
#
# Fits are produced by ``tools/backfill_weather_effective_n.py
# --persist-effective-n`` and read from kv_cache under the key
# ``weather_group_corr_<group>``.
_MODEL_GROUP: frozenset[str] = frozenset({"hrrr", "nbm", "nws_point", "weather"})
_OBS_GROUP: frozenset[str] = frozenset({"metar", "madis"})

_GROUP_RHO_KEY_PREFIX: str = "weather_group_corr_"
# When no fit has ever been persisted, fall back to the MVP.
_GROUP_RHO_FALLBACK: float = 1.0

# ── Learned skill curves (A3) ─────────────────────────────────────────
# kv_cache key per (source, horizon bucket) holds the realized RMSE,
# persisted by tools/backfill_weather_effective_n.py --persist-skill-curves.
# When present, we override the source's self-reported σ; when absent,
# we keep the source's hardcoded schedule.
_SKILL_KEY_PREFIX: str = "weather_skill_"
# Bucket edges — must match tools/backfill_weather_effective_n.py's
# _SKILL_BUCKET_EDGES. A drift-guard test pins the two together.
_SKILL_BUCKET_EDGES: tuple[int, ...] = (0, 6, 24, 48, 168)


def _group_of(source_name: str) -> str:
    if source_name in _MODEL_GROUP:
        return "model"
    if source_name in _OBS_GROUP:
        return "obs"
    return "other"


def _skill_bucket_for(horizon_hours: float) -> Optional[str]:
    """Return ``"lo_hi"`` for the bucket containing ``horizon_hours``.

    Half-open intervals ``[lo, hi)`` per _SKILL_BUCKET_EDGES. Outside the
    top edge returns None — we don't extrapolate learned σ past 7 days.
    """
    if horizon_hours is None or horizon_hours < 0:
        return None
    for lo, hi in zip(_SKILL_BUCKET_EDGES[:-1], _SKILL_BUCKET_EDGES[1:]):
        if lo <= horizon_hours < hi:
            return f"{lo}_{hi}"
    return None


def _get_learned_sigma(
    source_name: str, horizon_hours: float, city_key: Optional[str] = None,
) -> Optional[float]:
    """Look up a learned σ for this (source, horizon) from kv_cache.

    When ``city_key`` is supplied, tries the per-city key
    ``weather_skill_<source>_<city>_<bucket>`` first and falls back to the
    pooled key. Returns None when no fit has been persisted or the cache
    isn't reachable, letting the caller keep the source's self-reported σ.

    Per-city fallback to pooled is critical: skill curves are fit nightly
    and a thin (city, source) cell stays absent until n ≥ 10. We don't
    want the cold-cell case to drop back to the *raw* source σ — pooled is
    already a learned upper-bound estimate of error spread.
    """
    bucket = _skill_bucket_for(horizon_hours)
    if bucket is None:
        return None
    try:
        conn = get_connection()
    except RuntimeError:
        return None

    keys_to_try: list[str] = []
    if city_key:
        keys_to_try.append(f"{_SKILL_KEY_PREFIX}{source_name}_{city_key}_{bucket}")
    keys_to_try.append(f"{_SKILL_KEY_PREFIX}{source_name}_{bucket}")

    for key in keys_to_try:
        try:
            payload = kv_get(conn, key)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        sigma = payload.get("sigma")
        if not isinstance(sigma, (int, float)):
            continue
        sigma_f = float(sigma)
        # Reject pathological σ. Weather-source RMSE stays in 0.1-15°F; outside
        # that range we suspect a stale / corrupt fit and fall back.
        if not (0.1 <= sigma_f <= 15.0):
            continue
        return sigma_f
    return None


# ── Source staleness assumption (B) ──────────────────────────────────
# Each forecast source has a publish cadence. NBM updates every 6 hours,
# HRRR every 1 hour, NWS Point every 1 hour. Between publishes, the
# forecast we hold becomes stale — atmosphere has evolved without that
# source observing it. Until each source plumbs an actual ``issued_at``
# timestamp through, we assume average staleness = cadence/2 and inflate
# σ to reflect that the forecast effectively represents an older forecast
# horizon than its label suggests.
#
# Inflation formula: σ_new = σ × sqrt(1 + staleness_h / horizon_h).
# Atmospheric error variance grows roughly with sqrt(time), so this is
# the right shape. At 12h horizon with 3h staleness, factor = sqrt(1 +
# 0.25) = 1.118 (12% inflation on NBM σ).
#
# METAR / MADIS are real-time observations — staleness is seconds, no
# meaningful inflation. Forecast sources that don't appear in this map
# default to 0 (no inflation) — safer than over-correcting.
_ASSUMED_STALENESS_HOURS: dict[str, float] = {
    "hrrr": 0.5,         # hourly cadence → average ~0.5h stale
    "nbm": 3.0,          # 6-hourly cadence
    "nws_point": 0.5,    # hourly cadence
    "weather": 1.0,      # Open-Meteo (cached aggregator)
    # METAR, MADIS, AFD: no staleness inflation
}

# B (per-source σ ceiling). Sources whose skill σ hasn't been fit (NWS
# Point, MADIS, AFD all have only n=12 backfill rows) inherit a wide
# pooled fallback (3+°F). At that σ, precision = 1/σ² ≈ 0.07 vs a
# well-fit source's 1/(1°F)² = 1.0 — they're effectively excluded from
# the combine. The ceiling caps σ so under-fit sources still contribute
# meaningfully. Set to 2.0°F: a fallback source contributes 25% as much
# precision as a well-fit one — under-weight, but not silenced.
_SOURCE_SIGMA_CEILING_F: float = 2.0


def _staleness_inflation_factor(
    g: GaussianForecast, now_unix: Optional[float] = None,
) -> float:
    """Multiplicative σ inflation factor for source staleness.

    Uses ``g.issued_at`` if populated; otherwise falls back to the
    per-source assumed staleness. Returns 1.0 when no inflation is
    warranted (live obs sources, unknown forecast sources, or pathological
    horizons).
    """
    horizon = g.horizon_hours
    if horizon is None or horizon <= 0:
        return 1.0

    staleness_h: Optional[float] = None
    if g.issued_at is not None:
        try:
            import time as _time
            ref = float(now_unix) if now_unix is not None else _time.time()
            staleness_h = max(0.0, (ref - float(g.issued_at)) / 3600.0)
        except (TypeError, ValueError):
            staleness_h = None
    if staleness_h is None:
        staleness_h = _ASSUMED_STALENESS_HOURS.get(g.source_name, 0.0)

    if staleness_h <= 0:
        return 1.0
    factor = math.sqrt(1.0 + staleness_h / horizon)
    return max(1.0, min(2.0, factor))


def _apply_staleness_inflation(g: GaussianForecast) -> GaussianForecast:
    """Apply per-source staleness inflation to σ. No-op for live sources
    or when the resulting factor is ~1. Preserves mean / horizon / tag."""
    factor = _staleness_inflation_factor(g)
    if factor <= 1.0001:
        return g
    return g.with_inflated_sigma(factor)


def _apply_learned_sigma(
    g: GaussianForecast, city_key: Optional[str] = None,
) -> GaussianForecast:
    """If a learned σ exists for (source, horizon[, city]), return
    ``g.with_sigma(learned)``. Otherwise return ``g`` unchanged.

    ``city_key`` defaults to None to preserve the original signature for
    callers that don't have city context. The returned object preserves
    ``source_name`` and ``source_tag`` so group routing and provenance
    keep working.
    """
    learned = _get_learned_sigma(g.source_name, g.horizon_hours, city_key=city_key)
    if learned is None:
        return g
    return g.with_sigma(learned)


# ── Learned MOS bias (A5) ─────────────────────────────────────────────
# Per (source, city) EWMA-weighted mean(forecast − observed) fit by
# tools/backfill_weather_effective_n.py --persist-mos-bias and shifted
# out of the Gaussian BEFORE combine.
#
# 2-tuple granularity (source, city) instead of (source, city, season, bucket):
# at 30 days of backfill depth, the 4-tuple split starves every cell below the
# minimum-sample gate. Once a (source, city) cell holds ≥ 200 EWMA-weight
# samples we can re-stratify and check whether season/bucket carry distinct
# biases — until then, pool. EWMA half-life weighting handles drift naturally.
_MOS_BIAS_KEY_PREFIX: str = "weather_mos_bias_"
# Cap on |bias| we'll apply even if the fit says more. Protects against
# a single outlier cell pulling the ensemble too far.
_MOS_BIAS_MAX_ABS_F: float = 5.0


def _city_key(raw: str) -> str:
    """Normalize a city value ('nyc', 'Los Angeles') for kv key use."""
    return raw.strip().lower().replace(" ", "_")


def _city_for_ticker(ticker: str) -> Optional[str]:
    """Resolve the Kalshi weather series ticker back to the backfill
    city key (nyc, chicago, miami, los_angeles, austin, denver)."""
    from bot.daemon.stations import STATION_BY_SERIES

    series = _series_from_ticker(ticker)
    ws = STATION_BY_SERIES.get(series)
    if ws is None:
        return None
    return _city_key(ws.city)


def _get_mos_bias(source_name: str, city_key: str) -> Optional[float]:
    """Return the persisted EWMA bias (°F) for this (source, city).

    None when no fit present or payload malformed; caller leaves the
    Gaussian untouched in that case.
    """
    try:
        conn = get_connection()
    except RuntimeError:
        return None
    key = f"{_MOS_BIAS_KEY_PREFIX}{source_name}_{city_key}"
    try:
        payload = kv_get(conn, key)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    bias = payload.get("bias")
    if not isinstance(bias, (int, float)):
        return None
    bias_f = float(bias)
    if not math.isfinite(bias_f):
        return None
    # Clamp — an outlier cell should never move the Gaussian by > cap.
    return max(-_MOS_BIAS_MAX_ABS_F, min(_MOS_BIAS_MAX_ABS_F, bias_f))


def _apply_mos_bias(
    g: GaussianForecast, city_key: Optional[str], now: Optional[datetime] = None,
) -> GaussianForecast:
    """Subtract the learned forecast-minus-observed bias from the
    Gaussian's mean. No-op when the fit is missing.

    ``bias = EWMA(forecast - observed)``, so to correct we shift the
    mean by ``-bias`` — a source that runs +1.5°F warm has its Gaussian
    pulled 1.5°F cooler before combine.

    ``now`` is accepted for backward compatibility with callers/tests
    that pass it; the 2-tuple key shape ignores time, since EWMA decay
    handles staleness inside the fit.
    """
    if city_key is None:
        return g
    bias = _get_mos_bias(g.source_name, city_key)
    if bias is None:
        return g
    return g.shifted(-bias)


def _get_group_rho(group_name: str) -> float:
    """Read learned pairwise error correlation from kv_cache.

    Returns ``_GROUP_RHO_FALLBACK`` (1.0 — MVP full-discount) when no fit
    is present or kv_cache isn't reachable. Clamped to
    ``[-1 / (n-1) + epsilon, 1.0]``-safe range at the call site; here we
    only defend against non-numeric payloads and runaway values.
    """
    try:
        conn = get_connection()
    except RuntimeError:
        return _GROUP_RHO_FALLBACK
    try:
        payload = kv_get(conn, f"{_GROUP_RHO_KEY_PREFIX}{group_name}")
    except Exception:
        return _GROUP_RHO_FALLBACK
    if not isinstance(payload, dict):
        return _GROUP_RHO_FALLBACK
    rho = payload.get("rho")
    if not isinstance(rho, (int, float)):
        return _GROUP_RHO_FALLBACK
    return max(-0.99, min(1.0, float(rho)))


# ── σ inflation (post-combine) ────────────────────────────────────────
# Multiplicative override applied to the combined Gaussian's σ AFTER the
# AFD shift and BEFORE projection. Default is 1.0 (no-op) because the A3
# skill-curve fitter (tools/backfill_weather_effective_n.py
# --persist-skill-curves) showed empirical RMSE ≈ 1.45°F on hrrr/nbm/
# open_meteo over 91 days at 6-24h horizon — within ~25% of the
# hardcoded priors, not the 2× too-tight pattern an earlier 24h shadow
# sample suggested. Kept as an emergency knob: the kv key
# ``weather_sigma_inflation`` and env ``WEATHER_SIGMA_INFLATION`` both
# override it, clamped to [1.0, 4.0]. Set kv → 1.5 if shadow data
# diverges and we need a fast band-aid before refitting.
_SIGMA_INFLATION_KEY: str = "weather_sigma_inflation"
_SIGMA_INFLATION_ENV: str = "WEATHER_SIGMA_INFLATION"
_SIGMA_INFLATION_DEFAULT: float = 1.0
_SIGMA_INFLATION_MIN: float = 1.0
_SIGMA_INFLATION_MAX: float = 4.0


def _clamp_inflation(x: float) -> float:
    return max(_SIGMA_INFLATION_MIN, min(_SIGMA_INFLATION_MAX, x))


def _get_sigma_inflation() -> float:
    """Resolve the σ-inflation factor.

    Precedence: kv_cache (``weather_sigma_inflation``) → env
    (``WEATHER_SIGMA_INFLATION``) → constant (``_SIGMA_INFLATION_DEFAULT``).
    Clamped to [_SIGMA_INFLATION_MIN, _SIGMA_INFLATION_MAX]; non-finite
    or non-numeric payloads fall through to the next layer.
    """
    try:
        conn = get_connection()
    except RuntimeError:
        conn = None
    if conn is not None:
        try:
            payload = kv_get(conn, _SIGMA_INFLATION_KEY)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            f = payload.get("factor")
            if isinstance(f, (int, float)) and math.isfinite(f):
                return _clamp_inflation(float(f))
        elif isinstance(payload, (int, float)) and math.isfinite(payload):
            return _clamp_inflation(float(payload))

    env = os.environ.get(_SIGMA_INFLATION_ENV)
    if env:
        try:
            v = float(env)
            if math.isfinite(v):
                return _clamp_inflation(v)
        except ValueError:
            pass
    return _clamp_inflation(_SIGMA_INFLATION_DEFAULT)


def _apply_sigma_inflation(g: GaussianForecast) -> GaussianForecast:
    """Multiply the combined Gaussian's σ by the resolved inflation factor.

    Mean / horizon / provenance are preserved.
    """
    factor = _get_sigma_inflation()
    if factor == 1.0:
        return g
    return g.with_sigma(g.sigma_f * factor)


# ── Running-high floor ───────────────────────────────────────────────
# Daily HIGH cannot be below the highest temperature already observed today.
# Without this, the precision-weighted combine can produce a μ below the
# observed running high — the forecasts (μ≈53°F, σ=1.4°F) outweigh the METAR
# Gaussian (μ=55°F, σ=5°F at hours_left=4) and pull the combined mean to
# 53.15°F while the thermometer literally reads 55°F. The market sees the
# observation; we ignore it; we post fairs that are catastrophically wrong
# and counterparties pick them off (live shadow PnL = −$0.66/fill).
#
# The fix: post-combine, raise the combined mean to METAR's mean if combined
# fell below it. METAR's ``get_metar_gaussian`` already exposes
# ``μ = max(predicted_high, running_high)`` — so its mean encodes the
# observation floor whether or not the diurnal fit is the binding source.
# σ is left unchanged for v1 of this fix; a follow-up should tighten σ when
# the floor is binding (a learned "residual peak" σ from the hourly METAR
# backfill table). Discovered 2026-04-27 via shadow Brier vs. market mid:
# pooled live Brier 0.31 vs market 0.10, fixed = should drop substantially.
def _apply_running_high_floor(
    combined: GaussianForecast,
    inputs: list[GaussianForecast],
) -> GaussianForecast:
    """Enforce the observed running daily-high as a lower bound on the
    combined mean. No-op when no METAR Gaussian was contributed or when
    the combined mean is already at/above the floor.
    """
    metar_mean: Optional[float] = None
    for g in inputs:
        if g.source_name == "metar":
            metar_mean = g.mean_f
            break
    if metar_mean is None:
        return combined
    if not math.isfinite(metar_mean):
        return combined
    if combined.mean_f >= metar_mean:
        return combined
    delta = metar_mean - combined.mean_f
    return combined.shifted(delta)


# AFD confidence ceiling so a single forecaster nudge can't overrun the
# combined Gaussian. Empirically AFDs drive maybe ±1-2°F of real edge on
# short-range forecasts; capping here prevents a runaway LLM extraction.
#
# AFD audit summary (2026-04-27, see tools/backtest_afd_signal.py +
# tools/audit_afd_stratified.py + tools/backtest_v2_replay.py):
#
#   * Direct point-accuracy test (NBM ± AFD shift vs observed daily high,
#     n=135): AFD shift HURTS by ~0.9°F mean |error|. The LLM's bias
#     direction is right ~59% of the time (just above chance) but the
#     magnitude is over-shot by 2-3× on average. Stratification by city,
#     confidence bin, model_agreement, shift magnitude, direction-only —
#     all neutral or worse.
#   * Bracket-Brier test (full v2 replay with vs without AFD, n=111):
#     keeping AFD ON improves pooled Brier by 0.013 (0.1325 vs 0.1453).
#     Helps in 4 of 6 families, neutral in 2 (NY, DEN).
#
#   The contradiction resolves because the two tests measure different
#   objectives. Point accuracy is continuous; bracket Brier is a discrete
#   geometry where a small wrong-direction shift can still tip probability
#   into the right 5°F bucket. AFD ON wins for OUR use case.
#
#   The current values (cap=3.0°F, LLM confidence=0.7, keyword
#   confidence=0.35×|bias| capped at 0.5) were guessed; they happen to
#   land AFD in a "small noisy shift that helps brackets on average"
#   regime. Increasing the cap or confidence would exit that regime —
#   tested ECMWF-as-larger-source and that hurt Brier by 0.023. Don't
#   change these values without re-running both tests.
#
#   Multi-signal AFD extraction (Option 3 in the audit) didn't beat
#   single-bias in calibration. Don't pursue without new evidence.
_MAX_AFD_SHIFT_ABS_F: float = 3.0

# NOAA weight in the final logit blend. Matches v1's DEFAULT_WEATHER_PRIORS['noaa'].
_NOAA_LOGIT_WEIGHT: float = 0.5

# σ floor applied to the combined Gaussian before bracket projection. See
# the long comment at the call site (step 4d in predict_v2). 1.0°F is the
# empirical optimum from the post-CF6 sweep (sigma_floor=1.0 gives
# +0.0025 pooled improvement vs 0.5; 1.5 starts to over-spread). The
# sweep with floor=0.5 sat right at the threshold of bracket-resolution
# uncertainty for late-day predictions, where μ is locked to METAR's
# running max and even small deviations from the true daily peak land
# entirely in the wrong 1°F bucket. 1.0°F gives meaningful probability
# in the adjacent bracket without over-spreading on the clear cases.
_COMBINED_SIGMA_FLOOR_F: float = 1.0


def _logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1.0 - p))


def _inv_logit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _series_from_ticker(ticker: str) -> str:
    if not ticker:
        return "UNKNOWN"
    return ticker.split("-", 1)[0].upper()


def _parse_market_for_projection(ticker: str, market_data: dict):
    """Return (is_bracket, threshold_f, is_above, bracket_lo_f, bracket_hi_f).

    Reuses v1 parsing helpers from ``bot.signals.sources.weather`` so the
    ticker→(threshold, direction) logic stays in one place. Returns None
    on parse failure.
    """
    import re as _re
    from bot.signals.sources.weather import _parse_threshold

    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    threshold, is_above = _parse_threshold(ticker, market_data)
    if threshold is None:
        return None

    ticker_upper = (ticker or "").upper()
    is_bracket = "-B" in ticker_upper

    if not is_bracket:
        return False, float(threshold), bool(is_above), None, None

    # Bracket resolution: prefer explicit floor/cap fields on the Kalshi
    # market payload, fall back to parsing "X to Y" from the title, finally
    # fall back to a narrow 2°F window around the parsed threshold.
    bracket_floor: float = float(threshold)
    bracket_cap: float = float(threshold) + 2.0

    fs = market_data.get("floor_strike")
    cs = market_data.get("cap_strike")
    if fs is not None and cs is not None:
        try:
            bracket_floor = float(fs)
            bracket_cap = float(cs)
        except (ValueError, TypeError):
            pass
    else:
        m = _re.search(
            r"(\d+\.?\d*)\s*°?[fF]?\s*(?:to|and|[-\u2013])\s*(\d+\.?\d*)",
            title,
        )
        if m:
            bracket_floor = float(m.group(1))
            bracket_cap = float(m.group(2))

    if bracket_cap < bracket_floor:
        bracket_floor, bracket_cap = bracket_cap, bracket_floor
    return True, None, True, bracket_floor, bracket_cap


def _collect_gaussians(ticker: str, market_data: dict) -> list[GaussianForecast]:
    """Call every Gaussian-capable source's ``get_<name>_gaussian()``.

    Network exceptions and None returns are swallowed to per-source
    granularity; a flaky HRRR fetch shouldn't drop the entire ensemble.
    """
    city_key = _city_for_ticker(ticker)
    from bot.signals.sources.hrrr import get_hrrr_gaussian
    from bot.signals.sources.madis import get_madis_gaussian
    from bot.signals.sources.metar_observations import get_metar_gaussian
    from bot.signals.sources.ndfd_nbm import get_nbm_gaussian
    from bot.signals.sources.nws_point import get_nws_point_gaussian
    from bot.signals.sources.weather import get_weather_gaussian

    # 2026-04-26: Tomorrow.io dropped — TOS storage clause + reanalysis-only
    # historical endpoint (can't backfill calibration). See bot/config.py
    # TOMORROW_API_KEY note. The function still exists in
    # bot/signals/sources/weather.py for code-archeology / potential
    # Visual-Crossing-style replacement, but is no longer wired in here.
    getters = [
        ("hrrr", get_hrrr_gaussian),
        ("nbm", get_nbm_gaussian),
        ("nws_point", get_nws_point_gaussian),
        ("weather", get_weather_gaussian),
        ("metar", get_metar_gaussian),
        ("madis", get_madis_gaussian),
    ]
    out: list[GaussianForecast] = []
    for name, fn in getters:
        try:
            g = fn(ticker, market_data)
        except Exception as e:
            print(f"[weather_ensemble_v2] {name} raised {type(e).__name__}: {e}")
            continue
        if g is None:
            continue
        if g.source_name != name:
            # Defensive: source misidentified itself. Fix here so downstream
            # grouping uses the canonical key rather than the source's tag.
            g = GaussianForecast(
                mean_f=g.mean_f, sigma_f=g.sigma_f,
                horizon_hours=g.horizon_hours,
                source_name=name, source_tag=g.source_tag,
            )
        # A3: replace the source's self-reported σ with the learned RMSE
        # for this (source, [city,] horizon bucket) when available.
        # Per-city skill σ first (post-2026-04-26 — actual error std varies
        # 0.9-2.0°F by city), pooled fallback, then source's own prior.
        g = _apply_learned_sigma(g, city_key=city_key)
        # B: inflate σ for source staleness. NBM updates every 6 hours,
        # so a randomly-fetched NBM forecast is ~3h stale on average →
        # treat it as if it were a 3h-older forecast horizon. Live obs
        # sources (METAR, MADIS) skip this. Sources that populate
        # ``issued_at`` use the actual staleness; others use the per-source
        # cadence-based assumption.
        g = _apply_staleness_inflation(g)
        # A5: shift mean by -bias for this (source, city, season, bucket)
        # when a MOS bias fit has been persisted. Cold-cache = no shift.
        g = _apply_mos_bias(g, city_key)
        # B (per-source σ ceiling): cap σ so any source whose skill curve
        # hasn't been fit (NWS Point, MADIS, AFD have only n=12 backfill
        # rows and inherit a 3+°F pooled fallback) can't be effectively
        # excluded from the precision-weighted combine. The ceiling is set
        # so a worst-case fallback source still contributes ~25% as much
        # precision as a well-fit one (precision = 1/σ²; cap σ=2.0 vs
        # well-fit σ=1.0 → precision ratio 0.25). Properly fits later via
        # the snapshots-based skill fitter (Option A).
        if g.sigma_f > _SOURCE_SIGMA_CEILING_F:
            g = g.with_sigma(_SOURCE_SIGMA_CEILING_F)
        out.append(g)
    return out


def _weighted_inputs_with_group_discount(
    gaussians: list[GaussianForecast],
) -> list[WeightedForecast]:
    """Pre-scale each source's weight by the learned group effective-N.

    For each source in a group of ``n`` actually-present members with
    learned correlation ``rho``, the per-source weight is

        weight = n_eff / n   where   n_eff = n / (1 + (n-1) * rho)

    so the group's total precision contribution to
    ``combine_gaussian`` is ``n_eff × (1/σ²)`` — exactly what
    ``n_eff`` independent sources would contribute.

    With ``rho=1.0`` (MVP fallback when no fit persisted), ``n_eff=1``
    and ``weight=1/n`` — byte-identical to the A2.2 "full discount"
    MVP. With a learned ``rho < 1``, correlated-but-not-identical
    sources get more weight.
    """
    group_counts: dict[str, int] = {}
    for g in gaussians:
        grp = _group_of(g.source_name)
        group_counts[grp] = group_counts.get(grp, 0) + 1

    group_rho: dict[str, float] = {
        grp: _get_group_rho(grp) for grp in group_counts
    }

    inputs: list[WeightedForecast] = []
    for g in gaussians:
        grp = _group_of(g.source_name)
        n = max(1, group_counts.get(grp, 1))
        rho = group_rho.get(grp, _GROUP_RHO_FALLBACK)
        denom = 1.0 + (n - 1) * rho
        # Defensive: pathological anti-correlation could make denom ≤ 0,
        # which would blow n_eff up or flip its sign. Cap at "fully
        # independent" (n_eff = n, weight = 1) — conservative given that
        # real model/obs families never anti-correlate this strongly.
        if denom <= 0:
            n_eff = float(n)
        else:
            n_eff = n / denom
        weight = n_eff / n
        inputs.append(WeightedForecast(forecast=g, weight=weight))
    return inputs


def _snapshot_rows(
    series: str, ticker: str, now_iso: str,
    gaussians: list[GaussianForecast],
    combined: GaussianForecast,
    afd_tag: Optional[str], afd_bias: Optional[float],
    combined_prob: float,
):
    """Build the list of ``weather_forecast_snapshots`` rows to insert.

    Columns per db.py: (recorded_at, series, ticker, source, forecast_prob,
    forecast_high_f, sigma_f, hours_out). For Gaussian components we
    record forecast_high_f + sigma_f + hours_out and leave prob NULL
    (projection happens centrally). For the combined row we record both
    the combined Gaussian AND the projected prob so backtest readers
    don't need to re-project.
    """
    rows = []
    for g in gaussians:
        rows.append((
            now_iso, series, ticker, g.source_name,
            None,                         # forecast_prob (Gaussian sources log mean, not prob)
            g.mean_f, g.sigma_f,
            int(round(g.horizon_hours)),
        ))
    if afd_tag is not None and afd_bias is not None:
        rows.append((
            now_iso, series, ticker, "afd_bias",
            None,                         # not a prob
            afd_bias, None,               # mean_f slot repurposed as bias_f
            None,
        ))
    rows.append((
        now_iso, series, ticker, "combined_v2",
        combined_prob,
        combined.mean_f, combined.sigma_f,
        int(round(combined.horizon_hours)),
    ))
    return rows


def _write_snapshots(rows):
    if not rows:
        return

    def _do(conn):
        conn.executemany(
            """INSERT INTO weather_forecast_snapshots
               (recorded_at, series, ticker, source, forecast_prob,
                forecast_high_f, sigma_f, hours_out)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    try:
        db_write(_do)
    except Exception as e:
        print(f"[weather_ensemble_v2] snapshot error: {type(e).__name__}: {e}")


def predict_v2(
    ticker: str, market_data: dict, yes_ask: Optional[float] = None,
) -> tuple[Optional[float], Optional[str]]:
    """Gaussian-first weather ensemble. Signature matches v1 ``predict``.

    Returns (prob, source_tag) or (None, None). Callable as a drop-in
    replacement for v1 when ``WEATHER_ENSEMBLE_V2`` is enabled.
    """
    if market_data is None:
        return None, None

    ticker_upper = (ticker or "").upper()
    is_weather_ticker = (
        ticker_upper.startswith("KXHIGH")
        or ticker_upper.startswith("KXHMONTHRANGE")
        or ticker_upper.startswith("KXHURR")
    )
    if not is_weather_ticker:
        return None, None

    # 1. Collect Gaussians from all sources that have an opinion
    gaussians = _collect_gaussians(ticker, market_data)
    if not gaussians:
        return None, None

    # 2. Parse market to know how to project. Bail if we can't resolve
    # the threshold or bracket bounds — a Gaussian we can't project is
    # worse than no estimate.
    projection = _parse_market_for_projection(ticker, market_data)
    if projection is None:
        return None, None
    is_bracket, threshold_f, is_above, bracket_lo_f, bracket_hi_f = projection

    # 3. Pre-scale weights by 1/group_size → precision-weighted combine
    weighted = _weighted_inputs_with_group_discount(gaussians)
    combined = combine_gaussian(weighted, combined_name="combined_v2")
    if combined is None:
        return None, None

    # 4. AFD bias shift (bias-space, not prob-space)
    afd_bias_f: Optional[float] = None
    afd_tag: Optional[str] = None
    try:
        from bot.signals.sources.afd import get_afd_bias

        bias_val, afd_conf, bias_tag = get_afd_bias(ticker, market_data)
        if bias_val is not None and afd_conf is not None:
            # Confidence-weighted shift: low-confidence keyword matches
            # contribute little, high-confidence LLM reads more. Capped at
            # ±_MAX_AFD_SHIFT_ABS_F to keep a runaway parse from blowing
            # the combined mean past climatology.
            effective_shift = bias_val * afd_conf
            effective_shift = max(
                -_MAX_AFD_SHIFT_ABS_F,
                min(_MAX_AFD_SHIFT_ABS_F, effective_shift),
            )
            combined = combined.shifted(effective_shift)
            afd_bias_f = effective_shift
            afd_tag = bias_tag
    except Exception as e:
        print(f"[weather_ensemble_v2] afd_bias error: {type(e).__name__}: {e}")

    # 4b. Inflate combined σ to compensate for too-tight source priors.
    # See _SIGMA_INFLATION_DEFAULT note for the empirical rationale; this
    # is a temporary global correction until A3 skill-curve fits override
    # σ per (source, horizon).
    combined = _apply_sigma_inflation(combined)

    # 4c. Enforce running-high floor: daily HIGH cannot be below already-
    # observed running max. Belt-and-suspenders with C: even with the
    # truncated projection in step 5 below, raising the combined mean
    # avoids weird shapes when the floor is far above combined μ.
    combined = _apply_running_high_floor(combined, gaussians)

    # 4d. σ floor for bracket-resolution uncertainty.
    #
    # Late-day per-(station, LST hour) residual σ values fit at
    # 0.20-0.31°F (we measured these from 90 days of hourly METAR vs
    # observed daily-high pairs). Those values are correct as estimates
    # of std(eventual_daily_high − running_max_at_h) — but they don't
    # account for *bracket-resolution* uncertainty: which specific 1°F
    # Kalshi bracket the actual peak ends up in.
    #
    # With combined σ = 0.20°F, a Gaussian projection puts ~99% of
    # probability in a single 1°F bracket. If our μ is 0.5°F off the
    # actual peak (sensor lag, late-arriving observations, rounding,
    # post-snapshot temperature movement), we post 99% on the *wrong*
    # bracket and eat a Brier of ~0.98 per quote. Discovered 2026-04-28
    # via late-night Brier audit on hot cities (NY/MIA/LAX): ~0.13 Brier
    # vs market's ~0.0001 in evening/late-night windows when σ collapses.
    #
    # The floor ensures meaningful probability lands in adjacent
    # brackets too. 0.5°F balances tightness (we still concentrate most
    # mass in the right bracket when we know it) vs robustness (we
    # don't catastrophically miss when we're slightly off).
    if combined.sigma_f < _COMBINED_SIGMA_FLOOR_F:
        combined = combined.with_sigma(_COMBINED_SIGMA_FLOOR_F)

    # 5. Project combined Gaussian onto the market.
    # Option C (H3 conditional, 2026-04-29): only pass METAR's μ as the
    # truncation floor when combined.μ is materially below the observed
    # running max. Step 4c already shifts combined.μ up to METAR.μ in the
    # forecast-disagrees-with-observation case, so by step 5 combined.μ ≥
    # METAR.μ in every path where METAR is present. Applying truncation
    # unconditionally then re-amplifies the residual upper-tail by
    # 1/p_above_t — ≈2× when combined.μ ≈ METAR.μ — and pushes us to the
    # 0.995 clamp on whichever bracket sits at the running max.
    #
    # The pre-CF6 sweep showed unconditional truncation was -0.0046 worse
    # than trunc-off; we kept it because removing it net-hurt. After the
    # 2026-04-28 CF6 ground-truth fix (which moved learned μ closer to
    # truth across all stations), trunc-off is now +0.0146 BETTER than
    # the unconditional path — the same amplification that accidentally
    # rescued cold-biased predictions now over-amplifies on accurate
    # ones. Validated with sweep_v2_hypotheses.py post-CF6, n=143;
    # 4 of 6 families improved (MIA −0.034, NY −0.026, AUS −0.024,
    # DEN −0.014), CHI/LAX neutral.
    #
    # The 0.5°F dead-zone is defensive: keeps truncation available for
    # edge cases where step 4c is somehow bypassed, preserving the
    # catastrophic-miss correction (forecast 65°F, METAR 73°F) that the
    # original truncation was designed for.
    truncation_floor: Optional[float] = None
    for g in gaussians:
        if g.source_name == "metar" and math.isfinite(g.mean_f):
            if combined.mean_f < g.mean_f - 0.5:
                truncation_floor = g.mean_f
            break
    try:
        gaussian_prob = probability_for_market(
            combined,
            is_bracket=is_bracket,
            threshold_f=threshold_f,
            is_above=is_above,
            bracket_lo_f=bracket_lo_f,
            bracket_hi_f=bracket_hi_f,
            truncation_floor_f=truncation_floor,
        )
    except ValueError as e:
        print(f"[weather_ensemble_v2] projection error: {e}")
        return None, None

    # 6. NOAA alerts: logit-space blend. Rare signal; weak secondary vote.
    final_prob = gaussian_prob
    noaa_tag: Optional[str] = None
    try:
        from bot.signals.sources.weather import get_noaa_alerts_for_market

        noaa_prob, noaa_src = get_noaa_alerts_for_market(ticker, market_data)
        if noaa_prob is not None:
            # Weight Gaussian by effective group count (roughly how many
            # independent votes it represents); NOAA weight is fixed.
            groups_present = len({_group_of(g.source_name) for g in gaussians})
            w_g = max(1.0, float(groups_present))
            w_n = _NOAA_LOGIT_WEIGHT
            blended_logit = (
                w_g * _logit(gaussian_prob) + w_n * _logit(noaa_prob)
            ) / (w_g + w_n)
            final_prob = max(0.02, min(0.98, _inv_logit(blended_logit)))
            noaa_tag = noaa_src
    except Exception as e:
        print(f"[weather_ensemble_v2] noaa error: {type(e).__name__}: {e}")

    # 7. Snapshot log (best-effort)
    series = _series_from_ticker(ticker)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        rows = _snapshot_rows(
            series, ticker, now_iso, gaussians, combined,
            afd_tag, afd_bias_f, final_prob,
        )
        _write_snapshots(rows)
    except Exception as e:
        print(f"[weather_ensemble_v2] snapshot build error: {type(e).__name__}: {e}")

    # 8. Build provenance tag
    parts = [g.source_name for g in gaussians]
    if afd_tag:
        parts.append("afd")
    if noaa_tag:
        parts.append("noaa")
    tag = f"weather_ensemble_v2:{'+'.join(parts)}"

    print(
        f"[weather_ensemble_v2] {series} n={len(gaussians)} "
        f"μ={combined.mean_f:.1f}°F σ={combined.sigma_f:.2f}°F "
        f"afd={afd_bias_f if afd_bias_f is not None else 'none'} "
        f"p={final_prob:.3f}"
    )
    return final_prob, tag
