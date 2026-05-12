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
# 2026-04-30: ICON and UKMO added to _MODEL_GROUP. They're global NWP
# models trained on similar physics + reanalysis data as HRRR / NWS Point /
# Open-Meteo, so they belong in the model-correlation group. Pre-fix they
# fell into _group_of() == "other" which had no correlation discount, so
# each contributed full-weight precision and effectively double-counted
# the model family. Verified live: `_group_of('icon') == 'model'` after
# this change. Pinned by tests/signals/test_weather_ensemble_v2.py.
# 2026-04-30 (later): GEM, METNO, ECMWF added — same NWP family, same
# group routing. Validated independence ρ vs HRRR: GEM 0.61, MetNo 0.73,
# ECMWF 0.53 — all clearly correlated enough to belong with the models.
_MODEL_GROUP: frozenset[str] = frozenset(
    {"hrrr", "nbm", "nws_point", "weather", "icon", "ukmo",
     "gem", "metno", "ecmwf"}
)
# 2026-04-30: nws_5min added as a sub-hourly observation channel parallel
# to METAR. Both read the same ASOS sensor family; group correlation
# discount applies. METAR remains the canonical hourly observation;
# nws_5min adds 5-min resolution during the peak heating window.
# 2026-05-02: nws_5min_diurnal added — METAR's diurnal regression
# fed by 5-min readings. Same physical sensor family as METAR + nws_5min
# so group correlation discount applies. nws_5min_analog is registered
# here for forward-compat (it shares the same sensor data) but is
# currently NOT in the live combine — see _collect_gaussians for why.
_OBS_GROUP: frozenset[str] = frozenset({
    "metar", "madis", "nws_5min", "nws_5min_diurnal", "nws_5min_analog",
})

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

    # Two-pass lookup: per-(source, city, bucket) first, then pooled
    # (source, bucket). The per-city key is gated on a minimum sample
    # size (_PER_CITY_SIGMA_MIN_SAMPLES) — thin fits routinely produce
    # noise σ values 3-5x off pooled (see constant docstring).
    per_city_key = (
        f"{_SKILL_KEY_PREFIX}{source_name}_{city_key}_{bucket}"
        if city_key else None
    )
    pooled_key = f"{_SKILL_KEY_PREFIX}{source_name}_{bucket}"

    def _read_sigma(key: str, require_min_samples: bool) -> Optional[float]:
        try:
            payload = kv_get(conn, key)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if require_min_samples:
            n = payload.get("n")
            if not isinstance(n, (int, float)) or n < _PER_CITY_SIGMA_MIN_SAMPLES:
                return None
        sigma = payload.get("sigma")
        if not isinstance(sigma, (int, float)):
            return None
        sigma_f = float(sigma)
        # Reject pathological σ. Weather-source RMSE stays in 0.1-15°F; outside
        # that range we suspect a stale / corrupt fit and fall back.
        if not (0.1 <= sigma_f <= 15.0):
            return None
        return sigma_f

    if per_city_key is not None:
        sigma_per_city = _read_sigma(per_city_key, require_min_samples=True)
        if sigma_per_city is not None:
            return sigma_per_city
    return _read_sigma(pooled_key, require_min_samples=False)


# Sample-size gate on per-(source, city) σ fits.
#
# Per-city skill σ values fit on small n (n<60 days) are noisy enough that
# they routinely produce nonsense — e.g. ``weather_skill_hrrr_los_angeles_0_6``
# was fit at σ=6.0°F + bias=+5.5°F on n=18, while pooled HRRR sits at σ=1.20°F
# on n=684. The thin-cell payload is technically a "fit" but it's a sample-
# variance overshoot, not a real signal.
#
# Below this floor we drop the per-city key and fall back to pooled. 60 was
# chosen by inspection of the persisted kv values on 2026-04-30: at n≥60 the
# (source, city) σ values cluster within 1.5x of pooled; below that they
# spread 3-5x.
_PER_CITY_SIGMA_MIN_SAMPLES: int = 60


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

