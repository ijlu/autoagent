"""Database initialization, migrations, and persistent key-value cache.

Extracted from trade.py. This is the canonical source for all table schemas.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable, Optional, TypeVar

from bot.config import DB_PATH
from bot.daemon.locks import DB_WRITE_LOCK


# Global persistent connection (set by init_db, used by oneshot + daemon).
# Under the daemon, this single connection is shared by all threads.
# SQLite 3.11+ is thread-safe for a shared connection when built with
# threadsafe=1 (the default on macOS/Ubuntu). We additionally serialize
# WRITES through DB_WRITE_LOCK so no two threads hit "database is locked".
_PERSIST_CONN: Optional[sqlite3.Connection] = None

_T = TypeVar("_T")


def init_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Initialize database: create tables, run migrations, return connection.

    Daemon-ready: sets WAL journal mode (lock-free concurrent readers),
    synchronous=NORMAL (durable enough for our purposes, ~3x faster than FULL),
    and busy_timeout=5000ms so writers retry rather than raising immediately
    on lock contention. These pragmas persist for the lifetime of the DB file,
    but re-running is harmless.

    Args:
        db_path: Override path (for testing). Defaults to config.DB_PATH.

    Returns:
        sqlite3.Connection with all tables ready.
    """
    global _PERSIST_CONN
    path = db_path or DB_PATH
    # check_same_thread=False because the daemon shares one connection across
    # poller threads and the cycle-runner thread. Safety is preserved by
    # serializing writes via DB_WRITE_LOCK (see db_write() helper below).
    conn = sqlite3.connect(path, check_same_thread=False)

    # ── Daemon pragmas ─────────────────────────────────────────────────
    # WAL: lock-free concurrent readers, single-writer semantics. Survives
    # across connections (PRAGMA is persistent on-disk).
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is durable against app crashes, sacrifices durability only
    # against OS-level crashes. Acceptable for trading bot (we replay
    # last-known state from Kalshi API on restart anyway).
    conn.execute("PRAGMA synchronous=NORMAL")
    # 5-second busy_timeout: if another writer holds the lock, retry for
    # this long before raising. Prevents transient sqlite3.OperationalError.
    conn.execute("PRAGMA busy_timeout=5000")
    # Foreign keys off by default in SQLite; we don't use them but make it
    # explicit so future schema changes don't silently enable them.
    conn.execute("PRAGMA foreign_keys=OFF")

    # ── Core trading tables ──
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, ticker TEXT, side TEXT,
        action TEXT, score REAL, reason TEXT, strategy TEXT, price_cents INTEGER,
        contracts INTEGER, volume REAL, spread_cents REAL, independent_prob REAL,
        market_prob REAL, edge REAL, dry_run INTEGER, order_id TEXT, error TEXT,
        fill_status TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT, order_id TEXT UNIQUE,
        ticker TEXT, side TEXT, price_cents INTEGER, contracts INTEGER,
        revenue_cents INTEGER, profit_cents INTEGER, won INTEGER,
        volume REAL, spread_cents REAL, strategy TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, balance_cents INTEGER,
        portfolio_cents INTEGER, markets_scanned INTEGER, opportunities_found INTEGER,
        orders_attempted INTEGER, positions_managed INTEGER, orders_pruned INTEGER,
        dry_run INTEGER, halted INTEGER, halt_reason TEXT, patterns_avoided TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS position_exits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, ticker TEXT, side TEXT,
        entry_price_cents INTEGER, exit_price_cents INTEGER, contracts INTEGER,
        exit_reason TEXT, order_id TEXT, error TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS position_health_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        ticker TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER,
        health_score REAL, remaining_edge REAL, edge_trend REAL,
        action TEXT, exit_qty INTEGER,
        settlement_result TEXT DEFAULT NULL,
        settlement_pnl_cents INTEGER DEFAULT NULL)""")

    # ── Learning tables ──
    conn.execute("""CREATE TABLE IF NOT EXISTS calibration (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT, ticker TEXT,
        estimated_prob REAL, actual_outcome INTEGER,
        source_desc TEXT, n_sources INTEGER, bucket TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        entry_type TEXT, category TEXT, title TEXT, detail TEXT,
        metric_value REAL, metric_name TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS loss_postmortems (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT, ticker TEXT, category TEXT, loss_type TEXT,
        source_combo TEXT, estimated_prob REAL, market_prob REAL,
        edge_at_entry REAL, price_at_settlement REAL, detail TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS pipeline_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        source TEXT, status TEXT,
        markets_attempted INTEGER, markets_returned INTEGER,
        avg_latency_ms REAL, error_rate REAL, detail TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS edge_convergence (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        ticker TEXT, side TEXT,
        our_estimate REAL, market_price_at_entry REAL,
        market_price_after_1h REAL, market_price_after_6h REAL,
        market_price_after_24h REAL,
        converged INTEGER, convergence_pct REAL)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS timing_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT, hour_utc INTEGER, day_of_week INTEGER,
        category TEXT, source TEXT,
        edge REAL, won INTEGER, profit_cents INTEGER)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS hyperparam_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        param_name TEXT, current_value REAL, shadow_value REAL,
        ticker TEXT, actual_contracts INTEGER, shadow_contracts INTEGER,
        actual_profit REAL, shadow_profit REAL)""")

    # ── Market making tables ──
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        ticker TEXT, side TEXT, price_cents INTEGER, contracts INTEGER,
        order_id TEXT, status TEXT DEFAULT 'posted', fill_qty INTEGER DEFAULT 0,
        fair_value_cents INTEGER, inventory_at_post INTEGER,
        tag TEXT DEFAULT 'mm_v1')""")

    conn.execute("""CREATE TABLE IF NOT EXISTS mm_inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, updated_at TEXT,
        ticker TEXT UNIQUE, net_position INTEGER DEFAULT 0,
        total_bought INTEGER DEFAULT 0, total_sold INTEGER DEFAULT 0,
        realized_pnl_cents INTEGER DEFAULT 0,
        avg_entry_cents REAL DEFAULT 0,
        first_fill_at TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS mm_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        markets_quoted INTEGER, orders_posted INTEGER, orders_cancelled INTEGER,
        fills_detected INTEGER, inventory_value_cents INTEGER,
        realized_pnl_cents INTEGER, unrealized_pnl_cents INTEGER)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS mm_processed_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT, ticker TEXT, side TEXT,
        price_cents INTEGER, contracts INTEGER, fee_cents INTEGER,
        UNIQUE(order_id, ticker, side, price_cents))""")

    # ── Persistent key-value cache ──
    conn.execute("""CREATE TABLE IF NOT EXISTS kv_cache (
        key TEXT PRIMARY KEY, value TEXT, expires_at REAL)""")

    # ── Learned config (self-improvement storage) ──
    conn.execute("""CREATE TABLE IF NOT EXISTS learned_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        param_name TEXT UNIQUE,
        value TEXT,
        updated_at TEXT,
        evidence TEXT,
        previous_value TEXT,
        version INTEGER DEFAULT 1)""")

    # ── Opportunity log (AUDIT #6: log ALL candidates, traded + rejected) ──
    # Superset schema: includes both trade.py columns (source_count, sources_json)
    # and bot/db columns (source_estimates, four_factor_scores, regime, rank)
    conn.execute("""CREATE TABLE IF NOT EXISTS opportunity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT DEFAULT (datetime('now')),
        ticker TEXT,
        strategy TEXT,
        action TEXT,
        side TEXT,
        ensemble_prob REAL,
        market_prob REAL,
        edge REAL,
        source_count INTEGER,
        sources_json TEXT,
        source_estimates TEXT,
        four_factor_scores TEXT,
        regime TEXT,
        rank INTEGER,
        skip_reason TEXT,
        outcome TEXT)""")

    # ── Weather ensemble (Phase 2) ──
    # Per-source weights for the weather sub-ensemble. Nightly calibration
    # job updates these from strategy_journal settlements; defaults come
    # from bot/signals/weather_ensemble.DEFAULT_WEATHER_PRIORS.
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_source_weights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series TEXT NOT NULL,
        source TEXT NOT NULL,
        weight REAL NOT NULL,
        updated_at TEXT,
        n_samples INTEGER DEFAULT 0,
        UNIQUE(series, source))""")

    # Per-source component estimates logged per cycle for post-hoc
    # calibration. Joined against settlements to compute per-source Brier
    # and update weather_source_weights.
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_forecast_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT NOT NULL,
        series TEXT NOT NULL,
        ticker TEXT NOT NULL,
        source TEXT NOT NULL,
        forecast_prob REAL,
        forecast_high_f REAL,
        sigma_f REAL,
        hours_out INTEGER)""")

    # ── Decision log (full audit trail per decision) ──
    conn.execute("""CREATE TABLE IF NOT EXISTS decision_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        ticker TEXT,
        action TEXT,
        strategy TEXT,
        source_estimates TEXT,
        ensemble_prob REAL,
        market_prob REAL,
        four_factor TEXT,
        regime TEXT,
        active_feedback TEXT,
        outcome TEXT)""")

    # ── Migrations for existing tables (backward compat) ──
    _migrations = [
        ("trades", "action", "TEXT DEFAULT 'buy'"),
        ("trades", "score", "REAL DEFAULT 0"),
        ("trades", "strategy", "TEXT DEFAULT 'momentum'"),
        ("trades", "independent_prob", "REAL"),
        ("trades", "market_prob", "REAL"),
        ("trades", "edge", "REAL"),
        ("sessions", "positions_managed", "INTEGER DEFAULT 0"),
        ("sessions", "orders_pruned", "INTEGER DEFAULT 0"),
        ("sessions", "patterns_avoided", "TEXT DEFAULT '[]'"),
        ("trades", "fill_status", "TEXT"),
        ("mm_orders", "fill_qty", "INTEGER DEFAULT 0"),
        ("mm_orders", "tag", "TEXT DEFAULT 'mm_v1'"),
        ("mm_orders", "fair_value_cents", "INTEGER"),
        ("mm_orders", "inventory_at_post", "INTEGER"),
        ("mm_inventory", "avg_entry_cents", "REAL DEFAULT 0"),
        ("mm_inventory", "first_fill_at", "TEXT"),
        # opportunity_log backward compat columns
        ("opportunity_log", "source_count", "INTEGER"),
        ("opportunity_log", "sources_json", "TEXT"),
        ("opportunity_log", "source_estimates", "TEXT"),
        ("opportunity_log", "four_factor_scores", "TEXT"),
        ("opportunity_log", "regime", "TEXT"),
        ("opportunity_log", "rank", "INTEGER"),
    ]
    for table, col, coltype in _migrations:
        try:
            conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")

    conn.commit()
    _PERSIST_CONN = conn
    return conn


