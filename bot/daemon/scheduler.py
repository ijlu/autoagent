"""Periodic task scheduler for the daemon.

The Phase-1 daemon has three kinds of concurrent work:
- Long-lived pollers (METAR, ZQ, ADP, …) — each owns its own thread.
- Periodic cycle work (housekeeping, settlements, learning, scoring).
- One-shot post-event handlers (requote on temperature change).

This module owns the periodic cycle work. Pollers don't go through
the scheduler — they self-manage their threading via the `Poller`
base class and register their lifecycle callbacks with the scheduler
only to be start()/stop()'d at daemon boot/shutdown.

Design notes:
- Built on the stdlib `sched.scheduler` so there are zero new
  dependencies. One scheduler instance running on a single "clock"
  thread fires registered tasks.
- Tasks are registered as `(fn, interval_s, name)`. When fired, each
  task runs synchronously on the scheduler thread. If a task takes
  longer than its interval, subsequent firings are delayed — this is
  the right behavior for the cycle task (don't overlap cycles) but
  means the scheduler is NOT the right place to run slow I/O.
  Pollers get their own threads for that reason.
- Every task is wrapped in a try/except. A task crash logs and
  reschedules normally — it does not take down the scheduler.
- SIGTERM/SIGINT flip `_stop` and the run loop drains.
"""

from __future__ import annotations

import logging
import sched
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class _Task:
    name: str
    fn: Callable[[], None]
    interval_s: float
    next_run: float = 0.0
    run_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    last_run_duration_s: float = 0.0
    # Set when the task should stop firing (e.g. after one-shot tasks)
    cancelled: bool = False


class Scheduler:
    """Single-threaded periodic scheduler.

    Typical use:

        sched = Scheduler()
        sched.register("cycle", run_cycle, interval_s=60)
        sched.register("kv_cleanup", kv_cleanup_task, interval_s=3600)
        sched.run_forever()   # blocks until SIGTERM
    """

    def __init__(self) -> None:
        self._sched = sched.scheduler(time.monotonic, time.sleep)
        self._tasks: dict[str, _Task] = {}
        self._tasks_lock = threading.Lock()
        self._stop = threading.Event()
        self._on_start_hooks: list[Callable[[], None]] = []
        self._on_stop_hooks: list[Callable[[], None]] = []
        self._sigterm_registered = False

    # ------------------------------------------------------------------
    # Task registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        fn: Callable[[], None],
        interval_s: float,
        initial_delay_s: float = 0.0,
    ) -> None:
        """Register a periodic task.

        Args:
            name: unique identifier used in logs and health reports.
            fn: zero-arg callable. Exceptions are caught and logged.
            interval_s: seconds between successive fire times, measured
                from the START of one firing to the START of the next.
                Overrun behavior: if fn takes longer than interval_s,
                the next fire happens immediately (no artificial lag).
            initial_delay_s: delay before the first fire. Defaults to 0
                (fire ASAP after run_forever() starts).
        """
        with self._tasks_lock:
            if name in self._tasks:
                raise ValueError(f"Task {name!r} already registered")
            task = _Task(
                name=name,
                fn=fn,
                interval_s=interval_s,
                next_run=time.monotonic() + initial_delay_s,
            )
            self._tasks[name] = task
        logger.info(
            "[scheduler] registered %s (interval=%ss, initial_delay=%ss)",
            name, interval_s, initial_delay_s,
        )

    def cancel(self, name: str) -> bool:
        """Mark a task as cancelled. Returns True if found."""
        with self._tasks_lock:
            task = self._tasks.get(name)
            if task is None:
                return False
            task.cancelled = True
        return True

    def on_start(self, fn: Callable[[], None]) -> None:
        """Register a callback to run once at scheduler startup (before
        any tasks fire). Useful for starting pollers."""
        self._on_start_hooks.append(fn)

    def on_stop(self, fn: Callable[[], None]) -> None:
        """Register a callback to run once at scheduler shutdown (after
        the last task completes). Useful for stopping pollers and
        closing DB connections."""
        self._on_stop_hooks.append(fn)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def run_forever(self, install_signal_handlers: bool = True) -> None:
        """Block, firing registered tasks at their scheduled intervals.
        Returns when stop() is called or SIGTERM/SIGINT is received.
        """
        if install_signal_handlers and not self._sigterm_registered:
            self._install_signal_handlers()

        logger.info("[scheduler] running %d on_start hooks", len(self._on_start_hooks))
        for hook in self._on_start_hooks:
            try:
                hook()
            except Exception:
                logger.exception("[scheduler] on_start hook raised")

        logger.info(
            "[scheduler] entering run loop with %d tasks: %s",
            len(self._tasks), [t.name for t in self._tasks.values()],
        )

        # Prime the scheduler with each task's first firing.
        with self._tasks_lock:
            for task in self._tasks.values():
                self._sched.enterabs(task.next_run, 1, self._fire, argument=(task,))

        # Loop: process pending scheduler events in batches, checking
        # _stop between them so SIGTERM triggers a prompt exit.
        while not self._stop.is_set():
            # sched.run(blocking=False) returns the deadline of the next
            # event, or None if queue is empty.
            next_deadline = self._sched.run(blocking=False)
            if next_deadline is None:
                # Nothing scheduled (all tasks cancelled). Exit.
                logger.warning("[scheduler] no more scheduled events — exiting")
                break
            wait = max(0.0, next_deadline - time.monotonic())
            # Bounded wait so we recheck _stop periodically even if no
            # events fire for a long time.
            self._stop.wait(min(wait, 1.0))

        logger.info("[scheduler] stop requested, running %d on_stop hooks", len(self._on_stop_hooks))
        for hook in self._on_stop_hooks:
            try:
                hook()
            except Exception:
                logger.exception("[scheduler] on_stop hook raised")

    def stop(self) -> None:
        """Signal the scheduler to exit its run loop."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fire(self, task: _Task) -> None:
        """Run a single task and reschedule its next firing."""
        if task.cancelled or self._stop.is_set():
            return

        start = time.monotonic()
        try:
            task.fn()
            task.run_count += 1
        except Exception as exc:
            task.error_count += 1
            task.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[scheduler] task %s raised: %s", task.name, exc)
        task.last_run_duration_s = time.monotonic() - start

        # Schedule next firing.
        if not task.cancelled and not self._stop.is_set():
            # Fire at start + interval. If we've already blown past that
            # (task took longer than interval), schedule immediately.
            task.next_run = max(time.monotonic(), start + task.interval_s)
            self._sched.enterabs(task.next_run, 1, self._fire, argument=(task,))

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            sig_name = signal.Signals(signum).name
            logger.info("[scheduler] received %s — initiating shutdown", sig_name)
            self.stop()

        # Only works if called on the main thread (signal module restriction).
        try:
            signal.signal(signal.SIGTERM, handler)
            signal.signal(signal.SIGINT, handler)
            self._sigterm_registered = True
        except ValueError:
            # Not running on main thread — fall through, caller must
            # arrange their own shutdown (e.g. in tests).
            logger.debug("[scheduler] signal handlers skipped (not main thread)")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Snapshot of scheduler state for status reporting."""
        with self._tasks_lock:
            return {
                "stopped": self._stop.is_set(),
                "tasks": {
                    name: {
                        "run_count": t.run_count,
                        "error_count": t.error_count,
                        "last_error": t.last_error,
                        "last_run_duration_s": round(t.last_run_duration_s, 3),
                        "cancelled": t.cancelled,
                    }
                    for name, t in self._tasks.items()
                },
            }
