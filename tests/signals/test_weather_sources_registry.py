"""Drift-guard tests for ``bot.signals.weather_sources``.

Pins the relationship between the canonical name registry, the live
``_collect_gaussians`` getters, the backfill tool's source-key dict, and
the kv_cache key prefixes used by the A3/A5 reader and writer.

If any of these drift, the calibration system silently writes keys the
reader never looks up — exactly the bug we just discovered with
``"open_meteo"`` (backfill) vs ``"weather"`` (live).
"""

from __future__ import annotations

from bot.signals.weather_sources import (
    AFD,
    CANONICAL_WEATHER_SOURCES,
    GAUSSIAN_COMBINE_SOURCES,
    HRRR,
    MADIS,
    METAR,
    NBM,
    NWS_POINT,
    WEATHER,
    is_canonical,
)


def test_registry_contents_pinned():
    """Pin the membership so a quiet rename of one of these constants
    doesn't go unnoticed.

    Tomorrow.io was dropped 2026-04-26 (see weather_sources.py TOMORROW
    constant note); the bare constant is still exported but no longer a
    member of CANONICAL_WEATHER_SOURCES.

    NBM + MADIS were dropped from GAUSSIAN_COMBINE_SOURCES on 2026-04-29
    (see weather_sources.py for the rationale) but remain in
    CANONICAL_WEATHER_SOURCES so historical kv_cache rows still pass
    ``is_canonical``."""
    assert CANONICAL_WEATHER_SOURCES == frozenset({
        "hrrr", "nbm", "nws_point", "weather",
        "metar", "madis", "afd",
        "icon", "ukmo", "iem_1min",  # added 2026-04-29 (Phase B.2)
        "gem", "metno", "ecmwf", "nws_5min",  # added 2026-04-30
        "nws_5min_diurnal", "nws_5min_analog",  # added 2026-05-02
    })
    # GAUSSIAN_COMBINE_SOURCES — sources that fire in the live combine.
    # NBM and MADIS deprecated 2026-04-29 (NBM is duplicate of weather;
    # MADIS warming heuristic broken). ICON/UKMO added same day.
    # IEM_1MIN deliberately excluded: ~24h IEM publication latency means
    # it can never produce live data for today's market.
    # 2026-04-30: GEM, MetNo, ECMWF added after backtest validation
    # (12% / 4% / 3% pooled MAE improvement on the 30-day eval).
    # nws_5min wired in same day after live verification confirmed
    # the publication-lag handling (`issued_at` + staleness inflation)
    # behaves correctly. NWS api.weather.gov serves 5-min ASOS
    # observations free, no auth, no rate limit.
    # 2026-05-02: nws_5min_diurnal + nws_5min_analog added — both
    # forecasters built on top of the same 5-min observation feed.
    # 2026-05-05: `weather` removed from live combine — corr(hrrr, weather)
    # = 0.994 (NY) / 1.000 (LAX); both Open-Meteo (gfs_hrrr vs default
    # blend = GFS in US). See reports/POSTFIX_REASSESSMENT_2026-05-05.md.
    assert GAUSSIAN_COMBINE_SOURCES == frozenset({
        "hrrr", "nws_point", "metar",
        "icon", "ukmo",
        "gem", "metno", "ecmwf",
        "nws_5min", "nws_5min_diurnal",
    })
    assert "nws_5min_analog" in CANONICAL_WEATHER_SOURCES
    assert "nws_5min_analog" not in GAUSSIAN_COMBINE_SOURCES
    assert "iem_1min" not in GAUSSIAN_COMBINE_SOURCES
    assert "weather" in CANONICAL_WEATHER_SOURCES
    assert "weather" not in GAUSSIAN_COMBINE_SOURCES
    assert NBM not in GAUSSIAN_COMBINE_SOURCES
    assert MADIS not in GAUSSIAN_COMBINE_SOURCES


def test_is_canonical():
    for name in CANONICAL_WEATHER_SOURCES:
        assert is_canonical(name)
    assert not is_canonical("open_meteo")  # the bug we just fixed
    assert not is_canonical("OpenMeteo")
    assert not is_canonical("")


