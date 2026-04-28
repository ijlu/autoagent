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
    member of CANONICAL_WEATHER_SOURCES."""
    assert CANONICAL_WEATHER_SOURCES == frozenset({
        "hrrr", "nbm", "nws_point", "weather",
        "metar", "madis", "afd",
    })
    assert GAUSSIAN_COMBINE_SOURCES == CANONICAL_WEATHER_SOURCES - {AFD}


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
    # Names appear as the first element of a ("name", get_fn) tuple in the
    # ``getters`` list literal — extract them by string match against the
    # registry rather than parsing the AST.
    for name in CANONICAL_WEATHER_SOURCES - {AFD}:
        # Each name MUST appear quoted somewhere in the function body.
        assert f'"{name}"' in src, (
            f"_collect_gaussians no longer references canonical source "
            f"{name!r}. If the source was deprecated, drop it from "
            f"GAUSSIAN_COMBINE_SOURCES; otherwise re-add it to the getters "
            f"list."
        )

    # And the only quoted source-shaped strings in _collect_gaussians MUST
    # be canonical names — defends against typo'd new entries.
    suspect_names = ("open_meteo", "openmeteo", "openMeteo", "tom_io",
                     "noaa", "nbm_ml", "hrrrx")
    for s in suspect_names:
        assert f'"{s}"' not in src, (
            f"_collect_gaussians references non-canonical source {s!r}. "
            f"Use one of {sorted(CANONICAL_WEATHER_SOURCES)}."
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
        hrrr, madis, metar_observations, ndfd_nbm, nws_point, weather,
    )

    expected = {
        hrrr: HRRR,
        ndfd_nbm: NBM,
        nws_point: NWS_POINT,
        madis: MADIS,
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
