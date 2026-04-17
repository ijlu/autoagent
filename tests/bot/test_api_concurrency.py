"""Concurrency tests for bot/api.py under the daemon's threaded use.

The oneshot era never had concurrent API callers — each cycle ran in a single
process. Phase 1 introduces poller threads + a cycle-runner thread, which
share bot.api's module-level state (_RATE_HISTORY dict, _CACHE TTLCache,
and the RSA-signing path). These tests prove the locks actually serialize
mutations and that TTLCache bounds work as expected.

Running with `pytest -n auto` (xdist) won't stress these — we need THREADS,
not processes. Use explicit threading inside each test.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from bot.api import _CACHE, _RATE_HISTORY, _sign, cached_get, rate_limit_wait
from bot.daemon.locks import API_LOCK


# ═════════════════════════════════════════════════════════════════════════════
# TTLCache bounds and concurrency
# ═════════════════════════════════════════════════════════════════════════════

def test_cache_is_bounded_ttlcache():
    """_CACHE must be a TTLCache, not an unbounded dict — a 30-day daemon
    with an unbounded _CACHE would OOM."""
    from cachetools import TTLCache
    assert isinstance(_CACHE, TTLCache), f"_CACHE is {type(_CACHE)}, not TTLCache"
    assert _CACHE.maxsize > 0
    assert _CACHE.ttl > 0


def test_cache_evicts_at_maxsize():
    """Filling cache past maxsize evicts oldest entries (LRU)."""
    _CACHE.clear()
    original_maxsize = _CACHE.maxsize
    # TTLCache LRU-evicts when full. Fill it past capacity and verify size bound.
    with API_LOCK:
        for i in range(original_maxsize + 50):
            _CACHE[f"key_{i}"] = {"val": i}
    assert len(_CACHE) <= original_maxsize, f"Cache grew to {len(_CACHE)}, exceeds {original_maxsize}"


def test_concurrent_cache_writes_dont_corrupt():
    """20 threads write 100 entries each. No exceptions, final state sane."""
    _CACHE.clear()
    errors = []

    def writer(tid: int):
        try:
            for i in range(100):
                key = f"t{tid}_k{i}"
                with API_LOCK:
                    _CACHE[key] = {"tid": tid, "i": i}
        except Exception as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(writer, range(20)))

    assert errors == [], f"Concurrent writes raised: {errors}"
    # Cache respected its bound regardless of how many keys were written
    assert len(_CACHE) <= _CACHE.maxsize


# ═════════════════════════════════════════════════════════════════════════════
# _sign() serialization
# ═════════════════════════════════════════════════════════════════════════════

def test_sign_takes_api_lock():
    """_sign() must acquire API_LOCK. Verified by holding the lock from
    another thread and confirming _sign() blocks until we release.

    We stub the full crypto path so the test doesn't need a real RSA key
    or the cryptography library to be importable.
    """
    import bot.api as api_mod

    class FakePK:
        def sign(self, msg, pad, hash):
            return b"fake_sig_bytes"

    class FakeMGF1:
        def __init__(self, *a, **kw): pass

    class FakePSS:
        MAX_LENGTH = 0
        def __init__(self, *a, **kw): pass

    class FakePaddingMod:
        PSS = FakePSS
        MGF1 = FakeMGF1

    class FakeHashesMod:
        class SHA256: pass

    sign_returned = threading.Event()

    def sign_task():
        try:
            _sign("GET", "/trade-api/v2/exchange/status")
        finally:
            sign_returned.set()

    with patch.object(api_mod, "_get_private_key", return_value=FakePK()), \
         patch.object(api_mod, "_ensure_crypto"), \
         patch.object(api_mod, "_padding", FakePaddingMod, create=True), \
         patch.object(api_mod, "_hashes", FakeHashesMod, create=True):

        # Acquire the lock from THIS thread, then kick off _sign() on another.
        # It should block until we release.
        API_LOCK.acquire()
        try:
            t = threading.Thread(target=sign_task, daemon=True)
            t.start()
            # Give the signer a chance to start and block
            time.sleep(0.1)
            assert not sign_returned.is_set(), \
                "_sign() returned while API_LOCK was held by another thread — lock not taken"
        finally:
            API_LOCK.release()

        # Now _sign() should be able to proceed
        t.join(timeout=2.0)
        assert sign_returned.is_set(), "_sign() never returned after lock was released"


def test_concurrent_signs_dont_deadlock():
    """20 threads each call _sign() 10 times — must complete within a few
    seconds (no deadlocks, no unbounded contention).

    Stubs the crypto path so the test runs without a real RSA key or the
    cryptography library being importable in the test env."""
    import bot.api as api_mod

    class FakePK:
        def sign(self, msg, pad, hash):
            return b"fake_sig_bytes"

    class FakeMGF1:
        def __init__(self, *a, **kw): pass

    class FakePSS:
        MAX_LENGTH = 0
        def __init__(self, *a, **kw): pass

    class FakePaddingMod:
        PSS = FakePSS
        MGF1 = FakeMGF1

    class FakeHashesMod:
        class SHA256: pass

    errors = []

    def signer(tid: int):
        try:
            for _ in range(10):
                try:
                    _sign("GET", f"/test/{tid}")
                except Exception as e:
                    errors.append(("unexpected", type(e).__name__, str(e)))
        except Exception as e:
            errors.append(("outer", type(e).__name__, str(e)))

    with patch.object(api_mod, "_get_private_key", return_value=FakePK()), \
         patch.object(api_mod, "_ensure_crypto"), \
         patch.object(api_mod, "_padding", FakePaddingMod, create=True), \
         patch.object(api_mod, "_hashes", FakeHashesMod, create=True):
        start = time.time()
        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(signer, range(20)))
        elapsed = time.time() - start

    assert errors == [], f"Concurrent signing raised: {errors[:3]}"
    assert elapsed < 10.0, f"200 concurrent signs took {elapsed:.1f}s — likely deadlock/contention"


# ═════════════════════════════════════════════════════════════════════════════
# rate_limit_wait() race — two threads must not both blast through burst limit
# ═════════════════════════════════════════════════════════════════════════════

def test_rate_limit_serializes_under_contention():
    """N threads hitting rate_limit_wait('polymarket…') simultaneously must
    queue serially rather than all bursting through. Polymarket config is
    (min_interval=0.5, max_burst=4), so 12 concurrent calls cannot complete
    in less than (12-4) * 0.5 = 4 seconds.
    """
    from bot.config import RATE_LIMITS
    min_interval, max_burst = RATE_LIMITS["polymarket"]

    # Clear _RATE_HISTORY for this domain
    with API_LOCK:
        _RATE_HISTORY.pop("polymarket", None)

    call_count = 12
    done_times = []
    lock = threading.Lock()

    def caller():
        rate_limit_wait("https://gamma-api.polymarket.com/markets")
        with lock:
            done_times.append(time.time())

    start = time.time()
    with ThreadPoolExecutor(max_workers=call_count) as pool:
        list(pool.map(lambda _: caller(), range(call_count)))
    elapsed = time.time() - start

    # Times should be monotonically non-decreasing (API_LOCK serializes
    # the read-decide-append inside rate_limit_wait, and the sleep happens
    # inside the lock so senders FIFO-queue).
    assert done_times == sorted(done_times), "Rate-limited calls returned out of order"

    # The limiter must have actually throttled. First `max_burst` calls can
    # go through quickly; each subsequent call must wait for a slot to free.
    # Lower bound: (call_count - max_burst) * min_interval, with some slack
    # for scheduler jitter on macOS.
    min_expected = (call_count - max_burst) * min_interval * 0.8
    assert elapsed >= min_expected, (
        f"{call_count} concurrent calls completed in {elapsed:.2f}s — "
        f"rate limiter didn't throttle (expected ≥{min_expected:.2f}s)"
    )
