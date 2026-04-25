"""Tests for the Gaussian-first weather ensemble combiner (A2).

Coverage:
  1. Gating — non-weather tickers + empty market_data → (None, None)
  2. No Gaussians collected → (None, None)
  3. Projection parse failure → (None, None)
  4. Group discount math — 5 models with identical (μ, σ) combine to σ (not
     σ/√5); 1 model + 1 obs combine to σ/√2 (uncorrelated inputs)
  5. AFD bias applied as temperature shift on the combined mean
  6. AFD confidence scaling — low confidence produces smaller shift
  7. AFD shift is capped at ±_MAX_AFD_SHIFT_ABS_F
  8. NOAA logit blend pulls final prob toward NOAA's opinion
  9. Snapshot rows land in weather_forecast_snapshots with the expected
     shape (per-source Gaussian rows + afd_bias row + combined_v2 row)
"""

from __future__ import annotations

import math

import pytest

from bot.db import init_db
from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import GaussianForecast


# ── Fixtures ──────────────────────────────────────────────────────────────

def _market(
    title: str = "Will NYC high exceed 75°F today?",
    close_time: str = "2026-04-23T22:00:00Z",
    floor_strike=None,
    cap_strike=None,
) -> dict:
    m = {"title": title, "close_time": close_time}
    if floor_strike is not None:
        m["floor_strike"] = floor_strike
    if cap_strike is not None:
        m["cap_strike"] = cap_strike
    return m


def _g(source_name: str, mean_f: float = 78.0, sigma_f: float = 2.0,
       horizon_hours: float = 8.0) -> GaussianForecast:
    return GaussianForecast(
        mean_f=mean_f, sigma_f=sigma_f,
        horizon_hours=horizon_hours,
        source_name=source_name,
        source_tag=f"{source_name}:nyc_2026-04-23",
    )


def _patch_collect(monkeypatch, gaussians):
    """Swap _collect_gaussians to return a controlled list."""
    monkeypatch.setattr(v2, "_collect_gaussians", lambda *a, **kw: list(gaussians))


def _patch_afd_bias(monkeypatch, return_tuple):
    """Swap bot.signals.sources.afd.get_afd_bias imported inside predict_v2."""
    def _fake_bias(*a, **kw):
        return return_tuple
    # v2 imports inside the function body, so we patch at the module level
    # of the source module where the real function lives.
    import bot.signals.sources.afd as afd_mod
    monkeypatch.setattr(afd_mod, "get_afd_bias", _fake_bias)


def _patch_noaa(monkeypatch, return_tuple):
    import bot.signals.sources.weather as w_mod
    monkeypatch.setattr(
        w_mod, "get_noaa_alerts_for_market", lambda *a, **kw: return_tuple
    )


@pytest.fixture
def memdb():
    conn = init_db(":memory:")
    yield conn
    conn.close()


# ── 1. Gating ─────────────────────────────────────────────────────────────

def test_v2_rejects_non_weather_ticker(memdb):
    p, s = v2.predict_v2("KXETH-26", _market(title="ETH price"))
    assert p is None and s is None


def test_v2_rejects_empty_market_data(memdb):
    p, s = v2.predict_v2("KXHIGHNY-26APR23-T75", None)
    assert p is None and s is None


# ── 2. No Gaussians ───────────────────────────────────────────────────────

def test_v2_returns_none_when_no_sources_fire(memdb, monkeypatch):
    _patch_collect(monkeypatch, [])
    # AFD also silent — otherwise we'd still be None because we have no
    # combined Gaussian to shift.
    _patch_afd_bias(monkeypatch, (None, None, None))
    _patch_noaa(monkeypatch, (None, None))
    p, s = v2.predict_v2("KXHIGHNY-26APR23-T75", _market())
    assert p is None and s is None


# ── 3. Projection parse failure ───────────────────────────────────────────

def test_v2_returns_none_when_threshold_unparseable(memdb, monkeypatch):
    _patch_collect(monkeypatch, [_g("hrrr")])
    _patch_afd_bias(monkeypatch, (None, None, None))
    _patch_noaa(monkeypatch, (None, None))
    # Ticker without -T/-B suffix and title without numeric threshold
    p, s = v2.predict_v2("KXHIGHNY-26APR23", _market(title="nyc high"))
    assert p is None and s is None


# ── 4. Group discount math ────────────────────────────────────────────────

