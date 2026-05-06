"""T0.3 — AsyncEventDispatcher invariants.

Protects against regression of the contract:
- dispatch() returns in O(1); never blocks on the callback.
- Same-key events serialize (never two concurrent calls for one key).
- Different-key events run in parallel (no cross-key blocking).
- A newer event for a key replaces the pending one (size-1 coalescing).
- shutdown() drains in-flight work and exits all worker threads.
"""
from __future__ import annotations

import threading
import time

import pytest

from bot.daemon.dispatcher import AsyncEventDispatcher


def test_dispatch_returns_immediately_even_for_slow_callback():
    d = AsyncEventDispatcher(name="test")
    gate = threading.Event()

    def slow():
        gate.wait(timeout=2.0)

    t0 = time.time()
    d.dispatch("A", slow)
    elapsed = time.time() - t0
    # Dispatch must not block on the callback — well under the callback
    # runtime. Gate keeps the worker busy so we prove we didn't wait for it.
    assert elapsed < 0.05, f"dispatch took {elapsed:.3f}s — should be near-zero"
    gate.set()
    assert d.shutdown(timeout=2.0)


def test_same_key_events_serialize():
    """Two dispatches to the same key must run sequentially, never overlap."""
    d = AsyncEventDispatcher(name="test")
    in_flight = 0
    max_concurrent = 0
    lock = threading.Lock()

    def work():
        nonlocal in_flight, max_concurrent
        with lock:
            in_flight += 1
            if in_flight > max_concurrent:
                max_concurrent = in_flight
        time.sleep(0.05)
        with lock:
            in_flight -= 1

    # Submit both before either can finish. Same key → worker runs them back
    # to back. Size-1 coalescing means second one queues, first runs, then
    # second runs.
    d.dispatch("A", work)
    d.dispatch("A", work)
    assert d.shutdown(timeout=3.0)

    assert max_concurrent == 1, (
        f"same-key work overlapped ({max_concurrent} concurrent) — "
        "per-key serialization broken"
    )


def test_different_keys_run_in_parallel():
    """Different keys get different workers; work for A must not block B."""
    d = AsyncEventDispatcher(name="test")
    gate = threading.Event()
    done = threading.Event()

    def wait_on_gate():
        # A never finishes until gate is set — parks the A-worker thread.
        gate.wait(timeout=3.0)

    def quick():
        done.set()

    d.dispatch("A", wait_on_gate)
    time.sleep(0.05)  # let A-worker start and block
    d.dispatch("B", quick)

    # B should complete despite A being stuck.
    assert done.wait(timeout=1.0), (
        "B did not run while A was blocked — cross-key blocking detected"
    )
    gate.set()
    assert d.shutdown(timeout=2.0)


def test_coalescing_drops_stale_pending_event():
    """If a second event arrives while one is queued, the first is dropped."""
    d = AsyncEventDispatcher(name="test")
    start_barrier = threading.Event()
    release_first = threading.Event()
    ran: list[str] = []

    def first():
        start_barrier.set()
        # Hold the worker so second and third both pile up behind it,
        # coalescing against each other. Only the newest survives.
        release_first.wait(timeout=3.0)
        ran.append("first")

    def stale():
        ran.append("stale")

    def newest():
        ran.append("newest")

    d.dispatch("A", first)
    assert start_barrier.wait(timeout=1.0)
    # First is running; these two both queue to the size-1 slot —
    # `newest` should overwrite `stale`.
    d.dispatch("A", stale)
    d.dispatch("A", newest)
    release_first.set()

    assert d.shutdown(timeout=3.0)
    assert ran == ["first", "newest"], (
        f"coalescing broken — ran order {ran}; 'stale' should have been "
        "dropped when 'newest' displaced it"
    )
    h = d.health()
    assert h["coalesced"] >= 1, (
        f"coalesced counter should have incremented: health={h}"
    )


def test_exception_in_callback_does_not_kill_worker():
    """A raising callback is logged + counted; the worker keeps running."""
    d = AsyncEventDispatcher(name="test")

    ran: list[str] = []

    def boom():
        raise RuntimeError("intentional")

    def follow_up():
        ran.append("ok")

    d.dispatch("A", boom)
    # Give the worker a moment to process the failure.
    for _ in range(50):
        if d.health()["workers"] and d.health()["workers"][0]["errors"] >= 1:
            break
        time.sleep(0.02)

    d.dispatch("A", follow_up)
    assert d.shutdown(timeout=2.0)
    assert ran == ["ok"], "worker died after an exception — lifetime broken"

    h = d.health()
    worker = h["workers"][0]
    assert worker["errors"] >= 1
    assert worker["processed"] >= 1  # follow_up still ran


def test_shutdown_is_idempotent_and_rejects_new_dispatches():
    d = AsyncEventDispatcher(name="test")
    d.dispatch("A", lambda: None)
    assert d.shutdown(timeout=2.0)
    # Second shutdown is a no-op.
    assert d.shutdown(timeout=0.1)

    with pytest.raises(RuntimeError):
        d.dispatch("A", lambda: None)


def test_health_reports_worker_count_after_dispatch():
    d = AsyncEventDispatcher(name="test")
    assert d.health()["worker_count"] == 0

    d.dispatch("A", lambda: None)
    d.dispatch("B", lambda: None)
    d.dispatch("A", lambda: None)  # same key, no new worker

    # Give workers a moment to register + drain.
    time.sleep(0.1)
    h = d.health()
    assert h["worker_count"] == 2, h
    assert h["dispatched"] == 3
    assert d.shutdown(timeout=2.0)
