"""Async per-key event dispatcher with size-1 coalescing.

Motivation (T0.3 in the audit backlog):

``METARPoller`` calls ``on_result(changes)`` synchronously on the poller
thread. ``WeatherChangeHandler.__call__`` iterates every ``change`` and
invokes ``quoter.requote_city(...)`` inline — which makes several
authenticated Kalshi API calls (cancel + post + post). If a single
requote for KXHIGHNY takes 6s, the poller's 30s cadence for *every other
station* slips by 6s. A burst of changes across five cities on the same
poll tick would serialize behind each other and blow the cadence budget
for 30+ seconds.

This module hands those slow callbacks off to per-key worker threads:

* Events are dispatched by ``key`` (series / city). Same-key events
  serialize (never two concurrent requotes for the same city — that's
  the invariant we already rely on inside ``WeatherQuoter``).
* Different-key events run in parallel on their own workers, so a slow
  KXHIGHNY requote does not delay KXHIGHAUS.
* Each worker has a size-1 coalescing slot: if a newer event arrives
  while one is already pending for the same key, the older one is
  discarded. We only care about latest-state; stale requotes waste an
  API round-trip.
* ``dispatch(key, fn)`` is non-blocking — the poller returns in
  microseconds regardless of downstream latency.

Not used for fairness / ordering across keys (there is none by design),
and not a general-purpose thread pool. Keep it single-purpose so the
invariants stay obvious.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class _Worker:
    """Internal per-key worker. One thread, one size-1 pending slot.

    State transitions under a single Condition. That's important because
    the naive Event-based design has a race where a pending stop signal
    can be consumed by the worker's own post-run pop, leaving the worker
    blocked forever. With Condition, the predicate (``slot or stop``) is
    checked atomically with respect to notify, so the worker cannot
    miss a stop.
    """

    __slots__ = (
        "key", "_slot", "_cond", "_stop_flag",
        "_thread", "_owner", "processed", "coalesced", "errors",
        "_last_error",
    )

    def __init__(self, key: str, owner: "AsyncEventDispatcher") -> None:
        self.key = key
        self._slot: Optional[Callable[[], None]] = None
        self._cond = threading.Condition()
        self._stop_flag = False
        self._owner = owner
        self.processed = 0
        self.coalesced = 0
        self.errors = 0
        self._last_error: Optional[str] = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"dispatch-{owner.name}-{key}",
            daemon=True,
        )
        self._thread.start()

    def submit(self, fn: Callable[[], None]) -> bool:
        """Place ``fn`` in the size-1 pending slot.

        Returns True if a previously pending callable was displaced
        (coalesced), False if the slot was empty.
        """
        with self._cond:
            displaced = self._slot is not None
            self._slot = fn
            self._cond.notify()
        return displaced

    def _run(self) -> None:
        # Invariant: drain any pending slot before honoring stop. A caller
        # that dispatched and then called shutdown() must still see their
        # callable run.
        while True:
            with self._cond:
                while self._slot is None and not self._stop_flag:
                    self._cond.wait()
                fn = self._slot
                self._slot = None
                if fn is None:
                    # woken by stop only, no pending work → exit cleanly
                    return
            # Run outside the lock so a long callback doesn't block
            # submit() on this same worker.
            try:
                fn()
                self.processed += 1
            except Exception as exc:
                self.errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "[dispatch-%s-%s] callback raised: %s",
                    self._owner.name, self.key, exc,
                )

    def stop(self, timeout: float) -> bool:
        with self._cond:
            self._stop_flag = True
            self._cond.notify()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()


class AsyncEventDispatcher:
    """Per-key dispatcher with size-1 coalescing.

    ``dispatch(key, fn)`` is O(1), never blocks the caller. Same-key
    callables run serially on a dedicated worker thread; different keys
    run concurrently.

    Workers are spawned lazily on first dispatch to a given key and live
    until ``shutdown()``.

    Intentionally *not* a general pool: no fairness, no priority, no
    cross-key ordering.
    """

    def __init__(self, name: str = "dispatcher") -> None:
        self.name = name
        self._workers: dict[str, _Worker] = {}
        self._workers_lock = threading.Lock()
        self._stopped = False
        self._dispatched = 0
        self._coalesced = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(self, key: str, fn: Callable[[], None]) -> None:
        """Hand ``fn`` off to the worker for ``key``.

        If the worker is currently running a previous callable and a
        newer one was already pending, the pending one is discarded
        (coalesced). The currently-running callable is never interrupted
        — only the *queued* callable is replaced.
        """
        if self._stopped:
            raise RuntimeError(
                f"AsyncEventDispatcher[{self.name}] is stopped"
            )
        worker = self._get_or_create_worker(key)
        displaced = worker.submit(fn)
        self._dispatched += 1
        if displaced:
            self._coalesced += 1
            worker.coalesced += 1

    def _get_or_create_worker(self, key: str) -> _Worker:
        # Fast path — no lock if worker exists.
        w = self._workers.get(key)
        if w is not None:
            return w
        with self._workers_lock:
            w = self._workers.get(key)
            if w is None:
                w = _Worker(key, owner=self)
                self._workers[key] = w
            return w

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Stop all workers, waiting up to ``timeout`` seconds total.

        Returns True if every worker exited cleanly.
        """
        self._stopped = True
        with self._workers_lock:
            workers = list(self._workers.values())
        if not workers:
            return True
        deadline = time.time() + timeout
        all_clean = True
        for w in workers:
            remaining = max(0.0, deadline - time.time())
            if not w.stop(timeout=remaining):
                all_clean = False
                logger.warning(
                    "[dispatch-%s-%s] did not stop within timeout",
                    self.name, w.key,
                )
        return all_clean

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Snapshot of dispatcher + per-worker stats."""
        with self._workers_lock:
            workers_snapshot = [
                {
                    "key": w.key,
                    "alive": w._thread.is_alive(),
                    "processed": w.processed,
                    "coalesced": w.coalesced,
                    "errors": w.errors,
                    "last_error": w._last_error,
                }
                for w in self._workers.values()
            ]
        return {
            "name": self.name,
            "stopped": self._stopped,
            "worker_count": len(workers_snapshot),
            "dispatched": self._dispatched,
            "coalesced": self._coalesced,
            "workers": workers_snapshot,
        }