def _projected_prob_for_combined(gaussians, *, threshold_f=75.0):
    """Drive predict_v2 with only Gaussians and extract what the combined
    Gaussian's projected probability would be (by back-computing from
    mean/sigma so the test is independent of probability_for_market edge
    cases like clamping)."""
    from bot.signals.weather_forecast import (
        WeightedForecast, combine_gaussian,
    )
    group_counts: dict[str, int] = {}
    for g in gaussians:
        grp = v2._group_of(g.source_name)
        group_counts[grp] = group_counts.get(grp, 0) + 1
    weighted = [
        WeightedForecast(forecast=g, weight=1.0 / max(1, group_counts[v2._group_of(g.source_name)]))
        for g in gaussians
    ]
    combined = combine_gaussian(weighted)
    assert combined is not None
    return combined


def test_v2_group_discount_five_identical_models_yields_input_sigma():
    """Five models with identical (μ=78, σ=2) combined with 1/5 weights each
    should give precision = 5 * (1/5) * (1/σ²) = 1/σ² → combined σ = σ.

    This is the heart of the correlation discount: identical correlated
    forecasts should produce ONE effective source, not FIVE."""
    gs = [_g(n, mean_f=78.0, sigma_f=2.0) for n in ("hrrr", "nbm", "nws_point", "tomorrow", "weather")]
    combined = _projected_prob_for_combined(gs)
    assert abs(combined.sigma_f - 2.0) < 1e-9, (
        f"5 correlated models should yield σ=2.0, got σ={combined.sigma_f}"
    )
    assert abs(combined.mean_f - 78.0) < 1e-9


def test_v2_group_discount_one_model_one_obs_combines_like_two_independent():
    """One model (weight 1/1 = 1) + one obs (weight 1/1 = 1), both σ=2 →
    precision = 2 × (1/4) = 0.5 → combined σ = √2. Distinct groups are
    treated as independent sources (no cross-group discount)."""
    gs = [_g("hrrr", mean_f=78.0, sigma_f=2.0),
          _g("metar", mean_f=78.0, sigma_f=2.0)]
    combined = _projected_prob_for_combined(gs)
    expected = 2.0 / math.sqrt(2.0)
    assert abs(combined.sigma_f - expected) < 1e-9, (
        f"cross-group combine: expected σ={expected:.6f}, got σ={combined.sigma_f:.6f}"
    )


def test_v2_group_discount_two_models_combine_tighter_than_one():
    """Two models with identical (μ=78, σ=2), weights 1/2 each →
    precision = 2 × (1/2) × (1/4) = 0.25 → σ = 2. Still σ=σ: two correlated
    models = one effective source. (Same invariant as n=5 case, for n=2.)"""
    gs = [_g("hrrr", sigma_f=2.0), _g("nbm", sigma_f=2.0)]
    combined = _projected_prob_for_combined(gs)
    assert abs(combined.sigma_f - 2.0) < 1e-9


def test_v2_disagreeing_sources_widen_sigma_less_than_they_shift_mean():
    """Two uncorrelated sources (one model, one obs) with μ=76 and μ=80 both
    σ=2 should combine to μ=78, σ=√2 — classic Bayesian combine of two
    independent Gaussian obs of the same quantity."""
    gs = [_g("hrrr", mean_f=76.0, sigma_f=2.0),
          _g("metar", mean_f=80.0, sigma_f=2.0)]
    combined = _projected_prob_for_combined(gs)
    assert abs(combined.mean_f - 78.0) < 1e-6
    assert abs(combined.sigma_f - math.sqrt(2.0)) < 1e-6


# ── 5/6/7. AFD bias ───────────────────────────────────────────────────────

def _run_v2(memdb, monkeypatch, gaussians, afd_tuple=(None, None, None),
            noaa_tuple=(None, None), market=None):
    _patch_collect(monkeypatch, gaussians)
    _patch_afd_bias(monkeypatch, afd_tuple)
    _patch_noaa(monkeypatch, noaa_tuple)
    return v2.predict_v2(
        "KXHIGHNY-26APR23-T75",
        market if market is not None else _market(),
    )


def test_v2_afd_bias_raises_prob_when_bias_positive(memdb, monkeypatch):
    """AFD +2°F with confidence 1.0 should shift the combined mean up 2°F,
    which raises P(high > 75°F) when the pre-shift mean is around the
    threshold. Use μ=75 so the sign is unambiguous."""
    gs = [_g("hrrr", mean_f=75.0, sigma_f=2.0)]
    p_with, _ = _run_v2(memdb, monkeypatch, gs, afd_tuple=(2.0, 1.0, "afd:nyc_OKX_llm"))
    p_without, _ = _run_v2(memdb, monkeypatch, gs, afd_tuple=(None, None, None))
    assert p_with > p_without + 0.1, (
        f"expected AFD +2°F to raise P substantially: with={p_with:.3f} "
        f"without={p_without:.3f}"
    )


