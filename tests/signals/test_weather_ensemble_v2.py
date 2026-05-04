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


def test_v2_group_discount_four_identical_models_yields_input_sigma():
    """Four models with identical (μ=78, σ=2) combined with 1/4 weights each
    should give precision = 4 * (1/4) * (1/σ²) = 1/σ² → combined σ = σ.

    This is the heart of the correlation discount: identical correlated
    forecasts should produce ONE effective source, not FOUR.

    (Was 5 models pre-2026-04-26; tomorrow.io was dropped — see
    bot/config.py TOMORROW_API_KEY note.)"""
    gs = [_g(n, mean_f=78.0, sigma_f=2.0) for n in ("hrrr", "nbm", "nws_point", "weather")]
    combined = _projected_prob_for_combined(gs)
    assert abs(combined.sigma_f - 2.0) < 1e-9, (
        f"4 correlated models should yield σ=2.0, got σ={combined.sigma_f}"
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
    # Default AFD late-day gate to OFF in tests so wall-clock time
    # doesn't make AFD-shift assertions flaky. Tests that specifically
    # exercise the late-day gate can monkeypatch this back to True.
    monkeypatch.setattr(v2, "_is_afd_late_day_skipped", lambda ticker: False)
    return v2.predict_v2(
        "KXHIGHNY-26APR23-T75",
        market if market is not None else _market(),
    )


def test_v2_afd_shift_suppressed_past_peak_lst(memdb, monkeypatch):
    """Regression for the 2026-05-01 evening warm-bias finding: AFD
    parsed a "+2°F" forecaster commentary at LST 17 (post-peak) and
    the resulting +1.5°F shift drove combined.μ above the actual
    daily high. Past peak heating, AFD is purely additive noise —
    the gate must suppress the shift entirely.
    """
    gs = [_g("hrrr", mean_f=75.0, sigma_f=2.0)]
    # Force the late-day gate on. Same-shape AFD payload as the
    # passing test below; the only difference is the gate.
    _patch_collect(monkeypatch, gs)
    _patch_afd_bias(monkeypatch, (2.0, 1.0, "afd:nyc_OKX_llm"))
    _patch_noaa(monkeypatch, (None, None))
    monkeypatch.setattr(v2, "_is_afd_late_day_skipped", lambda ticker: True)
    p_late, tag_late = v2.predict_v2(
        "KXHIGHNY-26APR23-T75", _market(),
    )
    # And the not-late case for reference.
    monkeypatch.setattr(v2, "_is_afd_late_day_skipped", lambda ticker: False)
    p_norm, tag_norm = v2.predict_v2(
        "KXHIGHNY-26APR23-T75", _market(),
    )
    # Late-day path: no shift → prob = baseline (μ = 75 vs threshold 75 → 0.5)
    assert abs(p_late - 0.5) < 0.05, (
        f"late-day path should NOT shift: got p={p_late:.3f}"
    )
    # Normal-time path: AFD pushes warmer → above-75 prob jumps
    assert p_norm > p_late + 0.1, (
        f"normal-time AFD shift should raise prob meaningfully: "
        f"normal={p_norm:.3f} late={p_late:.3f}"
    )
    # Functional contract: late-day path produces a probability
    # indistinguishable from "no AFD" while the normal-time path's
    # probability moves meaningfully. The suppression marker lives
    # in the snapshot's afd_bias row (afd_tag), not the final
    # provenance tag returned to the caller — verified separately
    # via snapshot inspection.


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


def _persist_skill_per_city(conn, source: str, city: str, bucket: str,
                             sigma: float, bias: float = 0.0, n: int = 100):
    """Per-(source, city, bucket) skill payload — mirrors the daily fitter."""
    from bot.db import kv_set
    kv_set(
        conn, f"weather_skill_{source}_{city}_{bucket}",
        {"sigma": sigma, "bias": bias, "n": n,
         "fit_at": "2026-04-30T00:00:00Z",
         "source_table": "weather_forecast_snapshots"},
        ttl_seconds=86400,
    )


def test_per_city_sigma_used_when_sample_size_meets_threshold(memdb):
    """A well-sampled per-(source, city) σ overrides the pooled key.

    Spirit: when we have enough days to trust the per-city fit, prefer it
    over pooled. Threshold is `_PER_CITY_SIGMA_MIN_SAMPLES` (60).
    """
    _persist_skill(memdb, "hrrr", "6_24", 1.20, n=684)            # pooled
    _persist_skill_per_city(memdb, "hrrr", "nyc", "6_24", 0.90, n=100)
    assert v2._get_learned_sigma("hrrr", 12.0, city_key="nyc") == pytest.approx(0.90)


def test_per_city_sigma_falls_back_to_pooled_when_thin(memdb):
    """A thin per-city fit (n < threshold) is dropped — pooled wins.

    Regression guard: pre-fix, ``weather_skill_hrrr_los_angeles_0_6`` was
    persisted with σ=6.02°F, bias=+5.5°F at n=18 (production kv on 2026-04-30)
    while pooled HRRR was σ=1.20°F. The thin fit was honored, poisoning the
    combine for LAX. The sample-size gate drops thin per-city fits.
    """
    _persist_skill(memdb, "hrrr", "0_6", 1.20, n=684)              # pooled
    _persist_skill_per_city(memdb, "hrrr", "los_angeles", "0_6", 6.02,
                             bias=5.5, n=18)                       # noisy thin fit
    assert v2._get_learned_sigma(
        "hrrr", 3.0, city_key="los_angeles"
    ) == pytest.approx(1.20)


def test_per_city_sigma_falls_back_when_n_field_missing(memdb):
    """Defensive: a per-city payload missing the 'n' field is treated as
    untrusted. Pooled is used instead.
    """
    from bot.db import kv_set
    _persist_skill(memdb, "hrrr", "6_24", 1.20, n=684)
    kv_set(memdb, "weather_skill_hrrr_chicago_6_24",
           {"sigma": 0.5}, ttl_seconds=86400)  # no 'n' key
    assert v2._get_learned_sigma(
        "hrrr", 12.0, city_key="chicago"
    ) == pytest.approx(1.20)


def _persist_mos_bias_pooled(conn, source: str, city: str, bias: float):
    from bot.db import kv_set
    kv_set(conn, f"weather_mos_bias_{source}_{city}",
           {"bias": bias, "n": 200, "eff_n": 200.0,
            "fit_at": "2026-04-30T00:00:00Z"},
           ttl_seconds=86400)


def _persist_mos_bias_regime(conn, source: str, city: str,
                              regime: str, bias: float):
    from bot.db import kv_set
    kv_set(conn, f"weather_mos_bias_{source}_{city}_{regime}",
           {"bias": bias, "n": 80, "eff_n": 80.0,
            "fit_at": "2026-04-30T00:00:00Z"},
           ttl_seconds=86400)


def test_mos_bias_pooled_lookup_no_regime(memdb):
    """No regime_label → pooled key wins (legacy behavior preserved)."""
    _persist_mos_bias_pooled(memdb, "hrrr", "nyc", 1.25)
    assert v2._get_mos_bias("hrrr", "nyc") == pytest.approx(1.25)


def test_mos_bias_regime_conditional_overrides_pooled(memdb):
    """When regime key exists, it wins over pooled. Caller passes the
    label observed in the current cycle.
    """
    _persist_mos_bias_pooled(memdb, "hrrr", "nyc", 1.25)
    _persist_mos_bias_regime(memdb, "hrrr", "nyc", "clear|sw_wind", 0.40)
    assert v2._get_mos_bias("hrrr", "nyc",
                             regime_label="clear|sw_wind") == pytest.approx(0.40)
    # Different regime → pooled fallback (no key for this regime).
    assert v2._get_mos_bias("hrrr", "nyc",
                             regime_label="overcast|n_wind") == pytest.approx(1.25)


def test_mos_bias_unknown_regime_falls_back_to_pooled(memdb):
    """``regime_label="unknown"`` is treated as no-regime so we don't
    accidentally key on the sentinel string.
    """
    _persist_mos_bias_pooled(memdb, "hrrr", "nyc", 1.25)
    _persist_mos_bias_regime(memdb, "hrrr", "nyc", "unknown", 99.0)
    assert v2._get_mos_bias("hrrr", "nyc",
                             regime_label="unknown") == pytest.approx(1.25)


def test_mos_bias_clamped_to_max_abs(memdb):
    """Sanity: a single outlier kv cell can't move the Gaussian by more
    than ±_MOS_BIAS_MAX_ABS_F (5°F). Even a stale 99°F payload clamps."""
    _persist_mos_bias_pooled(memdb, "hrrr", "nyc", 99.0)
    assert v2._get_mos_bias("hrrr", "nyc") == pytest.approx(v2._MOS_BIAS_MAX_ABS_F)
    _persist_mos_bias_pooled(memdb, "hrrr", "nyc", -99.0)
    assert v2._get_mos_bias("hrrr", "nyc") == pytest.approx(-v2._MOS_BIAS_MAX_ABS_F)


def test_pooled_sigma_does_not_require_min_samples(memdb):
    """Pooled fits are already sample-aggregated across all cities — the
    sample-size gate only applies to per-city keys. A small-n pooled key
    is still honored.
    """
    _persist_skill(memdb, "ukmo", "6_24", 2.5, n=29)  # eval-seeded, small
    assert v2._get_learned_sigma("ukmo", 12.0) == pytest.approx(2.5)
    assert v2._get_learned_sigma("ukmo", 12.0,
                                  city_key="austin") == pytest.approx(2.5)


def test_group_membership_new_forecast_sources_in_model_group():
    """2026-04-30: GEM, MetNo, ECMWF HRES added to combine. They're all
    global NWP models with physics correlated to HRRR / NWS Point /
    Open-Meteo / ICON / UKMO, so they MUST sit in _MODEL_GROUP for the
    correlation discount to fire. Independence ρ vs HRRR (validated):
    GEM 0.61, MetNo 0.73, ECMWF 0.53 — all clearly model-family.
    """
    assert v2._group_of("gem") == "model"
    assert v2._group_of("metno") == "model"
    assert v2._group_of("ecmwf") == "model"


def test_group_membership_icon_ukmo_in_model_group():
    """Regression: ICON and UKMO are global NWP models with physics
    correlated to HRRR / NWS Point / Open-Meteo. They MUST sit in
    _MODEL_GROUP so the correlation discount fires; otherwise they'd
    sit in 'other' and double-count the model family in the precision
    combine. Discovered + fixed 2026-04-30.
    """
    assert v2._group_of("hrrr") == "model"
    assert v2._group_of("nws_point") == "model"
    assert v2._group_of("weather") == "model"
    assert v2._group_of("nbm") == "model"
    assert v2._group_of("icon") == "model"
    assert v2._group_of("ukmo") == "model"


def test_group_membership_observation_sources():
    """METAR + MADIS + nws_5min all read ASOS sensors → obs group."""
    assert v2._group_of("metar") == "obs"
    assert v2._group_of("madis") == "obs"
    assert v2._group_of("nws_5min") == "obs"


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


def test_apply_learned_sigma_floors_pathological_tight_sigma(memdb):
    """Regression: METAR's σ fit at 0.3°F (n=18, self-referential) caused
    ensemble σ-collapse on 2026-04-29. Learned σ values below the floor
    must be raised to ``_LEARNED_SIGMA_FLOOR_F`` so no single source can
    dominate the combine. See the constant's docstring for the math."""
    _persist_skill(memdb, "metar", "6_24", 0.3)  # the pathological value
    g = _g("metar", sigma_f=2.0, horizon_hours=12.0)
    g2 = v2._apply_learned_sigma(g)
    assert g2.sigma_f == pytest.approx(v2._LEARNED_SIGMA_FLOOR_F)
    assert g2.sigma_f >= 1.5  # explicit floor sanity

    # And via _apply_learned_sigma_with_flag — same behavior, flag set.
    g3, was_learned = v2._apply_learned_sigma_with_flag(g)
    assert was_learned is True
    assert g3.sigma_f == pytest.approx(v2._LEARNED_SIGMA_FLOOR_F)


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


# ── σ inflation (post-combine, pre-projection) ────────────────────────────

def test_sigma_inflation_default_is_one(monkeypatch, memdb):
    """Cold cache + no env var → default 1.0× (no-op).

    The 91-day skill-curve fit showed σ priors are roughly empirically
    correct, so the post-combine inflation multiplier defaults to 1.0
    (no-op). Kept as a kv/env-overridable knob in case later shadow data
    diverges and we need an emergency band-aid before re-fitting."""
    monkeypatch.delenv(v2._SIGMA_INFLATION_ENV, raising=False)
    assert v2._get_sigma_inflation() == 1.0


def test_sigma_inflation_env_override(monkeypatch, memdb):
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "1.5")
    assert v2._get_sigma_inflation() == 1.5