def test_live_collect_gaussians_uses_only_canonical_names():
    """``_collect_gaussians`` builds a ``getters`` list pairing names with
    fetcher functions. Every name MUST be in the canonical registry."""
    import inspect

    from bot.signals import weather_ensemble_v2 as v2

    src = inspect.getsource(v2._collect_gaussians)
    import re
    # Each combine-active name appears either as the first element of a
    # ("name", get_fn) tuple OR is referenced inside a sentinel-routed
    # multi-getter channel (e.g., ``__observation_channel__`` dispatches
    # to ``get_iem_1min_gaussian`` / ``get_metar_gaussian``). For sentinel
    # routes we check that the underlying getter is referenced.
    for name in GAUSSIAN_COMBINE_SOURCES:
        assert re.search(rf'\(\s*"{re.escape(name)}"\s*,', src), (
            f"_collect_gaussians no longer references canonical source "
            f"{name!r}. If the source was deprecated, drop it from "
            f"GAUSSIAN_COMBINE_SOURCES; otherwise re-add it to the getters "
            f"list."
        )

    # The only ("name", ...) tuples in the getters list MUST be in
    # GAUSSIAN_COMBINE_SOURCES — defends against silently re-adding NBM /
    # MADIS / a typo'd new entry. Exception: sentinel keys starting with
    # ``__`` are intentional (multi-getter channels).
    deprecated_or_typo = {NBM, MADIS, "open_meteo", "openmeteo", "openMeteo",
                          "tom_io", "noaa", "nbm_ml", "hrrrx"}
    for s in deprecated_or_typo:
        assert not re.search(rf'\(\s*"{re.escape(s)}"\s*,', src), (
            f"_collect_gaussians references deprecated/non-combine source "
            f"{s!r}. Use one of {sorted(GAUSSIAN_COMBINE_SOURCES)} or update "
            f"the registry."
        )


def test_backfill_tool_source_keys_are_canonical():
    """The backfill tool's ``_OM_MODELS`` and ``sigma_fn`` dicts in
    ``_sources_and_rows`` set the source string written to the
    ``weather_gaussian_snapshots_backfill.source`` column. They MUST emit
    only canonical names."""
    from tools import backfill_weather_effective_n as bf

    # _OM_MODELS values are (source_key, sigma_fn) — first element is the
    # source string. Every one MUST be canonical.
    for _model_param, (source_key, _) in bf._OM_MODELS.items():
        assert source_key in CANONICAL_WEATHER_SOURCES, (
            f"_OM_MODELS emits non-canonical source key {source_key!r}"
        )

    # _MODEL_GROUP_SOURCES drives correlation-fit grouping. Same constraint.
    for source_key in bf._MODEL_GROUP_SOURCES:
        assert source_key in CANONICAL_WEATHER_SOURCES, (
            f"_MODEL_GROUP_SOURCES contains non-canonical {source_key!r}"
        )


def test_live_source_modules_emit_canonical_source_name():
    """Each ``get_<src>_gaussian`` function returning a non-None
    ``GaussianForecast`` MUST set ``source_name`` to the canonical name.
    Tested via static inspection — no network calls.

    This catches the case where a source's ``GaussianForecast(...)``
    constructor call hard-codes a name that drifted from the registry.
    """
    import inspect

    from bot.signals.sources import (
        hrrr, icon, iem_1min_asos, madis, metar_observations,
        ndfd_nbm, nws_point, ukmo, weather,
    )

    expected = {
        hrrr: HRRR,
        ndfd_nbm: NBM,
        nws_point: NWS_POINT,
        madis: MADIS,
        icon: "icon",
        ukmo: "ukmo",
        iem_1min_asos: "iem_1min",
        metar_observations: METAR,
        # weather.py emits 'weather' (Open-Meteo). It still has a
        # get_tomorrow_forecast helper kept for code-archeology, but
        # tomorrow is no longer a canonical live source post-2026-04-26.
    }
    for module, name in expected.items():
        src = inspect.getsource(module)
        assert f'source_name="{name}"' in src, (
            f"{module.__name__} does not emit canonical source_name="
            f"{name!r}. If it now writes a different name, update the "
            f"registry; if it has been retired, remove from the live "
            f"getters in _collect_gaussians."
        )

    weather_src = inspect.getsource(weather)
    assert f'source_name="{WEATHER}"' in weather_src


