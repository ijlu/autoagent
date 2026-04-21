"""Database initialization, migrations, and persistent key-value cache.

Extracted from trade.py. This is the canonical source for all table schemas.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from typing import Any, Callable, Iterator, Optional, TypeVar

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
        settlement_pnl_cents INTEGER DEFAULT NULL,
        fresh_prob REAL DEFAULT NULL,
        fresh_source_count INTEGER DEFAULT NULL,
        entry_edge REAL DEFAULT NULL)""")
    # Idempotent backfill for DBs created before these columns existed.
    for _col, _type in (("fresh_prob", "REAL"),
                         ("fresh_source_count", "INTEGER"),
                         ("entry_edge", "REAL")):
        try:
            conn.execute(f"ALTER TABLE position_health_log ADD COLUMN {_col} {_type}")
        except Exception:
            pass

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

    # ── alpha_backtest (Phase 1 gate evaluation) ──
    # Atomic decision-time log for the Phase 0 gate's "beat market-mid by ≥0.005"
    # leg. Every decision (MM quote, directional shadow/live, weather shadow) writes
    # one row with ensemble estimate + raw market snapshot + (later) settlement.
    #
    # Raw market fields (yes_bid/ask/last_cents, spread, age) are stored in addition
    # to the canonical market_prob_yes so analysis can re-run the gate under multiple
    # definitions without re-collecting data. See bot/learning/alpha_log.py for the
    # resolution rules and market_prob_source tags.
    conn.execute("""CREATE TABLE IF NOT EXISTS alpha_backtest (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_decision TEXT NOT NULL,
        ts_decision_unix REAL NOT NULL,
        ticker TEXT NOT NULL,
        family TEXT,
        decision_type TEXT NOT NULL,
        decision_outcome TEXT NOT NULL,
        side TEXT,
        price_cents INTEGER,
        contracts INTEGER,
        skip_reason TEXT,
        ensemble_p_yes REAL NOT NULL,
        ensemble_confidence REAL,
        source_count INTEGER,
        sources_json TEXT,
        source_estimates_json TEXT,
        yes_bid_cents INTEGER,
        yes_ask_cents INTEGER,
        yes_last_cents INTEGER,
        last_trade_age_s REAL,
        spread_cents INTEGER,
        volume_fp INTEGER,
        market_prob_yes REAL,
        market_prob_source TEXT,
        ts_settle TEXT,
        ts_settle_unix REAL,
        settlement_result TEXT,
        won_yes INTEGER,
        realized_pnl_cents INTEGER,
        cycle_id TEXT,
        notes TEXT)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alpha_bt_ticker_ts "
        "ON alpha_backtest(ticker, ts_decision_unix)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alpha_bt_family_settle "
        "ON alpha_backtest(family, ts_settle_unix)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alpha_bt_type_outcome "
        "ON alpha_backtest(decision_type, decision_outcome)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alpha_bt_pending_settle "
        "ON alpha_backtest(ticker, side) WHERE ts_settle_unix IS NULL"
    )

    # ── Phase 1: weather MM shadow log ──
    # Every quote the event-driven WeatherQuoter WOULD have posted (when
    # WEATHER_MM_LIVE=false) is written here — with the FV, proposed bid/ask,
    # the market snapshot at shadow time, the gate decision, and the METAR
    # context that triggered the requote. The step-9 shadow-to-live gate joins
    # this against `settlements` to compute counterfactual P&L: did the +4.7¢
    # historical markout convert to realized P&L under the new cancel-replace
    # path? Flip to live only if yes.
    #
    # All prices are stored as YES-equivalent cents (per-side normalization,
    # CLAUDE.md bug pattern #13) so joins against `settlements` and
    # `opportunity_log` don't need side-aware CASE expressions.
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_mm_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_unix INTEGER NOT NULL,
        ts_iso TEXT NOT NULL,
        ticker TEXT NOT NULL,
        series TEXT NOT NULL,
        station TEXT NOT NULL,
        old_temp_f REAL,
        new_temp_f REAL,
        running_high_f REAL,
        forecast_high_f REAL,
        hours_left REAL,
        trajectory_f_per_hr REAL,
        fair_value_cents INTEGER,
        proposed_bid_cents INTEGER,
        proposed_ask_cents INTEGER,
        half_spread_cents INTEGER,
        market_yes_bid INTEGER,
        market_yes_ask INTEGER,
        market_mid INTEGER,
        inventory INTEGER,
        gate_should_quote INTEGER,
        gate_reason TEXT,
        gate_spread_mult REAL,
        latency_ms REAL,
        live_mode INTEGER NOT NULL DEFAULT 0)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wx_shadow_ticker_ts "
        "ON weather_mm_shadow(ticker, ts_unix)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wx_shadow_series_ts "
        "ON weather_mm_shadow(series, ts_unix)"
    )

    # ── Phase 1 step 9: shadow-to-live promotion event log ──
    # One row per state transition (shadow → canary → full and reverse).
    # Used for post-mortems and to preserve the history that kv_cache loses
    # on overwrite. `metrics_json` captures the decision-time stats
    # (brier beat, realized P&L, N, OOS split) so we can reproduce why a
    # family was promoted or demoted weeks later.
    conn.execute("""CREATE TABLE IF NOT EXISTS promotion_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_unix REAL NOT NULL,
        ts_iso TEXT NOT NULL,
        family TEXT NOT NULL,
        old_state TEXT NOT NULL,
        new_state TEXT NOT NULL,
        reason TEXT NOT NULL,
        trigger TEXT NOT NULL,
        metrics_json TEXT,
        manual INTEGER NOT NULL DEFAULT 0)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promo_family_ts "
        "ON promotion_events(family, ts_unix)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promo_trigger_ts "
        "ON promotion_events(trigger, ts_unix)"
    )

    # ── Phase 1 step 10: threshold-tuner proposal log (T.8) ──
    # Weekly tuner job writes one row per proposed threshold change. `applied`
    # flips when an operator (or, eventually, an auto-apply guard) merges the
    # proposal. The table is the forever-audit-log for "why did we move
    # min_pnl_floor from $30 to $35 on date X."
    conn.execute("""CREATE TABLE IF NOT EXISTS threshold_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_unix REAL NOT NULL,
        ts_iso TEXT NOT NULL,
        tuner TEXT NOT NULL,
        evidence_window_days INTEGER,
        n_observations INTEGER,
        current_thresholds_json TEXT NOT NULL,
        proposed_thresholds_json TEXT NOT NULL,
        objective_current REAL,
        objective_proposed REAL,
        objective_delta REAL,
        supporting_metrics_json TEXT,
        applied INTEGER NOT NULL DEFAULT 0,
        applied_ts_unix REAL,
        applied_by TEXT)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_threshold_proposals_tuner_ts "
        "ON threshold_proposals(tuner, ts_unix)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_threshold_proposals_applied "
        "ON threshold_proposals(applied, ts_unix)"
    )

    # ── T3.1: canonical fills ledger ──
    # Append-only, Kalshi-owned primary key (trade_id). One row per Kalshi
    # fill event. Every reader of "realized fill P&L" must read from here
    # rather than deriving from mm_processed_fills/weather_mm_shadow/
    # settlements. The scoping doc at reports/T3_FILLS_LEDGER_SCOPING.md
    # has the full rationale.
    #
    # Schema decisions (see scoping doc §7 open questions):
    #   Q6: ingested_ts_unix is first-write-only via INSERT OR IGNORE.
    #   Q7: no cycle_id column — we don't have a producer today, and
    #       weather-MM fills come from an event-driven path with no cycle.
    #   Q4: source values are drawn from a closed set of client_order_id
    #       prefix matches: mm_quote, safe_compounder, exit, directional,
    #       legacy, manual. Never null, never "unknown".
    conn.execute("""CREATE TABLE IF NOT EXISTS fills_ledger (
        -- Identity (immutable, Kalshi-owned)
        trade_id              TEXT    PRIMARY KEY,
        order_id              TEXT    NOT NULL,
        client_order_id       TEXT,
        ticker                TEXT    NOT NULL,
        series                TEXT    NOT NULL,
        family                TEXT    NOT NULL,

        -- Fill semantics
        side                  TEXT    NOT NULL,
        action                TEXT    NOT NULL,
        contracts             INTEGER NOT NULL,
        yes_price_cents       INTEGER NOT NULL,
        no_price_cents        INTEGER NOT NULL,
        is_taker              INTEGER NOT NULL,
        fee_cents             INTEGER NOT NULL,

        -- Time
        fill_ts_iso           TEXT    NOT NULL,
        fill_ts_unix          REAL    NOT NULL,
        ingested_ts_unix      REAL    NOT NULL,

        -- Write-time context (derived once, never updated)
        live_mode             INTEGER NOT NULL,
        source                TEXT    NOT NULL
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fills_ticker_ts "
        "ON fills_ledger(ticker, fill_ts_unix)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fills_order_id "
        "ON fills_ledger(order_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fills_series_ts "
        "ON fills_ledger(series, fill_ts_unix)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fills_family_ts "
        "ON fills_ledger(family, fill_ts_unix)"
    )

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
        # Phase 1: link learning rows back to their alpha_backtest source for
        # idempotent population. Legacy rows pass through as alpha_id IS NULL.
        ("calibration", "alpha_id", "INTEGER"),
        ("timing_patterns", "alpha_id", "INTEGER"),
        ("edge_convergence", "alpha_id", "INTEGER"),
        ("loss_postmortems", "alpha_id", "INTEGER"),
        # Phase 1 step 10: MM promotion gate needs per-shadow-quote fill
        # status and settlement P&L. Matcher populates shadow_bid_filled /
        # shadow_ask_filled from subsequent market snapshots; settlement
        # annotator fills shadow_pnl_cents + (if live) live_pnl_cents. T.6
        # paired-logging: live_pnl_cents is non-null on live-mode rows so
        # mm_promotion can compute realized/shadow ratio for fill-model
        # calibration monitoring.
        ("weather_mm_shadow", "ticker_settled_yes", "INTEGER"),
        ("weather_mm_shadow", "ts_settle_unix", "REAL"),
        ("weather_mm_shadow", "shadow_bid_filled", "INTEGER"),
        ("weather_mm_shadow", "shadow_bid_fill_ts_unix", "REAL"),
        ("weather_mm_shadow", "shadow_ask_filled", "INTEGER"),
        ("weather_mm_shadow", "shadow_ask_fill_ts_unix", "REAL"),
        ("weather_mm_shadow", "shadow_pnl_cents", "INTEGER"),
        ("weather_mm_shadow", "live_pnl_cents", "INTEGER"),
        ("weather_mm_shadow", "live_order_id_bid", "TEXT"),
        ("weather_mm_shadow", "live_order_id_ask", "TEXT"),
        ("weather_mm_shadow", "order_size", "INTEGER"),
        # T1.2: trigger attribution. `metar_change` = METAR delta ≥1°F
        # (legacy default), `time_decay` = sigma-shrink requote fired by
        # TimeDecayDriver, `forecast_change` = Open-Meteo forecast high
        # moved ≥1°F between refreshes. The step-9 shadow-to-live gate
        # breaks P&L out by reason so we can measure whether added
        # requotes earn edge or just burn fees.
        ("weather_mm_shadow", "trigger_reason", "TEXT DEFAULT 'metar_change'"),
    ]
    for table, col, coltype in _migrations:
        try:
            conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")

    # Partial unique indexes on alpha_id (null rows exempt, non-null must be unique)
    for tbl in ("calibration", "timing_patterns", "edge_convergence", "loss_postmortems"):
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{tbl}_alpha_id "
            f"ON {tbl}(alpha_id) WHERE alpha_id IS NOT NULL"
        )

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


@contextlib.contextmanager
def db_write_ctx(conn: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
    """Context-manager form of db_write() — holds DB_WRITE_LOCK for the whole
    `with` block, commits on clean exit, rolls back on exception.

    Prefer this over bare `conn.execute(...); conn.commit()` pairs in any
    daemon-shared connection context: two threads calling .execute() on the
    same connection object concurrently is not safe in sqlite3, and the lock
    must protect the full execute→commit region, not just the commit.

    Example:
        with db_write_ctx(conn):
            conn.execute("INSERT INTO settlements ...", row)
            conn.execute("UPDATE ... WHERE ...", params)
        # commit happens at `with` exit
    """
    c = conn or _PERSIST_CONN
    if c is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    with DB_WRITE_LOCK:
        try:
            yield c
            c.commit()
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