# Per-source learned σ FLOOR. Symmetric counterpart to the ceiling.
# Without this, a self-referential or under-sampled fit can produce a
# σ ≪ 1°F (e.g. METAR fit on n=18 with the observation already baked
# into "ground truth" → σ=0.3°F seen 2026-04-29). Such a tight σ gives
# precision = 1/σ² = 11+, which dominates the combine and collapses
# the ensemble's σ to <0.5°F → adjacent-bracket probabilities go to
# 99% / 1% extremes → directional shadow Brier blew up from 0.16 to
# 0.46 on KXHIGHNY (4×).
#
# 1.5°F floor: leaves room for genuinely-skilled sources (HRRR fit at
# 1.16°F pooled) while preventing pathological tight σ from any single
# source dominating. Caps any one source's precision contribution at
# 1/2.25 = 0.44.
_LEARNED_SIGMA_FLOOR_F: float = 1.5


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

    The learned σ is floored at ``_LEARNED_SIGMA_FLOOR_F`` (1.5°F) — see
    the constant's docstring for why. Briefly: pathological self-
    referential fits can produce σ ≪ 1°F that dominates the combine and
    collapses ensemble σ to <0.5°F.
    """
    learned = _get_learned_sigma(g.source_name, g.horizon_hours, city_key=city_key)
    if learned is None:
        return g
    learned = max(learned, _LEARNED_SIGMA_FLOOR_F)
    return g.with_sigma(learned)


def _apply_learned_sigma_with_flag(
    g: GaussianForecast, city_key: Optional[str] = None,
) -> tuple[GaussianForecast, bool]:
    """Like ``_apply_learned_sigma`` but returns whether a learned value
    was actually applied. The σ ceiling logic in ``_collect_gaussians``
    uses this flag — sources WITH a learned σ are trusted (no ceiling
    clip), sources WITHOUT one are protected by the ceiling so they
    contribute meaningfully on day 1.

    2026-04-29: previously the ceiling clipped EVERY source unconditionally,
    which lied about NWS Point's true ~5°F RMSE (clipped to 2.0°F →
    over-weighted in combine). Now: ceiling only fires for unfit sources;
    learned σ values pass through but are floored at
    ``_LEARNED_SIGMA_FLOOR_F`` (regression discovered when METAR's fit
    produced σ=0.3°F → ensemble σ-collapse → Brier blew up).
    """
    learned = _get_learned_sigma(g.source_name, g.horizon_hours, city_key=city_key)
    if learned is None:
        return g, False
    learned = max(learned, _LEARNED_SIGMA_FLOOR_F)
    return g.with_sigma(learned), True


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
# a single outlier cell pulling the ensemble too far. 2026-05-03: bumped
# 5.0 → 8.0 so persistent regime biases get applied (e.g. LAX marine
# layer producing sustained +5-7°F warm bias across HRRR/weather/UKMO).
# Must stay in sync with weather_mos_materializer + backfill_weather_*.
_MOS_BIAS_MAX_ABS_F: float = 8.0


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


def _get_mos_bias(
    source_name: str, city_key: str,
    regime_label: Optional[str] = None,
) -> Optional[float]:
    """Return the persisted EWMA bias (°F) for this (source, city, [regime]).

    Two-tier lookup: if ``regime_label`` is supplied AND a regime-conditional
    key exists for it, that wins. Otherwise falls back to the pooled
    (source, city) bias. Returns None when neither key is present so the
    caller leaves the Gaussian untouched.

    Why regime-conditional: MOS bias is not constant across weather regimes.
    HRRR may run +0.5°F warm on clear days but +1.8°F warm on overcast days;
    pooling these gives a single number that's wrong for both conditions.
    On 2026-04-30 we saw a residual −0.88°F cool bias on settlement winners
    even after pooled MOS subtraction — likely a regime-mixing artifact.
    Regime-conditional keys are produced by
    ``bot.learning.mos_bias_regime_fitter``.
    """
    try:
        conn = get_connection()
    except RuntimeError:
        return None

    keys_to_try: list[str] = []
    if regime_label and regime_label != "unknown":
        keys_to_try.append(
            f"{_MOS_BIAS_KEY_PREFIX}{source_name}_{city_key}_{regime_label}"
        )
    keys_to_try.append(f"{_MOS_BIAS_KEY_PREFIX}{source_name}_{city_key}")

    for key in keys_to_try:
        try:
            payload = kv_get(conn, key)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        bias = payload.get("bias")
        if not isinstance(bias, (int, float)):
            continue
        bias_f = float(bias)
        if not math.isfinite(bias_f):
            continue
        # Clamp — an outlier cell should never move the Gaussian by > cap.
        return max(-_MOS_BIAS_MAX_ABS_F, min(_MOS_BIAS_MAX_ABS_F, bias_f))
    return None


def _current_regime_label_for_city(city_key: str) -> Optional[str]:
    """Look up the regime label active for ``city_key`` right now.

    Reads METAR's per-station regime telemetry side-channel (populated
    when METAR's Gaussian is built earlier in the same cycle). Returns
    None when no METAR Gaussian fired yet for this city — the caller
    falls back to pooled MOS bias.
    """
    try:
        from bot.daemon.stations import STATION_BY_CITY
        from bot.signals.sources.metar_observations import get_residual_tier_meta
    except Exception:
        return None
    ws = STATION_BY_CITY.get(city_key) if hasattr(STATION_BY_CITY, "get") else None
    if ws is None:
        return None
    meta = get_residual_tier_meta(ws.icao) if ws else None
    if not isinstance(meta, dict):
        return None
    label = meta.get("regime_label")
    if not isinstance(label, str) or not label or label == "unknown":
        return None
    return label


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
    # Regime-conditional lookup first (when the active regime label is
    # known for this city), pooled fallback inside _get_mos_bias.
    regime_label = _current_regime_label_for_city(city_key)
    bias = _get_mos_bias(g.source_name, city_key, regime_label=regime_label)
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
# Per-family override: ``weather_sigma_inflation_<KXHIGHMIA>`` etc. Read
# first; falls through to the global key when absent. Set by the per-
# family Brier sweep (tools/sigma_inflation_per_family.py); each family's
# combined-σ has a different optimal multiplier because the underlying
# source-disagreement profile differs (LAX marine layer → very wide
# optimal; DEN well-calibrated → no inflation needed).
_SIGMA_INFLATION_FAMILY_KEY_PREFIX: str = "weather_sigma_inflation_"
_SIGMA_INFLATION_ENV: str = "WEATHER_SIGMA_INFLATION"
_SIGMA_INFLATION_DEFAULT: float = 1.0
_SIGMA_INFLATION_MIN: float = 1.0
_SIGMA_INFLATION_MAX: float = 4.0


def _clamp_inflation(x: float) -> float:
    return max(_SIGMA_INFLATION_MIN, min(_SIGMA_INFLATION_MAX, x))


def _payload_to_factor(payload: object) -> Optional[float]:
    """Extract a numeric factor from a kv payload. Accepts both
    ``{"factor": float}`` and bare-number shapes; returns None when the
    payload is missing or unparseable so the caller can fall through to
    the next layer."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        f = payload.get("factor")
        if isinstance(f, (int, float)) and math.isfinite(f):
            return float(f)
        return None
    if isinstance(payload, (int, float)) and math.isfinite(payload):
        return float(payload)
    return None


def _family_from_ticker(ticker: Optional[str]) -> Optional[str]:
    """Extract the family prefix from a Kalshi ticker. ``KXHIGHMIA-…``
    → ``KXHIGHMIA``. Returns None when input is missing/malformed."""
    if not ticker:
        return None
    base = ticker.split("-")[0].strip().upper()
    return base or None