def test_kv_cache_prefixes_have_no_legacy_drift():
    """The kv prefixes used by A5 bias and A3 sigma fits are pinned against
    the v2 reader. If a prefix here changes, every persisted row goes dark.
    """
    from bot.signals import weather_ensemble_v2 as v2
    from tools import backfill_weather_effective_n as bf

    assert v2._MOS_BIAS_KEY_PREFIX == bf._MOS_BIAS_KEY_PREFIX
    assert v2._SKILL_KEY_PREFIX == bf._SKILL_KEY_PREFIX


# ── Per-city source exclusions (2026-05-04 postmortem regression) ────


def test_per_city_exclusions_known_pinned():
    """Pin EXCLUDED_SOURCES_BY_CITY entries against the regression that
    drove them. Removing or adding entries should be intentional + backed
    by fresh evidence (regression bias > 3°F, n >= 30, multi-day
    persistence). See weather_sources.py docstring for the methodology.
    """
    from bot.signals.weather_sources import EXCLUDED_SOURCES_BY_CITY

    # NYC: nws_point cold-bias -5.86°F (regression 2026-05-04)
    assert "nws_point" in EXCLUDED_SOURCES_BY_CITY["nyc"]

    # Chicago: nws_point -5.79°F, nws_5min -7.35°F
    assert "nws_point" in EXCLUDED_SOURCES_BY_CITY["chicago"]
    assert "nws_5min" in EXCLUDED_SOURCES_BY_CITY["chicago"]

    # Miami: nws_point -3.07°F, nws_5min -4.21°F
    assert "nws_point" in EXCLUDED_SOURCES_BY_CITY["miami"]
    assert "nws_5min" in EXCLUDED_SOURCES_BY_CITY["miami"]

    # LAX: metno +9.67°F, gem +8.86°F (worst marine-layer outliers)
    assert "metno" in EXCLUDED_SOURCES_BY_CITY["los_angeles"]
    assert "gem" in EXCLUDED_SOURCES_BY_CITY["los_angeles"]

    # KAUS, KDEN: no exclusions (all biases within ±3°F per regression)
    assert "austin" not in EXCLUDED_SOURCES_BY_CITY or \
        len(EXCLUDED_SOURCES_BY_CITY.get("austin", set())) == 0
    assert "denver" not in EXCLUDED_SOURCES_BY_CITY or \
        len(EXCLUDED_SOURCES_BY_CITY.get("denver", set())) == 0


def test_is_excluded_for_city():
    """The lookup helper must (a) return True when both city + source
    match, (b) return False on missing city, missing source, or None."""
    from bot.signals.weather_sources import is_excluded_for_city

    # Positive cases — actually excluded
    assert is_excluded_for_city("nws_point", "nyc") is True
    assert is_excluded_for_city("nws_point", "chicago") is True
    assert is_excluded_for_city("metno", "los_angeles") is True

    # Negative — source allowed for that city
    assert is_excluded_for_city("hrrr", "nyc") is False
    assert is_excluded_for_city("metar", "los_angeles") is False

    # Negative — city has no entry
    assert is_excluded_for_city("nws_point", "austin") is False
    assert is_excluded_for_city("nws_point", "denver") is False

    # Defensive — None inputs
    assert is_excluded_for_city("nws_point", None) is False
    assert is_excluded_for_city(None, "nyc") is False
    assert is_excluded_for_city(None, None) is False

    # Defensive — unknown city defaults to "not excluded"
    assert is_excluded_for_city("nws_point", "atlantis") is False
