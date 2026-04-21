"""T0.4 — poll-cadence integration test.

Proves the T0.3 invariant end-to-end: a slow downstream handler callback
does NOT block the METAR poller's 30s cadence. The poller must keep
fetching on schedule even when ``WeatherQuoter.requote_city`` takes longer
than the poll interval.

This is the whole point of moving requotes off the poller thread. If a
regression re-introduces synchronous calling, the poller will miss ticks
while a handler is busy and this test will catch it.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from bot.daemon.dispatcher import AsyncEventDispatcher
from bot.daemon.forecast_cache import ForecastCache
from bot.daemon.metar_poller import METARPoller, TemperatureChange, StationReading
from bot.daemon.weather_handler import WeatherChangeHandler


def _make_fake_change(series="KXHIGHNY", station="KJFK") -> TemperatureChange:
    reading = StationReading(
        station=station, temp_f=72.0, temp_c=22.2,
        obs_time="2026-04-16T18:00:00Z", poll_time=time.time(),
    )
    return TemperatureChange(
        station=station, city="nyc", series=series,
        old_temp_f=70.0, new_temp_f=72.0,
        running_high_f=72.0, hours_left=8.0, trajectory_f_per_hr=1.0,
        reading=reading,
    )


class _FakePoller(METARPoller):
    """METARPoller variant that emits a fixed change list on every tick,
    bypassing the live HTTP fetch. Interval stays tight (0.1s) so the test
    runs fast."""

    interval_s = 0.1

    def __init__(self, on_result=None, changes=None):
        super().__init__(on_result=on_result)
        self._fake_changes = changes or []
        self._fake_poll_count = 0

    def _poll_once(self):
        self._fake_poll_count += 1
        return list(self._fake_changes) if self._fake_changes else None


def test_slow_handler_does_not_stall_poller():
    """With dispatcher wired, a handler that takes longer than interval_s
    still lets the poller tick on schedule."""
    # Quoter's shadow_requote_city blocks for 500ms — 5× the poll interval.
    quoter = MagicMock()
    block_started = threading.Event()

    def slow_shadow(**_):
        block_started.set()
        time.sleep(0.5)
        return []

    quoter.shadow_requote_city.side_effect = slow_shadow
    fcache = ForecastCache()
    fcache.set("KJFK", 78.0)

    dispatcher = AsyncEventDispatcher(name="test")
    handler = WeatherChangeHandler(
        quoter=quoter, forecast_cache=fcache, live=False,
        dispatcher=dispatcher, cooldown_s=0.0,
    )

    poller = _FakePoller(on_result=handler, changes=[_make_fake_change()])
    poller.start()
    try:
        # Wait until at least one slow_shadow call has started (proves the
        # handler is exercising the callback path) and then some.
        assert block_started.wait(timeout=1.0), "handler never ran"
        # Now give the poller 500ms — in the OLD synchronous-handler world
        # it would have completed roughly 1 tick during that window because
        # slow_shadow blocks the poller thread. With the dispatcher, the
        # poller should keep ticking on its 100ms cadence → 3+ ticks.
        t0 = time.time()
        while time.time() - t0 < 0.5:
            time.sleep(0.02)
        ticks = poller._fake_poll_count
    finally:
        poller.stop(timeout=1.0)
        dispatcher.shutdown(timeout=2.0)

    # With 100ms interval over 500ms window we'd expect ~5 ticks; allow slop
    # for test-runner variance. 3 is a comfortable floor that still rules out
    # "poller was blocked by slow callback" (which would give 1–2).
    assert ticks >= 3, (
        f"poller only ticked {ticks} times in 500ms — dispatcher "
        "did not offload the slow callback"
    )


def test_dispatcher_coalesces_bursty_same_series():
    """If the poller emits the same series on successive ticks while the
    handler is slow, stale events must coalesce — we never want a queue
    of 10 requotes pending for a city."""
    quoter = MagicMock()
    gate = threading.Event()
    run_count = 0
    run_lock = threading.Lock()

    def slow_shadow(**_):
        nonlocal run_count
        with run_lock:
            run_count += 1
        gate.wait(timeout=2.0)
        return []

    quoter.shadow_requote_city.side_effect = slow_shadow
    fcache = ForecastCache()
    fcache.set("KJFK", 78.0)

    dispatcher = AsyncEventDispatcher(name="test")
    handler = WeatherChangeHandler(
        quoter=quoter, forecast_cache=fcache, live=False,
        dispatcher=dispatcher, cooldown_s=0.0,
    )

    poller = _FakePoller(
        on_result=handler, changes=[_make_fake_change(series="KXHIGHNY")]
    )
    poller.start()
    try:
        # Let several ticks fire while the first callback is parked in gate.wait.
        time.sleep(0.4)  # ~4 ticks at 100ms
        # By now dispatcher received 4+ dispatches for KXHIGHNY but only 1
        # can be running (first call parked in gate). The other 3+ should
        # have collapsed into the size-1 slot.
        dispatched = dispatcher.health()["dispatched"]
        assert dispatched >= 2, (
            f"expected multiple dispatches within the window, got {dispatched}"
        )
    finally:
        gate.set()
        poller.stop(timeout=1.0)
        dispatcher.shutdown(timeout=3.0)

    health = dispatcher.health()
    # Coalescing counter incremented, proving size-1 behavior kicked in.
    assert health["coalesced"] >= 1, (
        f"no coalescing occurred despite bursty same-series events: "
        f"health={health}"
    )
    # Total runs capped by the slot — nowhere near the dispatch count.
    assert run_count <= health["dispatched"], run_count
    # And definitively less than the dispatch count (coalescing dropped
    # at least one).
    assert run_count < health["dispatched"], (
        f"coalescing happened but slow_shadow ran {run_count}× vs "
        f"{health['dispatched']} dispatches — expected fewer runs"
    )
