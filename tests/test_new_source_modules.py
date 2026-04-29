"""Tests for the 3 new weather source modules: IEM 1-min, ICON, UKMO.

Pin the contract:
- Each source returns a GaussianForecast with the canonical source_name
- Each respects the past-date guard from _determine_day_index
- ICON / UKMO pass timezone= to Open-Meteo (avoids the UTC-vs-LST bug)
- IEM 1-min only fires for day_idx == 0 (observation, not forecast)
- IEM 1-min returns None when fewer than _IEM_MIN_OBS_PER_DAY samples
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest

from bot.signals.sources.icon import (
    get_icon_gaussian, _icon_sigma_for_day, _ICON_MODEL,
)
from bot.signals.sources.ukmo import (
    get_ukmo_gaussian, _ukmo_sigma_for_day, _UKMO_MODEL,
)
from bot.signals.sources.iem_1min_asos import (
    get_iem_1min_gaussian, _icao_to_iem_id, _IEM_MIN_OBS_PER_DAY,
)


def _market_today_open(threshold=75):
    """Construct a weather market with close_time later today (LST) so
    _determine_day_index returns 0."""
    # Close at LST 18:00 today — definitely later today regardless of when
    # the test runs.
    lst_tz = timezone(timedelta(hours=-5))
    today_lst = datetime.now(lst_tz).date()
    close_dt = datetime(
        today_lst.year, today_lst.month, today_lst.day, 18, 0, 0,
        tzinfo=lst_tz,
    ).astimezone(timezone.utc)
    return {
        "ticker": "KXHIGHNY-26APR30-T75",
        "title": "Will the high temperature in NYC be above 75?",
        "subtitle": "high temp",
        "yes_sub_title": f"{threshold} or above",
        "close_time": close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── ICON ──────────────────────────────────────────────────────────────────
class TestICON:
    def test_returns_none_for_non_weather(self):
        market = {"ticker": "KXBTC-26APR30-B50000",
                  "title": "BTC price",
                  "yes_sub_title": "above 50000"}
        assert get_icon_gaussian("KXBTC-26APR30-B50000", market) is None

    def test_returns_none_for_unknown_city(self):
        market = {"ticker": "KXHIGHZZZ-26APR30-T75",
                  "title": "weather in zzz", "yes_sub_title": "75 or above"}
        assert get_icon_gaussian("KXHIGHZZZ-26APR30-T75", market) is None

    def test_url_contains_timezone_and_model(self):
        # Capture the URL passed to requests.get
        captured = {}
        def fake_get(url, *args, **kwargs):
            captured["url"] = url
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {
                "daily": {"temperature_2m_max": [72.0, 70.0],
                          "time": ["2026-04-30", "2026-05-01"]}
            }
            return mock

        with patch("bot.signals.sources.icon.requests.get", side_effect=fake_get), \
             patch("bot.signals.sources.icon._CACHE", {}):
            result = get_icon_gaussian("KXHIGHNY-26APR30-T75", _market_today_open())

        assert "url" in captured, "ICON didn't call the API"
        assert f"models={_ICON_MODEL}" in captured["url"]
        assert "timezone=" in captured["url"], (
            "ICON URL missing timezone= → daily_max computed in UTC, off LST window. "
            "This is the bug we explicitly fixed in the eval framework."
        )

    def test_returns_gaussian_with_canonical_source_name(self):
        with patch("bot.signals.sources.icon.requests.get") as mock_get, \
             patch("bot.signals.sources.icon._CACHE", {}):
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "daily": {"temperature_2m_max": [73.5, 70.0],
                          "time": ["2026-04-30", "2026-05-01"]}
            }
            g = get_icon_gaussian("KXHIGHNY-26APR30-T75", _market_today_open())

        assert g is not None
        assert g.source_name == "icon"
        assert g.mean_f == 73.5
        # σ prior at day 0 = 2.5
        assert g.sigma_f == _icon_sigma_for_day(0)


# ── UKMO ──────────────────────────────────────────────────────────────────
class TestUKMO:
    def test_url_uses_ukmo_model(self):
        captured = {}
        def fake_get(url, *args, **kwargs):
            captured["url"] = url
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {
                "daily": {"temperature_2m_max": [71.0, 69.0],
                          "time": ["2026-04-30", "2026-05-01"]}
            }
            return mock
        with patch("bot.signals.sources.ukmo.requests.get", side_effect=fake_get), \
             patch("bot.signals.sources.ukmo._CACHE", {}):
            get_ukmo_gaussian("KXHIGHNY-26APR30-T75", _market_today_open())
        assert f"models={_UKMO_MODEL}" in captured["url"]
        assert "timezone=" in captured["url"]

    def test_source_name_is_ukmo(self):
        with patch("bot.signals.sources.ukmo.requests.get") as mock_get, \
             patch("bot.signals.sources.ukmo._CACHE", {}):
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "daily": {"temperature_2m_max": [71.0, 69.0],
                          "time": ["2026-04-30", "2026-05-01"]}
            }
            g = get_ukmo_gaussian("KXHIGHNY-26APR30-T75", _market_today_open())
        assert g.source_name == "ukmo"
        assert g.sigma_f == _ukmo_sigma_for_day(0)


# ── IEM 1-min ASOS ────────────────────────────────────────────────────────
class TestIEM1min:
    def test_icao_to_iem_id_strips_leading_K(self):
        assert _icao_to_iem_id("KNYC") == "NYC"
        assert _icao_to_iem_id("KMDW") == "MDW"
        # Non-K-prefix passes through (e.g., international stations)
        assert _icao_to_iem_id("EGLL") == "EGLL"

    def test_only_fires_for_day_idx_0(self):
        # Tomorrow — day_idx > 0
        lst_tz = timezone(timedelta(hours=-5))
        tomorrow = datetime.now(lst_tz).date() + timedelta(days=1)
        close_dt = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, 18, 0, 0,
            tzinfo=lst_tz,
        ).astimezone(timezone.utc)
        market = {
            "ticker": "KXHIGHNY-26APR30-T75",
            "title": "high temp",
            "yes_sub_title": "75 or above",
            "close_time": close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # Should return None — we're an observation source, not forecast
        assert get_iem_1min_gaussian("KXHIGHNY-26APR30-T75", market) is None

    def test_returns_none_when_too_few_observations(self):
        # 30 rows — under the _IEM_MIN_OBS_PER_DAY=60 threshold
        body = "station,station_name,valid(UTC),tmpf\n"
        # generate observations within today's LST
        lst_tz = timezone(timedelta(hours=-5))
        today_lst = datetime.now(lst_tz).date()
        for i in range(30):
            utc_t = (datetime(today_lst.year, today_lst.month, today_lst.day,
                              12, i, tzinfo=lst_tz)
                     .astimezone(timezone.utc))
            body += f"NYC,NEW YORK CITY,{utc_t.strftime('%Y-%m-%d %H:%M')},65\n"

        with patch("bot.signals.sources.iem_1min_asos.requests.get") as mock_get, \
             patch("bot.signals.sources.iem_1min_asos._CACHE", {}):
            mock_get.return_value.status_code = 200
            mock_get.return_value.text = body
            g = get_iem_1min_gaussian("KXHIGHNY-26APR30-T75", _market_today_open())

        assert g is None  # Below 60-obs threshold

    def test_returns_max_with_canonical_source_name(self):
        body = "station,station_name,valid(UTC),tmpf\n"
        lst_tz = timezone(timedelta(hours=-5))
        today_lst = datetime.now(lst_tz).date()
        # 70 observations across 2 hours, max 78°F at #30
        for i in range(70):
            temp = 78 if i == 30 else 65 + (i % 5)
            hour = 12 + (i // 60)
            minute = i % 60
            utc_t = (datetime(today_lst.year, today_lst.month, today_lst.day,
                              hour, minute, tzinfo=lst_tz)
                     .astimezone(timezone.utc))
            body += f"NYC,NEW YORK CITY,{utc_t.strftime('%Y-%m-%d %H:%M')},{temp}\n"

        with patch("bot.signals.sources.iem_1min_asos.requests.get") as mock_get, \
             patch("bot.signals.sources.iem_1min_asos._CACHE", {}):
            mock_get.return_value.status_code = 200
            mock_get.return_value.text = body
            g = get_iem_1min_gaussian("KXHIGHNY-26APR30-T75", _market_today_open())

        assert g is not None
        assert g.source_name == "iem_1min"
        assert g.mean_f == 78.0  # the max from our synthetic data
        assert g.sigma_f == 1.5  # σ_prior

    def test_handles_M_missing_marker(self):
        # IEM uses 'M' for missing observations
        body = "station,station_name,valid(UTC),tmpf\n"
        lst_tz = timezone(timedelta(hours=-5))
        today_lst = datetime.now(lst_tz).date()
        for i in range(70):
            tmpf = "M" if i % 5 == 0 else "65"  # 1 in 5 missing
            hour = 12 + (i // 60)
            minute = i % 60
            utc_t = (datetime(today_lst.year, today_lst.month, today_lst.day,
                              hour, minute, tzinfo=lst_tz)
                     .astimezone(timezone.utc))
            body += f"NYC,NEW YORK CITY,{utc_t.strftime('%Y-%m-%d %H:%M')},{tmpf}\n"
        with patch("bot.signals.sources.iem_1min_asos.requests.get") as mock_get, \
             patch("bot.signals.sources.iem_1min_asos._CACHE", {}):
            mock_get.return_value.status_code = 200
            mock_get.return_value.text = body
            g = get_iem_1min_gaussian("KXHIGHNY-26APR30-T75", _market_today_open())
        # 56 valid obs (70 - 14 missing) — under 60 threshold → None
        # If exactly at threshold we'd need different counts; this is intentional
        assert g is None