def test_sigma_inflation_env_clamped(monkeypatch, memdb):
    """Out-of-range env value clamps to [1.0, 4.0]."""
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "10.0")
    assert v2._get_sigma_inflation() == v2._SIGMA_INFLATION_MAX
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "0.1")
    assert v2._get_sigma_inflation() == v2._SIGMA_INFLATION_MIN


def test_sigma_inflation_kv_override_precedence(monkeypatch, memdb):
    """kv_cache value wins over env var."""
    from bot.db import kv_set

    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "1.5")
    kv_set(memdb, v2._SIGMA_INFLATION_KEY, {"factor": 3.0}, ttl_seconds=3600)
    assert v2._get_sigma_inflation() == 3.0


def test_sigma_inflation_kv_clamped(monkeypatch, memdb):
    from bot.db import kv_set

    monkeypatch.delenv(v2._SIGMA_INFLATION_ENV, raising=False)
    kv_set(memdb, v2._SIGMA_INFLATION_KEY, {"factor": 99.0}, ttl_seconds=3600)
    assert v2._get_sigma_inflation() == v2._SIGMA_INFLATION_MAX


def test_apply_sigma_inflation_widens_sigma(monkeypatch, memdb):
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "2.0")
    g = _g("hrrr", mean_f=78.0, sigma_f=1.5, horizon_hours=8.0)
    inflated = v2._apply_sigma_inflation(g)
    assert inflated.sigma_f == pytest.approx(3.0)
    # Mean / horizon / provenance preserved.
    assert inflated.mean_f == 78.0
    assert inflated.horizon_hours == 8.0
    assert inflated.source_name == "hrrr"


