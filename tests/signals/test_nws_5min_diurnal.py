"""Tests for `bot.signals.sources.nws_5min_diurnal`.

Pin the contract:
  - LST gate inherits from nws_5min (returns None pre-LST-11)
  - When fit + fresh obs both present, returns Gaussian centered on
    α + β · T_5min with σ = RMSE
  - When no fit persisted, returns None (cold-start path)
  - When obs is stale (>15min), returns None
  - Sanity-caps prediction to ±30°F window around current temp
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bot.api import _CACHE
from bot.signals.sources import nws_5min as base
from bot.signals.sources import nws_5min_diurnal as src


@pytest.fixture(autouse=True)
def _clear_cache():
    _CACHE.clear()
    yield
    _CACHE.clear()


def _mk_response(payload):
    class _R:
        def __init__(self):
            self.status_code = 200
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    return _R()


def _feature_collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _feature(ts_iso, temp_c):
    return {
        "properties": {
            "timestamp": ts_iso,
            "temperature": {"value": temp_c, "unitCode": "wmoUnit:degC"},
            "rawMessage": "",
        },
    }


def _fresh_obs_response(temp_c=20.0, obs_time=None):
    obs_time = obs_time or datetime.now(timezone.utc) - timedelta(minutes=2)
    return _mk_response(_feature_collection(
        _feature(obs_time.isoformat().replace("+00:00", "Z"), temp_c),
    ))


def _force_lst_hour(monkeypatch, hour: int):
    """Pin "now" so the LST hour comes out as the requested hour
    regardless of when the test actually runs."""
    # KMIA uses UTC-5 (EST). LST hour 14 = UTC 19. Build a fixed UTC
    # datetime that produces the desired LST hour.
    utc_hour = (hour + 5) % 24
    fixed_utc = datetime(2026, 5, 2, utc_hour, 0, tzinfo=timezone.utc)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc if tz is None else fixed_utc.astimezone(tz)

    monkeypatch.setattr(src, "datetime", _FixedDT)


def test_returns_none_pre_lst_11(monkeypatch):
    """LST hour 8 (early morning) → gate fires, source returns None
    even when a fresh obs is available."""
    _force_lst_hour(monkeypatch, 8)
    monkeypatch.setattr(
        src, "_get_diurnal_fit",
        lambda station, hour: (20.0, 1.1, 3.0),
    )
    with patch("bot.signals.sources.nws_5min.requests.get",
               return_value=_fresh_obs_response()):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHMIA-26MAY02-T87", {},
        )
    assert g is None


def test_returns_none_when_no_diurnal_fit(monkeypatch):
    """Cold-start path: no fit persisted for (station, hour) →
    return None instead of guessing. Caller falls through to other
    obs sources."""
    _force_lst_hour(monkeypatch, 14)
    monkeypatch.setattr(src, "_get_diurnal_fit", lambda s, h: None)
    with patch("bot.signals.sources.nws_5min.requests.get",
               return_value=_fresh_obs_response()):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHMIA-26MAY02-T87", {},
        )
    assert g is None


def test_returns_none_when_obs_too_stale(monkeypatch):
    """Obs older than _MAX_OBS_AGE_S (30min) → return None. Don't
    feed a stale current_temp into the diurnal fit."""
    _force_lst_hour(monkeypatch, 14)
    monkeypatch.setattr(src, "_get_diurnal_fit",
                        lambda s, h: (20.0, 1.1, 3.0))
    # Create an obs 1 hour old
    stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
    with patch("bot.signals.sources.nws_5min.requests.get",
               return_value=_fresh_obs_response(
                   temp_c=25.0, obs_time=stale_time)):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHMIA-26MAY02-T87", {},
        )
    assert g is None


def test_returns_gaussian_with_diurnal_fit(monkeypatch):
    """Happy path: LST 14, fit (α=20, β=1.1, RMSE=3.0), 5-min reading
    25°C = 77°F → predicted = 20 + 1.1·77 = 104.7°F (clamped to
    77+30=107°F by sanity cap, so 104.7 is within range).
    """
    _force_lst_hour(monkeypatch, 14)
    monkeypatch.setattr(src, "_get_diurnal_fit",
                        lambda s, h: (20.0, 1.1, 3.0))
    with patch("bot.signals.sources.nws_5min.requests.get",
               return_value=_fresh_obs_response(temp_c=25.0)):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHMIA-26MAY02-T87", {},
        )
    assert g is not None
    assert g.source_name == "nws_5min_diurnal"
    # 25°C = 77°F. predicted = 20 + 1.1*77 = 104.7°F
    assert g.mean_f == pytest.approx(104.7, abs=0.1)
    assert g.sigma_f == pytest.approx(3.0)
    assert "lst14" in g.source_tag
    assert "KMIA" in g.source_tag


def test_sanity_cap_above(monkeypatch):
    """Pathological fit (α=200, β=0.1) on cold input would predict
    200°F. Sanity cap clamps to current_temp + 30°F max.
    """
    _force_lst_hour(monkeypatch, 14)
    monkeypatch.setattr(src, "_get_diurnal_fit",
                        lambda s, h: (200.0, 0.1, 3.0))
    with patch("bot.signals.sources.nws_5min.requests.get",
               return_value=_fresh_obs_response(temp_c=10.0)):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHMIA-26MAY02-T87", {},
        )
    assert g is not None
    # current = 50°F, max prediction = 50 + 30 = 80°F
    assert g.mean_f <= 80.0


def test_sanity_cap_below(monkeypatch):
    """Pathological fit predicting BELOW current temp gets clamped to
    current_temp - 5°F. Daily high can't be below the current obs
    minus a small slack for sensor noise.
    """
    _force_lst_hour(monkeypatch, 14)
    monkeypatch.setattr(src, "_get_diurnal_fit",
                        lambda s, h: (-100.0, 0.1, 3.0))
    with patch("bot.signals.sources.nws_5min.requests.get",
               return_value=_fresh_obs_response(temp_c=25.0)):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHMIA-26MAY02-T87", {},
        )
    assert g is not None
    assert g.mean_f >= 25.0 * 9.0 / 5.0 + 32.0 - 5.0  # >= 72°F


def test_unknown_ticker_returns_none(monkeypatch):
    """Non-weather ticker (no station mapping) → None."""
    _force_lst_hour(monkeypatch, 14)
    g = src.get_nws_5min_diurnal_gaussian("KXBTC-26MAY02-T100K", {})
    assert g is None


def test_uses_ground_truth_station_for_fit_lookup(monkeypatch):
    """For non-NYC cities the diurnal fit lookup uses the settlement
    station's ICAO. This test is on KMIA where poll_station == ws.icao
    so there's no proxy substitution to worry about.

    (NYC was originally tested here against a KLGA-substitution path.
    That path was removed 2026-05-04 — see test_nyc_returns_none for the
    new behavior + postmortem.)
    """
    _force_lst_hour(monkeypatch, 14)
    fit_lookups = []
    def _capturing_fit(station, hour):
        fit_lookups.append(station)
        return (20.0, 1.1, 3.0)

    monkeypatch.setattr(src, "_get_diurnal_fit", _capturing_fit)
    with patch("bot.signals.sources.nws_5min.requests.get",
               return_value=_fresh_obs_response(temp_c=27.0)):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHMIA-26MAY02-T80", {},
        )
    assert g is not None
    assert fit_lookups == ["KMIA"], (
        f"expected fit looked up under KMIA, got {fit_lookups}"
    )
    assert "KMIA" in g.source_tag


def test_nyc_returns_none_after_klga_proxy_drop(monkeypatch):
    """2026-05-04 postmortem: nws_5min_diurnal must return None for NYC
    tickers because KNYC has no 5-min observations and the KLGA proxy
    ran 3-5°F warmer than KNYC at peak hours, biasing the combine high
    and contributing to the KXHIGHNY canary loss.

    Both nws_5min and nws_5min_diurnal share PRIMARY_5MIN_STATION_BY_CITY,
    so dropping NYC from that map kills both sources for NYC tickers.
    """
    _force_lst_hour(monkeypatch, 14)
    # If the source ever DOES try to fetch, the test fails loudly —
    # the short-circuit must happen BEFORE any HTTP work.
    with patch("bot.signals.sources.nws_5min.requests.get",
                side_effect=AssertionError(
                    "diurnal source must skip NYC entirely")):
        g = src.get_nws_5min_diurnal_gaussian(
            "KXHIGHNY-26MAY02-T70", {},
        )
    assert g is None
