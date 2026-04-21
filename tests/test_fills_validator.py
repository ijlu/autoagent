"""Tests for bot.learning.fills_validator (T3.1 dual-run validator).

Covers the invariants the T3.3 reader-migration gate depends on:

  * Empty DB → clean + not meaningful (no false "green light")
  * Ledger and mm_processed_fills perfectly aligned → clean + meaningful
  * Divergence in ANY of contracts / cents / fees → non-empty report
  * Window boundary is strict — rows on either side of ``since_unix``
    and its ISO equivalent are handled identically
  * ``is_meaningful`` correctly distinguishes "both populated" from
    "one side empty" (the T3.1 bedding-in steady state)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest

from bot.db import init_db
from bot.learning.fills_validator import (
    Divergence,
    TickerSideStats,
    ValidationReport,
    compare_last_n_days,
    format_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LEDGER_COLS = (
    "trade_id", "order_id", "client_order_id",
    "ticker", "series", "family",
    "side", "action", "contracts",
    "yes_price_cents", "no_price_cents",
    "is_taker", "fee_cents",
    "fill_ts_iso", "fill_ts_unix", "ingested_ts_unix",
    "live_mode", "source",
)


def _insert_ledger(
    conn: sqlite3.Connection,
    *,
    trade_id: str,
    ticker: str = "KXHIGHNY-26APR20-B7476",
    side: str = "yes",
    contracts: int = 10,
    yes_price: int = 47,
    no_price: int | None = None,
    fee_cents: int = 5,
    fill_ts_unix: float = 1_776_100_000.0,
) -> None:
    if no_price is None:
        no_price = 100 - yes_price
    row = {
        "trade_id": trade_id,
        "order_id": f"o_{trade_id}",
        "client_order_id": "mm_wx_x",
        "ticker": ticker,
        "series": ticker.rsplit("-", 1)[0],
        "family": ticker.split("-", 1)[0],
        "side": side,
        "action": "buy",
        "contracts": contracts,
        "yes_price_cents": yes_price,
        "no_price_cents": no_price,
        "is_taker": 0,
        "fee_cents": fee_cents,
        "fill_ts_iso": datetime.fromtimestamp(
            fill_ts_unix, tz=timezone.utc,
        ).isoformat(),
        "fill_ts_unix": fill_ts_unix,
        "ingested_ts_unix": fill_ts_unix + 1,
        "live_mode": 0,
        "source": "mm_quote",
    }
    placeholders = ", ".join("?" * len(_LEDGER_COLS))
    conn.execute(
        f"INSERT INTO fills_ledger ({', '.join(_LEDGER_COLS)}) "
        f"VALUES ({placeholders})",
        tuple(row[c] for c in _LEDGER_COLS),
    )
    conn.commit()


def _insert_mm_fill(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    ticker: str = "KXHIGHNY-26APR20-B7476",
    side: str = "yes",
    price_cents: int = 47,  # for yes side; pass 53 on no side to match ledger
    contracts: int = 10,
    fee_cents: int = 5,
    recorded_at_unix: float = 1_776_100_000.0,
) -> None:
    recorded_iso = datetime.fromtimestamp(
        recorded_at_unix, tz=timezone.utc,
    ).isoformat()
    conn.execute(
        "INSERT INTO mm_processed_fills "
        "(recorded_at, order_id, ticker, side, price_cents, contracts, fee_cents) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (recorded_iso, order_id, ticker, side, price_cents, contracts, fee_cents),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Empty DB & meaningfulness flag
# ---------------------------------------------------------------------------

class TestMeaningfulness:
    def test_empty_db_is_clean_but_not_meaningful(self):
        conn = init_db(":memory:")
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert report.is_clean
        assert not report.is_meaningful
        assert report.ledger_contracts == 0
        assert report.reference_contracts == 0

    def test_only_ledger_populated_is_not_meaningful(self):
        """T3.1 steady state: mm_processed_fills legacy writer is gone,
        so its row count stays at zero while the ledger grows. Validator
        must NOT treat this as a divergence — is_meaningful=False
        signals the scheduler wrapper to skip alerting."""
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1", fill_ts_unix=1_776_100_000.0)
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert not report.is_meaningful
        # We still surface the ledger count for observability.
        assert report.ledger_contracts == 10
        assert report.reference_contracts == 0

    def test_only_reference_populated_is_not_meaningful(self):
        """Symmetric case — if somehow mm_processed_fills has rows but
        the ledger is empty, we also flag it as non-meaningful. Would
        mean the ledger writer is broken, which the scheduler sync
        stats would surface separately."""
        conn = init_db(":memory:")
        _insert_mm_fill(conn, order_id="o1", recorded_at_unix=1_776_100_000.0)
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert not report.is_meaningful
        assert report.ledger_contracts == 0
        assert report.reference_contracts == 10


# ---------------------------------------------------------------------------
# Perfect agreement (clean + meaningful)
# ---------------------------------------------------------------------------

class TestCleanAgreement:
    def test_identical_single_fill_is_clean(self):
        conn = init_db(":memory:")
        _insert_ledger(
            conn, trade_id="t1", contracts=10, yes_price=47, fee_cents=5,
        )
        _insert_mm_fill(
            conn, order_id="o_t1", contracts=10, price_cents=47, fee_cents=5,
        )
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert report.is_meaningful
        assert report.is_clean
        assert report.divergences == []

    def test_identical_across_multiple_fills_aggregates_match(self):
        conn = init_db(":memory:")
        # 3 fills on the same ticker, same side, different sizes
        _insert_ledger(conn, trade_id="t1", contracts=10, yes_price=47, fee_cents=5)
        _insert_ledger(conn, trade_id="t2", contracts=5, yes_price=47, fee_cents=3)
        _insert_ledger(conn, trade_id="t3", contracts=7, yes_price=47, fee_cents=4)

        _insert_mm_fill(conn, order_id="o1", contracts=10, price_cents=47, fee_cents=5)
        _insert_mm_fill(conn, order_id="o2", contracts=5, price_cents=47, fee_cents=3)
        _insert_mm_fill(conn, order_id="o3", contracts=7, price_cents=47, fee_cents=4)

        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert report.is_clean
        # Sanity: 22 contracts * 47 cents = 1034 cents transacted
        assert report.ledger_contracts == 22

    def test_no_side_uses_no_price(self):
        """Ledger stores yes_price + no_price (sum=100); validator uses
        no_price for the NO-side aggregate. mm_processed_fills stores
        price_cents (already side-appropriate). Parity must hold."""
        conn = init_db(":memory:")
        _insert_ledger(
            conn, trade_id="t1", side="no",
            yes_price=47, no_price=53,
            contracts=10, fee_cents=5,
        )
        _insert_mm_fill(
            conn, order_id="o_t1", side="no", price_cents=53,
            contracts=10, fee_cents=5,
        )
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert report.is_clean


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------

class TestDivergence:
    def test_contracts_mismatch_flags_divergence(self):
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1", contracts=10, yes_price=47, fee_cents=5)
        _insert_mm_fill(conn, order_id="o1", contracts=9, price_cents=47, fee_cents=5)
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert not report.is_clean
        assert len(report.divergences) == 1
        d = report.divergences[0]
        assert d.contracts_delta == 1  # ledger - reference

    def test_fee_mismatch_flags_divergence(self):
        """Same contracts, same price, different fee → divergence."""
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1", contracts=10, yes_price=47, fee_cents=5)
        _insert_mm_fill(conn, order_id="o1", contracts=10, price_cents=47, fee_cents=4)
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert not report.is_clean
        d = report.divergences[0]
        assert d.contracts_delta == 0
        assert d.fees_delta == 1  # ledger 5 - reference 4

    def test_price_mismatch_flags_divergence(self):
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1", contracts=10, yes_price=47, fee_cents=5)
        _insert_mm_fill(conn, order_id="o1", contracts=10, price_cents=48, fee_cents=5)
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        assert not report.is_clean
        d = report.divergences[0]
        # 10*47 - 10*48 = -10
        assert d.cents_delta == -10

    def test_ticker_present_only_in_one_source_is_divergence(self):
        conn = init_db(":memory:")
        _insert_ledger(
            conn, trade_id="t1", ticker="KXHIGHNY-26APR20-B7476",
            contracts=10, yes_price=47,
        )
        _insert_mm_fill(
            conn, order_id="o1", ticker="KXHIGHCHI-26APR20-B6000",
            contracts=10, price_cents=50,
        )
        report = compare_last_n_days(conn, now_unix=1_776_200_000.0)
        # Two buckets, each diverging vs empty on the other side.
        tickers = {d.ticker for d in report.divergences}
        assert "KXHIGHNY-26APR20-B7476" in tickers
        assert "KXHIGHCHI-26APR20-B6000" in tickers


# ---------------------------------------------------------------------------
# Window boundary
# ---------------------------------------------------------------------------

class TestWindow:
    def test_rows_older_than_window_excluded(self):
        """Rows outside the n_days window must not affect the report."""
        conn = init_db(":memory:")
        now = 1_776_200_000.0
        # n_days=7 → window is [now - 7*86400, now].
        old = now - 10 * 86_400  # older than window
        _insert_ledger(conn, trade_id="t_old", fill_ts_unix=old, contracts=100)
        _insert_mm_fill(conn, order_id="o_old", recorded_at_unix=old, contracts=200)

        report = compare_last_n_days(conn, n_days=7, now_unix=now)
        # Nothing in the window.
        assert report.ledger_contracts == 0
        assert report.reference_contracts == 0
        assert report.is_clean

    def test_rows_at_window_boundary_included(self):
        """Exactly at since_unix → included (>= boundary)."""
        conn = init_db(":memory:")
        now = 1_776_200_000.0
        # Exactly 7 days ago; the ``>=`` in the query includes it.
        boundary = now - 7 * 86_400
        _insert_ledger(
            conn, trade_id="t_boundary", fill_ts_unix=boundary,
            contracts=10, yes_price=50, fee_cents=5,
        )
        _insert_mm_fill(
            conn, order_id="o_boundary", recorded_at_unix=boundary,
            price_cents=50, contracts=10, fee_cents=5,
        )
        report = compare_last_n_days(conn, n_days=7, now_unix=now)
        assert report.ledger_contracts == 10
        assert report.reference_contracts == 10
        assert report.is_clean

    def test_custom_n_days_parameter(self):
        conn = init_db(":memory:")
        now = 1_776_200_000.0
        # Insert fill 2 days old — in window for n_days=7, out of window
        # for n_days=1.
        two_days_ago = now - 2 * 86_400
        _insert_ledger(conn, trade_id="t1", fill_ts_unix=two_days_ago)

        r7 = compare_last_n_days(conn, n_days=7, now_unix=now)
        assert r7.ledger_contracts == 10
        r1 = compare_last_n_days(conn, n_days=1, now_unix=now)
        assert r1.ledger_contracts == 0


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------

class TestFormatReport:
    def test_clean_meaningful_is_marked_clean(self):
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1")
        _insert_mm_fill(conn, order_id="o1")
        text = format_report(compare_last_n_days(conn, now_unix=1_776_200_000.0))
        assert "CLEAN" in text

    def test_informational_is_marked_informational(self):
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1")
        # no mm_processed_fills row
        text = format_report(compare_last_n_days(conn, now_unix=1_776_200_000.0))
        assert "INFORMATIONAL" in text

    def test_divergence_shows_delta_lines(self):
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1", contracts=10, yes_price=47, fee_cents=5)
        _insert_mm_fill(conn, order_id="o1", contracts=8, price_cents=47, fee_cents=5)
        text = format_report(compare_last_n_days(conn, now_unix=1_776_200_000.0))
        assert "diverging" in text
        assert "Δcontracts=+2" in text

    def test_max_lines_truncates_large_divergence_list(self):
        conn = init_db(":memory:")
        # 25 diverging tickers, cap at 5.
        for i in range(25):
            tkr = f"KXHIGHNY-26APR20-B{7400 + i:04d}"
            _insert_ledger(conn, trade_id=f"t{i}", ticker=tkr, contracts=10)
            _insert_mm_fill(conn, order_id=f"o{i}", ticker=tkr, contracts=9)
        text = format_report(
            compare_last_n_days(conn, now_unix=1_776_200_000.0),
            max_lines=5,
        )
        assert "20 more" in text  # 25 - 5


# ---------------------------------------------------------------------------
# Scheduler wrapper: _run_fills_validator
# ---------------------------------------------------------------------------

class TestRunFillsValidator:
    """Alerting policy: WARNING + Telegram only on meaningful divergence.
    Empty window and informational (one-sided) reports are silent except
    for the info log, preventing Telegram spam during T3.1 bedding-in."""

    def test_silent_when_empty_window(self, monkeypatch, caplog):
        from bot.daemon import main as main_module
        alerts = []
        monkeypatch.setattr(main_module, "send_alert",
                            lambda msg, level="info": alerts.append((msg, level)))
        import logging
        conn = init_db(":memory:")
        with caplog.at_level(logging.INFO, logger="bot.daemon.main"):
            main_module._run_fills_validator(conn)
        # No Telegram, but INFO log was produced.
        assert alerts == []
        assert any("INFORMATIONAL" in r.getMessage() for r in caplog.records)

    def test_silent_when_informational_one_side_empty(self, monkeypatch):
        from bot.daemon import main as main_module
        alerts = []
        monkeypatch.setattr(main_module, "send_alert",
                            lambda msg, level="info": alerts.append((msg, level)))
        conn = init_db(":memory:")
        _insert_ledger(conn, trade_id="t1", fill_ts_unix=time.time() - 3600)
        main_module._run_fills_validator(conn)
        assert alerts == []

    def test_alert_fires_on_meaningful_divergence(self, monkeypatch):
        from bot.daemon import main as main_module
        alerts = []
        monkeypatch.setattr(main_module, "send_alert",
                            lambda msg, level="info": alerts.append((msg, level)))
        conn = init_db(":memory:")
        now = time.time() - 3600
        _insert_ledger(conn, trade_id="t1", contracts=10, yes_price=47,
                       fee_cents=5, fill_ts_unix=now)
        _insert_mm_fill(conn, order_id="o1", contracts=8, price_cents=47,
                        fee_cents=5, recorded_at_unix=now)
        main_module._run_fills_validator(conn)
        assert len(alerts) == 1
        text, level = alerts[0]
        assert level == "warning"
        assert "diverging" in text

    def test_no_alert_on_clean_meaningful(self, monkeypatch):
        from bot.daemon import main as main_module
        alerts = []
        monkeypatch.setattr(main_module, "send_alert",
                            lambda msg, level="info": alerts.append((msg, level)))
        conn = init_db(":memory:")
        now = time.time() - 3600
        _insert_ledger(conn, trade_id="t1", contracts=10, yes_price=47,
                       fee_cents=5, fill_ts_unix=now)
        _insert_mm_fill(conn, order_id="o1", contracts=10, price_cents=47,
                        fee_cents=5, recorded_at_unix=now)
        main_module._run_fills_validator(conn)
        assert alerts == []