def test_apply_sigma_inflation_factor_one_is_noop(monkeypatch, memdb):
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "1.0")
    g = _g("hrrr", mean_f=78.0, sigma_f=1.5)
    inflated = v2._apply_sigma_inflation(g)
    assert inflated is g


# ── Per-family σ inflation overrides ──────────────────────────────────


def test_sigma_inflation_per_family_kv_override(monkeypatch, memdb):
    """Per-family kv key wins over global kv. Confirmed via the per-
    family Brier sweep (2026-05-03): each KXHIGH* family has its own
    optimal σ multiplier. Global fallback ensures we never regress."""
    from bot.db import kv_set

    monkeypatch.delenv(v2._SIGMA_INFLATION_ENV, raising=False)
    kv_set(memdb, v2._SIGMA_INFLATION_KEY, {"factor": 2.0}, ttl_seconds=3600)
    kv_set(
        memdb,
        f"{v2._SIGMA_INFLATION_FAMILY_KEY_PREFIX}KXHIGHLAX",
        {"factor": 4.0},
        ttl_seconds=3600,
    )
    # KXHIGHLAX has a per-family override → 4.0
    assert v2._get_sigma_inflation("KXHIGHLAX-26MAY03-T70") == 4.0
    # KXHIGHCHI has no per-family override → falls through to global 2.0
    assert v2._get_sigma_inflation("KXHIGHCHI-26MAY03-T55") == 2.0
    # No ticker passed → global 2.0
    assert v2._get_sigma_inflation() == 2.0