def _get_sigma_inflation(ticker: Optional[str] = None) -> float:
    """Resolve the σ-inflation factor.

    Precedence:
      1. kv_cache ``weather_sigma_inflation_<FAMILY>`` (per-family override)
      2. kv_cache ``weather_sigma_inflation`` (global override)
      3. env ``WEATHER_SIGMA_INFLATION``
      4. constant ``_SIGMA_INFLATION_DEFAULT``

    Clamped to [_SIGMA_INFLATION_MIN, _SIGMA_INFLATION_MAX]; non-finite
    or non-numeric payloads fall through to the next layer. ``ticker`` is
    optional — when None or unparseable, only the global/env/default
    layers are consulted.
    """
    try:
        conn = get_connection()
    except RuntimeError:
        conn = None
    if conn is not None:
        family = _family_from_ticker(ticker)
        if family:
            try:
                fam_payload = kv_get(
                    conn, f"{_SIGMA_INFLATION_FAMILY_KEY_PREFIX}{family}",
                )
            except Exception:
                fam_payload = None
            fam_factor = _payload_to_factor(fam_payload)
            if fam_factor is not None:
                return _clamp_inflation(fam_factor)
        try:
            payload = kv_get(conn, _SIGMA_INFLATION_KEY)
        except Exception:
            payload = None
        global_factor = _payload_to_factor(payload)
        if global_factor is not None:
            return _clamp_inflation(global_factor)

    env = os.environ.get(_SIGMA_INFLATION_ENV)
    if env:
        try:
            v = float(env)
            if math.isfinite(v):
                return _clamp_inflation(v)
        except ValueError:
            pass
    return _clamp_inflation(_SIGMA_INFLATION_DEFAULT)


# 2026-05-04 — σ inflation is TTE-aware. Pre-peak (TTE > _TTE_FULL_H) the
# full per-family inflation factor applies because forecasts genuinely
# disagree about the eventual peak. Post-peak (TTE < _TTE_NONE_H) the
# answer is essentially observed; the right thing for σ is to COLLAPSE
# toward the observation, not inflate. Linear decay between the two.
#
# Postmortem rationale: KXHIGHNY canary 2026-05-03 lost $1.45 because at
# TTE=5.9h the daemon applied σ × 3.0 to a combined μ that disagreed
# 2-3°F with already-observed peak. Wide σ said "any 1°F bracket has
# only 7-8% probability" exactly when reality had locked in the answer.
_TTE_FULL_H: float = 8.0
_TTE_NONE_H: float = 2.0


def _decay_factor_for_tte(
    base_factor: float, tte_hours: Optional[float],
) -> float:
    """Decay the per-family σ inflation factor as time-to-settlement
    shrinks. ``base_factor`` is the kv-resolved factor (1.0-4.0).
    ``tte_hours`` is hours-to-settle in the LST settlement window.

    Returns ``base_factor`` when TTE >= _TTE_FULL_H or unknown.
    Returns 1.0 when TTE <= _TTE_NONE_H.
    Linear interpolation between, capped at the original base.
    """
    if tte_hours is None or tte_hours >= _TTE_FULL_H:
        return base_factor
    if tte_hours <= _TTE_NONE_H:
        return 1.0
    span = _TTE_FULL_H - _TTE_NONE_H
    progress = (tte_hours - _TTE_NONE_H) / span  # 0 at TTE_NONE, 1 at TTE_FULL
    return 1.0 + (base_factor - 1.0) * progress


def _apply_sigma_inflation(
    g: GaussianForecast,
    ticker: Optional[str] = None,
    tte_hours: Optional[float] = None,
) -> GaussianForecast:
    """Multiply the combined Gaussian's σ by the resolved inflation factor.

    Per-family override is consulted when ``ticker`` is provided. The
    factor is then decayed toward 1.0 based on ``tte_hours`` so post-peak
    quotes don't suffer the wide-σ-spreads-mass-across-impossible-brackets
    pathology. When ``tte_hours`` is None, full base factor applies
    (back-compat for callers that don't pass it).

    Mean / horizon / provenance are preserved.
    """
    base_factor = _get_sigma_inflation(ticker)
    factor = _decay_factor_for_tte(base_factor, tte_hours)
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


# Snapshot writer health counters. Set after the 26APR26 outage —
# weather_forecast_snapshots had ~22h of zero writes (Apr 25 22:00 UTC →
# Apr 26 19:59 UTC) while shadow writes continued, so the diagnostic lost
# 19% of one day's settlement cohort. The original code swallowed write
# errors with print(...) which never surfaced in the daemon health log.
# Counters here are read once per HEALTH_LOG_INTERVAL_S window by
# bot.daemon.main._log_health and reset to 0 — so a recurring failure
# mode shows up as a non-zero failure count on every health line.
_SNAPSHOT_WRITE_OK: int = 0
_SNAPSHOT_WRITE_FAIL: int = 0
_SNAPSHOT_BUILD_FAIL: int = 0


def get_and_reset_snapshot_health_stats() -> dict[str, int]:
    """Return cumulative snapshot writer counters since the last call,
    then reset them. Designed for the periodic health-log emitter.
    """
    global _SNAPSHOT_WRITE_OK, _SNAPSHOT_WRITE_FAIL, _SNAPSHOT_BUILD_FAIL
    stats = {
        "write_ok": _SNAPSHOT_WRITE_OK,
        "write_fail": _SNAPSHOT_WRITE_FAIL,
        "build_fail": _SNAPSHOT_BUILD_FAIL,
    }
    _SNAPSHOT_WRITE_OK = 0
    _SNAPSHOT_WRITE_FAIL = 0
    _SNAPSHOT_BUILD_FAIL = 0
    return stats


