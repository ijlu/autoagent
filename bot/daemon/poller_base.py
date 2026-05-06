"""Abstract base class for background pollers.

Every poller in the daemon — METAR weather observations, ZQ futures,
FedWatch probabilities, MADIS citizen stations — needs the same
lifecycle: run on its own daemon thread, fetch on a fixed cadence,
isolate exceptions so one bad fetch doesn't kill the thread, and
expose health stats for monitoring.

This module provides that lifecycle once.

Design notes:
- Concrete classes implement `_poll_once()`. They return whatever type
  they want; the base class doesn't care. An optional `on_result`
  callback receives each result for downstream event dispatch.
- The internal loop sleeps `interval_s` between calls, measured from
  the START of the previous call (NOT its end). That means a slow fetch
  eats into the interval — if `_poll_once()` takes 25s on a 30s
  interval, the next call fires 5s later, not 30s. If `_poll_once()`
  exceeds the interval, the next call fires immediately.
- Errors in `_poll_once()` are caught and logged. The loop keeps
  running unless `stop()` is called. This matches the Phase 1 plan
  requirement that one crashing poller doesn't take down the daemon.
- `stop()` is best-effort graceful. The thread is daemon=True so the
  process can exit cleanly even if a thread is stuck in a hanging
  socket read.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class Poller(ABC):
    """Abstract poller running on a background thread.

    Subclasses must set `name` and `interval_s` as class attrs, and
    implement `_poll_once()`.
    """

    #: Short human-readable identifier used in logs and health reports.
    name: str = "unnamed"

    #: Seconds between the START of successive poll calls. Not inclusive
    #: of the call duration itself.
    interval_s: float = 60.0

    def __init__(self, on_result: Optional[Callable[[Any], None]] = None) -> None:
        """
        Args:
            on_result: optional callback invoked with each `_poll_once()`
                return value. Called on the poller thread — keep it fast
                (sub-millisecond) or hand work off to a queue. If the
                callback raises, the exception is logged and swallowed
                (the poller keeps running).
        """
        self._on_result = on_result
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_poll_start: float = 0.0
        self._last_poll_end: float = 0.0
        self._poll_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    def _poll_once(self) -> Any:
        """Execute a single poll. Return whatever you want; the base
        class passes the return value to `on_result` if set.

        Exceptions raised here are caught, logged, and counted. The
        poller thread keeps running.
        """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background thread. Idempotent — calling twice is
        a no-op (logged)."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[%s] start() called on already-running poller", self.name)
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"poller-{self.name}", daemon=True
        )
        self._thread.start()
        logger.info("[%s] poller started (interval=%ss)", self.name, self.interval_s)

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal the poller to stop and wait up to `timeout` seconds
        for the thread to exit. Returns True if it exited cleanly.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            alive = self._thread.is_alive()
            if alive:
                logger.warning(
                    "[%s] stop() thread still alive after %ss", self.name, timeout
                )
            return not alive
        return True

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._last_poll_start = time.time()
            try:
                result = self._poll_once()
                self._poll_count += 1
                if self._on_result is not None and result is not None:
                    try:
                        self._on_result(result)
                    except Exception as cb_exc:
                        logger.exception(
                            "[%s] on_result callback raised: %s",
                            self.name, cb_exc,
                        )
            except Exception as exc:
                self._error_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("[%s] poll failed: %s", self.name, exc)
            self._last_poll_end = time.time()

            # Sleep until the next scheduled start. Use Event.wait so
            # stop() returns promptly instead of waiting out the full
            # interval.
            elapsed = self._last_poll_end - self._last_poll_start
            remaining = max(0.0, self.interval_s - elapsed)
            if self._stop.wait(remaining):
                break

    # ------------------------------------------------------------------
    # Health stats (for status logging)
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Return a snapshot of poller health for status reporting."""
        return {
            "name": self.name,
            "running": self.is_running(),
            "poll_count": self._poll_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_poll_age_s": (
                time.time() - self._last_poll_end
                if self._last_poll_end > 0
                else None
            ),
            "interval_s": self.interval_s,
        }