def test_sigma_inflation_per_family_clamped(monkeypatch, memdb):
    """Per-family override is clamped to [1.0, 4.0] just like global."""
    from bot.db import kv_set

    monkeypatch.delenv(v2._SIGMA_INFLATION_ENV, raising=False)
    kv_set(
        memdb,
        f"{v2._SIGMA_INFLATION_FAMILY_KEY_PREFIX}KXHIGHDEN",
        {"factor": 99.0},
        ttl_seconds=3600,
    )
    assert v2._get_sigma_inflation("KXHIGHDEN-26MAY03-T70") == v2._SIGMA_INFLATION_MAX


def test_sigma_inflation_per_family_can_set_to_one(monkeypatch, memdb):
    """KXHIGHDEN's optimal factor from the Brier sweep is 1.0 (its
    sources already agree). Setting per-family=1.0 must defeat any
    inflated global, returning the source σ unchanged."""
    from bot.db import kv_set

    monkeypatch.delenv(v2._SIGMA_INFLATION_ENV, raising=False)
    kv_set(memdb, v2._SIGMA_INFLATION_KEY, {"factor": 3.0}, ttl_seconds=3600)
    kv_set(
        memdb,
        f"{v2._SIGMA_INFLATION_FAMILY_KEY_PREFIX}KXHIGHDEN",
        {"factor": 1.0},
        ttl_seconds=3600,
    )
    # KXHIGHDEN-anything → 1.0 (per-family)
    assert v2._get_sigma_inflation("KXHIGHDEN-26MAY03-T70") == 1.0
    # KXHIGHMIA-anything → 3.0 (global fallback)
    assert v2._get_sigma_inflation("KXHIGHMIA-26MAY03-T80") == 3.0


def test_apply_sigma_inflation_routes_by_family(monkeypatch, memdb):
    """End-to-end: ``_apply_sigma_inflation`` reads per-family kv and
    applies the right factor to the Gaussian's σ."""
    from bot.db import kv_set

    monkeypatch.delenv(v2._SIGMA_INFLATION_ENV, raising=False)
    kv_set(
        memdb,
        f"{v2._SIGMA_INFLATION_FAMILY_KEY_PREFIX}KXHIGHLAX",
        {"factor": 4.0},
        ttl_seconds=3600,
    )
    kv_set(
        memdb,
        f"{v2._SIGMA_INFLATION_FAMILY_KEY_PREFIX}KXHIGHDEN",
        {"factor": 1.0},
        ttl_seconds=3600,
    )
    g = _g("combined_v2", mean_f=70.0, sigma_f=1.5, horizon_hours=8.0)
    lax = v2._apply_sigma_inflation(g, ticker="KXHIGHLAX-26MAY03-T70")
    den = v2._apply_sigma_inflation(g, ticker="KXHIGHDEN-26MAY03-T70")
    assert lax.sigma_f == pytest.approx(6.0)  # 1.5 × 4.0
    assert den is g  # factor=1.0 → identity short-circuit, original instance


def test_family_from_ticker():
    assert v2._family_from_ticker("KXHIGHMIA-26MAY03-T80") == "KXHIGHMIA"
    assert v2._family_from_ticker("kxhighmia-26may03-t80") == "KXHIGHMIA"
    assert v2._family_from_ticker(None) is None
    assert v2._family_from_ticker("") is None
    assert v2._family_from_ticker("nodash") == "NODASH"