def _is_afd_late_day_skipped(ticker: str) -> bool:
    """True when the AFD shift should be suppressed for this ticker
    because we're past peak heating in the ticker's local time.

    AFD encodes the forecaster's commentary on how the day will unfold.
    Past LST 14 the daily high is mostly observed; AFD shifts after
    that just add noise. Verified live 2026-05-01: post-peak AFD added
    ~+1.5°F warm bias to evening combined μ for KMIA / KLAX where the
    day's high had already passed.

    Pulled out as a module-level function so tests can monkey-patch
    deterministically — without it, test outcomes depend on wall-clock
    time when pytest runs, which is the kind of flakiness we don't want.
    """
    try:
        from bot.daemon.stations import (
            lst_offset_for_station, station_for_ticker,
        )
        ws = station_for_ticker(ticker)
        if ws is None:
            return False
        lst_offset = lst_offset_for_station(ws.icao)
        lst_now = datetime.now(timezone.utc) + (
            datetime.now(timezone.utc) - datetime.now(timezone.utc)  # zero
        )
        # Compute LST hour explicitly without relying on tz arithmetic
        # confusion — UTC + offset_hours is the LST clock.
        from datetime import timedelta as _td
        lst = datetime.now(timezone.utc) + _td(hours=lst_offset)
        return lst.hour >= 14
    except Exception:
        return False


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
    from bot.signals.sources.ecmwf import get_ecmwf_gaussian
    from bot.signals.sources.gem import get_gem_gaussian
    from bot.signals.sources.hrrr import get_hrrr_gaussian
    from bot.signals.sources.icon import get_icon_gaussian
    from bot.signals.sources.metar_observations import get_metar_gaussian
    from bot.signals.sources.metno import get_metno_gaussian
    from bot.signals.sources.nws_5min import get_nws_5min_gaussian
    from bot.signals.sources.nws_5min_analog import get_nws_5min_analog_gaussian
    from bot.signals.sources.nws_5min_diurnal import get_nws_5min_diurnal_gaussian
    from bot.signals.sources.nws_point import get_nws_point_gaussian
    from bot.signals.sources.ukmo import get_ukmo_gaussian
    from bot.signals.sources.weather import get_weather_gaussian

    # 2026-04-29 architectural revert: IEM 1-min ASOS removed from the
    # live combine. Discovered post-deploy that IEM 1-min has ~24h
    # publication latency — its `asos1min.py` endpoint serves archival
    # historical data, not live observations. The eval recorded MAE
    # 1.15°F because it ran retroactively against past dates where
    # data WAS available; for forward-looking quotes IEM always returns
    # None for the current-day market.
    #
    # The IEM 1-min source module + state row + pre-seed values stay
    # in the codebase for two reasons:
    #   1. Future use as a high-accuracy retrospective ground truth for
    #      MOS bias fitting (CF6 is the canonical Kalshi-settlement
    #      source, but IEM 1-min adds higher-resolution validation).
    #   2. Documenting the lesson — re-adding IEM as a live source
    #      would be a regression.
    #
    # METAR via aviationweather.gov remains the only real-time
    # observation channel. Use it directly.

    # 2026-04-26: Tomorrow.io dropped — TOS storage clause + reanalysis-only
    # historical endpoint (can't backfill calibration). See bot/config.py
    # TOMORROW_API_KEY note. The function still exists in
    # bot/signals/sources/weather.py for code-archeology / potential
    # Visual-Crossing-style replacement, but is no longer wired in here.
    #
    # 2026-04-29: NBM removed from the combine. Live probe confirmed the
    # "nbm" source (Open-Meteo with ``models=gfs_seamless``) returns
    # values literally identical to the "weather" source (Open-Meteo
    # default) for US lat/lons — to 0.0°F across 7 forecast days. The
    # default Open-Meteo blend at temperate-zone US is GFS, full stop;
    # forcing gfs_seamless is a no-op. Including both let the precision-
    # weighted combine treat one source as two, halving the effective
    # weight of every other source. Real NBM lives at NOAA's NBM API,
    # not Open-Meteo. ``get_nbm_gaussian`` is preserved in
    # ``bot.signals.sources.ndfd_nbm`` for back-compat with audit tools
    # and tests that snapshot the v1 source list, but it is no longer
    # wired into the production combine. Pin:
    # tests/signals/test_weather_ensemble_v2_sources.py.
    #
    # 2026-04-29: MADIS removed from the combine. Its warming heuristic
    # (median current temp + min(8°F, (15-hour)*1.5) before 3pm LST) is a
    # strictly worse model than what ``metar_observations.get_metar_gaussian``
    # already produces via the learned residual-σ machinery. Empirically
    # produces -8 to -17°F bias on early-morning observations in spring
    # because the +8°F flat bump under-models 20-30°F morning-to-peak
    # swings. ``get_madis_gaussian`` preserved for back-compat as above.
    # 2026-04-29: ICON, UKMO added (Phase B.2). Pre-seeded σ + bias +
    # PROBATIONARY state from the eval data. Auto-promote to ACTIVE
    # after 50+ settled rows with non-regression on combined Brier.
    # IEM 1-min was added then removed same day (24h publication
    # latency makes it useless as live observation).
    # 2026-04-30: GEM, MetNo, ECMWF added. Validation in
    # tools/investigate_new_forecast_sources.py (n=174 city-days):
    #   GEM:   pooled MAE 1.80°F, ensemble Δ -0.215°F (-12%, best)
    #   MetNo: pooled MAE 1.96°F, ensemble Δ -0.069°F (-4%)
    #   ECMWF: pooled MAE 2.72°F, ensemble Δ -0.052°F (-3%, but
    #          residual ρ=0.34 with ICON — most independent of all)
    # All three start in PROBATIONARY via state_machine pre-seed;
    # auto-promote to ACTIVE after 50+ settled rows with non-regression.
    # 2026-05-05: `weather` removed from the live combine. Per-city
    # scorecard analysis (reports/PER_SOURCE_INVESTIGATION_2026-05-05.md +
    # POSTFIX_REASSESSMENT_2026-05-05.md) showed corr(hrrr, weather) =
    # 0.994 (NY) / 1.000 (LAX) at peak window — both are Open-Meteo
    # (gfs_hrrr vs default blend). Default blend at US lat/lons IS GFS,
    # so `weather` was duplicating the HRRR signal and halving the
    # effective weight of every other source.
    # Module + getter retained in code; if drift between the two endpoints
    # ever needs monitoring, re-enable as a separate snapshot-only path
    # (don't re-add to the combine).
    getters = [
        ("hrrr", get_hrrr_gaussian),
        ("nws_point", get_nws_point_gaussian),
        # ("weather", get_weather_gaussian),  # dropped 2026-05-05 (dup of hrrr)
        ("icon", get_icon_gaussian),
        ("ukmo", get_ukmo_gaussian),
        ("gem", get_gem_gaussian),
        ("metno", get_metno_gaussian),
        ("ecmwf", get_ecmwf_gaussian),
        ("metar", get_metar_gaussian),
        # 2026-04-30: nws_5min wired in. Sub-hourly ASOS observations
        # via NWS api.weather.gov. 5-min granularity, 15-25 min
        # publication lag (the source widens σ via the issued_at
        # path + staleness_inflation when readings are old).
        ("nws_5min", get_nws_5min_gaussian),
        # 2026-05-02: nws_5min_diurnal feeds the freshest 5-min reading
        # through METAR's existing per-(station, lst_hour) diurnal
        # regression. Updates every 5 min instead of METAR's 60-min
        # pace while reusing the 90+ days of CF6-corrected fit data.
        ("nws_5min_diurnal", get_nws_5min_diurnal_gaussian),
        # nws_5min_analog removed from live combine 2026-05-02:
        # post-deploy probe revealed (1) feature-vintage bug — the
        # AVG-across-ticker-lifetime statistic collapses to long-range
        # outlooks rather than morning-of consensus, so neighbors are
        # picked on stale features; (2) only 35 historical days,
        # peaks 78-88°F, can't span today's 93-95°F regime. Needs
        # latest-snapshot vintage strategy + ~4-6 weeks more history.
        # Module + tests retained for re-enable when both land.
        # ("nws_5min_analog", get_nws_5min_analog_gaussian),
    ]
    # 2026-05-04: per-city source exclusions land here. After we know
    # the city, drop sources the regression flagged as structurally
    # biased for that station (e.g., nws_point for KNYC at -5.86°F,
    # gem/metno at KLAX at +9°F). See weather_sources.EXCLUDED_SOURCES_BY_CITY.
    from bot.signals.weather_sources import is_excluded_for_city

    out: list[GaussianForecast] = []
    for name, fn in getters:
        try:
            g = fn(ticker, market_data)
        except Exception as e:
            print(f"[weather_ensemble_v2] {name} raised {type(e).__name__}: {e}")
            continue
        if g is None:
            continue
        if is_excluded_for_city(name, city_key):
            # Source ran (so its predictions are still recorded in
            # snapshots upstream of this filter) but is dropped from
            # the live combine for this city.
            continue
        if g.source_name != name and not name.startswith("__"):
            # Defensive: source misidentified itself. Fix here so downstream
            # grouping uses the canonical key rather than the source's tag.
            # ``__name__`` keys are sentinel for multi-getter channels
            # (e.g., __observation_channel__ which dispatches to either
            # iem_1min or metar) — preserve the underlying source's
            # actual name so learned σ / MOS bias / snapshots key by it.
            g = GaussianForecast(
                mean_f=g.mean_f, sigma_f=g.sigma_f,
                horizon_hours=g.horizon_hours,
                source_name=name, source_tag=g.source_tag,
            )
        # A3: replace the source's self-reported σ with the learned RMSE
        # for this (source, [city,] horizon bucket) when available.
        # Per-city skill σ first (post-2026-04-26 — actual error std varies
        # 0.9-2.0°F by city), pooled fallback, then source's own prior.
        # Returns a flag indicating whether σ was actually replaced — the
        # ceiling below only fires for un-fit sources (no learned value).
        g, sigma_was_learned = _apply_learned_sigma_with_flag(g, city_key=city_key)
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
        # B (per-source σ ceiling): only protects unfit sources (no
        # learned σ in kv_cache yet). Once `_apply_learned_sigma`
        # returns a fitted value, we trust it — the ceiling's "ensure
        # contribution to combine" purpose is moot if we've measured the
        # source's true performance. Pre-2026-04-29 the ceiling fired
        # unconditionally, which clipped NWS Point's true ~5°F σ down to
        # 2°F and over-weighted it in the combine.
        if not sigma_was_learned and g.sigma_f > _SOURCE_SIGMA_CEILING_F:
            g = g.with_sigma(_SOURCE_SIGMA_CEILING_F)
        # C (state-machine inflation): probationary sources get σ × 1.3
        # to cap their weight while on trial. Active sources unchanged.
        # Shadow / demoted sources are filtered out at the next step.
        g = _apply_state_machine_inflation(g, city_key)
        # D (state-machine filter): drop sources whose state excludes
        # them from the combine (shadow, demoted). Snapshots still
        # record them upstream of this filter.
        if not _is_state_machine_active(g, city_key):
            continue
        out.append(g)
    return _apply_sanity_gate(out)


