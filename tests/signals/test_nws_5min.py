"""Unit tests for `bot.signals.sources.nws_5min`.

Mocks the requests layer so tests don't depend on live network. Live
verification is done separately via `tools/verify_nws_5min_freshness.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bot.api import _CACHE
from bot.signals.sources import nws_5min as src


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_cache():
    """Each test starts with a clean cache so prior fetch results don't
    leak across cases."""
    _CACHE.clear()
    yield
    _CACHE.clear()


def _mk_response(status: int, payload: dict | None):
    """Build a fake `requests.Response` for the mocker."""
    class _R:
        def __init__(self):
            self.status_code = status
            self._payload = payload
            self.text = "" if payload is None else "..."

        def json(self):
            return self._payload

    return _R()


def _feature(ts_iso: str, temp_c: float | None,
             raw_message: str = "") -> dict:
    """One GeoJSON feature like NWS api.weather.gov returns."""
    return {
        "properties": {
            "timestamp": ts_iso,
            "temperature": {"value": temp_c, "unitCode": "wmoUnit:degC"},
            "rawMessage": raw_message,
        },
    }


def _feature_collection(*features) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


# ── fetch_recent_observations ─────────────────────────────────────────


def test_fetch_parses_5min_observations():
    """Standard 5-min cadence response: one METAR + several non-METAR
    5-min readings. All inside the freshness window get parsed."""
    now = datetime.now(timezone.utc)
    # Build a response with timestamps at 5-min intervals back in time.
    feats = []
    for i in range(6):
        t = now - timedelta(minutes=5 * i + 1)
        feats.append(_feature(
            t.isoformat().replace("+00:00", "Z"),
            temp_c=20.0 + i * 0.5,
            raw_message="KMIA ..." if i == 1 else "",
        ))
    resp = _mk_response(200, _feature_collection(*feats))

    # max_age_s=2700 (45 min) covers the full 6-feature span.
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        out = src.fetch_recent_observations("KMIA", max_age_s=2700)

    assert out is not None
    assert len(out) == 6
    # First entry should be the freshest reading (1 minute ago).
    assert out[0]["temp_f"] == pytest.approx(68.0)
    # is_metar flag tracks whether rawMessage is non-empty.
    assert out[1]["is_metar"] is True
    assert out[0]["is_metar"] is False


def test_fetch_filters_stale_observations():
    """Observations older than max_age_s are dropped."""
    now = datetime.now(timezone.utc)
    feats = [
        _feature((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                  temp_c=20.0),
        _feature((now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                  temp_c=22.0),
    ]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        out = src.fetch_recent_observations("KMIA", max_age_s=900)
    assert out is not None
    assert len(out) == 1  # the stale one filtered
    assert out[0]["temp_f"] == pytest.approx(68.0)


def test_fetch_handles_http_error():
    """Non-200 → returns None and caches None so we don't hammer."""
    resp = _mk_response(500, None)
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        out = src.fetch_recent_observations("KMIA")
    assert out is None


def test_fetch_handles_network_exception():
    """Requests-layer exception (timeout, SSL, DNS) → returns None,
    fail-closed. Caller falls through to other sources."""
    with patch("bot.signals.sources.nws_5min.requests.get",
                side_effect=Exception("boom")):
        out = src.fetch_recent_observations("KMIA")
    assert out is None