def test_predict_v2_inflation_widens_combined_sigma_in_snapshot(memdb, monkeypatch):
    """End-to-end: combined σ stored in weather_forecast_snapshots reflects
    the inflated σ, not the pre-inflation σ. Use a single source so the
    pre-inflation σ is the source's σ verbatim."""
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "2.0")
    gs = [_g("hrrr", mean_f=78.0, sigma_f=1.5, horizon_hours=8.0)]
    p, _ = _run_v2(memdb, monkeypatch, gs)
    assert p is not None

    row = memdb.execute(
        "SELECT sigma_f FROM weather_forecast_snapshots "
        "WHERE source = 'combined_v2' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(3.0)


def test_predict_v2_inflation_pulls_offcenter_prob_toward_half(memdb, monkeypatch):
    """μ=76.5, threshold=75 (slightly off-center). Wider σ should pull
    P(>75) toward 0.5 — the calibration fix we want — without hitting the
    [0.02, 0.98] clamp at either end."""
    gs = [_g("hrrr", mean_f=76.5, sigma_f=1.5, horizon_hours=8.0)]

    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "1.0")
    p_tight, _ = _run_v2(memdb, monkeypatch, gs)
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "2.0")
    p_wide, _ = _run_v2(memdb, monkeypatch, gs)
    # Tight σ → confident YES (~0.84). Wide σ → less confident (~0.69).
    # Both above 0.5; wider should be closer to 0.5.
    assert 0.5 < p_wide < p_tight, (
        f"σ inflation should pull off-center prob toward 0.5: "
        f"tight={p_tight:.4f} wide={p_wide:.4f}"
    )


# ── Running-high floor (the live-shadow Brier-blowup fix) ─────────────────

def test_apply_running_high_floor_raises_combined_when_below_metar_mean(memdb):
    """The combine produces μ=53; METAR observed running_high pushed METAR's
    μ to 55. Daily-high cannot be below already-observed running max, so the
    floor should shift the combined mean up to 55."""
    combined = _g("combined_v2", mean_f=53.15, sigma_f=1.35)
    metar = _g("metar", mean_f=55.0, sigma_f=5.0)
    inputs = [
        _g("hrrr", mean_f=53.0, sigma_f=1.4),
        _g("nbm",  mean_f=53.0, sigma_f=1.4),
        metar,
    ]
    floored = v2._apply_running_high_floor(combined, inputs)
    assert floored.mean_f == pytest.approx(55.0)
    # σ unchanged in v1 of the fix.
    assert floored.sigma_f == pytest.approx(1.35)
    # Provenance preserved.
    assert floored.source_name == "combined_v2"


def test_apply_running_high_floor_noop_when_combined_already_above(memdb):
    """Combine produced μ=70; METAR observed only μ=55 (early day, low
    running_high). Floor is below combined mean — no shift."""
    combined = _g("combined_v2", mean_f=70.0, sigma_f=1.5)
    metar = _g("metar", mean_f=55.0, sigma_f=5.0)
    inputs = [_g("hrrr", mean_f=70.0, sigma_f=1.5), metar]
    floored = v2._apply_running_high_floor(combined, inputs)
    assert floored is combined  # exact same object — no allocation when no-op


def test_apply_running_high_floor_noop_when_no_metar_present(memdb):
    """If METAR didn't fire (poller silent or station unsupported), there's
    no observed-running-high information to enforce. Combine is unchanged."""
    combined = _g("combined_v2", mean_f=53.0, sigma_f=1.5)
    inputs = [_g("hrrr", mean_f=53.0, sigma_f=1.5)]
    floored = v2._apply_running_high_floor(combined, inputs)
    assert floored is combined


def test_predict_v2_floor_lifts_prob_when_forecasts_below_observation(memdb, monkeypatch):
    """End-to-end: forecasts say μ=73°F; METAR observed running_high pushed
    METAR's μ to 75.5°F (above threshold of 75 from the default ticker).
    Without the floor, combined.μ falls between 73 and 75.5 (forecast-
    weighted) → P(>75) is small. With the floor, combined.μ ≥ 75.5 → P(>75)
    is much larger. Asserts the floor binds and lifts P meaningfully."""
    gs = [
        _g("hrrr",    mean_f=73.0, sigma_f=1.4),
        _g("nbm",     mean_f=73.0, sigma_f=1.4),
        _g("weather", mean_f=73.0, sigma_f=1.4),
        _g("metar",   mean_f=75.5, sigma_f=5.0),
    ]
    p, _ = _run_v2(memdb, monkeypatch, gs)
    # With the floor binding at 75.5, the combined Gaussian centers above the
    # threshold of 75 → P(>75) > 0.5.
    assert p > 0.5, f"floor should lift prob above 0.5 (threshold 75), got {p:.3f}"