# ── State-machine helpers (Phase B.2) ─────────────────────────────────
# `bot.learning.source_state_machine` owns the lifecycle. These wrappers
# are how `_collect_gaussians` consults it per-cycle. Both functions
# fail-safe on any error (default: include the source) — a transient DB
# read failure should not silently zero out the combine.


def _apply_state_machine_inflation(
    g: GaussianForecast, city_key: Optional[str],
) -> GaussianForecast:
    """If the source is in PROBATIONARY state, inflate σ by the configured
    multiplier (1.3) to cap its weight while on trial. Active sources
    unchanged. Shadow / demoted not handled here — they're filtered at
    the next step."""
    try:
        from bot.db import get_connection
        from bot.learning.source_state_machine import (
            get_source_state, sigma_inflation_for_state,
        )
        conn = get_connection()
        state = get_source_state(conn, g.source_name, city_key or "pooled")
        mult = sigma_inflation_for_state(state)
        if mult != 1.0:
            return g.with_sigma(g.sigma_f * mult)
        return g
    except Exception:
        # Fail-safe: don't inflate on error. Source contributes at its
        # natural σ; subsequent filter still applies.
        return g


def _is_state_machine_active(
    g: GaussianForecast, city_key: Optional[str],
) -> bool:
    """True iff the source's state allows combine inclusion (active or
    probationary). Shadow / demoted return False.

    Fail-safe: on read errors return True (include). Better to over-include
    on a transient DB hiccup than to silently empty the combine."""
    try:
        from bot.db import get_connection
        from bot.learning.source_state_machine import (
            get_source_state, is_source_in_combine,
        )
        conn = get_connection()
        state = get_source_state(conn, g.source_name, city_key or "pooled")
        return is_source_in_combine(state)
    except Exception:
        return True


