"""T0.4 — row-shape integration test.

End-to-end: drive a ``TemperatureChange`` through the full handler →
dispatcher → WeatherQuoter.shadow_requote_city path, then read the row
back and assert the columns and types match what shadow-promotion /
match_shadow_fills / backtests expect.

This is the integration seam the audit flagged as under-tested: the
unit tests mock the quoter, and the quoter unit tests mock the
Kalshi fetch, so nothing until now verified the write ends up in a
row with the shape readers depend on.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot.daemon.dispatcher import AsyncEventDispatcher
from bot.daemon.forecast_cache import ForecastCache
from bot.daemon.metar_poller import StationReading, TemperatureChange
from bot.daemon.weather_handler import WeatherChangeHandler
from bot.daemon.weather_quoter import WeatherMarket, WeatherQuoter
from bot.db import init_db


def _today_close_time() -> str:
    """Close-time anchored to KJFK's LST (EST, UTC-5) so the quoter's
    ``_is_today_market`` check passes regardless of what hour of UTC the
    test is run. Using UTC-today here would fail at night UTC (0:00-5:00
    UTC) when LST is still on the previous date."""
    lst_tz = timezone(timedelta(hours=-5))
    today_lst = datetime.now(lst_tz).date()
    # 12:00 LST is safely inside the LST day; convert to UTC for the ISO.
    noon_lst = datetime(
        today_lst.year, today_lst.month, today_lst.day,
        12, 0, 0, tzinfo=lst_tz,
    )
    return noon_lst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_bracket_market() -> WeatherMarket:
    return WeatherMarket(
        ticker="KXHIGHNY-26APR20-B7476",
        title="NYC high 74 to 76",
        series="KXHIGHNY",
        bracket_floor=74.0,
        bracket_cap=76.0,
        threshold=None,
        is_bracket=True,
        is_above=True,
        yes_bid=30,
        yes_ask=35,
        volume=100,
        close_time=_today_close_time(),
    )


def _make_change(series="KXHIGHNY", station="KJFK"):
    reading = StationReading(
        station=station, temp_f=74.5, temp_c=23.6,
        obs_time="2026-04-20T18:00:00Z", poll_time=time.time(),
    )
    return TemperatureChange(
        station=station, city="nyc", series=series,
        old_temp_f=72.0, new_temp_f=74.5,
        running_high_f=74.5, hours_left=8.0, trajectory_f_per_hr=1.5,
        reading=reading,
    )


# Expected columns on weather_mm_shadow rows read back from the DB.
# If schema evolves, update both this set AND downstream readers
# (bot/learning/mm_promotion.py, match_shadow_fills) together.
EXPECTED_COLUMNS = {
    "id", "ts_unix", "ts_iso",
    "ticker", "series", "station",
    "old_temp_f", "new_temp_f", "running_high_f", "forecast_high_f",
    "hours_left", "trajectory_f_per_hr",
    "fair_value_cents", "proposed_bid_cents", "proposed_ask_cents",
    "half_spread_cents",
    "market_yes_bid", "market_yes_ask", "market_mid",
    "inventory", "gate_should_quote", "gate_reason", "gate_spread_mult",
    "latency_ms", "live_mode",
    # The paired-logging columns added for step 10 live writes; shadow
    # rows leave them NULL.
    "live_order_id_bid", "live_order_id_ask", "order_size",
    # Match-status columns added by match_shadow_fills post-hoc.
    "shadow_bid_filled", "shadow_ask_filled",
    # T1.2 trigger attribution: metar_change / time_decay / forecast_change.
    "trigger_reason",
}


def test_shadow_write_round_trip_via_dispatcher():
    """Dispatch a change → shadow row lands → shape matches reader expectations."""
    conn = init_db(":memory:")
    try:
        quoter = WeatherQuoter(conn)
        fcache = ForecastCache()
        fcache.set("KJFK", 78.0)

        dispatcher = AsyncEventDispatcher(name="rowshape")
        handler = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=False,
            dispatcher=dispatcher, cooldown_s=0.0,
        )

        # Avoid the network — feed a bracket market directly.
        with patch.object(
            WeatherQuoter, "_fetch_weather_markets",
            return_value=[_make_bracket_market()],
        ):
            # Dispatch the change and wait for the worker to drain.
            handler([_make_change()])
            # Let the dispatcher's per-series worker process.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM weather_mm_shadow"
                ).fetchone()[0]
                if rows >= 1:
                    break
                time.sleep(0.01)
            # Drain cleanly before asserting.
            dispatcher.shutdown(timeout=2.0)

        # ── Read back ──────────────────────────────────────────────────
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM weather_mm_shadow WHERE ticker = ?",
            ("KXHIGHNY-26APR20-B7476",),
        ).fetchall()
        assert len(rows) == 1, (
            f"expected exactly 1 shadow row, got {len(rows)}"
        )
        row = rows[0]
        column_names = {d[0] for d in cursor.description}

        # Schema contract.
        missing = EXPECTED_COLUMNS - column_names
        assert not missing, (
            f"weather_mm_shadow is missing expected columns: {missing}. "
            "Add to bot/db.py schema or update the registry."
        )

        # Pull values via column name (index-free to survive column reorders).
        values = dict(zip((d[0] for d in cursor.description), row))

        # Shape assertions — the things readers depend on.
        assert values["ticker"] == "KXHIGHNY-26APR20-B7476"
        assert values["series"] == "KXHIGHNY"
        assert values["station"] == "KJFK"
        assert isinstance(values["ts_unix"], int) and values["ts_unix"] > 0
        assert isinstance(values["ts_iso"], str) and values["ts_iso"].startswith("2026")
        # Temp echoes what the handler received.
        assert values["old_temp_f"] == 72.0
        assert values["new_temp_f"] == 74.5
        assert values["running_high_f"] == 74.5
        assert values["forecast_high_f"] == 78.0
        # Quote numerics are cents (int) within 0-100 range.
        for col in ("fair_value_cents", "proposed_bid_cents", "proposed_ask_cents"):
            v = values[col]
            assert isinstance(v, int), f"{col} is not int: {type(v).__name__}"
            assert 0 <= v <= 100, f"{col}={v} outside cents range"
        # Bid < ask invariant (quoter cannot post an inverted spread).
        assert values["proposed_bid_cents"] < values["proposed_ask_cents"], values
        # Live-mode flag is 0 for shadow; the paired-logging columns are NULL.
        assert values["live_mode"] == 0
        assert values["live_order_id_bid"] is None
        assert values["live_order_id_ask"] is None
        # Gate bool stored as 0/1 int, not python bool.
        assert values["gate_should_quote"] in (0, 1)
        # Fill-match columns start NULL — populated later by match_shadow_fills.
        assert values["shadow_bid_filled"] is None
        assert values["shadow_ask_filled"] is None
        # T1.2: METAR-driven changes carry the default reason. The drivers
        # (TimeDecayDriver / ForecastChangeDriver) override to their own
        # labels via enqueue_synthetic.
        assert values["trigger_reason"] == "metar_change"
    finally:
        conn.close()