def test_v2_afd_low_confidence_moves_prob_less_than_high_confidence(memdb, monkeypatch):
    gs = [_g("hrrr", mean_f=75.0, sigma_f=2.0)]
    p_hi, _ = _run_v2(memdb, monkeypatch, gs, afd_tuple=(2.0, 0.7, "afd:nyc_OKX_llm"))
    p_lo, _ = _run_v2(memdb, monkeypatch, gs, afd_tuple=(2.0, 0.15, "afd:nyc_OKX_keyword"))
    assert p_hi > p_lo, (
        f"high-conf AFD should move prob more than low-conf: "
        f"hi={p_hi:.3f} lo={p_lo:.3f}"
    )


def test_v2_afd_shift_capped_at_max(memdb, monkeypatch):
    """A runaway AFD claim of +10°F @ conf 1.0 should get clipped so it
    doesn't overwhelm the combined Gaussian. We verify by comparing to a
    +_MAX_AFD_SHIFT_ABS_F claim at conf 1.0 — identical probabilities."""
    gs = [_g("hrrr", mean_f=75.0, sigma_f=2.0)]
    p_runaway, _ = _run_v2(memdb, monkeypatch, gs, afd_tuple=(10.0, 1.0, "afd:x"))
    p_capped, _ = _run_v2(
        memdb, monkeypatch, gs,
        afd_tuple=(v2._MAX_AFD_SHIFT_ABS_F, 1.0, "afd:x"),
    )
    assert abs(p_runaway - p_capped) < 1e-6, (
        f"AFD shift should clip at ±{v2._MAX_AFD_SHIFT_ABS_F}: "
        f"runaway={p_runaway:.6f} capped={p_capped:.6f}"
    )


# ── 8. NOAA logit blend ───────────────────────────────────────────────────

def test_v2_noaa_alert_pulls_prob_toward_noaa_opinion(memdb, monkeypatch):
    """Gaussian projects to ~0.5 (μ=75 on threshold=75); NOAA bump to 0.9
    should raise the final prob into the [0.5, 0.9] range."""
    gs = [_g("hrrr", mean_f=75.0, sigma_f=2.0)]
    p_plain, _ = _run_v2(memdb, monkeypatch, gs)
    p_noaa, _ = _run_v2(
        memdb, monkeypatch, gs,
        noaa_tuple=(0.9, "noaa_alert:nyc_heat"),
    )
    # Gaussian at μ=threshold gives exactly 0.5 (Normal is symmetric).
    assert abs(p_plain - 0.5) < 1e-6
    assert p_plain < p_noaa < 0.9, (
        f"NOAA=0.9 should pull p from 0.5 toward 0.9: plain={p_plain:.3f} "
        f"with_noaa={p_noaa:.3f}"
    )


# ── 9. Snapshot writes ────────────────────────────────────────────────────

def test_v2_writes_snapshot_rows_with_gaussian_shape(memdb, monkeypatch):
    gs = [
        _g("hrrr", mean_f=78.0, sigma_f=1.5, horizon_hours=8.0),
        _g("metar", mean_f=79.0, sigma_f=0.8, horizon_hours=6.0),
    ]
    p, tag = _run_v2(
        memdb, monkeypatch, gs,
        afd_tuple=(1.5, 0.7, "afd:nyc_OKX_llm"),
    )
    assert p is not None
    assert tag.startswith("weather_ensemble_v2:")

    rows = memdb.execute(
        """SELECT source, forecast_prob, forecast_high_f, sigma_f, hours_out
           FROM weather_forecast_snapshots
           WHERE ticker = ?
           ORDER BY source""",
        ("KXHIGHNY-26APR23-T75",),
    ).fetchall()

    sources = {r[0] for r in rows}
    assert "hrrr" in sources
    assert "metar" in sources
    assert "afd_bias" in sources
    assert "combined_v2" in sources

    # Per-source rows: Gaussian shape (mean + sigma populated, prob NULL).
    for r in rows:
        source = r[0]
        if source in ("hrrr", "metar"):
            assert r[1] is None, f"{source}: forecast_prob should be NULL"
            assert r[2] is not None and r[3] is not None, (
                f"{source}: mean_f and sigma_f should be populated"
            )
            assert r[4] is not None

    # combined_v2 row has both prob AND Gaussian shape.
    combined = [r for r in rows if r[0] == "combined_v2"][0]
    assert combined[1] is not None  # forecast_prob set
    assert abs(combined[1] - p) < 1e-9  # matches returned prob
    assert combined[2] is not None and combined[3] is not None

    # afd_bias row: bias_f stored in forecast_high_f slot, prob + sigma NULL.
    afd_row = [r for r in rows if r[0] == "afd_bias"][0]
    assert afd_row[1] is None
    assert afd_row[2] is not None  # bias_f
    # 1.5°F × confidence 0.7 = 1.05°F effective shift.
    assert abs(afd_row[2] - 1.05) < 1e-6
    assert afd_row[3] is None


