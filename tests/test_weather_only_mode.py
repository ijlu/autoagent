"""Pin: WEATHER_ONLY_MODE narrows TRADE_SERIES_ALLOWLIST to weather and
forces SC off. Defense against accidental scope creep when re-enabling
non-weather strategies."""
from __future__ import annotations

import importlib

import pytest


WEATHER_SERIES = {
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX",
    "KXHIGHAUS", "KXHIGHMIA", "KXHIGHDEN",
}


@pytest.fixture
def reload_cfg(monkeypatch):
    """Reload bot.config under monkey-patched env, return a snapshot of the
    relevant values. Snapshot is needed because the autouse cleanup that
    restores env after the test will reload the module again — we capture
    values here while the test env is still in effect."""
    def _do(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        import bot.config as cfg
        importlib.reload(cfg)
        return type("Snapshot", (), {
            "TRADE_SERIES_ALLOWLIST": cfg.TRADE_SERIES_ALLOWLIST,
            "SC_ENABLED": cfg.SC_ENABLED,
            "WEATHER_ONLY_MODE": cfg.WEATHER_ONLY_MODE,
        })
    yield _do
    # Restore module to whatever the ambient env says.
    import bot.config as cfg
    importlib.reload(cfg)


def test_weather_only_mode_off_default_includes_macro_and_crypto(reload_cfg):
    snap = reload_cfg(WEATHER_ONLY_MODE="false")
    series = set(snap.TRADE_SERIES_ALLOWLIST)
    assert WEATHER_SERIES.issubset(series)
    assert "KXFED" in series
    assert "KXBTC" in series
    assert "KXETH" in series


def test_weather_only_mode_on_drops_non_weather(reload_cfg):
    snap = reload_cfg(WEATHER_ONLY_MODE="true")
    series = set(snap.TRADE_SERIES_ALLOWLIST)
    assert series == WEATHER_SERIES, (
        f"WEATHER_ONLY_MODE must restrict allowlist to weather only; got {series}"
    )


def test_weather_only_mode_forces_sc_off(reload_cfg):
    snap = reload_cfg(WEATHER_ONLY_MODE="true", SC_ENABLED="true")
    assert snap.SC_ENABLED is False


def test_sc_respects_env_when_weather_only_off(reload_cfg):
    snap = reload_cfg(WEATHER_ONLY_MODE="false", SC_ENABLED="true")
    assert snap.SC_ENABLED is True
    snap = reload_cfg(WEATHER_ONLY_MODE="false", SC_ENABLED="false")
    assert snap.SC_ENABLED is False


def test_weather_only_mode_keeps_kxhighden(reload_cfg):
    """KXHIGHDEN is in DIRECTIONAL_BLOCKED_FAMILIES but should still be in
    the allowlist — the blocklist only suppresses directional ENTRY, not
    market scanning. Weather-only mode keeps DEN visible so cross-bracket
    + future MM can still see it."""
    snap = reload_cfg(WEATHER_ONLY_MODE="true")
    assert "KXHIGHDEN" in snap.TRADE_SERIES_ALLOWLIST
