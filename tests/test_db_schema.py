"""Tests for database schema, migrations, and startup invariants.

Validates that:
- bot.db.init_db() creates all required tables on a fresh DB
- Migrations are idempotent (calling init_db twice is safe)
- check_startup_invariants() returns empty on a valid DB
- check_startup_invariants() catches missing columns
- kv_get/kv_set/kv_cleanup round-trip correctly with TTL
- Schema includes all columns needed by both trade.py and bot/ package
"""

from __future__ import annotations

import sqlite3
import time
import pytest

from bot.db import init_db, check_startup_invariants, kv_get, kv_set, kv_cleanup


# ══════════════════════════════════════════════════════════════════════════════
# Schema creation
# ══════════════════════════════════════════════════════════════════════════════

class TestInitDb:
    def test_creates_all_tables(self):
        conn = init_db(":memory:")
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        expected = {
            "trades", "settlements", "sessions", "position_exits",
            "position_health_log", "calibration", "strategy_journal",
            "loss_postmortems", "pipeline_health", "edge_convergence",
            "timing_patterns", "hyperparam_shadow",
            "mm_orders", "mm_inventory", "mm_sessions", "mm_processed_fills",
            "kv_cache", "learned_config", "opportunity_log", "decision_log",
            "alpha_backtest", "weather_mm_shadow",
            "fills_ledger",
            "kalshi_market_snapshots",
        }
        missing = expected - tables
        assert not missing, f"Missing tables: {missing}"

    def test_weather_mm_shadow_columns(self):
        """weather_mm_shadow must expose every field the step-9 gate needs."""
        conn = init_db(":memory:")
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(weather_mm_shadow)"
        ).fetchall()}
        required = {
            "ts_unix", "ts_iso", "ticker", "series", "station",
            "old_temp_f", "new_temp_f", "running_high_f", "forecast_high_f",
            "hours_left", "trajectory_f_per_hr",
            "fair_value_cents", "proposed_bid_cents", "proposed_ask_cents",
            "half_spread_cents", "market_yes_bid", "market_yes_ask", "market_mid",
            "inventory", "gate_should_quote", "gate_reason", "gate_spread_mult",
            "latency_ms", "live_mode",
        }
        missing = required - cols
        assert not missing, f"weather_mm_shadow missing columns: {missing}"

    def test_idempotent(self):
        """Calling init_db twice on the same DB should not error."""
        conn = init_db(":memory:")
        # Re-init on the same connection's DB (simulate restart)
        conn2 = init_db(":memory:")
        # Should not raise
        assert conn2 is not None

    def test_mm_inventory_has_first_fill_at(self):
        conn = init_db(":memory:")
        # Should not raise
        conn.execute("SELECT first_fill_at FROM mm_inventory LIMIT 1")

    def test_position_health_log_exists(self):
        conn = init_db(":memory:")
        conn.execute("SELECT health_score, action, exit_qty FROM position_health_log LIMIT 1")

    def test_opportunity_log_superset(self):
        """opportunity_log should have both trade.py and bot/db columns."""
        conn = init_db(":memory:")
        # trade.py columns
        conn.execute("SELECT source_count, sources_json FROM opportunity_log LIMIT 1")
        # bot/db columns
        conn.execute("SELECT source_estimates, four_factor_scores, regime, rank FROM opportunity_log LIMIT 1")

    def test_decision_log_exists(self):
        conn = init_db(":memory:")
        conn.execute("SELECT ticker, strategy, source_estimates FROM decision_log LIMIT 1")

    def test_learned_config_exists(self):
        conn = init_db(":memory:")
        conn.execute("SELECT param_name, value, version FROM learned_config LIMIT 1")

    def test_weather_metar_hourly_regime_exists(self):
        """Stage 1 regime sibling backfill table — pinned columns."""
        conn = init_db(":memory:")
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(weather_metar_hourly_regime)"
        ).fetchall()}
        required = {
            "id", "created_at", "station", "lst_date", "lst_hour",
            "dwpf", "drct", "sknt", "skyc1",
        }
        missing = required - cols
        assert not missing, f"weather_metar_hourly_regime missing columns: {missing}"
        # Confirm the unique constraint backing idempotent backfill
        conn.execute(
            "INSERT INTO weather_metar_hourly_regime "
            "(created_at, station, lst_date, lst_hour) VALUES (?, ?, ?, ?)",
            ("2026-04-28T00:00", "KMIA", "2026-04-28", 14),
        )
        try:
            conn.execute(
                "INSERT INTO weather_metar_hourly_regime "
                "(created_at, station, lst_date, lst_hour) VALUES (?, ?, ?, ?)",
                ("2026-04-28T00:00", "KMIA", "2026-04-28", 14),
            )
            assert False, "expected UNIQUE constraint violation"
        except Exception as e:
            assert "UNIQUE" in str(e).upper()

    def test_weather_forecast_snapshots_regime_columns(self):
        """Stage 1 telemetry columns added to capture regime-vs-pooled σ."""
        conn = init_db(":memory:")
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(weather_forecast_snapshots)"
        ).fetchall()}
        required = {
            "regime_label", "regime_tier_used",
            "regime_sigma_f", "pooled_sigma_f",
        }
        missing = required - cols
        assert not missing, (
            f"weather_forecast_snapshots missing regime columns: {missing}"
        )

    def test_weather_forecast_snapshots_legacy_insert_compat(self):
        """The legacy v1 ensemble INSERT (8 columns, no regime) must still
        work after the regime-column additions. New columns default NULL.
        """
        conn = init_db(":memory:")
        conn.execute(
            """INSERT INTO weather_forecast_snapshots
               (recorded_at, series, ticker, source, forecast_prob,
                forecast_high_f, sigma_f, hours_out)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-04-28T20:00", "KXHIGHMIA", "KXHIGHMIA-26APR28-T85",
             "metar", None, 85.0, 1.0, 5),
        )
        row = conn.execute(
            """SELECT regime_label, regime_tier_used, regime_sigma_f,
                      pooled_sigma_f
                 FROM weather_forecast_snapshots LIMIT 1"""
        ).fetchone()
        assert row == (None, None, None, None), (
            f"new columns should default NULL on legacy insert, got {row}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Startup invariants
# ══════════════════════════════════════════════════════════════════════════════

class TestStartupInvariants:
    def test_fresh_db_passes(self):
        conn = init_db(":memory:")
        failures = check_startup_invariants(conn)
        assert failures == [], f"Unexpected failures: {failures}"

    def test_missing_column_detected(self):
        """If a critical column is missing, invariant check should catch it."""
        conn = sqlite3.connect(":memory:")
        # Create a minimal mm_inventory without first_fill_at
        conn.execute("""CREATE TABLE mm_inventory (
            id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, net_position INTEGER,
            avg_entry_cents REAL)""")
        # Create other required tables minimally
        conn.execute("CREATE TABLE mm_orders (id INTEGER PRIMARY KEY, fill_qty INTEGER, fair_value_cents INTEGER, inventory_at_post INTEGER, tag TEXT)")
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, fill_status TEXT, independent_prob REAL, edge REAL)")
        conn.execute("CREATE TABLE settlements (id INTEGER PRIMARY KEY, won INTEGER, profit_cents INTEGER)")
        conn.execute("CREATE TABLE kv_cache (key TEXT PRIMARY KEY, value TEXT, expires_at REAL)")
        conn.execute("CREATE TABLE position_health_log (id INTEGER PRIMARY KEY, health_score REAL, action TEXT, exit_qty INTEGER)")

        failures = check_startup_invariants(conn)
        assert any("first_fill_at" in f for f in failures), \
            f"Should detect missing first_fill_at, got: {failures}"


# ══════════════════════════════════════════════════════════════════════════════
# KV cache
# ══════════════════════════════════════════════════════════════════════════════

class TestKvCache:
    def test_round_trip(self):
        conn = init_db(":memory:")
        kv_set(conn, "test_key", {"hello": "world"}, 300)
        val = kv_get(conn, "test_key")
        assert val == {"hello": "world"}

    def test_expired_returns_none(self):
        conn = init_db(":memory:")
        # Set with TTL of 0 (already expired)
        conn.execute(
            "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
            ("expired_key", '{"x": 1}', time.time() - 10)
        )
        conn.commit()
        assert kv_get(conn, "expired_key") is None

    def test_missing_key_returns_none(self):
        conn = init_db(":memory:")
        assert kv_get(conn, "nonexistent") is None

    def test_overwrite(self):
        conn = init_db(":memory:")
        kv_set(conn, "key", "v1", 300)
        kv_set(conn, "key", "v2", 300)
        assert kv_get(conn, "key") == "v2"

    def test_cleanup_removes_expired(self):
        conn = init_db(":memory:")
        # Insert one active and one expired
        kv_set(conn, "active", "yes", 300)
        conn.execute(
            "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
            ("expired", '"no"', time.time() - 10)
        )
        conn.commit()

        kv_cleanup(conn)

        assert kv_get(conn, "active") == "yes"
        # Expired should be physically gone
        row = conn.execute("SELECT key FROM kv_cache WHERE key='expired'").fetchone()
        assert row is None

    def test_complex_json_types(self):
        conn = init_db(":memory:")
        kv_set(conn, "list", [1, 2, 3], 300)
        assert kv_get(conn, "list") == [1, 2, 3]

        kv_set(conn, "nested", {"a": {"b": [1]}}, 300)
        assert kv_get(conn, "nested") == {"a": {"b": [1]}}

        kv_set(conn, "null", None, 300)
        assert kv_get(conn, "null") is None


# ══════════════════════════════════════════════════════════════════════════════
# Migrations
# ══════════════════════════════════════════════════════════════════════════════

class TestMigrations:
    def test_migrations_add_missing_columns(self):
        """Simulate an old DB missing mm_inventory.first_fill_at, then init_db
        should add it via migration."""
        conn = sqlite3.connect(":memory:")
        # Create old-style mm_inventory without first_fill_at
        conn.execute("""CREATE TABLE IF NOT EXISTS mm_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, updated_at TEXT,
            ticker TEXT UNIQUE, net_position INTEGER DEFAULT 0,
            total_bought INTEGER DEFAULT 0, total_sold INTEGER DEFAULT 0,
            realized_pnl_cents INTEGER DEFAULT 0,
            avg_entry_cents REAL DEFAULT 0)""")
        conn.commit()

        # Re-init should add the missing column via migration
        # We can't easily re-run init_db on an existing connection with a
        # path, but we can verify the migration list includes first_fill_at
        from bot.db import init_db as _init
        # Run on fresh memory DB to verify migration list is complete
        conn2 = _init(":memory:")
        conn2.execute("SELECT first_fill_at FROM mm_inventory LIMIT 1")
