"""Gaussian temperature forecast types for the weather ensemble.

Every weather source (HRRR, NBM, METAR, Open-Meteo, Tomorrow.io, NWS Point,
MADIS) already computes an internal ``(mean, sigma)`` over the predicted
daily-high temperature before collapsing to a binary probability for the
specific ticker threshold. The v1 ensemble combined those probabilities with
a weighted average, which is information-lossy for three reasons:

  1. The combine happens in probability-space, after each source has projected
     onto a specific threshold. That discards the shape of the underlying
     temperature distribution — correct ensembling requires combining the
     distributions, not the point probabilities.
  2. Horizon, MOS bias, and sigma-scaling corrections are all naturally
     expressed at the temperature-distribution layer (shift mean / scale
     sigma). Applying them post-projection would require inverting the
     projection.
  3. Different sources have different sigmas (HRRR tighter than Open-Meteo,
     METAR tightening toward settlement). Precision-weighted combining
     automatically down-weights noisy sources; weighted probability-averaging
     does not.

This module exposes the Gaussian contract so sources can return the
distribution they already compute internally, and ``weather_ensemble_v2`` can
combine them properly.

No network or DB access here — pure data types + math. Keep it that way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# erf-based Normal CDF constant; pulled into module scope so we don't
# recompute it per-call in hot paths (this is called once per source per
# candidate market per scan).
_SQRT_TWO = math.sqrt(2.0)


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """P(X <= x) for X ~ Normal(mu, sigma).

    Uses math.erf (C-level, fast). Degenerates to a step function when
    sigma <= 0 so downstream code can treat "known-exact" forecasts
    (e.g. a METAR daily-high already observed above threshold) without
    special-casing.
    """
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / (sigma * _SQRT_TWO)
    return 0.5 * (1.0 + math.erf(z))


@dataclass(frozen=True)
class GaussianForecast:
    """A Gaussian forecast over the daily-high temperature in Fahrenheit.

    Attributes
    ----------
    mean_f
        Predicted NWS CLI daily-high temperature in °F.
    sigma_f
        Forecast standard deviation in °F. Must be strictly positive for a
        valid forecast. Sources are expected to capture their own horizon
        and model-skill-based sigma here; the combiner will scale it further
        only if an external skill curve says so (A3).
    horizon_hours
        Hours from "now" until the settlement boundary (23:59 LST at the
        settlement station). Lower horizon = closer to settlement = sigma
        should be tighter. Exposed so the combiner and learning layer can
        stratify.
    source_name
        Short key (e.g. ``"hrrr"``, ``"metar"``, ``"nbm"``) used to look up
        learned weights in ``weather_source_weights`` and skill curves in
        the future ``source_skill_curve`` table. Lowercase, no colons, no
        city/date suffix.
    source_tag
        Full provenance string (e.g. ``"hrrr:nyc_2026-04-23"``) preserved
        from v1 for logging into ``weather_forecast_snapshots`` and
        ``alpha_backtest.sources_json``.
    """

    mean_f: float
    sigma_f: float
    horizon_hours: float
    source_name: str
    source_tag: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.mean_f):
            raise ValueError(f"mean_f must be finite, got {self.mean_f}")
        if not math.isfinite(self.sigma_f) or self.sigma_f <= 0:
            raise ValueError(f"sigma_f must be positive and finite, got {self.sigma_f}")
        if not math.isfinite(self.horizon_hours):
            raise ValueError(f"horizon_hours must be finite, got {self.horizon_hours}")
        if not self.source_name:
            raise ValueError("source_name must be non-empty")

    # ── Precision (1/σ²) ─────────────────────────────────────────────
    @property
    def precision(self) -> float:
        return 1.0 / (self.sigma_f * self.sigma_f)

    # ── Probability projections ──────────────────────────────────────
    def prob_above(self, threshold_f: float) -> float:
        """P(eventual daily high > threshold_f)."""
        return 1.0 - _normal_cdf(threshold_f, self.mean_f, self.sigma_f)

    def prob_at_or_above(self, threshold_f: float) -> float:
        """Alias for prob_above; for a continuous Gaussian P(X > t) = P(X >= t).

        Kept as a separate name because Kalshi markets phrase themselves
        'at or above' and the intent is clearer at call sites.
        """
        return self.prob_above(threshold_f)

    def prob_below(self, threshold_f: float) -> float:
        """P(eventual daily high <= threshold_f)."""
        return _normal_cdf(threshold_f, self.mean_f, self.sigma_f)

    def prob_between(self, lo_f: float, hi_f: float) -> float:
        """P(lo_f <= eventual daily high <= hi_f).

        Swaps args if lo > hi (caller-safety). Returns 0 on zero-width.
        """
        if hi_f < lo_f:
            lo_f, hi_f = hi_f, lo_f
        return _normal_cdf(hi_f, self.mean_f, self.sigma_f) - _normal_cdf(
            lo_f, self.mean_f, self.sigma_f
        )

    # ── Adjustments ──────────────────────────────────────────────────
    def shifted(self, bias_f: float) -> "GaussianForecast":
        """Return a copy with mean_f += bias_f (for A5: MOS bias correction).

        Sigma and horizon are preserved.
        """
        return GaussianForecast(
            mean_f=self.mean_f + bias_f,
            sigma_f=self.sigma_f,
            horizon_hours=self.horizon_hours,
            source_name=self.source_name,
            source_tag=self.source_tag,
        )

    def with_sigma(self, sigma_f: float) -> "GaussianForecast":
        """Return a copy with sigma overridden (for A3: skill-curve sigma)."""
        return GaussianForecast(
            mean_f=self.mean_f,
            sigma_f=sigma_f,
            horizon_hours=self.horizon_hours,
            source_name=self.source_name,
            source_tag=self.source_tag,
        )

    # ── Debug repr ───────────────────────────────────────────────────
    def short(self) -> str:
        return (
            f"{self.source_name}(μ={self.mean_f:.1f}°F, σ={self.sigma_f:.2f}°F, "
            f"h={self.horizon_hours:.1f}h)"
        )


# ══════════════════════════════════════════════════════════════════════
# Horizon helper (shared by every Gaussian-capable source)
# ══════════════════════════════════════════════════════════════════════


def hours_until_settlement_end(
    lst_offset_hours: int,
    day_idx: int,
    *,
    now: Optional[datetime] = None,
) -> float:
    """Hours from ``now`` to 23:59:59 of LST on the settlement day.

    Kalshi weather markets settle on the NWS CLI daily report for a date
    in Local Standard Time (LST) — never daylight-saving. ``day_idx`` is
    the forecast offset: ``0`` = today (LST), ``1`` = tomorrow, etc. The
    returned value is the horizon a Gaussian source should stamp on its
    forecast; learning at A3 will stratify by (source, horizon_bucket).

    Parameters
    ----------
    lst_offset_hours
        Signed hours from UTC of the settlement station's LST (e.g. ``-5``
        for Eastern, ``-8`` for Pacific). Sources should pass the value
        from ``bot.daemon.stations`` or ``bot.signals.sources.weather.
        _CITY_LST_OFFSET``.
    day_idx
        Days from today (in the settlement LST) to settlement.
    now
        Injectable for testing; defaults to ``datetime.now(tz=UTC)``.
    """
    lst_tz = timezone(timedelta(hours=lst_offset_hours))
    now_lst = (now or datetime.now(timezone.utc)).astimezone(lst_tz)
    settlement_date = (now_lst + timedelta(days=day_idx)).date()
    end_of_day = datetime(
        settlement_date.year,
        settlement_date.month,
        settlement_date.day,
        23, 59, 59,
        tzinfo=lst_tz,
    )
    delta_seconds = (end_of_day - now_lst).total_seconds()
    return max(0.0, delta_seconds / 3600.0)


# ══════════════════════════════════════════════════════════════════════
# Market-specific probability projection
# ══════════════════════════════════════════════════════════════════════

# Clamp matches the v1 probability clamp. Prevents Kelly blowups on
# essentially-certain states while keeping the information that settlement
# is overwhelmingly likely.
_DEFAULT_CLAMP: tuple[float, float] = (0.02, 0.98)


def probability_for_market(
    forecast: GaussianForecast,
    *,
    is_bracket: bool,
    threshold_f: Optional[float] = None,
    is_above: bool = True,
    bracket_lo_f: Optional[float] = None,
    bracket_hi_f: Optional[float] = None,
    clamp: tuple[float, float] = _DEFAULT_CLAMP,
) -> float:
    """Project a Gaussian forecast onto a specific market's YES probability.

    Kalshi weather markets come in two flavors:
      * threshold (-T suffix): "high at/above X" (is_above=True) or
        "high at/below X" (is_above=False)
      * bracket (-B suffix):   "high in [floor, cap]"

    This is the only place ticker→prob projection should happen for
    Gaussian-sourced signals; downstream readers (ensemble combiner,
    directional gate, WeatherQuoter) should call this instead of
    reinventing the CDF.
    """
    if is_bracket:
        if bracket_lo_f is None or bracket_hi_f is None:
            raise ValueError(
                "bracket markets require bracket_lo_f and bracket_hi_f"
            )
        prob = forecast.prob_between(bracket_lo_f, bracket_hi_f)
    else:
        if threshold_f is None:
            raise ValueError("threshold markets require threshold_f")
        prob = forecast.prob_above(threshold_f) if is_above else forecast.prob_below(threshold_f)

    lo, hi = clamp
    return max(lo, min(hi, prob))


# ══════════════════════════════════════════════════════════════════════
# Gaussian combiner
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WeightedForecast:
    """A forecast with its combiner weight (from learned source weights).

    Keeping this as a named pair rather than a bare ``tuple`` makes the
    combiner call sites self-documenting.
    """

    forecast: GaussianForecast
    weight: float


def combine_gaussian(
    inputs: list[WeightedForecast],
    *,
    effective_n_fraction: float = 1.0,
    prior_mean_f: Optional[float] = None,
    prior_sigma_f: Optional[float] = None,
    combined_name: str = "combined",
) -> Optional[GaussianForecast]:
    """Precision-weighted Bayesian combine of Gaussian forecasts.

    Each input is ``(forecast, weight)``. The ``weight`` is the learned
    source weight in [0, 1]; it scales the *precision contribution* of
    that source rather than its mean, so noisy sources (high σ) and
    low-weight sources are both naturally down-weighted.

    Parameters
    ----------
    inputs
        List of ``WeightedForecast``. Zero- or negative-weight entries are
        dropped.
    effective_n_fraction
        Correlation discount in (0, 1]. Use ``1.0`` for fully independent
        inputs (e.g. METAR obs vs. model forecast). For correlated model
        families (HRRR, NBM, Open-Meteo all trained on similar grids),
        pass ``1 / n_correlated`` so the precision doesn't multi-count.
        A2 will apply this group-wise; A1 exposes the knob.
    prior_mean_f, prior_sigma_f
        Optional Bayesian prior. When both are given, adds ``1/σ_prior²``
        to the total precision centered at ``μ_prior``. A tight prior
        (small σ_prior) will pull the combined mean toward μ_prior; a
        vague prior (large σ_prior) contributes little. Useful for
        regularizing toward climatology when every source is confidently
        disagreeing. If either is None the prior is dropped.
    combined_name
        ``source_name`` to set on the output. Defaults to ``"combined"``
        so combiner outputs don't accidentally get treated as an
        individual source in weight lookups.

    Returns
    -------
    A combined ``GaussianForecast`` or ``None`` if no positive-weight
    inputs remain.
    """
    if not inputs:
        return None

    total_precision = 0.0
    weighted_mean_num = 0.0
    min_horizon = float("inf")
    contributing_tags: list[str] = []

    for item in inputs:
        f = item.forecast
        w = item.weight
        if w <= 0:
            continue
        # Contribution: (weight × precision × correlation-discount)
        contribution = w * f.precision * effective_n_fraction
        if contribution <= 0:
            continue
        total_precision += contribution
        weighted_mean_num += contribution * f.mean_f
        if f.horizon_hours < min_horizon:
            min_horizon = f.horizon_hours
        contributing_tags.append(f.source_tag or f.source_name)

    # Optional weak prior
    if (
        prior_mean_f is not None
        and prior_sigma_f is not None
        and prior_sigma_f > 0
        and math.isfinite(prior_mean_f)
        and math.isfinite(prior_sigma_f)
    ):
        prior_precision = 1.0 / (prior_sigma_f * prior_sigma_f)
        total_precision += prior_precision
        weighted_mean_num += prior_precision * prior_mean_f

    if total_precision <= 0:
        return None

    combined_mean = weighted_mean_num / total_precision
    combined_sigma = 1.0 / math.sqrt(total_precision)

    return GaussianForecast(
        mean_f=combined_mean,
        sigma_f=combined_sigma,
        horizon_hours=min_horizon if math.isfinite(min_horizon) else 0.0,
        source_name=combined_name,
        source_tag=f"{combined_name}:" + "+".join(contributing_tags),
    )
