"""Tests for METARPoller.seed_running_high."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from bot.daemon.metar_poller import METARPoller
from bot.daemon.stations import STATIONS


# ─── minimal in-memory DB with kv_cache ────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE kv_cache "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.commit()
    return conn


def _write_kv(conn, key: str, value, ttl: int = 90_000) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
        (key, json.dumps(value), time.time() + ttl),
    )
    conn.commit()


# ─── helpers ───────────────────────────────────────────────────────────────

def _pick_station() -> str:
    """Return any known primary station ICAO."""
    return next(iter(STATIONS))


def _today_lst(station: str) -> str:
    poller = METARPoller()
    return poller._get_lst_date(station)


# ─── tests ─────────────────────────────────────────────────────────────────

def test_seed_running_high_happy_path():
    """seed_running_high raises running_high_f from -999 to kv value."""
    station = _pick_station()
    conn = _make_conn()
    today = _today_lst(station)

    _write_kv(conn, f"metar_daily_high_{station}_{today}",
              {"high_f": 78.3, "last_obs_time": "2026-04-23T14:00Z", "obs_count": 120})

    poller = METARPoller()
    seeded = poller.seed_running_high(conn)

    assert station in seeded
    assert seeded[station] == pytest.approx(78.3)
    state = poller.get_state(station)
    assert state.running_high_f == pytest.approx(78.3)
    assert state.running_high_date == today


def test_seed_running_high_no_entry_leaves_state_unchanged():
    """When kv has no entry for today, running_high stays at -999."""
    station = _pick_station()
    conn = _make_conn()

    poller = METARPoller()
    seeded = poller.seed_running_high(conn)

    assert station not in seeded
    state = poller.get_state(station)
    assert state.running_high_f == -999.0


def test_seed_running_high_does_not_overwrite_higher_in_memory_value():
    """If running_high_f is already higher than the kv value, don't lower it."""
    station = _pick_station()
    conn = _make_conn()
    today = _today_lst(station)

    _write_kv(conn, f"metar_daily_high_{station}_{today}",
              {"high_f": 65.0, "obs_count": 30})

    poller = METARPoller()
    # Manually set in-memory to a higher value (simulates poller already warmed up)
    with poller._lock:
        poller._states[station].running_high_f = 82.0
        poller._states[station].running_high_date = today

    seeded = poller.seed_running_high(conn)

    assert station not in seeded
    state = poller.get_state(station)
    assert state.running_high_f == pytest.approx(82.0)


def test_seed_running_high_rejects_implausible_temperature():
    """Temperatures outside [-40, 130] are silently skipped."""
    station = _pick_station()
    conn = _make_conn()
    today = _today_lst(station)

    _write_kv(conn, f"metar_daily_high_{station}_{today}",
              {"high_f": 999.0, "obs_count": 1})

    poller = METARPoller()
    seeded = poller.seed_running_high(conn)

    assert station not in seeded
    state = poller.get_state(station)
    assert state.running_high_f == -999.0


def test_seed_running_high_rejects_expired_kv_entry():
    """Expired kv entries are not used (kv_get returns None for expired rows)."""
    station = _pick_station()
    conn = _make_conn()
    today = _today_lst(station)

    # Write with TTL=-1 so it's already expired
    conn.execute(
        "INSERT INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
        (f"metar_daily_high_{station}_{today}",
         json.dumps({"high_f": 85.0, "obs_count": 50}),
         time.time() - 1),
    )
    conn.commit()

    poller = METARPoller()
    seeded = poller.seed_running_high(conn)

    assert station not in seeded
    state = poller.get_state(station)
    assert state.running_high_f == -999.0


def test_seed_running_high_returns_all_seeded_stations():
    """All stations with valid kv entries today are seeded and returned."""
    conn = _make_conn()
    poller = METARPoller()

    # Seed kv for every known primary station
    expected: dict[str, float] = {}
    for station_id in STATIONS:
        today = poller._get_lst_date(station_id)
        high = 70.0 + len(station_id)  # arbitrary distinct value
        _write_kv(conn, f"metar_daily_high_{station_id}_{today}",
                  {"high_f": high, "obs_count": 10})
        expected[station_id] = high

    seeded = poller.seed_running_high(conn)

    assert set(seeded.keys()) == set(STATIONS.keys())
    for station_id, val in seeded.items():
        assert val == pytest.approx(expected[station_id])
        state = poller.get_state(station_id)
        assert state.running_high_f == pytest.approx(val)


# ─── _persist_running_highs tests ──────────────────────────────────────────

def test_persist_running_highs_writes_kv_entry():
    """After setting running_high_f in memory, _persist_running_highs writes to kv."""
    import bot.db as db_module

    station = _pick_station()
    conn = _make_conn()
    today = _today_lst(station)

    # Patch get_connection so _persist uses our in-memory DB
    orig = db_module._PERSIST_CONN
    db_module._PERSIST_CONN = conn
    try:
        poller = METARPoller()
        with poller._lock:
            poller._states[station].running_high_f = 88.0
            poller._states[station].running_high_date = today

        poller._persist_running_highs()
    finally:
        db_module._PERSIST_CONN = orig

    # kv_cache should now have the entry readable via seed_running_high
    new_poller = METARPoller()
    seeded = new_poller.seed_running_high(conn)

    assert station in seeded
    assert seeded[station] == pytest.approx(88.0)


def test_persist_running_highs_does_not_lower_existing_kv():
    """If kv already has a higher value (from a prior persist), don't overwrite."""
    import bot.db as db_module

    station = _pick_station()
    conn = _make_conn()
    today = _today_lst(station)

    # Pre-populate kv with a higher high
    _write_kv(conn, f"metar_daily_high_{station}_{today}",
              {"high_f": 95.0, "obs_count": 50})

    orig = db_module._PERSIST_CONN
    db_module._PERSIST_CONN = conn
    try:
        poller = METARPoller()
        with poller._lock:
            poller._states[station].running_high_f = 80.0  # lower than kv
            poller._states[station].running_high_date = today

        poller._persist_running_highs()
    finally:
        db_module._PERSIST_CONN = orig

    # kv should retain 95.0, not be overwritten with 80.0
    row = conn.execute(
        "SELECT value FROM kv_cache WHERE key = ?",
        (f"metar_daily_high_{station}_{today}",),
    ).fetchone()
    import json
    assert json.loads(row[0])["high_f"] == pytest.approx(95.0)
