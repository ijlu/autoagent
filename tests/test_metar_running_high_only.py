"""Regression tests for the F.4 shadow path: μ=running_high alternative
to the NWP-blended ``expected_eventual_high`` in get_metar_gaussian.

Motivation (2026-05-12 audit):
  The METAR Gaussian's μ is computed as a blend of running_high (observed)
  and forecast_high (NWP-derived). When NWP misses (cold fronts, late heat
  domes), the blended μ carries a +5–15°F bias into the post-peak fast-path,
  which then collapses σ to 1.0°F. Cross-bracket then fires confidently
  against well-priced markets and loses (5/5 KXHIGHDEN directional losses
  in the audit window).

This file pins:
  1. The side-channel stash always carries the alt μ and σ.
  2. The flag controls whether the LIVE Gaussian uses the alt.
  3. Past-peak return path also stashes (so we can see the σ comparison
     even though μ already matches).
"""

from __future__ import annotations

import sqlite3

import pytest

from bot.signals.sources import metar_observations as mo


@pytest.fixture(autouse=True)
def clear_alt_stash():
    """Side-channel is module-global; reset between tests."""
    mo._ALT_MU_RUNNING_HIGH.clear()
    yield
    mo._ALT_MU_RUNNING_HIGH.clear()


# ── _stash_running_high_only_alt unit tests ───────────────────────────


class TestStashRunningHighOnly:
    def test_stash_with_diurnal_rmse(self):
        mo._stash_running_high_only_alt("KDEN", 78.5, 16, diurnal_rmse=1.8)
        stashed = mo._ALT_MU_RUNNING_HIGH["KDEN"]
        assert stashed["mu_f"] == 78.5
        assert stashed["sigma_f"] == 1.8
        assert stashed["lst_hour"] == 16

    def test_stash_no_diurnal_uses_fallback(self):
        """When no diurnal fit is available the σ falls back to the
        conservative default — wider than past-peak's 0.3°F and the
        fast-path's 1.0°F. This matches Josh's intuition: claim less
        precision when we're grounding μ only on observed data."""
        mo._stash_running_high_only_alt("KAUS", 91.0, 14, diurnal_rmse=None)
        assert mo._ALT_MU_RUNNING_HIGH["KAUS"]["sigma_f"] == (
            mo._RUNNING_HIGH_ONLY_FALLBACK_SIGMA_F
        )

    def test_stash_zero_rmse_uses_fallback(self):
        """RMSE of zero is pathological (perfect fit on tiny sample);
        treat the same as missing fit and fall back to the conservative
        floor rather than claiming infinite precision."""
        mo._stash_running_high_only_alt("KMIA", 88.0, 15, diurnal_rmse=0.0)
        assert mo._ALT_MU_RUNNING_HIGH["KMIA"]["sigma_f"] == (
            mo._RUNNING_HIGH_ONLY_FALLBACK_SIGMA_F
        )

    def test_get_alt_mu_running_high_is_pop_semantics(self):
        """Pop semantics prevent a downstream consumer from reading the
        stale value on a cycle where the new fetch failed before
        re-stashing."""
        mo._stash_running_high_only_alt("KNYC", 75.0, 13, diurnal_rmse=1.5)
        assert mo.get_alt_mu_running_high("KNYC")["mu_f"] == 75.0
        # Second read returns None — first read consumed it.
        assert mo.get_alt_mu_running_high("KNYC") is None


# ── Flag-gated live cutover (integration via get_metar_gaussian) ──────


def _fake_metar_obs(temp_c=21.1):
    """Minimal metar payload that satisfies _extract_station_obs."""
    return [{
        "icaoId": "KAUS",
        "temp": temp_c,  # 21.1°C ≈ 70°F
        "reportTime": "2026-05-12T20:00:00Z",
    }]


def _setup_get_metar_gaussian_environment(monkeypatch, tmp_path):
    """Patch the network + persistence layers used by get_metar_gaussian
    so the function runs without hitting Kalshi/NOAA APIs or the
    daemon-shared DB."""
    # Mock the upstream METAR fetch.
    monkeypatch.setattr(mo, "_fetch_metar_data", lambda: _fake_metar_obs())

    # _update_running_daily_high reads + writes kv_cache, which needs a
    # real DB. Mock it to return a deterministic running_high.
    def _fake_update_running(station, temp_f, obs_time):
        return {"high_f": 92.0, "last_increase_lst_hour": 14, "last_obs_at": obs_time}
    monkeypatch.setattr(mo, "_update_running_daily_high", _fake_update_running)

    # _get_forecast_high reads kv_cache for the NWP forecast. Return a
    # known-wrong value so we can detect when it leaks into live μ.
    monkeypatch.setattr(mo, "_get_forecast_high", lambda s, rh: 105.0)

    # Side-channel: _sigma_for_hours reads regime configs from kv_cache;
    # short-circuit to a fixed σ to avoid touching the DB.
    monkeypatch.setattr(mo, "_sigma_for_hours", lambda *a, **kw: 3.0)

    # _get_lst_now is used in several places that need a real datetime.
    # Default to LST 13 (before past-peak clamp). Tests can re-monkeypatch
    # this single function to push past-peak.
    from datetime import datetime
    def _fake_lst_now(station):
        return datetime(2026, 5, 12, 13, 0, 0)
    monkeypatch.setattr(mo, "_get_lst_now", _fake_lst_now)

    # _get_diurnal_fit returns None → we exercise the cold-start /
    # forecast-blend branch.
    monkeypatch.setattr(mo, "_get_diurnal_fit", lambda *a: None)

    # hours_until_settlement_end / lst_offset_for_station: 6h to settle.
    monkeypatch.setattr(
        mo, "hours_until_settlement_end", lambda offset, day_idx=0: 6.0
    )
    monkeypatch.setattr(mo, "lst_offset_for_station", lambda s: -5)