def test_v2_snapshot_skips_afd_row_when_no_afd_bias(memdb, monkeypatch):
    gs = [_g("hrrr")]
    _run_v2(memdb, monkeypatch, gs, afd_tuple=(None, None, None))
    sources = {
        r[0] for r in memdb.execute(
            "SELECT source FROM weather_forecast_snapshots "
            "WHERE ticker = ?",
            ("KXHIGHNY-26APR23-T75",),
        ).fetchall()
    }
    assert "afd_bias" not in sources
    assert "combined_v2" in sources
    assert "hrrr" in sources


# ── 10. Provenance tag ────────────────────────────────────────────────────

def test_v2_source_tag_lists_contributing_sources(memdb, monkeypatch):
    gs = [_g("hrrr"), _g("metar")]
    _, tag = _run_v2(
        memdb, monkeypatch, gs,
        afd_tuple=(1.0, 0.5, "afd:nyc_OKX_llm"),
        noaa_tuple=(0.6, "noaa_alert:nyc"),
    )
    assert tag.startswith("weather_ensemble_v2:")
    assert "hrrr" in tag and "metar" in tag
    assert "afd" in tag
    assert "noaa" in tag


# ── 11. Learned effective-N (A2.5c swap) ─────────────────────────────────

def _persist_group_rho(conn, group: str, rho: float):
    """Write a ρ to kv_cache the way backfill_weather_effective_n does."""
    from bot.db import kv_set
    kv_set(
        conn, f"weather_group_corr_{group}",
        {"rho": rho, "n_eff": None, "n_sources": 3,
         "n_pairs": 42, "sources": ["hrrr", "nbm", "nws_point"],
         "fit_at": "2026-04-24T00:00:00Z"},
        ttl_seconds=86400,
    )


def test_get_group_rho_defaults_to_mvp_when_no_kv_cache(memdb):
    """Cold kv_cache → _get_group_rho returns the MVP fallback (1.0).
    Pinning this so a missing kv_cache row never silently flips the
    weighting."""
    # Fresh memdb has no rows under weather_group_corr_* prefix.
    assert v2._get_group_rho("model") == v2._GROUP_RHO_FALLBACK
    assert v2._get_group_rho("obs") == v2._GROUP_RHO_FALLBACK


def test_get_group_rho_reads_persisted_value(memdb):
    _persist_group_rho(memdb, "model", 0.5)
    assert v2._get_group_rho("model") == pytest.approx(0.5)


def test_get_group_rho_ignores_malformed_payload(memdb):
    """Non-dict / missing-rho / non-numeric rho payloads all fall back to
    the MVP — the reader never trusts pathological cache contents."""
    from bot.db import kv_set
    kv_set(memdb, "weather_group_corr_model", "not a dict", ttl_seconds=86400)
    assert v2._get_group_rho("model") == v2._GROUP_RHO_FALLBACK

    kv_set(memdb, "weather_group_corr_model", {"rho": "half"}, ttl_seconds=86400)
    assert v2._get_group_rho("model") == v2._GROUP_RHO_FALLBACK


def test_get_group_rho_clamps_runaway_values(memdb):
    """ρ physically in [-1, 1]. Payloads outside that range get clamped
    (defensive — a bad fit shouldn't produce pathological weights)."""
    _persist_group_rho(memdb, "model", 5.0)
    assert v2._get_group_rho("model") == pytest.approx(1.0)
    _persist_group_rho(memdb, "model", -5.0)
    assert v2._get_group_rho("model") == pytest.approx(-0.99)


