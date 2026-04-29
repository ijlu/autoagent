"""Tests for ``is_metar_fresh_for_ticker``.

Wraps the kv_cache lookup that powers the trade-decision METAR-required
gate. Important properties:
- Returns False (fail closed) on any error path. Trading should be
  refused when METAR can't be verified, not allowed.
- Returns True only when ``last_obs_time`` is within the freshness window.
- Default window is 30 min — pinned because shorter creates false
  negatives during normal IEM hiccups, longer admits stale data.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.db import init_db, kv_set
from bot.signals.sources.metar_observations import (
    DEFAULT_METAR_FRESHNESS_S,
    is_metar_fresh_for_ticker,
)


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """Stand up a per-test sqlite DB and point the module's persistent
    connection at it so ``get_connection()`` returns this DB."""
    db_path = tmp_path / "test.db"
    import bot.db as db_mod
    # Reset the singleton so init_db creates a fresh connection on this DB
    monkeypatch.setattr(db_mod, "_PERSIST_CONN", None, raising=False)
    conn = db_mod.init_db(str(db_path))
    yield conn
    # Teardown: clear the singleton so other tests don't see this DB
    monkeypatch.setattr(db_mod, "_PERSIST_CONN", None, raising=False)


def _set_metar_record(conn, station, last_obs_dt, today_lst_date):
    """Write a metar_daily_high record matching production schema."""
    record = {
        "high_f": 75.0,
        "last_obs_time": last_obs_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "obs_count": 5,
    }
    kv_key = f"metar_daily_high_{station}_{today_lst_date}"
    kv_set(conn, kv_key, record, 86400)
    return kv_key


def _today_lst_date(station: str) -> str:
    """Get today's lst_date for the station the way the helper does."""
    from bot.signals.sources.metar_observations import _get_lst_date
    return _get_lst_date(station)


class TestIsMetarFreshForTicker:
    def test_fresh_observation_passes(self, db_conn):
        # Recent METAR (1 min ago) → fresh
        station = "KNYC"
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        _set_metar_record(db_conn, station, recent, _today_lst_date(station))
        assert is_metar_fresh_for_ticker("KXHIGHNY-26APR30-B68.5") is True

    def test_stale_observation_fails(self, db_conn):
        # 2 hours old → way past freshness window
        station = "KNYC"
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        _set_metar_record(db_conn, station, stale, _today_lst_date(station))
        assert is_metar_fresh_for_ticker("KXHIGHNY-26APR30-B68.5") is False

    def test_missing_record_fails_closed(self, db_conn):
        # No kv entry at all → fail closed (refuse trade)
        assert is_metar_fresh_for_ticker("KXHIGHNY-26APR30-B68.5") is False

    def test_unknown_ticker_fails_closed(self, db_conn):
        # Ticker doesn't map to a known station → False
        assert is_metar_fresh_for_ticker("UNKNOWN-26APR30-B50") is False

    def test_at_window_edge_passes(self, db_conn):
        # Right at default freshness boundary: 30 min - 5s (safe side)
        station = "KMIA"
        edge = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_METAR_FRESHNESS_S - 5)
        _set_metar_record(db_conn, station, edge, _today_lst_date(station))
        assert is_metar_fresh_for_ticker("KXHIGHMIA-26APR30-T85") is True

    def test_just_past_window_fails(self, db_conn):
        # 30 min + 30s past boundary → fail
        station = "KMIA"
        past = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_METAR_FRESHNESS_S + 30)
        _set_metar_record(db_conn, station, past, _today_lst_date(station))
        assert is_metar_fresh_for_ticker("KXHIGHMIA-26APR30-T85") is False

    def test_custom_window(self, db_conn):
        # Caller can override default. With a 5-min window, anything
        # older than 5 min is stale.
        station = "KAUS"
        five_min_ten_sec_ago = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=10)
        _set_metar_record(db_conn, station, five_min_ten_sec_ago, _today_lst_date(station))
        assert is_metar_fresh_for_ticker(
            "KXHIGHAUS-26APR30-B85.5", max_age_seconds=300
        ) is False
        # Same record passes the wider default
        assert is_metar_fresh_for_ticker("KXHIGHAUS-26APR30-B85.5") is True

    def test_default_window_pinned_at_30min(self):
        # If the default loosens, stale data will sneak through. If it
        # tightens, normal IEM latency will cause false-negatives.
        assert DEFAULT_METAR_FRESHNESS_S == 30 * 60
