"""Process-wide locks for the persistent daemon.

Phase 1 of the MM-deletion pivot (2026-04-17): the bot is transitioning from
oneshot (new process per cycle, shared-nothing concurrency) to a single
persistent process with multiple threads (pollers + cycle runner + trading
thread). That requires explicit locks around any mutable module-level state
that threads share.

Only three things actually need protection:

  API_LOCK           — bot/api.py's _RATE_HISTORY dict and _sign() signing
                       (the Kalshi clock-skew window is tight; concurrent
                       signers could race on the timestamp → signature pair).
                       Also guards the shared cachetools.TTLCache so the
                       'check-then-set' pattern in cached_get() is atomic.

  PIPELINE_STATS_LOCK — bot/signals/ensemble.py's _PIPELINE_STATS dict.
                       Different poller threads increment the same counters
                       (e.g. 'metar: 3 calls, 2 ok'), and the cycle runner
                       reads them to print the health summary. Cheap lock.

  DB_WRITE_LOCK      — serializes sqlite3 writes. WAL mode allows lock-free
                       concurrent READERS, but still has a single-writer
                       lock at the SQLite level with a busy_timeout. Taking
                       the lock in Python too means we never actually hit
                       busy_timeout, and avoids 'database is locked'
                       surprises when two threads both COMMIT a large
                       transaction.

Design notes:
  - All three are RLock so a thread can re-enter without deadlock (e.g.
    api_get() takes API_LOCK to sign, and cached_get() takes it to swap
    cache entries — if cached_get() ever calls api_get() we'd deadlock on
    a plain Lock).
  - These are module-level so they survive any per-cycle re-initialization.
  - Tests should acquire them explicitly to deterministically reproduce
    contention; see tests/bot/test_api_concurrency.py.

This module imports nothing beyond threading, which means it can be imported
from anywhere (no circular-import risk).
"""

from __future__ import annotations

import threading

# ───────────────────────────────────────────────────────────────────────────
# Global locks
# ───────────────────────────────────────────────────────────────────────────

API_LOCK: threading.RLock = threading.RLock()
PIPELINE_STATS_LOCK: threading.RLock = threading.RLock()
DB_WRITE_LOCK: threading.RLock = threading.RLock()


__all__ = ["API_LOCK", "PIPELINE_STATS_LOCK", "DB_WRITE_LOCK"]
