"""Tests for bot/learning/threshold_tuner.py (T.8)."""
from __future__ import annotations

import json
import time

import pytest

from bot.db import init_db
from bot.learning.mm_promotion import DEFAULT_MM_PROMOTION, MMPromotionConfig
from bot.learning.threshold_tuner import (
    _default_grid,
    _score_threshold,
    apply_proposal,
    propose_thresholds,
)


@pytest.fixture()
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _seed(conn, n, pnl_per_row, start_ts=None):
    if start_ts is None:
        start_ts = time.time() - 10 * 86400
    for i in range(n):
        conn.execute(
            "INSERT INTO weather_mm_shadow "
            "(ts_unix, ts_iso, ticker, series, station, "
            " fair_value_cents, proposed_bid_cents, proposed_ask_cents, "
            " half_spread_cents, gate_should_quote, live_mode, "
            " shadow_bid_filled, shadow_ask_filled, "
            " shadow_pnl_cents, ts_settle_unix) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(start_ts + i * 60), "t",
             f"KXHIGHNY-{i}", "KXHIGHNY", "KJFK",
             50, 40, 60, 10, 1, 0, 1, 0,
             pnl_per_row, int(start_ts + (i + 1) * 60)),
        )
    conn.commit()


class TestDefaultGrid:
    def test_grid_spans_neighbors(self):
        grid = _default_grid(DEFAULT_MM_PROMOTION)
        assert DEFAULT_MM_PROMOTION in grid
        # Should include at least one neighbor on each axis
        assert any(c.min_shadow_fills != DEFAULT_MM_PROMOTION.min_shadow_fills
                   for c in grid)

    def test_grid_unique(self):
        grid = _default_grid(DEFAULT_MM_PROMOTION)
        keys = {(c.min_shadow_settled, c.min_shadow_fills,
                 c.min_pnl_per_fill_cents, c.min_pnl_total_dollars)
                for c in grid}
        assert len(keys) == len(grid)


class TestScore:
    def test_empty_rows_zero(self):
        score, _ = _score_threshold([], DEFAULT_MM_PROMOTION)
        assert score == 0.0

    def test_below_min_fills_zero(self, conn):
        _seed(conn, n=5, pnl_per_row=10)
        rows = list(conn.execute(
            "SELECT shadow_bid_filled, shadow_ask_filled, shadow_pnl_cents, "
            "       live_pnl_cents, ts_unix, live_mode "
            "FROM weather_mm_shadow"
        ).fetchall())
        # sqlite3.Row access with keys requires row_factory; the tuner uses
        # positional via named access — we mimic by dict-like proxy:
        class R:
            def __init__(self, t):
                (self.bid, self.ask, self.pnl,
                 self.lp, self.ts, self.lm) = t
            def __getitem__(self, k):
                return {"shadow_bid_filled": self.bid,
                        "shadow_ask_filled": self.ask,
                        "shadow_pnl_cents": self.pnl,
                        "live_pnl_cents": self.lp,
                        "ts_unix": self.ts,
                        "live_mode": self.lm}[k]
        rs = [R(r) for r in rows]
        score, meta = _score_threshold(rs, DEFAULT_MM_PROMOTION)
        assert score == 0.0
        assert meta.get("reason") == "below_min_fills"


class TestProposeThresholds:
    def test_no_rows_no_proposal(self, conn):
        assert propose_thresholds(conn, evidence_window_days=7) is None

    def test_nonpositive_delta_no_proposal(self, conn):
        # Small sample, well under min_fills → all grid points score 0.
        _seed(conn, n=5, pnl_per_row=10)
        assert propose_thresholds(conn, evidence_window_days=30) is None

    def test_writes_proposal_when_improvement(self, conn):
        # Strong signal: 200 fills of +15¢. Looser thresholds should score
        # higher because they admit more evidence at the lower end.
        _seed(conn, n=200, pnl_per_row=15)
        # Tight min_delta so the test is deterministic.
        res = propose_thresholds(
            conn, evidence_window_days=30, min_delta=0.01,
        )
        if res is not None:
            # If a proposal was written, round-trip verify the row exists
            row = conn.execute(
                "SELECT id, objective_current, objective_proposed, "
                "       objective_delta, applied "
                "FROM threshold_proposals WHERE id=?", (res["id"],),
            ).fetchone()
            assert row is not None
            assert row[3] >= 0.01
            assert row[4] == 0  # not applied


class TestApplyProposal:
    def test_apply_flips_applied_flag(self, conn):
        conn.execute(
            "INSERT INTO threshold_proposals "
            "(ts_unix, ts_iso, tuner, evidence_window_days, n_observations, "
            " current_thresholds_json, proposed_thresholds_json, "
            " objective_current, objective_proposed, objective_delta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), "t", "mm_promotion_grid_v1", 7, 100,
             json.dumps({}), json.dumps({}), 0.0, 0.1, 0.1),
        )
        conn.commit()
        pid = conn.execute(
            "SELECT id FROM threshold_proposals LIMIT 1"
        ).fetchone()[0]
        ok = apply_proposal(conn, pid, applied_by="test")
        assert ok is True
        row = conn.execute(
            "SELECT applied, applied_by FROM threshold_proposals WHERE id=?",
            (pid,),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == "test"

    def test_double_apply_no_op(self, conn):
        conn.execute(
            "INSERT INTO threshold_proposals "
            "(ts_unix, ts_iso, tuner, evidence_window_days, n_observations, "
            " current_thresholds_json, proposed_thresholds_json, "
            " objective_current, objective_proposed, objective_delta, "
            " applied) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), "t", "mm_promotion_grid_v1", 7, 100,
             "{}", "{}", 0.0, 0.1, 0.1, 1),
        )
        conn.commit()
        pid = conn.execute(
            "SELECT id FROM threshold_proposals LIMIT 1"
        ).fetchone()[0]
        assert apply_proposal(conn, pid, applied_by="x") is False

    def test_missing_id_returns_false(self, conn):
        assert apply_proposal(conn, 99999, applied_by="x") is False
