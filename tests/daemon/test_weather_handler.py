"""Tests for the METAR → WeatherQuoter adapter (bot/daemon/weather_handler.py).

Covers:
- Shadow mode routes through shadow_requote_city (never requote_city).
- Live mode routes through requote_city (never shadow_requote_city).
- Forecast fallback: missing forecast uses running_high + 5°F.
- Per-series cooldown suppresses rapid re-fires.
- Empty / None change lists are no-ops.
- Stats increment correctly on dispatched, throttled, and errored calls.
- Handler passes the expected smart_gate callable through to the quoter.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.daemon.forecast_cache import ForecastCache
from bot.daemon.metar_poller import StationReading, TemperatureChange
from bot.daemon.weather_handler import (
    DEFAULT_COOLDOWN_S,
    FORECAST_FALLBACK_DELTA_F,
    WeatherChangeHandler,
    default_smart_gate,
)
from bot.db import init_db
from bot.learning.directional_shadow import LiveState
from bot.learning.mm_promotion import set_mm_live_state


def _make_change(
    *, series="KXHIGHNY", station="KJFK", new_temp=72.0, old_temp=70.0,
    running_high=72.0, hours_left=8.0, trajectory=1.0,
) -> TemperatureChange:
    reading = StationReading(
        station=station, temp_f=new_temp, temp_c=(new_temp - 32) * 5 / 9,
        obs_time="2026-04-16T18:00:00Z", poll_time=time.time(),
    )
    return TemperatureChange(
        station=station,
        city="nyc",
        series=series,
        old_temp_f=old_temp,
        new_temp_f=new_temp,
        running_high_f=running_high,
        hours_left=hours_left,
        trajectory_f_per_hr=trajectory,
        reading=reading,
    )


@pytest.fixture()
def quoter():
    q = MagicMock()
    q.shadow_requote_city.return_value = [MagicMock(), MagicMock()]
    q.requote_city.return_value = [MagicMock(skipped=False), MagicMock(skipped=True)]
    return q


@pytest.fixture()
def fcache():
    c = ForecastCache()
    c.set("KJFK", 78.0)
    return c


class TestShadowMode:
    def test_shadow_is_default(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=False)
        h([_make_change()])
        quoter.shadow_requote_city.assert_called_once()
        quoter.requote_city.assert_not_called()

    def test_shadow_passes_forecast_high(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=False)
        h([_make_change()])
        kwargs = quoter.shadow_requote_city.call_args.kwargs
        assert kwargs["forecast_high_f"] == 78.0
        assert kwargs["series"] == "KXHIGHNY"
        assert kwargs["station"] == "KJFK"
        assert kwargs["smart_gates"] is default_smart_gate
        assert kwargs["old_temp_f"] == 70.0
        assert kwargs["new_temp_f"] == 72.0

    def test_shadow_stats(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, live=False)
        h([_make_change()])
        assert h.stats["changes_seen"] == 1
        assert h.stats["requotes_dispatched"] == 1
        assert h.stats["markets_shadowed"] == 2
        assert h.stats["errors"] == 0


class TestLiveMode:
    """Per-series live state gates the handler; master env flag ``live=True``
    is necessary but not sufficient. Tests flip the KXHIGHNY series to
    LIVE_FULL to get onto the requote_city path."""

    @pytest.fixture()
    def live_conn(self):
        c = init_db(":memory:")
        set_mm_live_state(c, "KXHIGHNY", LiveState.LIVE_FULL)
        yield c
        c.close()

    def test_live_routes_to_requote_city(self, quoter, fcache, live_conn):
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=True, conn=live_conn,
        )
        h([_make_change()])
        quoter.requote_city.assert_called_once()
        quoter.shadow_requote_city.assert_not_called()

    def test_live_passes_old_new_temps_for_paired_shadow_row(
        self, quoter, fcache, live_conn,
    ):
        """T.6 paired logging — the live path writes a weather_mm_shadow row
        with the observed old/new temp so the shadow-fill model can be
        compared to realized live fills at settlement."""
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=True, conn=live_conn,
        )
        h([_make_change()])
        kwargs = quoter.requote_city.call_args.kwargs
        assert kwargs["old_temp_f"] == 70.0
        assert kwargs["new_temp_f"] == 72.0
        # Multiplier is Thompson-sampled from shadow P&L posterior; with no
        # seeded data it falls back to 0.0 (insufficient_n). The precise
        # multiplier math is covered in test_sizing.py and test_mm_promotion.
        assert isinstance(kwargs["order_size_multiplier"], float)
        assert kwargs["order_size_multiplier"] >= 0.0

    def test_env_off_forces_shadow_even_when_series_live(
        self, quoter, fcache, live_conn,
    ):
        """Master env flag (``live=False``) overrides per-series kv state."""
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=False, conn=live_conn,
        )
        h([_make_change()])
        quoter.shadow_requote_city.assert_called_once()
        quoter.requote_city.assert_not_called()

    def test_live_env_but_series_shadow_goes_shadow(self, quoter, fcache):
        c = init_db(":memory:")
        # Series never promoted → default SHADOW
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=True, conn=c,
        )
        h([_make_change()])
        quoter.shadow_requote_city.assert_called_once()
        quoter.requote_city.assert_not_called()
        c.close()

    def test_live_counts_quoted_and_skipped(self, quoter, fcache, live_conn):
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=True, conn=live_conn,
        )
        h([_make_change()])
        assert h.stats["markets_quoted"] == 1
        assert h.stats["markets_skipped"] == 1


class TestForecastFallback:
    def test_missing_forecast_uses_running_high_plus_delta(self, quoter):
        empty = ForecastCache()  # no forecast for any station
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=empty)
        change = _make_change(running_high=71.0)
        h([change])

        kwargs = quoter.shadow_requote_city.call_args.kwargs
        assert kwargs["forecast_high_f"] == pytest.approx(
            71.0 + FORECAST_FALLBACK_DELTA_F
        )


class TestCooldown:
    def test_throttles_rapid_second_change(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, cooldown_s=10.0)
        h([_make_change()])
        h([_make_change()])  # second call within cooldown window
        assert quoter.shadow_requote_city.call_count == 1
        assert h.stats["changes_throttled"] == 1
        assert h.stats["requotes_dispatched"] == 1

    def test_different_series_not_throttled(self, quoter, fcache):
        fcache.set("KMIA", 88.0)
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, cooldown_s=10.0)
        h([_make_change(series="KXHIGHNY", station="KJFK")])
        h([_make_change(series="KXHIGHMIA", station="KMIA")])
        assert quoter.shadow_requote_city.call_count == 2
        assert h.stats["changes_throttled"] == 0

    def test_cooldown_elapsed_allows_requote(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache, cooldown_s=1.0)
        h([_make_change()])
        # Rewind the last-requote stamp to simulate elapsed cooldown
        h._last_requote["KXHIGHNY"] = time.time() - 5.0
        h([_make_change()])
        assert quoter.shadow_requote_city.call_count == 2


class TestEdgeCases:
    def test_none_changes_is_noop(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        h(None)
        quoter.shadow_requote_city.assert_not_called()
        quoter.requote_city.assert_not_called()

    def test_empty_changes_is_noop(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        h([])
        quoter.shadow_requote_city.assert_not_called()

    def test_quoter_exception_counted(self, fcache):
        quoter = MagicMock()
        quoter.shadow_requote_city.side_effect = RuntimeError("boom")
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        h([_make_change()])
        assert h.stats["errors"] == 1
        assert h.stats["requotes_dispatched"] == 0
        # After an error we should NOT stamp last_requote, so the next valid
        # event can retry immediately.
        assert "KXHIGHNY" not in h._last_requote


class TestDefaultCooldown:
    def test_default_cooldown_is_10s(self, quoter, fcache):
        h = WeatherChangeHandler(quoter=quoter, forecast_cache=fcache)
        assert h.cooldown_s == DEFAULT_COOLDOWN_S == 10.0


class TestDefaultSmartGate:
    def test_threshold_markets_get_sane_defaults(self):
        """Threshold markets pass None for floor/cap — the gate wrapper must
        expand those to 0/200°F so the proximity check is a no-op."""
        # Pick safe time-of-day + trajectory values that won't trip other gates.
        should_quote, reason, mult = default_smart_gate(
            station="KJFK",
            bracket_floor=None,
            bracket_cap=None,
            running_high=70.0,
            forecast_high=75.0,
            hours_left=10.0,  # ~2pm LST, well within 7am-7pm window
            trajectory_f_per_hr=0.5,
        )
        # Result must be callable-shaped; we don't assert True because other
        # gates may gate it down — the key property is no KeyError / TypeError.
        assert isinstance(should_quote, bool)
        assert isinstance(reason, str)
        assert isinstance(mult, float)


class TestDispatcherIntegration:
    """T0.3 — when wired with a dispatcher, the handler hands off to
    per-series worker threads instead of invoking the quoter inline on the
    poller thread. This keeps METAR polling cadence independent of
    downstream Kalshi latency."""

    def test_dispatcher_path_returns_before_quoter_runs(self, fcache):
        """Handler __call__ must return before the slow quoter finishes."""
        import threading
        from bot.daemon.dispatcher import AsyncEventDispatcher

        quoter = MagicMock()
        gate = threading.Event()
        done = threading.Event()

        def slow_shadow(**_):
            gate.wait(timeout=2.0)
            done.set()
            return []

        quoter.shadow_requote_city.side_effect = slow_shadow

        dispatcher = AsyncEventDispatcher(name="test")
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=False,
            dispatcher=dispatcher,
        )
        try:
            t0 = time.time()
            h([_make_change()])
            elapsed = time.time() - t0
            # Handler returned; worker is still blocked in slow_shadow.
            assert elapsed < 0.1, (
                f"handler blocked for {elapsed:.3f}s — should have handed off"
            )
            assert not done.is_set(), "quoter ran synchronously on caller thread"
            gate.set()
            assert done.wait(timeout=2.0), "worker never ran the callback"
        finally:
            dispatcher.shutdown(timeout=2.0)

    def test_dispatcher_path_parallelizes_across_series(self, fcache):
        """Two changes for different series must run concurrently on their
        own worker threads."""
        import threading
        from bot.daemon.dispatcher import AsyncEventDispatcher

        started = threading.Semaphore(0)
        release = threading.Event()
        call_keys: list[str] = []
        call_lock = threading.Lock()

        def slow_shadow(*, series, **_):
            with call_lock:
                call_keys.append(series)
            started.release()
            release.wait(timeout=3.0)
            return []

        quoter = MagicMock()
        quoter.shadow_requote_city.side_effect = slow_shadow

        dispatcher = AsyncEventDispatcher(name="test")
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=False,
            dispatcher=dispatcher,
        )
        try:
            h([
                _make_change(series="KXHIGHNY", station="KJFK"),
                _make_change(series="KXHIGHAUS", station="KJFK"),
            ])
            # Both workers should be running concurrently — each released
            # one semaphore ticket.
            assert started.acquire(timeout=1.0), "first series never started"
            assert started.acquire(timeout=1.0), (
                "second series did not run in parallel with first — "
                "cross-series blocking detected"
            )
            release.set()
        finally:
            dispatcher.shutdown(timeout=3.0)

        assert set(call_keys) == {"KXHIGHNY", "KXHIGHAUS"}

    def test_dispatcher_serializes_same_series(self, fcache):
        """Repeated events for the same series must never run concurrently —
        the WeatherQuoter assumes only one cancel-replace per city at a time."""
        import threading
        from bot.daemon.dispatcher import AsyncEventDispatcher

        in_flight = 0
        max_concurrent = 0
        gate = threading.Event()
        lock = threading.Lock()

        def tracked_shadow(**_):
            nonlocal in_flight, max_concurrent
            with lock:
                in_flight += 1
                if in_flight > max_concurrent:
                    max_concurrent = in_flight
            gate.wait(timeout=0.1)  # overlap window
            with lock:
                in_flight -= 1
            return []

        quoter = MagicMock()
        quoter.shadow_requote_city.side_effect = tracked_shadow

        dispatcher = AsyncEventDispatcher(name="test")
        # Cooldown=0 so both changes dispatch. The dispatcher's size-1 slot
        # means the second one queues behind the first — we expect one to
        # run, then the other.
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=False,
            dispatcher=dispatcher, cooldown_s=0.0,
        )
        try:
            h([_make_change(series="KXHIGHNY")])
            time.sleep(0.02)  # let first enter tracked_shadow
            h([_make_change(series="KXHIGHNY", new_temp=75.0)])
            gate.set()
        finally:
            dispatcher.shutdown(timeout=3.0)

        assert max_concurrent == 1, (
            f"same-series work overlapped ({max_concurrent} concurrent) — "
            "per-series serialization broken"
        )

    def test_no_dispatcher_is_synchronous(self, quoter, fcache):
        """Backwards-compat path: no dispatcher → inline call on caller thread."""
        h = WeatherChangeHandler(
            quoter=quoter, forecast_cache=fcache, live=False,
        )
        h([_make_change()])
        # Synchronous path → quoter was called before h() returned.
        quoter.shadow_requote_city.assert_called_once()