def check_startup_invariants(conn: sqlite3.Connection) -> list[str]:
    """Verify critical schema and data invariants at startup.

    Returns list of failure descriptions. Empty list = all OK.
    Called after init_db() to catch schema drift and missing migrations.
    """
    failures = []

    # Schema checks: critical columns must exist
    required_cols = {
        "mm_inventory": ["net_position", "avg_entry_cents", "first_fill_at"],
        "mm_orders": ["fill_qty", "fair_value_cents", "inventory_at_post", "tag"],
        "trades": ["fill_status", "independent_prob", "edge"],
        "settlements": ["won", "profit_cents"],
        "kv_cache": ["key", "value", "expires_at"],
        "position_health_log": ["health_score", "action", "exit_qty"],
    }
    for table, cols in required_cols.items():
        for col in cols:
            try:
                conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
            except Exception:
                failures.append(f"MISSING COLUMN: {table}.{col}")

    return failures


def get_connection() -> sqlite3.Connection:
    """Get the persistent connection. Must call init_db() first."""
    if _PERSIST_CONN is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _PERSIST_CONN


# ══════════════════════════════════════════════════════════════════════════════
# Thread-safe write helper (daemon-era)
# ══════════════════════════════════════════════════════════════════════════════

def db_write(fn: Callable[[sqlite3.Connection], _T], conn: Optional[sqlite3.Connection] = None) -> _T:
    """Run a write transaction under DB_WRITE_LOCK.

    Args:
        fn: callable taking a connection and returning some result. The callable
            is responsible for executing the write; this helper takes the lock,
            invokes fn, then commits atomically. On exception the transaction
            rolls back automatically (since COMMIT isn't reached).
        conn: optional connection; defaults to the persistent _PERSIST_CONN.

    WAL gives us lock-free READERS, so reads do NOT need to use this helper —
    call conn.execute(SELECT …).fetchall() directly. Reserve db_write() for
    INSERT/UPDATE/DELETE/DDL.

    Example:
        db_write(lambda c: c.execute("UPDATE x SET y=1 WHERE z=?", (z,)))
    """
    c = conn or _PERSIST_CONN
    if c is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    with DB_WRITE_LOCK:
        try:
            result = fn(c)
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