# Sanity gate: median-anchored outlier exclusion.
#
# Discovered 2026-04-29 that Open-Meteo and NWS Point regularly produce μ
# 5-15°F off the actual high at low TTE while reporting σ=2.0°F (way too
# tight for that error magnitude). Their precision weight stays at 1/4 in
# the combine, dragging combined.μ cold by 2-3°F. METAR+HRRR-only combine
# beats the 4-source combine by 44% Brier at TTE ≤1h.
#
# This gate excludes any source whose μ is more than ``_SANITY_GATE_F``
# away from the median of (METAR.μ, HRRR.μ) when both are present, else
# the median of all source μs. METAR is always kept (anchor source). The
# excluded source is still snapshot'd by the writer (``_write_snapshots``
# upstream of this filter) — only the live combine drops it.
#
# Threshold of 5°F: tight enough to catch the 5-15°F cold-bias cases we
# observed; loose enough that genuine forecast disagreements (~3°F at
# long TTE) are kept. Pinned by tests/signals/test_sanity_gate.py.
_SANITY_GATE_F: float = 5.0


_OBSERVATION_SOURCE_NAMES: frozenset[str] = frozenset({"metar", "iem_1min"})


# ─── METAR post-peak fast-path (2026-05-05) ─────────────────────────────────
#
# Phase 3c counterfactual ([reports/PHASE_3C_COUNTERFACTUAL_2026-05-05.md])
# showed that even with the May 3 lat/lon fix, post-fix combined σ at peak
# time is 5-7°F. The Gaussian projection onto narrow 2°F brackets gives
# every bracket ~10-15% probability — which the cross-bracket strategy
# interprets as "huge edge" against market prices that correctly reflect
# real-time METAR. Result: continued losses on the SAME settlements that
# pre-fix data also lost.
#
# Fix: when local solar time is past the city's typical peak hour AND
# METAR has a current observation AND the running max has been stable
# long enough that "peak surprise" is unlikely, replace the entire
# combine input with a single METAR-only Gaussian (μ = METAR running
# max, σ = 1.0°F). The 1°F σ captures residual uncertainty (METAR
# sample-rate misses an actual peak by 0.5-2°F on ~20% of days per
# scorecard `frac_within_1F` data).
#
# Why this is correct: post-peak, the day's daily high is essentially set
# and we observed it. NWP forecasts at this point add NOISE not info.
# The combine's precision-weighting was averaging sharp METAR (σ~0.3) with
# wide NWPs (σ~3) and producing moderate combined σ — losing the sharpness
# METAR alone offered.
#
# 2026-05-05 (Phase 3e): replaced fixed peak+2 buffer with adaptive
# stability detection. Per-city rules in
# bot.learning.cross_bracket_lst_gate.POST_PEAK_RULE_BY_SERIES encode
# the validated insight that later LST hours need less stability proof
# (solar heating has decayed), while earlier LST hours need more
# stability to catch convective/marine multi-modal days. Stability
# (hours since running max last increased) is read from the METAR
# poller's kv_cache state.

# σ to assign the synthetic METAR-only Gaussian when fast-path fires.
# 1.0°F captures the ~20% of days where official daily-high exceeds
# hourly METAR running max by ≥1°F (sample-rate misses, late spikes).
_METAR_POST_PEAK_SIGMA_F: float = 1.0