def test_fetch_skips_features_with_null_temperature():
    """NWS occasionally returns `temperature: {value: null}` when the
    sensor is briefly unavailable. Those get skipped."""
    now = datetime.now(timezone.utc)
    feats = [
        _feature(now.isoformat().replace("+00:00", "Z"), temp_c=None),
        _feature((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                  temp_c=20.0),
    ]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        out = src.fetch_recent_observations("KMIA")
    assert out is not None
    assert len(out) == 1
    assert out[0]["temp_f"] == pytest.approx(68.0)


def test_fetch_rejects_pathological_temperatures():
    """Sensor errors sometimes return -9999 or 999 °C. Reject."""
    now = datetime.now(timezone.utc)
    feats = [
        _feature(now.isoformat().replace("+00:00", "Z"), temp_c=-9999.0),
        _feature((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                  temp_c=20.0),
    ]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        out = src.fetch_recent_observations("KMIA")
    assert out is not None
    assert len(out) == 1
    assert out[0]["temp_f"] == pytest.approx(68.0)


def test_fetch_caches_result():
    """Two back-to-back calls should hit the cache (only one network
    call)."""
    now = datetime.now(timezone.utc)
    feats = [_feature(now.isoformat().replace("+00:00", "Z"), temp_c=25.0)]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get",
                return_value=resp) as m:
        a = src.fetch_recent_observations("KMIA")
        b = src.fetch_recent_observations("KMIA")
    assert a == b
    assert m.call_count == 1


# ── get_recent_max_temp_f ─────────────────────────────────────────────


def test_max_temp_returns_peak_over_window():
    """When 5-min readings dip below the actual peak (integer-Celsius
    rounding), we want the MAX across the last hour, not the latest."""
    now = datetime.now(timezone.utc)
    feats = [
        _feature((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                  temp_c=29.0),
        _feature((now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                  temp_c=30.0),  # the peak
        _feature((now - timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                  temp_c=29.0),
    ]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        pair = src.get_recent_max_temp_f("KMIA")
    assert pair is not None
    temp_f, _ = pair
    assert temp_f == pytest.approx(86.0)  # 30°C → 86°F


def test_max_temp_returns_none_when_no_data():
    with patch("bot.signals.sources.nws_5min.requests.get",
                return_value=_mk_response(200, _feature_collection())):
        assert src.get_recent_max_temp_f("KMIA") is None


# ── get_nws_5min_gaussian ─────────────────────────────────────────────


def test_gaussian_returns_none_for_unknown_ticker():
    """Tickers without a station mapping (non-weather, parlay, etc.)
    return None — caller falls through."""
    assert src.get_nws_5min_gaussian("KXBTC-26APR30-T100K", {}) is None


def test_gaussian_built_for_miami_uses_kmia():
    """KXHIGHMIA city = miami → poll station KMIA."""
    now = datetime.now(timezone.utc)
    feats = [_feature(now.isoformat().replace("+00:00", "Z"), temp_c=29.0)]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        g = src.get_nws_5min_gaussian("KXHIGHMIA-26APR30-B83.5", {})
    assert g is not None
    assert g.source_name == "nws_5min"
    assert "KMIA" in g.source_tag
    assert g.mean_f == pytest.approx(84.2)  # 29°C → 84.2°F
    assert g.sigma_f > 0


def test_gaussian_skips_nyc_after_klga_proxy_drop():
    """2026-05-04 postmortem replacement: KXHIGHNY now returns None
    (was previously KLGA-substituted). The KLGA proxy ran 3-5°F warmer
    than KNYC simultaneously and biased the precision-weighted combine
    high; cost the canary $1.45 on B59.5 in one night. NYC stays out
    until a learned KLGA→KNYC bias correction lands."""
    now = datetime.now(timezone.utc)
    feats = [_feature(now.isoformat().replace("+00:00", "Z"), temp_c=12.0)]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        g = src.get_nws_5min_gaussian("KXHIGHNY-26APR30-B62.5", {})
    assert g is None


def test_gaussian_returns_none_when_observations_too_stale():
    """Even if NWS returns a feature, if the latest valid obs is older
    than the freshness gate, return None."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    feats = [_feature(old.isoformat().replace("+00:00", "Z"), temp_c=29.0)]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        g = src.get_nws_5min_gaussian("KXHIGHMIA-26APR30-B83.5", {})
    assert g is None


def test_gaussian_accepts_typical_publication_lag():
    """Regression: NWS publication latency is 5-25 minutes (verified
    live 2026-04-30). Setting `_MAX_OBS_AGE_S` too tight (e.g. 15 min)
    rejected every reading in the lagging-window case. The gate must
    accept readings that are 15-25 min old so we don't lose an entire
    afternoon's signal during NWS publication backlogs.
    """
    now = datetime.now(timezone.utc)
    twenty_min_ago = now - timedelta(minutes=20)
    feats = [_feature(
        twenty_min_ago.isoformat().replace("+00:00", "Z"), temp_c=29.0,
    )]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        g = src.get_nws_5min_gaussian("KXHIGHMIA-26APR30-B83.5", {})
    assert g is not None
    assert g.mean_f == pytest.approx(84.2)


# ── _sigma_for_hours_left schedule ────────────────────────────────────


def test_sigma_schedule_monotonic_in_hours_left():
    """σ should grow with hours_left — more time = more uncertainty."""
    s_now = src._sigma_for_hours_left(0.0)
    s_1h = src._sigma_for_hours_left(1.0)
    s_4h = src._sigma_for_hours_left(4.0)
    s_12h = src._sigma_for_hours_left(12.0)
    assert s_now < s_1h < s_4h < s_12h


def test_sigma_schedule_handles_negative_hours():
    """Past-settle requests (shouldn't happen, but defensive) → tiny σ."""
    assert src._sigma_for_hours_left(-1.0) == src._sigma_for_hours_left(0.0)


def test_morning_call_returns_none_lst_under_11(monkeypatch):
    """Regression for the 2026-05-01 nws_5min warm-bias finding: pre-LST-11
    the running max is the dawn temp, not a peak signal. Source must
    return None in that window so the ensemble's obs group falls back
    to METAR alone.
    """
    # Pin "now" to 13:00 UTC = 08:00 LST in NY (UTC-5) → before our
    # LST-11 gate.
    fixed_utc = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc if tz is None else fixed_utc.astimezone(tz)

    monkeypatch.setattr(src, "datetime", _FixedDT)

    # Even with a juicy 30°C reading available, gate should suppress.
    now = datetime.now(timezone.utc)
    feats = [_feature(now.isoformat().replace("+00:00", "Z"), temp_c=30.0)]
    resp = _mk_response(200, _feature_collection(*feats))
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        g = src.get_nws_5min_gaussian("KXHIGHNY-26MAY01-T75", {})
    assert g is None, "pre-LST-11 must return None"


def test_post_lst_11_call_uses_today_running_max(monkeypatch):
    """Post-LST-11 the gate opens. Source should return a Gaussian
    whose μ is today's max obs across the fetched window — not just
    the latest reading.
    """
    fixed_utc = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)  # 13 LST NY

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc if tz is None else fixed_utc.astimezone(tz)

    monkeypatch.setattr(src, "datetime", _FixedDT)

    # Three readings today: 25, 28 (peak), 26. We expect μ=82.4°F (28°C).
    obs_times = [
        fixed_utc - timedelta(minutes=30),
        fixed_utc - timedelta(minutes=15),
        fixed_utc - timedelta(minutes=2),
    ]
    feats = [
        _feature(obs_times[0].isoformat().replace("+00:00", "Z"), temp_c=25.0),
        _feature(obs_times[1].isoformat().replace("+00:00", "Z"), temp_c=28.0),
        _feature(obs_times[2].isoformat().replace("+00:00", "Z"), temp_c=26.0),
    ]
    resp = _mk_response(200, _feature_collection(*feats))
    # Use Miami so the city is in PRIMARY_5MIN_STATION_BY_CITY and the
    # source actually fires. (NYC was dropped 2026-05-04; using KXHIGHMIA
    # exercises the same running-max-of-today's-obs logic.)
    with patch("bot.signals.sources.nws_5min.requests.get", return_value=resp):
        g = src.get_nws_5min_gaussian("KXHIGHMIA-26MAY01-T80", {})
    assert g is not None
    # 28°C → 82.4°F (the peak, not the latest 26°C → 78.8°F)
    assert g.mean_f == pytest.approx(82.4, abs=0.1)


def test_sigma_schedule_caps_at_top_of_table():
    """Beyond 12h horizon, σ caps at the schedule's top value (2.0°F)
    rather than extrapolating linearly."""
    s = src._sigma_for_hours_left(48.0)
    assert s == pytest.approx(2.0)


# ── 2026-05-04: NYC dropped after KLGA-vs-KNYC postmortem ────────────


def test_nyc_skipped_no_klga_substitution():
    """nws_5min must return None for NYC tickers after the 2026-05-04
    fix. Postmortem: KLGA (the prior 5-min proxy for KNYC) ran 3-5°F
    warmer than KNYC simultaneously, biasing the precision-weighted
    combine systematically high. KXHIGHNY-26MAY03 canary lost $1.45 in
    one night to this. NYC stays out until a learned KLGA→KNYC bias
    correction is wired up."""
    # Nothing in the requests mock is needed — the code should
    # short-circuit BEFORE making any HTTP call.
    with patch("bot.signals.sources.nws_5min.requests.get",
                side_effect=AssertionError(
                    "must not fetch any 5-min data for NYC")):
        result = src.get_nws_5min_gaussian("KXHIGHNY-26MAY03-B59.5", {})
    assert result is None


def test_nyc_not_in_primary_5min_map():
    """Pin the dict membership so a future hand-edit doesn't silently
    re-add NYC without going through the postmortem-bias-correction
    workflow."""
    assert "nyc" not in src.PRIMARY_5MIN_STATION_BY_CITY
    # The other 5 cities still in
    for city in ("chicago", "los_angeles", "austin", "miami", "denver"):
        assert city in src.PRIMARY_5MIN_STATION_BY_CITY
