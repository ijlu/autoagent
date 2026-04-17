"""WAL-mode and concurrency tests for bot/db.py.

Phase 1 moves the daemon from oneshot → persistent. That means:
- Multiple threads share one connection.
- Pollers read concurrently with the cycle-runner thread.
- kv_cache writes come from multiple threads.

WAL (`PRAGMA journal_mode=WAL`) gives us lock-free concurrent READERS
with single-writer semantics. We layer DB_WRITE_LOCK on top so two
threads never race the SQLite writer lock (which would manifest as
transient `sqlite3.OperationalError: database is locked` after the
5s busy_timeout).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from bot.daemon.locks import DB_WRITE_LOCK


@pytest.fixture
def tmp_db(monkeypatch):
    """Initialize a temp SQLite via init_db() and yield a namespace with
    `.conn` (the connection) and `.path` (the file path on disk).

    Cleans up the persistent connection after the test so state doesn't
    leak between tests."""
    import bot.db as db_mod

    # Use a file-backed DB (not ":memory:") because WAL mode requires a
    # disk file. tmpfile dir so it's isolated from kalshi_trades.db.
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    # Stash and restore the module-level connection so we don't trash
    # any live connection a concurrent test might hold.
    orig_conn = db_mod._PERSIST_CONN
    conn = None
    try:
        conn = db_mod.init_db(path)
        yield SimpleNamespace(conn=conn, path=path)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        db_mod._PERSIST_CONN = orig_conn
        for ext in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


# ═════════════════════════════════════════════════════════════════════════════
# WAL pragmas are set
# ═════════════════════════════════════════════════════════════════════════════

def test_wal_mode_enabled(tmp_db):
    """init_db must leave the DB in WAL journal mode with NORMAL sync and
    a non-zero busy_timeout. These are the daemon-era pragmas — a 30-day
    multi-threaded process with DELETE journal mode would hit locked-DB
    errors under every poller/cycle interleaving."""
    mode = tmp_db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"journal_mode is {mode}, expected wal"

    sync = tmp_db.conn.execute("PRAGMA synchronous").fetchone()[0]
    # NORMAL=1, FULL=2, OFF=0. We want NORMAL.
    assert sync == 1, f"synchronous is {sync}, expected 1 (NORMAL)"

    timeout_ms = tmp_db.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms >= 1000, f"busy_timeout is {timeout_ms}ms, expected ≥1000"


# ═════════════════════════════════════════════════════════════════════════════
# db_write() serializes writers
# ═════════════════════════════════════════════════════════════════════════════

def test_db_write_serializes_under_contention(tmp_db):
    """30 threads each insert 20 rows through db_write(). No rows lost,
    no sqlite3.OperationalError."""
    from bot.db import db_write

    errors = []

    def inserter(tid: int):
        try:
            for i in range(20):
                def do_insert(c, tid=tid, i=i):
                    c.execute(
                        "INSERT INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
                        (f"t{tid}_k{i}", f'"value_{tid}_{i}"', time.time() + 3600),
                    )
                db_write(do_insert, conn=tmp_db.conn)
        except Exception as e:
            errors.append((tid, type(e).__name__, str(e)))

    n_threads = 30
    n_per = 20
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(inserter, range(n_threads)))

    assert errors == [], f"Concurrent writes raised: {errors[:5]}"

    # All rows landed (no PRIMARY KEY conflicts because each thread's keys
    # are unique, and no transactions were rolled back).
    count = tmp_db.conn.execute("SELECT COUNT(*) FROM kv_cache").fetchone()[0]
    assert count == n_threads * n_per, f"Expected {n_threads * n_per} rows, got {count}"


def test_db_write_rolls_back_on_exception(tmp_db):
    """If the callable raises, db_write() must roll back and re-raise.
    Next call must see a clean state."""
    from bot.db import db_write

    # First, a successful write establishes a baseline row.
    db_write(lambda c: c.execute(
        "INSERT INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
        ("baseline", '"ok"', time.time() + 3600),
    ), conn=tmp_db.conn)

    # Now a write that raises mid-transaction.
    def failing(c):
        c.execute(
            "INSERT INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
            ("will_rollback", '"bad"', time.time() + 3600),
        )
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        db_write(failing, conn=tmp_db.conn)

    # The rollback should have undone the INSERT.
    row = tmp_db.conn.execute(
        "SELECT COUNT(*) FROM kv_cache WHERE key=?", ("will_rollback",)
    ).fetchone()
    assert row[0] == 0, "Failed write was not rolled back"

    # Baseline unaffected.
    row = tmp_db.conn.execute(
        "SELECT COUNT(*) FROM kv_cache WHERE key=?", ("baseline",)
    ).fetchone()
    assert row[0] == 1, "Successful earlier write was lost"


# ═════════════════════════════════════════════════════════════════════════════
# Concurrent readers are lock-free under WAL
# ═════════════════════════════════════════════════════════════════════════════

def test_concurrent_readers_dont_block_writers(tmp_db):
    """The whole point of WAL: readers and a single writer coexist.

    We start 10 reader threads continuously SELECTing, then run 50 writes
    and verify that:
      (a) writes complete within a small time budget (not blocked by readers)
      (b) readers observe monotonically-growing row counts (writes are visible)
      (c) no sqlite3.OperationalError fires
    """
    from bot.db import db_write

    errors = []
    stop_readers = threading.Event()
    # Per-thread observation lists so we can verify monotonicity within
    # each reader's own timeline (across threads counts interleave).
    per_reader_counts: list[list[int]] = []
    per_reader_lock = threading.Lock()

    def reader():
        my_counts: list[int] = []
        try:
            # Each reader opens its OWN connection — that's the realistic
            # pattern for pollers that only need read access. (The daemon
            # shares one conn across threads, but multi-conn readers are
            # also safe under WAL.)
            rconn = sqlite3.connect(tmp_db.path)
            try:
                while not stop_readers.is_set():
                    c = rconn.execute("SELECT COUNT(*) FROM kv_cache").fetchone()[0]
                    my_counts.append(c)
                    time.sleep(0.001)
            finally:
                rconn.close()
        except Exception as e:
            errors.append(("reader", type(e).__name__, str(e)))
        finally:
            with per_reader_lock:
                per_reader_counts.append(my_counts)

    # Spin up readers.
    reader_threads = [threading.Thread(target=reader, daemon=True) for _ in range(10)]
    for t in reader_threads:
        t.start()

    # Now hammer writes.
    start = time.time()
    for i in range(50):
        db_write(
            lambda c, i=i: c.execute(
                "INSERT INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
                (f"w_{i}", f'"v{i}"', time.time() + 3600),
            ),
            conn=tmp_db.conn,
        )
    write_elapsed = time.time() - start

    stop_readers.set()
    for t in reader_threads:
        t.join(timeout=2.0)

    assert errors == [], f"Reader/writer error: {errors[:3]}"

    # Writes should not have been starved by readers — 50 tiny inserts
    # with concurrent readers should finish in well under 10s.
    assert write_elapsed < 10.0, f"50 writes took {write_elapsed:.1f}s under read contention"

    # Final count matches.
    final = tmp_db.conn.execute("SELECT COUNT(*) FROM kv_cache").fetchone()[0]
    assert final == 50, f"Expected 50 rows, got {final}"

    # Readers observed at least some intermediate values (proves they ran
    # concurrently with writes, not only before or only after).
    total_obs = sum(len(r) for r in per_reader_counts)
    assert total_obs > 0, "Readers never observed any count"

    # Within each reader's timeline, counts are monotonically non-decreasing
    # (INSERT-only workload + WAL gives consistent snapshots per statement).
    for i, counts in enumerate(per_reader_counts):
        prev = -1
        for c in counts:
            assert c >= prev, f"Reader {i} saw count go backwards: {prev} → {c}"
            prev = c

    # At least one reader saw an intermediate count (not just 0 or 50) — proves
    # readers and writers were actually concurrent, not serialized.
    saw_intermediate = any(
        any(0 < c < 50 for c in counts) for counts in per_reader_counts
    )
    assert saw_intermediate, "No reader observed an intermediate count — not actually concurrent"