def _apply_metar_post_peak_override(
    gaussians: list[GaussianForecast],
    ticker: str,
    *,
    now_ts: Optional[float] = None,
    last_increase_lst_hour_override: Optional[int] = None,
) -> list[GaussianForecast]:
    """Post-peak: replace the combine input with a single METAR-only
    Gaussian. Returns ``gaussians`` unchanged if any condition fails.

    Conditions for fast-path to fire:
      1. LST at decision time is ≥ peak_hour + ``_METAR_POST_PEAK_BUFFER_HOURS``
         on the **target settlement day** (not next day's pre-peak).
      2. A METAR Gaussian is present in ``gaussians`` (i.e., the
         observation channel is currently working).
      3. METAR's mean is finite and inside a sane temperature band.

    When the override fires, returned list has exactly one element with
    ``source_name = "metar_post_peak_override"`` so downstream snapshot
    writers tag rows with the override's identity.

    ``now_ts`` is exposed for testing — production callers should leave
    it None (uses ``time.time()``).
    """
    if not gaussians:
        return gaussians

    # Late imports to avoid cycles (cross_bracket_lst_gate imports from
    # bot.daemon, which can recursively pull weather modules).
    try:
        from bot.daemon.stations import station_for_ticker
        from bot.learning.cross_bracket_lst_gate import (
            get_running_high_state, is_post_peak_safe,
        )
        from tools.lst_align import lst_hour, lst_date
    except Exception:
        return gaussians

    station = station_for_ticker(ticker)
    if station is None:
        return gaussians

    metar = next((g for g in gaussians if g.source_name == "metar"), None)
    if metar is None:
        return gaussians
    if not math.isfinite(metar.mean_f):
        return gaussians
    # Sanity: METAR must report a temperature in Earth-likely range
    if not (-50.0 <= metar.mean_f <= 150.0):
        return gaussians

    # Identify the target settlement-day LST date from the ticker.
    target_lst_date = _target_lst_date_from_ticker(ticker, station.lst_offset)
    if target_lst_date is None:
        return gaussians

    if now_ts is None:
        import time as _time
        now_ts = _time.time()
    cur_lst_hour = lst_hour(now_ts, lst_offset=station.lst_offset)
    cur_lst_date = lst_date(now_ts, lst_offset=station.lst_offset)

    # Must be on the target day; pre-peak of the previous day or
    # post-settle next day disable the fast-path.
    if cur_lst_date != target_lst_date:
        return gaussians

    # Stability detection: read METAR poller's persisted last-increase
    # hour from kv_cache. If unavailable (poller hasn't run today), fall
    # back to a conservative LST 18 threshold rather than firing blind.
    # Tests inject ``last_increase_lst_hour_override`` to bypass kv_cache.
    if last_increase_lst_hour_override is not None:
        last_inc = last_increase_lst_hour_override
    else:
        state = get_running_high_state(station.icao, target_lst_date)
        last_inc = state.get("last_increase_lst_hour", -1) if state else -1
    if last_inc < 0:
        if cur_lst_hour < 18:
            return gaussians
        stability_hours = 0  # conservative fallback
    else:
        stability_hours = max(0, cur_lst_hour - last_inc)

    if not is_post_peak_safe(station.series, cur_lst_hour, stability_hours):
        return gaussians

    # Conditions met — build the synthetic Gaussian.
    overridden = GaussianForecast(
        mean_f=metar.mean_f,
        sigma_f=_METAR_POST_PEAK_SIGMA_F,
        horizon_hours=metar.horizon_hours,
        source_name="metar_post_peak_override",
        source_tag=(
            f"metar_post_peak:lst{cur_lst_hour:02d}_stable{stability_hours}h"
        ),
    )
    return [overridden]


def _target_lst_date_from_ticker(
    ticker: str, lst_offset: int,
) -> Optional[str]:
    """Parse the settlement LST date from a ticker like
    ``KXHIGHNY-26MAY04-B72.5`` → ``"2026-05-04"``."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    raw = parts[1]
    if len(raw) != 7:
        return None
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    try:
        yr = 2000 + int(raw[:2])
        mon = months[raw[2:5].upper()]
        day = int(raw[5:7])
        return f"{yr:04d}-{mon:02d}-{day:02d}"
    except (ValueError, KeyError):
        return None


def _apply_sanity_gate(gaussians: list[GaussianForecast]) -> list[GaussianForecast]:
    """Exclude sources whose μ is too far from the observation/HRRR consensus.

    The "observation" source name can be either ``metar`` or ``iem_1min``
    depending on which fired in the observation channel — both read the
    same physical ASOS station, so either makes a fine anchor.
    """
    if len(gaussians) <= 2:
        # With ≤2 sources we can't triangulate; keep them all.
        return gaussians

    obs = next(
        (g for g in gaussians if g.source_name in _OBSERVATION_SOURCE_NAMES),
        None,
    )
    hrrr = next((g for g in gaussians if g.source_name == "hrrr"), None)

    # Anchor: prefer obs+HRRR median (most-skillful pair). Fall back to
    # all-source median when either is absent.
    if obs is not None and hrrr is not None:
        anchor = (obs.mean_f + hrrr.mean_f) / 2.0
    else:
        mus = sorted(g.mean_f for g in gaussians)
        anchor = mus[len(mus) // 2]

    kept: list[GaussianForecast] = []
    rejected_names: list[str] = []
    for g in gaussians:
        if g is obs:
            kept.append(g)  # observation is the anchor; never drop
            continue
        if abs(g.mean_f - anchor) <= _SANITY_GATE_F:
            kept.append(g)
        else:
            rejected_names.append(
                f"{g.source_name}({g.mean_f:.1f}vs{anchor:.1f})"
            )

    if rejected_names:
        # Per-cycle visibility — these reasons are interesting for ops
        # but not a fatal log; they show up in daemon.log alongside the
        # snapshot health line.
        print(
            f"[weather_ensemble_v2] sanity gate excluded "
            f"{len(rejected_names)} source(s): {', '.join(rejected_names)}"
        )
    return kept


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
    forecast_high_f, sigma_f, hours_out, regime_label, regime_tier_used,
    regime_sigma_f, pooled_sigma_f). For Gaussian components we record
    forecast_high_f + sigma_f + hours_out and leave prob NULL (projection
    happens centrally). For the combined row we record both the combined
    Gaussian AND the projected prob so backtest readers don't need to
    re-project.

    Stage 1 regime telemetry: only the METAR row carries non-NULL regime_*
    columns — the σ side-channel from
    ``bot.signals.sources.metar_observations._RESIDUAL_TIER_META`` is keyed
    by station, so we look up the station from the ticker and pop the
    metadata for the METAR row only. Other source rows leave the regime
    columns NULL (they're METAR-specific by design).
    """
    # Resolve station from the ticker so we can fetch the regime
    # telemetry side-channel for the METAR row (if any).
    metar_meta: Optional[dict] = None
    # F.4 shadow: also pop the alt μ=running_high side-channel. Emitted
    # below as a parallel ``metar_running_only`` snapshot row for
    # offline calibration comparison against the live ``metar`` row.
    alt_running_only: Optional[dict] = None
    try:
        from bot.daemon.stations import station_for_ticker
        from bot.signals.sources.metar_observations import (
            get_alt_mu_running_high,
            get_residual_tier_meta,
        )
        ws = station_for_ticker(ticker)
        if ws is not None:
            metar_meta = get_residual_tier_meta(ws.icao)
            alt_running_only = get_alt_mu_running_high(ws.icao)
    except Exception:
        metar_meta = None
        alt_running_only = None

    def _meta_cols(source_name: str):
        # Only the METAR row carries the regime telemetry; other sources
        # leave these four columns NULL.
        if source_name != "metar" or metar_meta is None:
            return (None, None, None, None)
        return (
            metar_meta.get("regime_label"),
            metar_meta.get("regime_tier_used"),
            metar_meta.get("regime_sigma_f"),
            metar_meta.get("pooled_sigma_f"),
        )

    rows = []
    for g in gaussians:
        ml, tier, rsig, psig = _meta_cols(g.source_name)
        rows.append((
            now_iso, series, ticker, g.source_name,
            None,                         # forecast_prob (Gaussian sources log mean, not prob)
            g.mean_f, g.sigma_f,
            int(round(g.horizon_hours)),
            ml, tier, rsig, psig,
        ))
    if afd_tag is not None and afd_bias is not None:
        rows.append((
            now_iso, series, ticker, "afd_bias",
            None,                         # not a prob
            afd_bias, None,               # mean_f slot repurposed as bias_f
            None,
            None, None, None, None,       # regime cols NULL for non-METAR
        ))
    # F.4 shadow: alt μ=running_high (no NWP contamination) for
    # offline comparison vs the live ``metar`` row above. Always
    # emitted when the stash is present, regardless of whether the
    # live path is using it. The flag-gated live cutover comes later.
    if alt_running_only is not None:
        rows.append((
            now_iso, series, ticker, "metar_running_only",
            None,
            alt_running_only["mu_f"], alt_running_only["sigma_f"],
            # horizon_hours not tracked on the alt — use the combined
            # value so the row's columns are non-NULL where downstream
            # readers expect them.
            int(round(combined.horizon_hours)),
            None, None, None, None,
        ))
    rows.append((
        now_iso, series, ticker, "combined_v2",
        combined_prob,
        combined.mean_f, combined.sigma_f,
        int(round(combined.horizon_hours)),
        None, None, None, None,           # regime cols NULL for combined
    ))
    return rows