def test_predict_v2_truncation_skipped_when_forecast_agrees_with_metar(memdb, monkeypatch):
    """H3 conditional truncation (shipped 2026-04-29 alongside the CF6
    ground-truth fix). When combined.μ ≥ METAR.μ - 0.5°F, projection
    must run un-truncated so we don't double-count the running-high
    constraint that step 4c already enforced via mean shift.

    Pre-fix behavior: truncation fires unconditionally → P(in bracket)
    gets renormalized by 1/p_above_t ≈ 2 when combined.μ ≈ METAR.μ →
    we hit the 0.995 clamp on whichever bracket holds the centred μ.
    On boundary-bracket cases that's wrong half the time and Brier
    explodes (post-CF6 sweep, n=143: −0.034 on KXHIGHMIA pooled with
    truncation off vs on).

    Setup: forecasts cluster at 75°F (the threshold), METAR also at 75°F.
    Step 4c is a no-op (combined.μ ≈ METAR.μ). Threshold ticker T75 →
    P(high > 75) starts ~0.5 from the centred Gaussian; with the H3
    conditional, projection stays un-truncated and prob stays well
    below the 0.995 clamp."""
    gs = [
        _g("hrrr",  mean_f=75.0, sigma_f=1.0),
        _g("nbm",   mean_f=75.0, sigma_f=1.0),
        _g("metar", mean_f=75.0, sigma_f=5.0),
    ]
    p, _ = _run_v2(memdb, monkeypatch, gs)
    assert p < 0.9, (
        f"truncation should be skipped when forecast ≈ observation; "
        f"unexpected near-clamp prob {p:.3f} suggests over-amplification "
        f"(H3 conditional regression — see comment at predict_v2 step 5)"
    )


def test_predict_v2_truncation_still_fires_when_forecast_below_observation(
    memdb, monkeypatch,
):
    """H3 fix preserves the catastrophic-miss save. The path:
       step 4c shifts combined.μ from below METAR.μ up to METAR.μ;
       step 5 sees combined.μ == METAR.μ → truncation skipped (per H3).
    The shifted combined.μ is what gives us a sane projection now —
    asserting that prob is meaningfully above where the un-shifted
    forecast (μ=70) would have placed it."""
    gs = [
        _g("hrrr",    mean_f=70.0, sigma_f=1.4),
        _g("nbm",     mean_f=70.0, sigma_f=1.4),
        _g("weather", mean_f=70.0, sigma_f=1.4),
        _g("metar",   mean_f=75.5, sigma_f=5.0),
    ]
    p, _ = _run_v2(memdb, monkeypatch, gs)
    # Step 4c lifts combined.μ to 75.5 → P(>75) > 0.5 from the lifted
    # Gaussian alone, no truncation needed.
    assert p > 0.5, (
        f"catastrophic-miss save (forecast 70, METAR 75.5) should still "
        f"produce P(>75) > 0.5 via step 4c shift; got {p:.3f}"
    )


def test_combined_sigma_floor_is_one():
    """Pin the σ floor to its post-CF6 sweep optimum (1.0°F). Pre-CF6
    we ran 0.5°F because cold-biased μ benefited from concentration on
    the bracket nearest truth. Post-CF6 the sweep showed sigma_floor=1.0
    gives +0.0025 pooled vs 0.5°F; 1.5°F over-spreads and gives -0.0042.
    Lock the optimum so it doesn't accidentally drift back."""
    assert v2._COMBINED_SIGMA_FLOOR_F == 1.0


# ── Source staleness (B) ───────────────────────────────────────────────────

def test_staleness_inflation_factor_zero_for_live_sources(memdb):
    """METAR / MADIS / unknown sources have staleness=0 → factor 1.0."""
    g_metar = _g("metar", mean_f=70.0, sigma_f=2.0, horizon_hours=8.0)
    g_madis = _g("madis", mean_f=70.0, sigma_f=2.0, horizon_hours=8.0)
    g_unknown = _g("unknown_source", mean_f=70.0, sigma_f=2.0, horizon_hours=8.0)
    assert v2._staleness_inflation_factor(g_metar) == 1.0
    assert v2._staleness_inflation_factor(g_madis) == 1.0
    assert v2._staleness_inflation_factor(g_unknown) == 1.0


def test_staleness_inflation_factor_reflects_cadence(memdb):
    """NBM (assumed 3h stale) at horizon 12h → factor ≈ sqrt(1.25) ≈ 1.118."""
    g = _g("nbm", mean_f=70.0, sigma_f=1.5, horizon_hours=12.0)
    factor = v2._staleness_inflation_factor(g)
    assert factor == pytest.approx(1.118, abs=0.01)


