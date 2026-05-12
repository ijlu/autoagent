"""Regression test for tools/backfill_hedge_settlements.py.

Pins the one-time hedge-settlement backfill's logic:
  - Hedged winners go from revenue=0 to revenue=100, profit recomputed.
  - Hedged losers (where cost > 100) stay losers but with corrected profit.
  - Pure positions on the winning side also get fixed.
  - Rows with no bot fills are left alone.
  - Rows that are already correct are not modified (idempotent re-run).
"""

from __future__ import annotations

import sqlite3

import pytest

from bot.db import init_db
from tools.backfill_hedge_settlements import backfill


def _insert_fill(conn, *, trade_id, ticker, side, yes_p, no_p, contracts=1,
                 fee=1, fill_ts="2026-05-07T20:00:00Z"):
    conn.execute(
        """INSERT INTO fills_ledger
        (trade_id, order_id, client_order_id, ticker, series, family,
         side, action, contracts, yes_price_cents, no_price_cents,
         is_taker, fee_cents, fill_ts_iso, fill_ts_unix,
         ingested_ts_unix, live_mode, source)
        VALUES (?, ?, ?, ?, 'KXHIGHAUS', 'KXHIGHAUS',
                ?, 'buy', ?, ?, ?, 1, ?, ?, 1778000000, 1778000000,
                1, 'cross_bracket')""",
        (trade_id, f"oid-{trade_id}", f"mm_xb_{trade_id}",
         ticker, side, contracts, yes_p, no_p, fee, fill_ts),
    )


def _insert_settlement(conn, *, ticker, side, price, contracts, revenue,
                       profit, won, strategy="cross_bracket"):
    conn.execute(
        """INSERT INTO settlements
        (recorded_at, order_id, ticker, side, price_cents, contracts,
         revenue_cents, profit_cents, won, volume, spread_cents, strategy)
        VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)""",
        (f"settlement_{ticker}", ticker, side, price, contracts,
         revenue, profit, won, strategy),
    )


def _insert_alpha_settlement(conn, *, ticker, result):
    conn.execute(
        """INSERT INTO alpha_backtest
        (ts_decision, ts_decision_unix, ticker, family, decision_type,
         decision_outcome, side, ensemble_p_yes, settlement_result, won_yes)
        VALUES ('2026-05-07T19:00:00Z', 1778000000, ?, 'KXHIGHAUS',
                'cross_bracket_live', 'posted', 'no', 0.05, ?, NULL)""",
        (ticker, result),
    )


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = init_db(path)
    conn.close()
    return path


def test_hedged_winner_corrected(db):
    """1 YES @ 12¢ + 1 NO @ 75¢ = 87¢ premium + 3¢ fees. Settled NO →
    NO leg pays $1.00, YES leg pays $0. Pre-fix: revenue=0 → profit=-90.
    Post-fix: revenue=100 → profit=+10, won=1.
    """
    conn = sqlite3.connect(db)
    _insert_fill(conn, trade_id="t1", ticker="KXTEST-B1", side="no",
                 yes_p=25, no_p=75, fee=2)
    _insert_fill(conn, trade_id="t2", ticker="KXTEST-B1", side="yes",
                 yes_p=12, no_p=88, fee=1)
    _insert_alpha_settlement(conn, ticker="KXTEST-B1", result="no")
    _insert_settlement(conn, ticker="KXTEST-B1", side="no", price=43,
                       contracts=2, revenue=0, profit=-90, won=0)
    conn.commit()
    conn.close()

    backfill(db, apply=True)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT revenue_cents, profit_cents, won FROM settlements "
        "WHERE ticker='KXTEST-B1'"
    ).fetchone()
    conn.close()
    assert row == (100, 10, 1)


def test_hedged_loser_stays_loser_with_corrected_profit(db):
    """Asymmetric hedge mirroring the real KXHIGHNY-26MAY11-T62
    settlement: 1 YES @ 86¢ + 2 NO @ 16¢ (= NO price column on a
    yes_price=84 book) → total premium 86 + 32 = 118¢ + 3¢ fees.
    Settled YES → YES leg pays $1.00.  Profit = 100 - 118 - 3 = -21.
    Still a loss (we paid more in premium than the single winning
    leg returned), but corrected from the -121¢ the pre-fix code
    reported when it saw revenue=0.
    """
    conn = sqlite3.connect(db)
    _insert_fill(conn, trade_id="t1", ticker="KXTEST-T62", side="yes",
                 yes_p=86, no_p=14, contracts=1, fee=1)
    # NO buy at 16¢: yes_price=84, no_price=16; the bot paid 16¢ per NO.
    _insert_fill(conn, trade_id="t2", ticker="KXTEST-T62", side="no",
                 yes_p=84, no_p=16, contracts=2, fee=2)
    _insert_alpha_settlement(conn, ticker="KXTEST-T62", result="yes")
    _insert_settlement(conn, ticker="KXTEST-T62", side="no", price=9,
                       contracts=3, revenue=0, profit=-121, won=0)
    conn.commit()
    conn.close()

    backfill(db, apply=True)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT revenue_cents, profit_cents, won FROM settlements "
        "WHERE ticker='KXTEST-T62'"
    ).fetchone()
    conn.close()
    # Cost = yes_paid + no_paid = 86 + 2*16 = 118.  Fees = 3.
    # Revenue = 1 YES × 100¢ = 100.  Profit = 100 - 118 - 3 = -21.
    assert row == (100, -21, 0)