def _write_snapshots(rows):
    if not rows:
        return

    def _do(conn):
        conn.executemany(
            """INSERT INTO weather_forecast_snapshots
               (recorded_at, series, ticker, source, forecast_prob,
                forecast_high_f, sigma_f, hours_out,
                regime_label, regime_tier_used, regime_sigma_f, pooled_sigma_f)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    global _SNAPSHOT_WRITE_OK, _SNAPSHOT_WRITE_FAIL
    try:
        db_write(_do)
        _SNAPSHOT_WRITE_OK += 1
    except Exception as e:
        _SNAPSHOT_WRITE_FAIL += 1
        print(
            f"[weather_ensemble_v2][ERROR] snapshot write failed: "
            f"{type(e).__name__}: {e}"
        )


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

    # 1b. METAR post-peak fast-path: post-peak, the day's high is locked
    # and METAR observed it. Replacing the combine input with METAR-only
    # avoids the precision-weighted-combine pathology where wide-σ NWPs
    # dilute METAR's sharpness. See _apply_metar_post_peak_override
    # docstring + reports/PHASE_3C_COUNTERFACTUAL_2026-05-05.md.
    gaussians = _apply_metar_post_peak_override(gaussians, ticker)

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
    afd_late_day_skipped = _is_afd_late_day_skipped(ticker)

    try:
        from bot.signals.sources.afd import get_afd_bias

        bias_val, afd_conf, bias_tag = get_afd_bias(ticker, market_data)
        if bias_val is not None and afd_conf is not None:
            if afd_late_day_skipped:
                # Log the parse for snapshot continuity but apply 0
                # shift. afd_tag preserves the original parse so the
                # snapshot writer can still record what AFD said.
                afd_bias_f = 0.0
                afd_tag = f"{bias_tag}:suppressed_past_peak"
            else:
                # Confidence-weighted shift: low-confidence keyword
                # matches contribute little, high-confidence LLM reads
                # more. Capped at ±_MAX_AFD_SHIFT_ABS_F to keep a runaway
                # parse from blowing the combined mean past climatology.
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
    # Pass ticker so the per-family override (set 2026-05-03 from the
    # Brier sweep) applies. KXHIGHDEN gets factor=1.0 (no-op, sources
    # already agree); LAX/CHI/AUS get factor=4.0; MIA/NY get 3.0.
    # Also pass horizon_hours so the factor decays toward 1.0 post-peak
    # (added 2026-05-04 after the KXHIGHNY canary postmortem). Wide σ
    # post-peak misallocates probability across impossible brackets when
    # observations have already locked in the answer.
    combined = _apply_sigma_inflation(
        combined, ticker=ticker, tte_hours=combined.horizon_hours,
    )

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
    # Regime-aware floor: when the metar source is in past-peak mode
    # (μ pinned to running_high, σ=0.30°F), the day is effectively
    # decided and inflating σ to 1.0°F smears probability across
    # brackets that are physically very unlikely. Detect via the
    # metar source's tag suffix and let the precision-weighted σ
    # come through (with a tighter 0.4°F floor). 2026-05-05 cross-
    # bracket post-mortem: KXHIGHLAX-B68.5 NO bought because model
    # said P(high in [68, 69]) = 7% at σ=5.94°F when reality landed
    # in that bracket; with regime-aware floor σ would have been
    # ~0.4°F and bracket prob ~70%, no edge to fire on.
    metar_in_past_peak = any(
        g.source_name == "metar"
        and "past_peak" in (g.source_tag or "")
        for g in gaussians
    )
    floor = 0.4 if metar_in_past_peak else _COMBINED_SIGMA_FLOOR_F
    if combined.sigma_f < floor:
        combined = combined.with_sigma(floor)

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
        global _SNAPSHOT_BUILD_FAIL
        _SNAPSHOT_BUILD_FAIL += 1
        print(
            f"[weather_ensemble_v2][ERROR] snapshot build failed: "
            f"{type(e).__name__}: {e}"
        )

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