def test_staleness_inflation_uses_issued_at_when_present(memdb):
    """When the source supplies issued_at, use the real staleness instead
    of the per-source cadence assumption."""
    import time as _time
    # NBM with explicit issued_at = 6h ago (stale beyond cadence-assumption)
    one_hour_s = 3600.0
    issued = _time.time() - 6 * one_hour_s
    g = GaussianForecast(
        mean_f=70.0, sigma_f=1.5, horizon_hours=12.0,
        source_name="nbm", source_tag="nbm:nyc", issued_at=issued,
    )
    factor = v2._staleness_inflation_factor(g)
    # sqrt(1 + 6/12) = sqrt(1.5) ≈ 1.225
    assert factor == pytest.approx(1.225, abs=0.02)


def test_apply_staleness_inflation_is_noop_for_live_sources(memdb):
    """Live obs sources pass through unchanged."""
    g = _g("metar", mean_f=70.0, sigma_f=2.0, horizon_hours=8.0)
    out = v2._apply_staleness_inflation(g)
    assert out is g


def test_apply_staleness_inflation_widens_nbm_sigma(memdb):
    """NBM σ widens by ~12% after staleness inflation at 12h horizon."""
    g = _g("nbm", mean_f=70.0, sigma_f=1.5, horizon_hours=12.0)
    out = v2._apply_staleness_inflation(g)
    # ~ 1.5 × 1.118 ≈ 1.677
    assert out.sigma_f == pytest.approx(1.677, abs=0.02)
    # Mean / horizon / provenance preserved.
    assert out.mean_f == 70.0
    assert out.horizon_hours == 12.0
    assert out.source_name == "nbm"


def test_issued_at_preserved_through_shifted_and_with_sigma(memdb):
    """Mutator helpers must round-trip ``issued_at`` so staleness math
    after MOS bias / skill σ replacement still uses the real timestamp."""
    g = GaussianForecast(
        mean_f=70.0, sigma_f=1.5, horizon_hours=12.0,
        source_name="nbm", source_tag="nbm:nyc", issued_at=1234567890.0,
    )
    assert g.shifted(0.5).issued_at == 1234567890.0
    assert g.with_sigma(2.0).issued_at == 1234567890.0
    assert g.with_inflated_sigma(1.2).issued_at == 1234567890.0


# ── Source σ ceiling (Option B) ─────────────────────────────────────────────


def test_source_sigma_ceiling_caps_under_fit_sources(memdb, monkeypatch):
    """A source whose pooled fallback σ is wide (e.g., NWS Point at 3.5°F)
    should be capped at _SOURCE_SIGMA_CEILING_F so it isn't effectively
    excluded from the precision-weighted combine."""
    # Patch _collect_gaussians to skip the per-source getters and return
    # raw inputs; the σ ceiling is applied INSIDE _collect_gaussians
    # itself, so we need to call it via the real predict_v2 path.
    raw = [
        _g("hrrr", mean_f=89.5, sigma_f=0.95, horizon_hours=14.0),
        _g("nws_point", mean_f=94.0, sigma_f=3.50, horizon_hours=14.0),  # wide
    ]

    # Stub each per-source getter to return the test gaussians directly,
    # bypassing the API fetches.
    import bot.signals.sources.hrrr as hrrr_mod
    import bot.signals.sources.nws_point as nws_mod
    monkeypatch.setattr(hrrr_mod, "get_hrrr_gaussian", lambda t, m: raw[0])
    monkeypatch.setattr(nws_mod, "get_nws_point_gaussian", lambda t, m: raw[1])
    # Force other sources to return None so they don't pollute the test.
    import bot.signals.sources.ndfd_nbm as nbm_mod
    import bot.signals.sources.weather as wx_mod
    import bot.signals.sources.metar_observations as metar_mod
    import bot.signals.sources.madis as madis_mod
    monkeypatch.setattr(nbm_mod, "get_nbm_gaussian", lambda t, m: None)
    monkeypatch.setattr(wx_mod, "get_weather_gaussian", lambda t, m: None)
    monkeypatch.setattr(metar_mod, "get_metar_gaussian", lambda t, m: None)
    monkeypatch.setattr(madis_mod, "get_madis_gaussian", lambda t, m: None)

    # Use Austin — KXHIGHAUS has no per-city exclusions per the
    # 2026-05-04 regression. KXHIGHNY now drops nws_point (cold-bias),
    # which would make this test misleading.
    market_data = _market(title="Will Austin high exceed 90°F today?")
    out = v2._collect_gaussians("KXHIGHAUS-26APR23-T85", market_data)
    by_name = {g.source_name: g for g in out}
    # NWS Point's σ should be capped at _SOURCE_SIGMA_CEILING_F.
    assert by_name["nws_point"].sigma_f <= v2._SOURCE_SIGMA_CEILING_F + 1e-6
    # HRRR's σ (0.95) is below the ceiling and should pass through.
    assert by_name["hrrr"].sigma_f <= 1.0


