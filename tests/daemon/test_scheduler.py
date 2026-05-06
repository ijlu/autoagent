"""Tests for bot/daemon/scheduler.py."""

from __future__ import annotations

import threading
import time

import pytest

from bot.daemon.scheduler import Scheduler


# ═════════════════════════════════════════════════════════════════════════════
# Basic firing
# ═════════════════════════════════════════════════════════════════════════════

def test_scheduler_fires_task_at_interval():
    """A registered task fires N times over N×interval."""
    s = Scheduler()
    calls = []

    def task():
        calls.append(time.time())

    s.register("tick", task, interval_s=0.05)

    # Run the scheduler on a background thread so we can stop it.
    t = threading.Thread(
        target=lambda: s.run_forever(install_signal_handlers=False), daemon=True
    )
    t.start()
    time.sleep(0.25)
    s.stop()
    t.join(timeout=2.0)

    # 0.25s / 0.05s ≈ 5 firings, allow some jitter.
    assert 3 <= len(calls) <= 7, f"Expected ~5 firings, got {len(calls)}"


# ═════════════════════════════════════════════════════════════════════════════
# Isolation: one task's crash doesn't poison the scheduler
# ═════════════════════════════════════════════════════════════════════════════

def test_scheduler_isolates_task_exceptions():
    """A task that always raises keeps getting rescheduled. Other tasks
    continue to fire normally."""
    s = Scheduler()
    good_calls = []
    bad_calls = []

    def good():
        good_calls.append(1)

    def bad():
        bad_calls.append(1)
        raise RuntimeError("synthetic task failure")

    s.register("good", good, interval_s=0.05)
    s.register("bad", bad, interval_s=0.05)

    t = threading.Thread(
        target=lambda: s.run_forever(install_signal_handlers=False), daemon=True
    )
    t.start()
    time.sleep(0.25)
    s.stop()
    t.join(timeout=2.0)

    assert len(good_calls) >= 3, f"Good task was starved: {len(good_calls)}"
    assert len(bad_calls) >= 3, f"Bad task stopped firing: {len(bad_calls)}"

    # Health report shows the bad task errored each time it fired.
    h = s.health()
    assert h["tasks"]["bad"]["error_count"] == len(bad_calls)
    assert h["tasks"]["good"]["error_count"] == 0
    assert "synthetic task failure" in (h["tasks"]["bad"]["last_error"] or "")


# ═════════════════════════════════════════════════════════════════════════════
# Task overrun: slow task doesn't block the scheduler forever but serializes
# with itself
# ═════════════════════════════════════════════════════════════════════════════

def test_slow_task_does_not_overlap_with_itself():
    """If task_fn takes longer than interval_s, the next firing waits
    for the previous one to finish (no overlap)."""
    s = Scheduler()
    concurrent_calls = 0
    max_concurrent = 0
    concurrency_lock = threading.Lock()
    call_count = 0

    def slow_task():
        nonlocal concurrent_calls, max_concurrent, call_count
        with concurrency_lock:
            concurrent_calls += 1
            max_concurrent = max(max_concurrent, concurrent_calls)
            call_count += 1
        time.sleep(0.1)
        with concurrency_lock:
            concurrent_calls -= 1

    # 20ms interval, 100ms work: second firing must wait for first.
    s.register("slow", slow_task, interval_s=0.02)

    t = threading.Thread(
        target=lambda: s.run_forever(install_signal_handlers=False), daemon=True
    )
    t.start()
    time.sleep(0.35)
    s.stop()
    t.join(timeout=2.0)

    assert max_concurrent == 1, f"Task overlapped with itself ({max_concurrent} concurrent)"
    # In 350ms with 100ms per call, we expect 2-3 sequential completions.
    assert call_count >= 2, f"Too few sequential firings: {call_count}"


# ═════════════════════════════════════════════════════════════════════════════
# stop() triggers clean shutdown
# ═════════════════════════════════════════════════════════════════════════════

def test_stop_exits_promptly():
    """stop() returns promptly even if there are long intervals."""
    s = Scheduler()

    def never_fires_in_test():
        pass

    # Long intervals — without stop() working, this would hang.
    s.register("slow_1", never_fires_in_test, interval_s=100.0, initial_delay_s=100.0)

    t = threading.Thread(
        target=lambda: s.run_forever(install_signal_handlers=False), daemon=True
    )
    t.start()
    time.sleep(0.05)

    start = time.time()
    s.stop()
    t.join(timeout=2.0)
    elapsed = time.time() - start

    assert not t.is_alive(), "Scheduler didn't exit on stop()"
    assert elapsed < 1.5, f"stop() took {elapsed:.2f}s — not interrupting wait"


# ═════════════════════════════════════════════════════════════════════════════
# on_start / on_stop hooks fire in order
# ═════════════════════════════════════════════════════════════════════════════

def test_start_and_stop_hooks_fire():
    """on_start hooks fire before any tasks, on_stop fires after stop()."""
    s = Scheduler()
    events: list[str] = []
    event_lock = threading.Lock()

    def record(name: str):
        with event_lock:
            events.append(name)

    s.on_start(lambda: record("start1"))
    s.on_start(lambda: record("start2"))
    s.on_stop(lambda: record("stop1"))
    s.on_stop(lambda: record("stop2"))

    def task_fn():
        record("task")

    s.register("task", task_fn, interval_s=0.05)

    t = threading.Thread(
        target=lambda: s.run_forever(install_signal_handlers=False), daemon=True
    )
    t.start()
    time.sleep(0.15)
    s.stop()
    t.join(timeout=2.0)

    # start hooks fired before any task, in registration order
    assert events[0] == "start1"
    assert events[1] == "start2"
    # task fired at least once between start and stop
    assert "task" in events
    # stop hooks fired last, in registration order
    assert events[-2:] == ["stop1", "stop2"]


# ═════════════════════════════════════════════════════════════════════════════
# Duplicate task names rejected
# ═════════════════════════════════════════════════════════════════════════════

def test_duplicate_registration_raises():
    s = Scheduler()
    s.register("x", lambda: None, interval_s=1.0)
    with pytest.raises(ValueError, match="already registered"):
        s.register("x", lambda: None, interval_s=1.0)


# ═════════════════════════════════════════════════════════════════════════════
# health() returns expected structure
# ═════════════════════════════════════════════════════════════════════════════

def test_health_reports_task_stats():
    s = Scheduler()
    s.register("foo", lambda: None, interval_s=0.05)
    t = threading.Thread(
        target=lambda: s.run_forever(install_signal_handlers=False), daemon=True
    )
    t.start()
    time.sleep(0.15)
    s.stop()
    t.join(timeout=2.0)

    h = s.health()
    assert "tasks" in h
    assert "foo" in h["tasks"]
    foo = h["tasks"]["foo"]
    assert foo["run_count"] >= 2
    assert foo["error_count"] == 0
    assert foo["cancelled"] is False