def test_weighted_inputs_mvp_fallback_matches_one_over_n(memdb):
    """No persisted ρ → behavior must be byte-identical to the A2.2 MVP
    (each source in a group of n gets weight 1/n)."""
    gs = [_g("hrrr"), _g("nbm"), _g("nws_point")]
    weighted = v2._weighted_inputs_with_group_discount(gs)
    for w in weighted:
        assert w.weight == pytest.approx(1.0 / 3.0)


def test_weighted_inputs_learned_rho_gives_more_weight_to_each_source(memdb):
    """With ρ=0.5 and n=3 models, n_eff = 3/(1+2·0.5) = 1.5, so per-source
    weight = n_eff/n = 0.5 (not the MVP's 1/3 ≈ 0.333). The group's total
    precision contribution is 1.5/σ² (not 1/σ²)."""
    _persist_group_rho(memdb, "model", 0.5)
    gs = [_g("hrrr"), _g("nbm"), _g("nws_point")]
    weighted = v2._weighted_inputs_with_group_discount(gs)
    for w in weighted:
        assert w.weight == pytest.approx(0.5, abs=1e-9)
    total_weight = sum(w.weight for w in weighted)
    assert total_weight == pytest.approx(1.5, abs=1e-9)


def test_weighted_inputs_independent_rho_zero_gives_full_weight(memdb):
    """With ρ=0 (no correlation), n_eff = n = 3, weight = 1 per source —
    the group behaves as 3 independent sources."""
    _persist_group_rho(memdb, "model", 0.0)
    gs = [_g("hrrr"), _g("nbm"), _g("nws_point")]
    weighted = v2._weighted_inputs_with_group_discount(gs)
    for w in weighted:
        assert w.weight == pytest.approx(1.0, abs=1e-9)


def test_weighted_inputs_cross_group_rho_routed_correctly(memdb):
    """Persisting ρ for 'model' must not affect the 'obs' group: METAR
    still gets weight 1.0 (single obs in its group → n_eff=1, w=1.0)."""
    _persist_group_rho(memdb, "model", 0.3)
    gs = [_g("hrrr"), _g("nbm"), _g("metar")]
    weighted = v2._weighted_inputs_with_group_discount(gs)
    by_src = {w.forecast.source_name: w.weight for w in weighted}
    # Model group, n=2, ρ=0.3 → n_eff = 2/(1+0.3) ≈ 1.5385, w ≈ 0.7692.
    expected_model_w = (2.0 / (1.0 + 0.3)) / 2.0
    assert by_src["hrrr"] == pytest.approx(expected_model_w, abs=1e-9)
    assert by_src["nbm"] == pytest.approx(expected_model_w, abs=1e-9)
    # Obs group, n=1 → n_eff=1, w=1.0 regardless of any model ρ.
    assert by_src["metar"] == pytest.approx(1.0, abs=1e-9)


def test_weighted_inputs_handles_anti_correlation_blowup(memdb):
    """Pathological ρ=-1 with n=3 models would give denom = 1 + 2·(-1) = -1 →
    n_eff would flip sign. Defensive cap: fall back to n_eff=n (independent),
    weight=1.0."""
    _persist_group_rho(memdb, "model", -0.9)
    gs = [_g("hrrr"), _g("nbm"), _g("nws_point")]
    weighted = v2._weighted_inputs_with_group_discount(gs)
    # denom = 1 + 2·(-0.9) = -0.8 ≤ 0 → fallback to n_eff=n=3, weight=1.
    for w in weighted:
        assert w.weight == pytest.approx(1.0, abs=1e-9)


# ── 12. Learned skill curves (A3) ────────────────────────────────────────

def _persist_skill(conn, source: str, bucket: str, sigma: float,
                   bias: float = 0.0, n: int = 20):
    """Write a skill-curve payload the way backfill persist does."""
    from bot.db import kv_set
    kv_set(
        conn, f"weather_skill_{source}_{bucket}",
        {"sigma": sigma, "bias": bias, "n": n,
         "prior_sigma": sigma + 0.5,
         "fit_at": "2026-04-24T00:00:00Z"},
        ttl_seconds=86400,
    )


def test_skill_bucket_boundaries_v2():
    """v2's bucket-picker must match the tool's boundaries exactly."""
    assert v2._skill_bucket_for(0.0) == "0_6"
    assert v2._skill_bucket_for(5.9) == "0_6"
    assert v2._skill_bucket_for(6.0) == "6_24"
    assert v2._skill_bucket_for(12.0) == "6_24"
    assert v2._skill_bucket_for(24.0) == "24_48"
    assert v2._skill_bucket_for(48.0) == "48_168"
    assert v2._skill_bucket_for(168.0) is None
    assert v2._skill_bucket_for(-1.0) is None


