"""Tests for the Poller ABC lifecycle."""

from __future__ import annotations

import threading
import time

import pytest

from bot.daemon.poller_base import Poller


# ═════════════════════════════════════════════════════════════════════════════
# Test fixtures — concrete Poller subclasses for each scenario
# ═════════════════════════════════════════════════════════════════════════════

class CountingPoller(Poller):
    """Counts calls and records the thread-id each call happened on."""
    name = "counting"
    interval_s = 0.05

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0
        self.thread_ids: set[int] = set()

    def _poll_once(self):
        self.calls += 1
        self.thread_ids.add(threading.get_ident())
        return self.calls


class FailingPoller(Poller):
    """Raises every other call."""
    name = "failing"
    interval_s = 0.05

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def _poll_once(self):
        self.calls += 1
        if self.calls % 2 == 0:
            raise RuntimeError(f"synthetic failure on call {self.calls}")
        return self.calls


class SlowPoller(Poller):
    """Takes 0.1s per call, interval is 0.02s — so calls should be
    back-to-back with minimal sleep."""
    name = "slow"
    interval_s = 0.02

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def _poll_once(self):
        self.calls += 1
        time.sleep(0.05)  # longer than the interval
        return self.calls


# ═════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═════════════════════════════════════════════════════════════════════════════

def test_poller_runs_on_its_own_thread():
    """start() spawns a thread, _poll_once runs there, stop() exits
    cleanly."""
    p = CountingPoller()
    assert not p.is_running()

    p.start()
    assert p.is_running()
    # Main thread id
    main_tid = threading.get_ident()

    time.sleep(0.2)
    assert p.stop(timeout=2.0), "Poller thread didn't exit after stop()"
    assert not p.is_running()

    assert p.calls >= 2, f"Expected ≥2 polls in 200ms @ 50ms interval, got {p.calls}"
    assert main_tid not in p.thread_ids, "Poller ran on main thread"
    assert len(p.thread_ids) == 1, f"Poller ran on multiple threads: {p.thread_ids}"


def test_poller_on_result_callback_fires():
    """Each _poll_once return value must be passed to on_result."""
    received: list[int] = []
    lock = threading.Lock()

    def on_result(val):
        with lock:
            received.append(val)

    p = CountingPoller(on_result=on_result)
    p.start()
    time.sleep(0.2)
    p.stop(timeout=2.0)

    assert len(received) == p.calls, f"Got {len(received)} callbacks for {p.calls} polls"
    # Values are monotonic (Poller processes each result before firing next)
    assert received == sorted(received)


def test_poller_isolates_callback_exceptions():
    """A raising on_result callback must NOT kill the poller loop."""

    def bad_callback(val):
        raise ValueError(f"bad callback on {val}")

    p = CountingPoller(on_result=bad_callback)
    p.start()
    time.sleep(0.2)
    p.stop(timeout=2.0)

    # Poller kept running through callback failures.
    assert p.calls >= 2


# ═════════════════════════════════════════════════════════════════════════════
# Error isolation
# ═════════════════════════════════════════════════════════════════════════════

def test_poller_survives_poll_exceptions():
    """A failing _poll_once doesn't kill the thread — it keeps retrying
    and the error_count climbs."""
    p = FailingPoller()
    p.start()
    time.sleep(0.3)
    p.stop(timeout=2.0)

    # Expect some successes and some failures.
    assert p.calls >= 4, f"Too few calls: {p.calls}"
    assert p._error_count >= 1
    assert p._error_count <= p.calls
    # Health snapshot reports last error.
    health = p.health()
    assert health["error_count"] >= 1
    assert "synthetic failure" in (health["last_error"] or "")


# ═════════════════════════════════════════════════════════════════════════════
# Stop mid-poll is clean
# ═════════════════════════════════════════════════════════════════════════════

def test_poller_stop_interrupts_sleep_not_poll():
    """stop() returns promptly even if the poller is sleeping between
    polls (doesn't wait out the full interval)."""

    class LongIntervalPoller(Poller):
        name = "long-interval"
        interval_s = 10.0  # 10s between polls

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = 0

        def _poll_once(self):
            self.calls += 1
            return self.calls

    p = LongIntervalPoller()
    p.start()
    # Let it do its first poll (fast) and enter the sleep.
    time.sleep(0.1)

    start = time.time()
    assert p.stop(timeout=2.0)
    elapsed = time.time() - start
    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s — not interrupting sleep"
    assert p.calls == 1


# ═════════════════════════════════════════════════════════════════════════════
# Slow poll doesn't over-sleep
# ═════════════════════════════════════════════════════════════════════════════

def test_poller_runs_back_to_back_when_poll_exceeds_interval():
    """If _poll_once takes longer than interval_s, the next call must
    fire immediately (or at least promptly). No artificial delay."""
    p = SlowPoller()
    p.start()
    time.sleep(0.3)
    p.stop(timeout=2.0)

    # 50ms poll, 20ms interval → effectively ~50ms per cycle, so in 300ms
    # we expect ~5-6 calls.
    assert p.calls >= 4, f"Too few calls under slow-poll regime: {p.calls}"


# ═════════════════════════════════════════════════════════════════════════════
# Idempotent start
# ═════════════════════════════════════════════════════════════════════════════

def test_poller_double_start_is_safe():
    """Calling start() twice doesn't spawn two threads or crash."""
    p = CountingPoller()
    p.start()
    p.start()  # should log warning, not spawn another thread
    time.sleep(0.15)
    p.stop(timeout=2.0)

    # Still only ran on one thread.
    assert len(p.thread_ids) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Health snapshot
# ═════════════════════════════════════════════════════════════════════════════

def test_health_snapshot_has_all_fields():
    p = CountingPoller()
    p.start()
    time.sleep(0.15)
    p.stop(timeout=2.0)

    h = p.health()
    for key in ("name", "running", "poll_count", "error_count",
                "last_error", "last_poll_age_s", "interval_s"):
        assert key in h, f"health() missing {key}"
    assert h["name"] == "counting"
    assert h["running"] is False  # after stop
    assert h["poll_count"] == p.calls
    assert h["last_poll_age_s"] is not None
