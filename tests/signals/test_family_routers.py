"""Tests for bot/signals/family_routers.py.

The family router is a thin, deterministic prefix-match dispatch.
Core invariants under test:
  1. Longest prefix wins (KXHURR not caught by KXH* catch-all).
  2. Unknown prefixes → None (caller falls through to generic ensemble).
  3. Router raises → returns None, never bubbles up (keeps ensemble alive).
  4. All six registered prefixes dispatch to the expected backend.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bot.signals import family_routers


def _install_fakes(monkeypatch, weather_ret=(0.42, "wfake"), adp_ret=(0.5, "afake"),
                   gdp_ret=(0.6, "gfake"), cpi_ret=(0.3, "cfake")):
    """Stub each router backend with a tuple-returning fake."""
    monkeypatch.setattr(family_routers, "_weather", lambda t, m: weather_ret)
    monkeypatch.setattr(family_routers, "_kxjob", lambda t, m: adp_ret)
    monkeypatch.setattr(family_routers, "_kxgdp", lambda t, m: gdp_ret)
    monkeypatch.setattr(family_routers, "_kxcpi", lambda t, m: cpi_ret)
    # Rebuild registry so the patches are visible
    family_routers.FAMILY_ROUTERS[:] = [
        ("KXHIGH", family_routers._weather),
        ("KXHMONTHRANGE", family_routers._weather),
        ("KXHURR", family_routers._weather),
        ("KXJOB", family_routers._kxjob),
        ("KXGDP", family_routers._kxgdp),
        ("KXCPI", family_routers._kxcpi),
    ]


def test_kxhigh_dispatches_to_weather(monkeypatch):
    _install_fakes(monkeypatch)
    out = family_routers.route_family("KXHIGHNY-26APR16", {"title": "NYC high"})
    assert out == (0.42, "wfake")


def test_kxhurr_not_caught_by_kxh_prefix(monkeypatch):
    # If matching was simply .startswith without longest-prefix, KXHURR could
    # be swallowed by a KXH rule. Confirm it lands on weather (correct).
    _install_fakes(monkeypatch, weather_ret=(0.77, "hurricane"))
    out = family_routers.route_family("KXHURRCAT5-26", {})
    assert out == (0.77, "hurricane")


def test_kxhmonthrange_takes_precedence_over_kxh(monkeypatch):
    # Longest prefix wins: KXHMONTHRANGE beats a hypothetical KXH rule.
    _install_fakes(monkeypatch, weather_ret=(0.5, "mrange"))
    out = family_routers.route_family("KXHMONTHRANGENYC-26APR", {})
    assert out == (0.5, "mrange")


def test_kxjob_dispatches_to_adp(monkeypatch):
    _install_fakes(monkeypatch)
    out = family_routers.route_family("KXJOB-26MAY-T150", {})
    assert out == (0.5, "afake")


def test_kxgdp_dispatches_to_gdpnow(monkeypatch):
    _install_fakes(monkeypatch)
    out = family_routers.route_family("KXGDP-26Q2-T2.5", {})
    assert out == (0.6, "gfake")


def test_kxcpi_dispatches_to_commodity(monkeypatch):
    _install_fakes(monkeypatch)
    out = family_routers.route_family("KXCPI-26MAY-T3.0", {})
    assert out == (0.3, "cfake")


def test_unknown_ticker_returns_none(monkeypatch):
    _install_fakes(monkeypatch)
    assert family_routers.route_family("KXFED-26JUL-T5.0", {}) is None
    assert family_routers.route_family("KXETH-26APR", {}) is None


def test_empty_ticker_returns_none():
    assert family_routers.route_family("", {}) is None
    assert family_routers.route_family(None, {}) is None


def test_lowercase_ticker_still_matches(monkeypatch):
    _install_fakes(monkeypatch)
    out = family_routers.route_family("kxhighny", {})
    assert out == (0.42, "wfake")


def test_router_exception_returns_none(monkeypatch):
    def boom(t, m):
        raise RuntimeError("simulated source failure")
    monkeypatch.setattr(family_routers, "_weather", boom)
    family_routers.FAMILY_ROUTERS[:] = [("KXHIGH", boom)]
    out = family_routers.route_family("KXHIGHNY", {})
    assert out is None  # swallowed, never raises


def test_router_returning_none_propagates(monkeypatch):
    # A router that returns (None, None) is a valid "no signal" response.
    _install_fakes(monkeypatch, weather_ret=(None, None))
    out = family_routers.route_family("KXHIGHNY", {})
    assert out == (None, None)