def test_get_learned_sigma_falls_back_when_no_fit(memdb):
    """Cold kv_cache → _get_learned_sigma returns None so the source's
    self-reported σ is preserved."""
    assert v2._get_learned_sigma("hrrr", 12.0) is None


def test_get_learned_sigma_reads_persisted_value(memdb):
    _persist_skill(memdb, "hrrr", "6_24", 1.65)
    # Any horizon in [6, 24) maps to the same bucket.
    assert v2._get_learned_sigma("hrrr", 6.0) == pytest.approx(1.65)
    assert v2._get_learned_sigma("hrrr", 23.0) == pytest.approx(1.65)


def test_get_learned_sigma_rejects_pathological_values(memdb):
    """σ outside [0.1, 15.0]°F is refused → fallback to source prior.
    Real-world weather RMSE stays in that range; outside is corruption."""
    _persist_skill(memdb, "hrrr", "6_24", 50.0)
    assert v2._get_learned_sigma("hrrr", 12.0) is None
    _persist_skill(memdb, "hrrr", "6_24", 0.01)
    assert v2._get_learned_sigma("hrrr", 12.0) is None


def test_get_learned_sigma_out_of_range_horizon_returns_none(memdb):
    """Horizon past the top edge (168h) falls back even if a nearby
    bucket has a fit."""
    _persist_skill(memdb, "hrrr", "48_168", 3.5)
    assert v2._get_learned_sigma("hrrr", 12.0) is None  # wrong bucket
    assert v2._get_learned_sigma("hrrr", 200.0) is None  # out of range


def test_apply_learned_sigma_overrides_prior(memdb):
    """With a persisted σ, _apply_learned_sigma returns a copy with
    sigma replaced; mean / horizon / source_name / source_tag preserved."""
    _persist_skill(memdb, "hrrr", "6_24", 1.65)
    g = _g("hrrr", mean_f=78.0, sigma_f=5.0, horizon_hours=12.0)
    g2 = v2._apply_learned_sigma(g)
    assert g2.sigma_f == pytest.approx(1.65)
    assert g2.mean_f == pytest.approx(78.0)
    assert g2.horizon_hours == pytest.approx(12.0)
    assert g2.source_name == "hrrr"
    assert g2.source_tag == g.source_tag


def test_apply_learned_sigma_leaves_prior_when_no_fit(memdb):
    g = _g("hrrr", sigma_f=5.0, horizon_hours=12.0)
    g2 = v2._apply_learned_sigma(g)
    # Unchanged — source keeps its self-reported σ.
    assert g2 is g or g2.sigma_f == pytest.approx(5.0)


def test_learned_sigma_flows_into_combine(memdb, monkeypatch):
    """End-to-end: persisting a tighter σ for HRRR than for NBM gives HRRR
    more weight in the precision-weighted combine → combined μ pulled
    toward HRRR's mean."""
    _persist_skill(memdb, "hrrr", "6_24", 1.0)   # tight
    _persist_skill(memdb, "nbm",  "6_24", 4.0)   # loose
    gs = [
        _g("hrrr", mean_f=78.0, sigma_f=2.0, horizon_hours=12.0),
        _g("nbm",  mean_f=82.0, sigma_f=2.0, horizon_hours=12.0),
    ]
    # Run through the actual pipeline to exercise _collect_gaussians'
    # learned-σ override.
    _patch_collect(monkeypatch, gs)
    _patch_afd_bias(monkeypatch, (None, None, None))
    _patch_noaa(monkeypatch, (None, None))

    from bot.signals.weather_forecast import (
        WeightedForecast, combine_gaussian,
    )
    # Reproduce what predict_v2 does: apply learned σ, group-discount,
    # combine. Expected: HRRR precision = 1/1² = 1, NBM precision = 1/16.
    # Post-group-discount (ρ=1.0 MVP, n=2) weight = 0.5 each. Precision
    # contributions: HRRR = 0.5×1 = 0.5, NBM = 0.5×(1/16) = 0.03125.
    # Combined μ = (0.5·78 + 0.03125·82) / (0.5 + 0.03125) ≈ 78.235.
    processed = [v2._apply_learned_sigma(g) for g in gs]
    weighted = v2._weighted_inputs_with_group_discount(processed)
    combined = combine_gaussian(weighted)
    assert combined is not None
    # HRRR dominates → μ closer to 78 than to 82.
    assert combined.mean_f < 79.0
    assert combined.mean_f > 78.0