# ══════════════════════════════════════════════════════════════════════════════
# Key-value cache (persistent across oneshot runs)
# ══════════════════════════════════════════════════════════════════════════════

def kv_get(conn: sqlite3.Connection, key: str) -> Any:
    """Read from persistent kv_cache. Returns parsed JSON value or None if expired/missing.

    Lock-free under WAL — readers don't need DB_WRITE_LOCK.
    """
    try:
        row = conn.execute(
            "SELECT value, expires_at FROM kv_cache WHERE key=?", (key,)
        ).fetchone()
        if row and row[1] > time.time():
            return json.loads(row[0])
    except Exception as e:
        print(f"[kv_cache] get({key!r}) failed: {e}")
    return None


def kv_set(conn: sqlite3.Connection, key: str, value: Any, ttl_seconds: int) -> None:
    """Write to persistent kv_cache with TTL. Takes DB_WRITE_LOCK."""
    with DB_WRITE_LOCK:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time() + ttl_seconds),
            )
            conn.commit()
        except Exception as e:
            print(f"[kv_cache] set({key!r}) failed: {e}")


def kv_cleanup(conn: sqlite3.Connection) -> None:
    """Remove expired entries from kv_cache. Takes DB_WRITE_LOCK."""
    with DB_WRITE_LOCK:
        try:
            conn.execute("DELETE FROM kv_cache WHERE expires_at < ?", (time.time(),))
            conn.commit()
        except Exception as e:
            print(f"[kv_cache] cleanup failed: {e}")