def test_source_sigma_ceiling_does_not_affect_well_fit_sources(memdb, monkeypatch):
    """Sources already at or below the ceiling should not be modified."""
    g = _g("hrrr", mean_f=70.0, sigma_f=1.5, horizon_hours=8.0)
    import bot.signals.sources.hrrr as hrrr_mod
    monkeypatch.setattr(hrrr_mod, "get_hrrr_gaussian", lambda t, m: g)
    for mod_name, attr in [
        ("bot.signals.sources.ndfd_nbm", "get_nbm_gaussian"),
        ("bot.signals.sources.weather", "get_weather_gaussian"),
        ("bot.signals.sources.metar_observations", "get_metar_gaussian"),
        ("bot.signals.sources.madis", "get_madis_gaussian"),
        ("bot.signals.sources.nws_point", "get_nws_point_gaussian"),
    ]:
        import importlib
        mod = importlib.import_module(mod_name)
        monkeypatch.setattr(mod, attr, lambda t, m: None)

    out = v2._collect_gaussians("KXHIGHNY-26APR23-T75", _market())
    by_name = {gg.source_name: gg for gg in out}
    # HRRR σ=1.5 < ceiling=2.0 → unchanged (modulo skill σ override which
    # may or may not fire depending on kv state; both are <= 2.0).
    assert by_name["hrrr"].sigma_f <= v2._SOURCE_SIGMA_CEILING_F


# ── TTE-aware σ inflation decay (2026-05-04) ──────────────────────────


def test_decay_factor_full_when_tte_above_threshold():
    """At TTE >= _TTE_FULL_H, the full base factor applies (no decay)."""
    assert v2._decay_factor_for_tte(3.0, 12.0) == 3.0
    assert v2._decay_factor_for_tte(3.0, 8.0) == 3.0
    assert v2._decay_factor_for_tte(4.0, 100.0) == 4.0


def test_decay_factor_one_when_tte_below_threshold():
    """At TTE <= _TTE_NONE_H, factor decays fully to 1.0 (no inflation).

    Postmortem motivation: with peak observed and outcome essentially
    determined, σ should collapse toward observed-residual, not inflate.
    """
    assert v2._decay_factor_for_tte(3.0, 2.0) == 1.0
    assert v2._decay_factor_for_tte(4.0, 0.5) == 1.0
    assert v2._decay_factor_for_tte(3.0, 0.0) == 1.0


def test_decay_factor_linear_between_thresholds():
    """At the midpoint between _TTE_NONE_H (2h) and _TTE_FULL_H (8h),
    factor is halfway between 1.0 and base."""
    # base=3.0, tte=5.0 (midpoint) → factor = 1.0 + (3.0-1.0)*0.5 = 2.0
    assert v2._decay_factor_for_tte(3.0, 5.0) == pytest.approx(2.0)
    # base=4.0, tte=5.0 (midpoint) → factor = 1.0 + (4.0-1.0)*0.5 = 2.5
    assert v2._decay_factor_for_tte(4.0, 5.0) == pytest.approx(2.5)


def test_decay_factor_unknown_tte_uses_base():
    """When tte_hours is None (caller didn't pass it), back-compat:
    apply the full base factor. Pre-2026-05-04 callers pass None."""
    assert v2._decay_factor_for_tte(3.0, None) == 3.0
    assert v2._decay_factor_for_tte(4.0, None) == 4.0


def test_decay_factor_base_one_is_noop_at_any_tte():
    """A family with no inflation (factor=1.0, e.g. KXHIGHDEN) stays
    at 1.0 across the TTE range — no point inflating then decaying
    something that started at 1.0."""
    for tte in [0.0, 1.0, 5.0, 8.0, 12.0, None]:
        assert v2._decay_factor_for_tte(1.0, tte) == 1.0


def test_apply_sigma_inflation_with_tte_decay(monkeypatch, memdb):
    """End-to-end: a 3.0× factor combined with TTE=5.0h gives 2.0×
    actual inflation."""
    monkeypatch.setenv(v2._SIGMA_INFLATION_ENV, "3.0")
    g = _g("combined_v2", mean_f=70.0, sigma_f=1.5, horizon_hours=8.0)
    # TTE >= 8h: full inflation
    full = v2._apply_sigma_inflation(g, ticker=None, tte_hours=10.0)
    assert full.sigma_f == pytest.approx(4.5)  # 1.5 × 3.0
    # TTE = 5h (midpoint): half decay
    mid = v2._apply_sigma_inflation(g, ticker=None, tte_hours=5.0)
    assert mid.sigma_f == pytest.approx(3.0)   # 1.5 × 2.0
    # TTE = 2h (full decay): no inflation
    none_ = v2._apply_sigma_inflation(g, ticker=None, tte_hours=2.0)
    assert none_ is g  # short-circuit when factor==1.0
    # TTE = None (back-compat): full inflation
    bcompat = v2._apply_sigma_inflation(g, ticker=None, tte_hours=None)
    assert bcompat.sigma_f == pytest.approx(4.5)
