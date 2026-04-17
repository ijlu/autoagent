"""Tests for backtest_comprehensive.py.

Verifies all analysis functions work correctly with known test data.
"""

import sqlite3
import time
import json
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_comprehensive import (
    kalshi_maker_fee, kalshi_taker_fee,
    family_of, prob_bucket, wilson_ci, brier_score,
    analyze_calibration, analyze_edge_vs_winrate,
    analyze_source_accuracy, analyze_family_performance,
    analyze_mm_performance, analyze_loss_postmortems,
    analyze_opportunity_cost, analyze_timing,
    analyze_statistical_significance, analyze_inventory,
    analyze_learning_system,
)


@pytest.fixture
def conn():
    """Create in-memory DB with all required tables and test data."""
    c = sqlite3.connect(":memory:")

    # Create all tables
    c.execute("""CREATE TABLE settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT, order_id TEXT UNIQUE,
        ticker TEXT, side TEXT, price_cents INTEGER, contracts INTEGER,
        revenue_cents INTEGER, profit_cents INTEGER, won INTEGER,
        volume REAL, spread_cents REAL, strategy TEXT)""")

    c.execute("""CREATE TABLE mm_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        ticker TEXT, side TEXT, price_cents INTEGER, contracts INTEGER,
        order_id TEXT, status TEXT DEFAULT 'posted', fill_qty INTEGER DEFAULT 0,
        fair_value_cents INTEGER, inventory_at_post INTEGER,
        tag TEXT DEFAULT 'mm_v1')""")

    c.execute("""CREATE TABLE mm_inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, updated_at TEXT,
        ticker TEXT UNIQUE, net_position INTEGER DEFAULT 0,
        total_bought INTEGER DEFAULT 0, total_sold INTEGER DEFAULT 0,
        realized_pnl_cents INTEGER DEFAULT 0,
        avg_entry_cents REAL DEFAULT 0, first_fill_at TEXT)""")

    c.execute("""CREATE TABLE mm_processed_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT, ticker TEXT, side TEXT,
        price_cents INTEGER, contracts INTEGER, fee_cents INTEGER,
        UNIQUE(order_id, ticker, side, price_cents))""")

    c.execute("""CREATE TABLE opportunity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT DEFAULT (datetime('now')),
        ticker TEXT, strategy TEXT, action TEXT, side TEXT,
        ensemble_prob REAL, market_prob REAL, edge REAL,
        source_count INTEGER, sources_json TEXT,
        source_estimates TEXT, four_factor_scores TEXT,
        regime TEXT, rank INTEGER, skip_reason TEXT, outcome TEXT)""")

    c.execute("""CREATE TABLE calibration (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT, ticker TEXT,
        estimated_prob REAL, actual_outcome INTEGER,
        source_desc TEXT, n_sources INTEGER, bucket TEXT)""")

    c.execute("""CREATE TABLE loss_postmortems (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT, ticker TEXT, category TEXT, loss_type TEXT,
        source_combo TEXT, estimated_prob REAL, market_prob REAL,
        edge_at_entry REAL, price_at_settlement REAL, detail TEXT)""")

    c.execute("""CREATE TABLE timing_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT, hour_utc INTEGER, day_of_week INTEGER,
        category TEXT, source TEXT,
        edge REAL, won INTEGER, profit_cents INTEGER)""")

    c.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, ticker TEXT, side TEXT,
        action TEXT, score REAL, reason TEXT, strategy TEXT, price_cents INTEGER,
        contracts INTEGER, volume REAL, spread_cents REAL, independent_prob REAL,
        market_prob REAL, edge REAL, dry_run INTEGER, order_id TEXT, error TEXT,
        fill_status TEXT)""")

    c.execute("""CREATE TABLE edge_convergence (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        ticker TEXT, side TEXT, our_estimate REAL, market_price_at_entry REAL,
        market_price_after_1h REAL, market_price_after_6h REAL,
        market_price_after_24h REAL, converged INTEGER, convergence_pct REAL)""")

    c.execute("""CREATE TABLE hyperparam_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        param_name TEXT, current_value REAL, shadow_value REAL,
        ticker TEXT, actual_contracts INTEGER, shadow_contracts INTEGER,
        actual_profit REAL, shadow_profit REAL)""")

    c.execute("""CREATE TABLE pipeline_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        source TEXT, status TEXT,
        markets_attempted INTEGER, markets_returned INTEGER,
        avg_latency_ms REAL, error_rate REAL, detail TEXT)""")

    c.execute("""CREATE TABLE strategy_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        entry_type TEXT, category TEXT, title TEXT, detail TEXT,
        metric_value REAL, metric_name TEXT)""")

    c.execute("""CREATE TABLE position_health_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        ticker TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER,
        health_score REAL, remaining_edge REAL, edge_trend REAL,
        action TEXT, exit_qty INTEGER,
        settlement_result TEXT DEFAULT NULL,
        settlement_pnl_cents INTEGER DEFAULT NULL)""")

    c.execute("""CREATE TABLE kv_cache (key TEXT PRIMARY KEY, value TEXT, expires_at REAL)""")

    c.execute("""CREATE TABLE learned_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        param_name TEXT UNIQUE, value TEXT, updated_at TEXT,
        evidence TEXT, previous_value TEXT, version INTEGER DEFAULT 1)""")

    # ── Insert test data ──

    now = "2026-04-15T12:00:00"

    # Settlements: 20 trades, mix of wins and losses
    # KXFED: 10 trades, 4 wins (40% WR) → good family
    for i in range(10):
        won = 1 if i < 4 else 0
        profit = 200 if won else -150
        c.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"settle_KXFED-{i}", f"KXFED-{i}", "yes", 45, 10,
                   profit + 450 if won else 0, profit, won, 1000, 5.0, "mm:mm_v1"))

    # KXHIGH: 10 trades, 1 win (10% WR) → bad family
    for i in range(10):
        won = 1 if i == 0 else 0
        profit = 300 if won else -200
        c.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"settle_KXHIGH-{i}", f"KXHIGHNY-{i}", "yes", 50, 10,
                   profit + 500 if won else 0, profit, won, 500, 4.0, "mm:mm_v1"))

    # MM orders with fills and fair values
    for i in range(20):
        ticker = f"KXFED-{i % 10}"
        fv = 45 + (i % 5)
        fill_price = fv - 2 if i < 15 else fv + 3  # 75% favorable fills
        c.execute("INSERT INTO mm_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, ticker, "yes", fill_price, 10, f"ord_{i}",
                   "filled", 10, fv, 0, "mm_v1"))

    for i in range(20):
        ticker = f"KXHIGHNY-{i % 10}"
        fv = 50 + (i % 5)
        fill_price = fv + 3 if i < 14 else fv - 2  # 70% adverse fills
        c.execute("INSERT INTO mm_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, ticker, "yes", fill_price, 10, f"ord_wx_{i}",
                   "filled", 10, fv, 0, "mm_v1"))

    # Opportunity log — with ensemble probs and sources
    for i in range(10):
        ep = 0.45 + (i * 0.01)
        c.execute("INSERT INTO opportunity_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"KXFED-{i}", "mm", "candidate", "both",
                   ep, 0.50, abs(ep - 0.50), 3,
                   "ensemble(fedwatch+fred+clevfed)", None, None, None, None, None, None))

    for i in range(10):
        ep = 0.50 + (i * 0.02)
        c.execute("INSERT INTO opportunity_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"KXHIGHNY-{i}", "mm", "candidate", "both",
                   ep, 0.55, abs(ep - 0.55), 2,
                   "ensemble(weather+metar)", None, None, None, None, None, None))

    # Skipped opportunities
    for i in range(5):
        c.execute("INSERT INTO opportunity_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"KXSKIP-{i}", "mm", "skip", "both",
                   0.50, 0.50, 0.02, 1, "ensemble(llm)", None, None, None, None,
                   "edge_too_low", None))

    # Calibration data
    for i in range(20):
        ep = 0.3 + (i * 0.02)  # 0.30 to 0.68
        outcome = 1 if ep > 0.50 else 0  # Perfectly calibrated above 50%
        c.execute("INSERT INTO calibration VALUES (?,?,?,?,?,?,?,?)",
                  (None, now, f"CAL-{i}", ep, outcome, "test", 3, prob_bucket(ep)))

    # Loss postmortems
    for i in range(8):
        c.execute("INSERT INTO loss_postmortems VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"loss_{i}", f"KXHIGHNY-{i}", "weather",
                   "mm_adverse_selection", "mm:mm_v1", 0.5, 0.55, 0.05, 0.0,
                   "Filled on wrong side"))
    for i in range(3):
        c.execute("INSERT INTO loss_postmortems VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"loss_fee_{i}", f"KXFED-{i}", "economics",
                   "mm_fee_erosion", "mm:mm_v1", 0.45, 0.50, 0.05, 45.0,
                   "Spread too narrow"))
    for i in range(2):
        c.execute("INSERT INTO loss_postmortems VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (None, now, f"loss_dir_{i}", f"KXFED-{i+5}", "economics",
                   "mm_directional_loss", "mm:mm_v1", 0.45, 0.50, 0.05, 0.0,
                   "Wrong side of settlement"))

    # Timing patterns
    for h in range(24):
        for i in range(3):
            won = 1 if h in (14, 15, 16) else 0  # Wins cluster at 14-16 UTC
            pnl = 100 if won else -50
            c.execute("INSERT INTO timing_patterns VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (None, now, f"tp_{h}_{i}", h, h % 7, "weather", "metar",
                       0.05, won, pnl))

    # MM inventory
    for i in range(5):
        c.execute("INSERT INTO mm_inventory VALUES (?,?,?,?,?,?,?,?,?)",
                  (None, now, f"KXFED-INV-{i}", 10 + i * 5, 20, 5, -50 + i * 20,
                   45.0, now))
    c.execute("INSERT INTO mm_inventory VALUES (?,?,?,?,?,?,?,?,?)",
              (None, now, "KXHIGHNY-INV-0", 30, 40, 10, -500, 50.0, now))

    # Processed fills with fees
    for i in range(15):
        c.execute("INSERT INTO mm_processed_fills VALUES (?,?,?,?,?,?,?,?)",
                  (None, now, f"pf_{i}", f"KXFED-{i % 10}", "yes", 45, 10, 4))

    c.commit()
    return c


# ── Unit tests for utility functions ──

class TestFeeFormulas:
    def test_maker_fee_50cents(self):
        # 0.0175 * 10 * 0.5 * 0.5 * 100 = 4.375 → ceil = 5
        assert kalshi_maker_fee(10, 50) == 5

    def test_taker_fee_50cents(self):
        # 0.07 * 10 * 0.5 * 0.5 * 100 = 17.5 → ceil = 18
        assert kalshi_taker_fee(10, 50) == 18

    def test_fee_at_extremes(self):
        # Very low/high prices have minimal fees
        assert kalshi_maker_fee(10, 5) < kalshi_maker_fee(10, 50)
        assert kalshi_maker_fee(10, 95) < kalshi_maker_fee(10, 50)


class TestUtilities:
    def test_family_of(self):
        assert family_of("KXFED-2526-T47.25") == "KXFED"
        assert family_of("KXHIGHNY-26APR14-B55") == "KXHIGHNY"
        assert family_of("KXBTC") == "KXBTC"

    def test_prob_bucket(self):
        assert prob_bucket(0.05) == "0.0-0.1"
        assert prob_bucket(0.55) == "0.5-0.6"
        assert prob_bucket(0.99) == "0.9-1.0"

    def test_wilson_ci(self):
        lo, hi = wilson_ci(50, 100)
        assert 0.35 < lo < 0.45
        assert 0.55 < hi < 0.65

    def test_wilson_ci_zero(self):
        lo, hi = wilson_ci(0, 0)
        assert lo == 0.0
        assert hi == 1.0

    def test_brier_perfect(self):
        preds = [(1.0, 1), (0.0, 0), (1.0, 1), (0.0, 0)]
        assert brier_score(preds) == 0.0

    def test_brier_random(self):
        preds = [(0.5, 1), (0.5, 0)] * 50
        assert abs(brier_score(preds) - 0.25) < 0.01

    def test_brier_bad(self):
        preds = [(0.0, 1), (1.0, 0)] * 50
        assert brier_score(preds) == 1.0


# ── Integration tests for analysis sections ──

class TestCalibrationAnalysis:
    def test_returns_brier_score(self, conn):
        result = analyze_calibration(conn)
        assert "brier_score" in result
        assert result["brier_score"] is not None
        assert 0 <= result["brier_score"] <= 1

    def test_has_buckets(self, conn):
        result = analyze_calibration(conn)
        assert len(result["buckets"]) > 0

    def test_n_samples_positive(self, conn):
        result = analyze_calibration(conn)
        assert result["n_samples"] > 0


class TestEdgeVsWinRate:
    def test_returns_edge_buckets(self, conn):
        result = analyze_edge_vs_winrate(conn)
        assert "edge_buckets" in result

    def test_edge_buckets_have_data(self, conn):
        result = analyze_edge_vs_winrate(conn)
        total = sum(d["n"] for d in result["edge_buckets"].values())
        assert total > 0


class TestSourceAccuracy:
    def test_identifies_sources(self, conn):
        result = analyze_source_accuracy(conn)
        # Should find fedwatch, fred, clevfed, weather, metar from our test data
        assert len(result["sources"]) > 0

    def test_combo_stats(self, conn):
        result = analyze_source_accuracy(conn)
        assert len(result["combos"]) > 0


class TestFamilyPerformance:
    def test_identifies_families(self, conn):
        result = analyze_family_performance(conn)
        assert "KXFED" in result["families"]
        assert "KXHIGHNY" in result["families"]

    def test_kxfed_better_than_kxhigh(self, conn):
        result = analyze_family_performance(conn)
        assert result["families"]["KXFED"]["wr"] > result["families"]["KXHIGHNY"]["wr"]

    def test_pnl_sign_matches_wr(self, conn):
        result = analyze_family_performance(conn)
        # KXFED has 40% WR → should depend on P&L magnitude
        # KXHIGHNY has 10% WR → should be negative
        assert result["families"]["KXHIGHNY"]["pnl_cents"] < 0


class TestMMPerformance:
    def test_total_fills(self, conn):
        result = analyze_mm_performance(conn)
        assert result["total_fills"] == 40  # 20 + 20

    def test_adverse_selection_data(self, conn):
        result = analyze_mm_performance(conn)
        assert "avg_markout" in result["adverse_selection"]

    def test_mm_settlement_data(self, conn):
        result = analyze_mm_performance(conn)
        assert "mm_pnl_cents" in result


class TestLossPostmortems:
    def test_classifies_losses(self, conn):
        result = analyze_loss_postmortems(conn)
        assert result["total_losses"] == 13  # 8 + 3 + 2
        assert "mm_adverse_selection" in result["loss_types"]

    def test_adverse_selection_dominant(self, conn):
        result = analyze_loss_postmortems(conn)
        assert result["loss_types"]["mm_adverse_selection"]["count"] == 8
        assert result["loss_types"]["mm_adverse_selection"]["pct"] > 0.5


class TestOpportunityCost:
    def test_counts_skips(self, conn):
        result = analyze_opportunity_cost(conn)
        assert result["skipped_total"] == 5

    def test_skip_reasons(self, conn):
        result = analyze_opportunity_cost(conn)
        assert "edge_too_low" in result["skip_reasons"]


class TestTiming:
    def test_has_hour_data(self, conn):
        result = analyze_timing(conn)
        assert len(result["by_hour"]) > 0


class TestSignificance:
    def test_returns_wr_and_ci(self, conn):
        result = analyze_statistical_significance(conn)
        assert "overall_wr" in result
        assert "overall_ci" in result
        assert result["n_settlements"] == 20

    def test_ci_contains_wr(self, conn):
        result = analyze_statistical_significance(conn)
        ci = result["overall_ci"]
        assert ci[0] <= result["overall_wr"] <= ci[1]


class TestInventory:
    def test_counts_positions(self, conn):
        result = analyze_inventory(conn)
        assert result["positions"] == 6  # 5 KXFED + 1 KXHIGHNY

    def test_exposure_positive(self, conn):
        result = analyze_inventory(conn)
        assert result["exposure_cents"] > 0


class TestLearningSystem:
    def test_checks_all_tables(self, conn):
        result = analyze_learning_system(conn)
        assert "calibration" in result["tables"]
        assert "loss_postmortems" in result["tables"]

    def test_calibration_count(self, conn):
        result = analyze_learning_system(conn)
        assert result["tables"]["calibration"]["count"] == 20