def test_pure_winner_corrected(db):
    """No hedge: 1 NO @ 75¢. Settled NO → 100¢ payout. Pre-fix:
    revenue=0 → profit=-78. Post-fix: revenue=100 → profit=+22.
    """
    conn = sqlite3.connect(db)
    _insert_fill(conn, trade_id="t1", ticker="KXTEST-B2", side="no",
                 yes_p=25, no_p=75, fee=2)
    _insert_alpha_settlement(conn, ticker="KXTEST-B2", result="no")
    _insert_settlement(conn, ticker="KXTEST-B2", side="no", price=75,
                       contracts=1, revenue=0, profit=-77, won=0)
    conn.commit()
    conn.close()

    backfill(db, apply=True)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT revenue_cents, profit_cents, won FROM settlements "
        "WHERE ticker='KXTEST-B2'"
    ).fetchone()
    conn.close()
    assert row == (100, 23, 1)  # 100 - 75 - 2 = 23


def test_pure_loser_unchanged(db):
    """1 YES @ 25¢. Settled NO → YES leg pays 0. Loss = 25 + 1 = 26.
    Pre-fix already had revenue=0 (correctly!) → profit=-26. Should
    not be re-written because new computation matches existing.
    """
    conn = sqlite3.connect(db)
    _insert_fill(conn, trade_id="t1", ticker="KXTEST-B3", side="yes",
                 yes_p=25, no_p=75, fee=1)
    _insert_alpha_settlement(conn, ticker="KXTEST-B3", result="no")
    _insert_settlement(conn, ticker="KXTEST-B3", side="yes", price=25,
                       contracts=1, revenue=0, profit=-26, won=0)
    conn.commit()
    conn.close()

    changes = backfill(db, apply=True)
    assert changes == 0, "row already correct — backfill should not touch it"


def test_no_bot_fills_row_skipped(db):
    """A settlement row for a ticker the bot has no fills_ledger entries
    on (e.g., legacy pre-fills_ledger MM positions) is left alone.
    """
    conn = sqlite3.connect(db)
    _insert_alpha_settlement(conn, ticker="KXLEGACY-T70", result="no")
    _insert_settlement(conn, ticker="KXLEGACY-T70", side="no", price=10,
                       contracts=5, revenue=0, profit=-55, won=0,
                       strategy="mm:mm_v1")
    conn.commit()
    conn.close()

    changes = backfill(db, apply=True)
    assert changes == 0

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT revenue_cents, profit_cents, won FROM settlements "
        "WHERE ticker='KXLEGACY-T70'"
    ).fetchone()
    conn.close()
    assert row == (0, -55, 0), "untouched"


def test_dry_run_writes_nothing(db):
    conn = sqlite3.connect(db)
    _insert_fill(conn, trade_id="t1", ticker="KXTEST-B4", side="no",
                 yes_p=25, no_p=75)
    _insert_alpha_settlement(conn, ticker="KXTEST-B4", result="no")
    _insert_settlement(conn, ticker="KXTEST-B4", side="no", price=75,
                       contracts=1, revenue=0, profit=-76, won=0)
    conn.commit()
    conn.close()

    backfill(db, apply=False)  # dry run

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT revenue_cents, profit_cents, won FROM settlements "
        "WHERE ticker='KXTEST-B4'"
    ).fetchone()
    conn.close()
    # Stored values unchanged
    assert row == (0, -76, 0)


def test_idempotent_apply(db):
    """Running --apply twice yields the same result as once."""
    conn = sqlite3.connect(db)
    _insert_fill(conn, trade_id="t1", ticker="KXTEST-B5", side="no",
                 yes_p=25, no_p=75, fee=2)
    _insert_alpha_settlement(conn, ticker="KXTEST-B5", result="no")
    _insert_settlement(conn, ticker="KXTEST-B5", side="no", price=75,
                       contracts=1, revenue=0, profit=-77, won=0)
    conn.commit()
    conn.close()

    n1 = backfill(db, apply=True)
    n2 = backfill(db, apply=True)
    assert n1 == 1
    assert n2 == 0, "second run should find nothing to change"