def test_flag_off_live_mu_uses_blended_alt_still_stashed(monkeypatch, tmp_path):
    """Default state: live μ is the NWP-blended expected_eventual_high
    (this preserves current behavior), but the alt is stashed for
    shadow-mode comparison."""
    _setup_get_metar_gaussian_environment(monkeypatch, tmp_path)
    import bot.config as cfg
    monkeypatch.setattr(cfg, "WEATHER_METAR_USE_RUNNING_HIGH_ONLY", False)

    g = mo.get_metar_gaussian(
        "KXHIGHAUS-26MAY12-B92.5",
        {"title": "Will the high be above 92.5", "subtitle": ""},
    )
    assert g is not None
    # Blended path: μ = something between running_high (92) and the
    # NWP forecast (105). NOT pure running_high.
    assert g.mean_f > 92.0
    # Side-channel still stashed for shadow comparison.
    stashed = mo.get_alt_mu_running_high("KAUS")
    assert stashed is not None
    assert stashed["mu_f"] == 92.0  # running_high


def test_flag_on_live_mu_uses_running_high(monkeypatch, tmp_path):
    """With the flag flipped, the live Gaussian's μ == running_high
    (NWP forecast is no longer in the channel)."""
    _setup_get_metar_gaussian_environment(monkeypatch, tmp_path)
    import bot.config as cfg
    monkeypatch.setattr(cfg, "WEATHER_METAR_USE_RUNNING_HIGH_ONLY", True)

    g = mo.get_metar_gaussian(
        "KXHIGHAUS-26MAY12-B92.5",
        {"title": "Will the high be above 92.5", "subtitle": ""},
    )
    assert g is not None
    assert g.mean_f == 92.0  # running_high exactly
    # σ falls back to the conservative default since no diurnal fit
    assert g.sigma_f == mo._RUNNING_HIGH_ONLY_FALLBACK_SIGMA_F


def test_past_peak_clamp_still_stashes_alt(monkeypatch):
    """The past-peak return path also stashes the alt. In that branch
    μ already equals running_high, but σ differs (past-peak 0.3°F vs
    alt's diurnal-or-fallback). Shadow comparison should still see
    both rows so the σ choice can be evaluated independently."""
    _setup_get_metar_gaussian_environment(monkeypatch, monkeypatch)

    # Force past-peak: LST 19 (>= _PAST_PEAK_HARD_HOUR=18)
    from datetime import datetime
    monkeypatch.setattr(
        mo, "_get_lst_now", lambda s: datetime(2026, 5, 12, 19, 0, 0),
    )

    g = mo.get_metar_gaussian(
        "KXHIGHAUS-26MAY12-B92.5",
        {"title": "Will the high be above 92.5", "subtitle": ""},
    )
    assert g is not None
    # In past-peak branch the live σ is the tight 0.3°F (existing
    # behavior — the constant is local to get_metar_gaussian, so we
    # pin the literal value here. If the past-peak σ is ever changed
    # this test fires and prompts the audit to be re-evaluated.
    assert g.sigma_f == 0.3
    # But the shadow stash uses the wider fallback σ — Josh's intuition
    # was "don't claim more precision than warranted from observations
    # alone". The shadow row will let us calibrate-vs-actual to compare.
    stashed = mo.get_alt_mu_running_high("KAUS")
    assert stashed is not None
    assert stashed["sigma_f"] == mo._RUNNING_HIGH_ONLY_FALLBACK_SIGMA_F


def test_default_flag_value_is_false():
    """Belt-and-suspenders against the flag accidentally flipping on
    via a default change. F.4 shadow mode REQUIRES the live path to
    stay on the current NWP-blended μ until shadow data validates the
    alternative."""
    import bot.config as cfg
    # Reload to pick up environment state (mostly tests sanity)
    assert cfg.WEATHER_METAR_USE_RUNNING_HIGH_ONLY is False
