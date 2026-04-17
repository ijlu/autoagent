"""Tests for MM opportunity logging with ensemble_prob and sources_json.

Validates that:
- _log_opportunity inserts rows with non-NULL ensemble_prob and sources_json
- mm_run logs "quoted" rows with full ensemble data after posting quotes
- mm_run logs "skip" rows with skip_reason for no-data and extreme-FV cases
- The opportunity_log columns match the schema in bot/db.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from bot.db import init_db
from bot.market_maker.core import _log_opportunity


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def conn():
    """In-memory DB with full schema."""
    return init_db(":memory:")


def _make_market(ticker, yes_bid=40, yes_ask=55, volume=100, close_time=None):
    """Build a minimal market dict."""
    if close_time is None:
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        close_time = future.isoformat()
    return {
        "ticker": ticker,
        "title": f"Test Market {ticker}",
        "subtitle": "",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "volume": volume,
        "open_interest": 50,
        "close_time": close_time,
    }


# ═══════════════════════════════════════════════════════════════════════════
# _log_opportunity unit tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLogOpportunity:
    def test_inserts_with_all_fields(self, conn):
        """All fields including ensemble_prob and sources_json should be stored."""
        _log_opportunity(conn, "KXFED-26APR16-T4.50",
                         strategy="mm", action="quoted", side="both",
                         ensemble_prob=0.42,
                         market_prob=0.47,
                         edge=0.05,
                         source_count=3,
                         sources_json="fred(0.44) clevfed(0.40) fedwatch(0.43)")
        conn.commit()

        row = conn.execute(
            "SELECT ticker, strategy, action, side, ensemble_prob, market_prob, "
            "edge, source_count, sources_json, skip_reason "
            "FROM opportunity_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

        assert row is not None
        ticker, strategy, action, side, ep, mp, edge, sc, sj, sr = row
        assert ticker == "KXFED-26APR16-T4.50"
        assert strategy == "mm"
        assert action == "quoted"
        assert side == "both"
        assert abs(ep - 0.42) < 1e-6
        assert abs(mp - 0.47) < 1e-6
        assert abs(edge - 0.05) < 1e-6
        assert sc == 3
        assert "fred" in sj
        assert "clevfed" in sj
        assert "fedwatch" in sj
        assert sr is None

    def test_inserts_skip_with_reason(self, conn):
        """Skip rows should have skip_reason populated."""
        _log_opportunity(conn, "KXHIGHNY-26APR16-B72",
                         strategy="mm", action="skip", side="both",
                         ensemble_prob=0.03,
                         market_prob=0.50,
                         source_count=2,
                         sources_json="weather(0.03) tomorrow(0.04)",
                         skip_reason="extreme fair value 3c")
        conn.commit()

        row = conn.execute(
            "SELECT ensemble_prob, skip_reason FROM opportunity_log "
            "WHERE ticker='KXHIGHNY-26APR16-B72'"
        ).fetchone()

        assert row is not None
        assert abs(row[0] - 0.03) < 1e-6
        assert row[1] == "extreme fair value 3c"

    def test_no_data_skip(self, conn):
        """No-data skip should have NULL ensemble_prob and a skip_reason."""
        _log_opportunity(conn, "KXUNKNOWN-T1",
                         strategy="mm", action="skip", side="both",
                         ensemble_prob=None,
                         source_count=0,
                         sources_json=None,
                         skip_reason="no data source")
        conn.commit()

        row = conn.execute(
            "SELECT ensemble_prob, source_count, sources_json, skip_reason "
            "FROM opportunity_log WHERE ticker='KXUNKNOWN-T1'"
        ).fetchone()

        assert row is not None
        assert row[0] is None  # ensemble_prob
        assert row[1] == 0
        assert row[2] is None  # sources_json
        assert row[3] == "no data source"

    def test_never_crashes(self, conn):
        """Even with a broken connection, _log_opportunity should not raise."""
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("no such table")
        # Should not raise
        _log_opportunity(bad_conn, "KXFED-T1", "mm", "quoted",
                         ensemble_prob=0.5, source_count=1)


# ═══════════════════════════════════════════════════════════════════════════
# Integration: mm_run logs ensemble data
# ═══════════════════════════════════════════════════════════════════════════

class TestMmRunLogsEnsembleData:
    """Verify that mm_run populates ensemble_prob in opportunity_log."""

    @patch("bot.market_maker.core.mm_liquidate_expiring", return_value=0)
    @patch("bot.market_maker.core.mm_check_fills", return_value=0)
    @patch("bot.market_maker.core._compute_adverse_selection_signals")
    @patch("bot.market_maker.core.mm_cancel_all_orders", return_value=0)
    @patch("bot.market_maker.core.api_get", return_value={"markets": []})
    @patch("bot.market_maker.core.mm_select_markets")
    @patch("bot.market_maker.core.get_independent_estimate")
    @patch("bot.market_maker.core.mm_get_inventory", return_value=(0, 0))
    @patch("bot.market_maker.core.mm_post_quotes", return_value=(2, 500))
    @patch("bot.market_maker.core.mm_reset_adverse_cache")
    @patch("bot.market_maker.core.MM_ENABLED", True)
    @patch("bot.market_maker.core.MM_DRY_RUN", False)
    def test_quoted_market_has_ensemble_prob(
        self, mock_reset, mock_post, mock_inv, mock_est,
        mock_select, mock_api, mock_cancel, mock_adverse,
        mock_fills, mock_liq, conn,
    ):
        """When a market is quoted, opportunity_log should have non-NULL ensemble data."""
        market = _make_market("KXFED-26APR16-T4.50")

        # mm_select_markets returns one candidate: (score, market, ticker, spread, mid, inventory, category)
        mock_select.return_value = [
            (10.0, market, "KXFED-26APR16-T4.50", 8, 47, 0, "economics")
        ]
        # Ensemble returns a real probability with source descriptions
        mock_est.return_value = (0.42, "fred(0.44) fedwatch(0.41)", 2)

        # Create mm_sessions table entry expectation
        from bot.market_maker.core import mm_run
        stats = mm_run(conn, [market], 100000, 50000)

        assert stats["markets_quoted"] == 1

        rows = conn.execute(
            "SELECT ticker, action, ensemble_prob, source_count, sources_json "
            "FROM opportunity_log WHERE strategy='mm' AND ensemble_prob IS NOT NULL"
        ).fetchall()

        assert len(rows) >= 1, f"Expected at least 1 row with non-NULL ensemble_prob, got {len(rows)}"
        ticker, action, ep, sc, sj = rows[-1]
        assert ticker == "KXFED-26APR16-T4.50"
        assert action == "quoted"
        assert abs(ep - 0.42) < 1e-6
        assert sc == 2
        assert "fred" in sj
        assert "fedwatch" in sj

    @patch("bot.market_maker.core.mm_liquidate_expiring", return_value=0)
    @patch("bot.market_maker.core.mm_check_fills", return_value=0)
    @patch("bot.market_maker.core._compute_adverse_selection_signals")
    @patch("bot.market_maker.core.mm_cancel_all_orders", return_value=0)
    @patch("bot.market_maker.core.api_get", return_value={"markets": []})
    @patch("bot.market_maker.core.mm_select_markets")
    @patch("bot.market_maker.core.get_independent_estimate")
    @patch("bot.market_maker.core.mm_get_inventory", return_value=(0, 0))
    @patch("bot.market_maker.core.mm_post_quotes", return_value=(0, 0))
    @patch("bot.market_maker.core.mm_reset_adverse_cache")
    @patch("bot.market_maker.core.MM_ENABLED", True)
    def test_skip_no_data_logged(
        self, mock_reset, mock_post, mock_inv, mock_est,
        mock_select, mock_api, mock_cancel, mock_adverse,
        mock_fills, mock_liq, conn,
    ):
        """When ensemble returns no real sources, skip is logged with reason."""
        market = _make_market("KXUNKNOWN-T1")
        mock_select.return_value = [
            (5.0, market, "KXUNKNOWN-T1", 6, 50, 0, "unknown")
        ]
        # Only LLM source (not in _MM_REAL_SOURCES)
        mock_est.return_value = (0.50, "llm(0.50)", 1)

        from bot.market_maker.core import mm_run
        stats = mm_run(conn, [market], 100000, 50000)

        assert stats.get("skipped_no_data", 0) == 1

        row = conn.execute(
            "SELECT skip_reason, ensemble_prob, sources_json "
            "FROM opportunity_log WHERE ticker='KXUNKNOWN-T1' AND action='skip'"
        ).fetchone()

        assert row is not None, "Expected a skip row in opportunity_log"
        assert row[0] == "LLM-only (no real data)"
        assert abs(row[1] - 0.50) < 1e-6
        assert row[2] == "llm(0.50)"

    @patch("bot.market_maker.core.mm_liquidate_expiring", return_value=0)
    @patch("bot.market_maker.core.mm_check_fills", return_value=0)
    @patch("bot.market_maker.core._compute_adverse_selection_signals")
    @patch("bot.market_maker.core.mm_cancel_all_orders", return_value=0)
    @patch("bot.market_maker.core.api_get", return_value={"markets": []})
    @patch("bot.market_maker.core.mm_select_markets")
    @patch("bot.market_maker.core.get_independent_estimate")
    @patch("bot.market_maker.core.mm_get_inventory", return_value=(0, 0))
    @patch("bot.market_maker.core.mm_post_quotes", return_value=(0, 0))
    @patch("bot.market_maker.core.mm_reset_adverse_cache")
    @patch("bot.market_maker.core.MM_ENABLED", True)
    def test_skip_extreme_fv_logged(
        self, mock_reset, mock_post, mock_inv, mock_est,
        mock_select, mock_api, mock_cancel, mock_adverse,
        mock_fills, mock_liq, conn,
    ):
        """When fair value is extreme (<=3 or >=85), skip is logged with ensemble data."""
        market = _make_market("KXCPI-26APR16-T0.1")
        mock_select.return_value = [
            (5.0, market, "KXCPI-26APR16-T0.1", 6, 50, 0, "economics")
        ]
        # Extreme probability -> fair_value_cents=2 (<=3 threshold)
        mock_est.return_value = (0.02, "fred(0.02)", 1)

        from bot.market_maker.core import mm_run
        stats = mm_run(conn, [market], 100000, 50000)

        assert stats.get("skipped_extreme_fv", 0) == 1

        row = conn.execute(
            "SELECT skip_reason, ensemble_prob, source_count, sources_json "
            "FROM opportunity_log WHERE ticker='KXCPI-26APR16-T0.1' AND action='skip'"
        ).fetchone()

        assert row is not None, "Expected a skip row in opportunity_log"
        assert "extreme fair value" in row[0]
        assert abs(row[1] - 0.02) < 1e-6
        assert row[2] == 1
        assert "fred" in row[3]
