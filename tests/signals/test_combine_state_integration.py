"""Integration tests for state-machine-aware combine inclusion.

Confirms ``_collect_gaussians`` correctly:
  - Drops sources in SHADOW / DEMOTED state
  - Includes ACTIVE sources at full weight
  - Inflates σ for PROBATIONARY sources by 1.3×
  - Skips the σ ceiling when learned σ is available
  - Applies the ceiling when no learned σ (fail-safe for new sources)

These are exactly the rules a wrong implementation could silently break,
producing weighted-wrong combines that take days to surface in Brier.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from bot.db import init_db, kv_set
from bot.learning.source_state_machine import (
    SourceState, upsert_state,
)
from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import GaussianForecast


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    import bot.db as db_mod
    monkeypatch.setattr(db_mod, "_PERSIST_CONN", None, raising=False)
    conn = init_db(str(db_path))
    yield conn
    monkeypatch.setattr(db_mod, "_PERSIST_CONN", None, raising=False)


def _g(name, mu=70.0, sigma=2.0, hours=24):
    return GaussianForecast(
        mean_f=mu, sigma_f=sigma, horizon_hours=hours,
        source_name=name, source_tag=f"{name}:test",
    )


def _market_data():
    return {"ticker": "KXHIGHNY-26APR30-T75",
            "title": "high temp NYC",
            "yes_sub_title": "75 or above",
            "close_time": "2030-04-30T23:59:59Z"}


_GETTER_PATCHES = {
    "hrrr": "bot.signals.sources.hrrr.get_hrrr_gaussian",
    "nws_point": "bot.signals.sources.nws_point.get_nws_point_gaussian",
    "icon": "bot.signals.sources.icon.get_icon_gaussian",
    "ukmo": "bot.signals.sources.ukmo.get_ukmo_gaussian",
    "gem": "bot.signals.sources.gem.get_gem_gaussian",
    "metno": "bot.signals.sources.metno.get_metno_gaussian",
    "ecmwf": "bot.signals.sources.ecmwf.get_ecmwf_gaussian",
    "metar": "bot.signals.sources.metar_observations.get_metar_gaussian",
    "nws_5min": "bot.signals.sources.nws_5min.get_nws_5min_gaussian",
    "nws_5min_diurnal": (
        "bot.signals.sources.nws_5min_diurnal.get_nws_5min_diurnal_gaussian"
    ),
}


def _patch_getters(stack: ExitStack, **returns):
    mocks = {}
    for name, target in _GETTER_PATCHES.items():
        mocks[name] = stack.enter_context(
            patch(target, return_value=returns.get(name))
        )
    return mocks


# ── State filtering ─────────────────────────────────────────────────────
class TestStateFilter:
    def test_shadow_source_excluded_from_combine(self, db):
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.SHADOW)
        ticker = "KXHIGHNY-26APR30-T75"
        # Mock _collect_gaussians' input getters to return one gaussian
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian",
                   return_value=_g("hrrr", 70.0, 1.5, hours=20.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.weather.get_weather_gaussian",
                   return_value=None):
            out = v2._collect_gaussians(ticker, _market_data())
        # HRRR was SHADOW → excluded
        assert all(g.source_name != "hrrr" for g in out)

    def test_active_source_included(self, db):
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.ACTIVE)
        ticker = "KXHIGHNY-26APR30-T75"
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian",
                   return_value=_g("hrrr", 70.0, 1.5, hours=20.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.weather.get_weather_gaussian",
                   return_value=None):
            out = v2._collect_gaussians(ticker, _market_data())
        assert any(g.source_name == "hrrr" for g in out)

    def test_demoted_source_excluded(self, db):
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.DEMOTED)
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian",
                   return_value=_g("hrrr", 70.0, 1.5, hours=20.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.weather.get_weather_gaussian",
                   return_value=None):
            out = v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        assert all(g.source_name != "hrrr" for g in out)

    def test_demoted_source_not_fetched(self, db):
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.DEMOTED)
        with ExitStack() as stack:
            mocks = _patch_getters(
                stack, hrrr=_g("hrrr", 70.0, 1.5, hours=20.0),
            )
            out = v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        assert out == []
        mocks["hrrr"].assert_not_called()

    def test_state_lookup_failure_fetches_source(self, db):
        with ExitStack() as stack:
            stack.enter_context(
                patch("bot.db.get_connection", side_effect=RuntimeError("db down"))
            )
            mocks = _patch_getters(
                stack, hrrr=_g("hrrr", 70.0, 1.5, hours=20.0),
            )
            out = v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        mocks["hrrr"].assert_called_once()
        assert any(g.source_name == "hrrr" for g in out)

    def test_slow_source_getter_logs_timing(self, db, monkeypatch, capsys):
        monkeypatch.setattr(v2, "_SOURCE_GETTER_SLOW_MS", 0.0)
        with ExitStack() as stack:
            _patch_getters(
                stack, hrrr=_g("hrrr", 70.0, 1.5, hours=20.0),
            )
            v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        assert (
            "source_timing hrrr KXHIGHNY-26APR30-T75"
            in capsys.readouterr().out
        )


class TestProbationaryInflation:
    def test_probationary_inflates_sigma(self, db):
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.PROBATIONARY)
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian",
                   return_value=_g("hrrr", 70.0, 1.5, hours=20.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.weather.get_weather_gaussian",
                   return_value=None):
            out = v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        hrrr_out = next(g for g in out if g.source_name == "hrrr")
        # 1.5 × 1.3 = 1.95 base; staleness inflation may add ~2% → ~1.99.
        # Tolerance covers both with margin against future small shifts.
        assert 1.93 < hrrr_out.sigma_f < 2.05

    def test_active_no_inflation(self, db):
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.ACTIVE)
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian",
                   return_value=_g("hrrr", 70.0, 1.5, hours=20.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.weather.get_weather_gaussian",
                   return_value=None):
            out = v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        hrrr_out = next(g for g in out if g.source_name == "hrrr")
        # Active state — no inflation. (σ may shift via MOS bias / staleness
        # which are already-tested behaviors. We expect σ near 1.5, not 1.95.)
        assert hrrr_out.sigma_f < 1.9


class TestCeilingHonorsLearnedSigma:
    def test_learned_sigma_above_ceiling_passes_through(self, db):
        # Pre-seed a learned σ of 3.0 for hrrr in kv_cache. Key format
        # is `weather_skill_<source>_<city>_<bucket>` per _get_learned_sigma;
        # bucket comes from _skill_bucket_for(24h) — see _SKILL_BUCKET_EDGES.
        # The bucket for 24h is "12_24" (we set hours=24 in _g, but the
        # half-open interval check needs a hours_out strictly inside an edge).
        from bot.signals.weather_ensemble_v2 import _skill_bucket_for
        bucket = _skill_bucket_for(20.0)  # known to land in a defined bucket
        kv_set(db, f"weather_skill_hrrr_nyc_{bucket}",
               {"sigma": 3.0, "n": 100}, 86400)
        # Pooled fallback in case city resolution differs
        kv_set(db, f"weather_skill_hrrr_{bucket}",
               {"sigma": 3.0, "n": 100}, 86400)
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.ACTIVE)
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian",
                   return_value=_g("hrrr", 70.0, 1.5, hours=20.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.weather.get_weather_gaussian",
                   return_value=None):
            out = v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        hrrr_out = next(g for g in out if g.source_name == "hrrr")
        # σ stayed at 3.0 (learned), did NOT clip to 2.0 (ceiling)
        assert hrrr_out.sigma_f >= 2.5, (
            f"Ceiling clipped a learned σ ({hrrr_out.sigma_f}). The "
            f"_apply_learned_sigma_with_flag → conditional-ceiling path is "
            f"broken; production NWS Point's true σ would still be lying."
        )

    def test_no_learned_sigma_ceiling_applies(self, db):
        # No kv_cache entry for hrrr_pooled or hrrr_<city>. Source
        # reports σ=4.0, ceiling should clip to 2.0.
        upsert_state(db, source="hrrr", city="pooled",
                     state=SourceState.ACTIVE)
        with patch("bot.signals.sources.hrrr.get_hrrr_gaussian",
                   return_value=_g("hrrr", 70.0, 4.0)), \
             patch("bot.signals.sources.metar_observations.get_metar_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.nws_point.get_nws_point_gaussian",
                   return_value=None), \
             patch("bot.signals.sources.weather.get_weather_gaussian",
                   return_value=None):
            out = v2._collect_gaussians("KXHIGHNY-26APR30-T75", _market_data())
        hrrr_out = next(g for g in out if g.source_name == "hrrr")
        # σ clipped to ceiling 2.0 (since no learned σ was present)
        assert hrrr_out.sigma_f == pytest.approx(2.0)
