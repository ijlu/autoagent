"""Database initialization, migrations, and persistent key-value cache.

Extracted from trade.py. This is the canonical source for all table schemas.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from bot.config import DB_PATH


# Global persistent connection (set by init_db, used by oneshot architecture)
_PERSIST_CONN: Optional[sqlite3.Connection] = None


def init_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Initialize database: create tables, run migrations, return connection.

    Args:
        db_path: Override path (for testing). Defaults to config.DB_PATH.

    Returns:
        sqlite3.Connection with all tables ready.
    """
    global _PERSIST_CONN
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)

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
# Key-value cache (persistent across oneshot runs)
# ══════════════════════════════════════════════════════════════════════════════

def kv_get(conn: sqlite3.Connection, key: str) -> Any:
    """Read from persistent kv_cache. Returns parsed JSON value or None if expired/missing."""
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
    """Write to persistent kv_cache with TTL."""
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time() + ttl_seconds),
        )
        conn.commit()
    except Exception as e:
        print(f"[kv_cache] set({key!r}) failed: {e}")


def kv_cleanup(conn: sqlite3.Connection) -> None:
    """Remove expired entries from kv_cache."""
    try:
        conn.execute("DELETE FROM kv_cache WHERE expires_at < ?", (time.time(),))
        conn.commit()
    except Exception as e:
        print(f"[kv_cache] cleanup failed: {e}")
