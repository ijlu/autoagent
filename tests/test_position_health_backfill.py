"""Verify record_settlements back-fills position_health_log.settlement_result
+ settlement_pnl_cents on every prior health row for the ticker.

The bandit trains on these rows — each per-cycle health-score snapshot paired
with the eventual ticker outcome tells it which health-band holds won versus
lost. Without back-fill the rows stay NULL forever and the bandit is starved.
"""
from __future__ import annotations

from bot.db import init_db


def _seed_health_row(conn, *, ticker, health, action, settlement_result=None):
    conn.execute(
        "INSERT INTO position_health_log "
        "(timestamp, ticker, side, quantity, health_score, remaining_edge, "
        " edge_trend, action, exit_qty, settlement_result, settlement_pnl_cents) "
        "VALUES ('t',?,?,?,?,0.0,0.0,?,0,?,NULL)",
        (ticker, "yes", 10, health, action, settlement_result),
    )


class TestBackfillSQL:
    """Exercise the exact UPDATE the record_settlements path runs."""

    def _backfill(self, conn, ticker, result, profit):
        conn.execute(
            "UPDATE position_health_log "
            "SET settlement_result=?, settlement_pnl_cents=? "
            "WHERE ticker=? AND settlement_result IS NULL",
            (result, profit, ticker),
        )
        conn.commit()

    def test_backfills_unset_rows(self):
        conn = init_db(":memory:")
        try:
            _seed_health_row(conn, ticker="KXHIGHNY-26APR16", health=0.7, action="hold")
            _seed_health_row(conn, ticker="KXHIGHNY-26APR16", health=0.4, action="trim")
            self._backfill(conn, "KXHIGHNY-26APR16", "yes", 200)
            rows = conn.execute(
                "SELECT settlement_result, settlement_pnl_cents "
                "FROM position_health_log WHERE ticker='KXHIGHNY-26APR16'"
            ).fetchall()
            assert rows == [("yes", 200), ("yes", 200)]
        finally:
            conn.close()

    def test_does_not_overwrite_already_set_rows(self):
        """Idempotent: a prior back-fill run must not be stomped by a later one
        for a different settlement on the same ticker (shouldn't happen, but
        defensive)."""
        conn = init_db(":memory:")
        try:
            _seed_health_row(conn, ticker="T1", health=0.7, action="hold",
                             settlement_result="yes")
            conn.execute(
                "UPDATE position_health_log SET settlement_pnl_cents=? "
                "WHERE ticker='T1'", (500,),
            )
            conn.commit()
            self._backfill(conn, "T1", "no", -999)
            (res, pnl) = conn.execute(
                "SELECT settlement_result, settlement_pnl_cents "
                "FROM position_health_log WHERE ticker='T1'"
            ).fetchone()
            assert res == "yes"
            assert pnl == 500
        finally:
            conn.close()

    def test_scoped_to_ticker(self):
        conn = init_db(":memory:")
        try:
            _seed_health_row(conn, ticker="T1", health=0.7, action="hold")
            _seed_health_row(conn, ticker="T2", health=0.7, action="hold")
            self._backfill(conn, "T1", "yes", 100)
            t1 = conn.execute(
                "SELECT settlement_result FROM position_health_log WHERE ticker='T1'"
            ).fetchone()
            t2 = conn.execute(
                "SELECT settlement_result FROM position_health_log WHERE ticker='T2'"
            ).fetchone()
            assert t1[0] == "yes"
            assert t2[0] is None
        finally:
            conn.close()
