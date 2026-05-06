"""Regression test for the 2026-04-22 audit finding:
``mm_processed_fills`` on production DBs created under the old schema
does not have the ``side`` / ``contracts`` / ``price_cents`` / ``recorded_at``
columns. The validator used to crash every scheduled run with
``no such column: side``; the wrapper in ``bot/daemon/main.py`` then
swallowed the exception, causing the scheduler health block to report
``errors=0`` — the same silent-failure surface that hid the Apr 20
shadow corruption for four days.

Fix: ``_aggregate_mm_processed_fills`` probes ``PRAGMA table_info``
and returns an empty dict when any required column is missing. The
resulting report has ``is_meaningful=False`` so alerting correctly
skips it. Fresh DBs (``init_db``) still exercise the real comparison.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot.learning.fills_validator import (
    _mm_processed_fills_is_comparable,
    compare_last_n_days,
    format_report,
)


# ---------------------------------------------------------------------------
# Fixtures — legacy vs fresh-init schema
# ---------------------------------------------------------------------------

def _make_legacy_prod_schema() -> sqlite3.Connection:
    """Recreate the production schema that lacks side/contracts/price_cents.

    This exact shape comes from the 2026-04-22 VPS DB pull
    (``06_SCHEMA.sql`` in the audit bundle).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE mm_processed_fills (
        fill_id TEXT PRIMARY KEY,
        processed_at TEXT,
        fee_cents REAL DEFAULT 0,
        order_id TEXT DEFAULT '',
        ticker TEXT DEFAULT ''
    )""")
    # Ledger must exist for compare_last_n_days to run.
    conn.execute("""CREATE TABLE fills_ledger (
        trade_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        client_order_id TEXT,
        ticker TEXT NOT NULL,
        side TEXT NOT NULL,
        contracts INTEGER NOT NULL,
        yes_price_cents INTEGER,
        no_price_cents INTEGER,
        fee_cents INTEGER,
        fill_ts_unix REAL NOT NULL,
        series TEXT,
        family TEXT
    )""")
    return conn


def _make_full_schema() -> sqlite3.Connection:
    """Use the real init_db so the validator actually compares."""
    from bot.db import init_db

    return init_db(":memory:")


# ---------------------------------------------------------------------------
# Schema probe
# ---------------------------------------------------------------------------

def test_probe_returns_false_for_legacy_schema():
    conn = _make_legacy_prod_schema()
    assert _mm_processed_fills_is_comparable(conn) is False


def test_probe_returns_true_for_fresh_init():
    conn = _make_full_schema()
    assert _mm_processed_fills_is_comparable(conn) is True


def test_probe_returns_false_when_table_missing():
    conn = sqlite3.connect(":memory:")
    # No mm_processed_fills at all.
    assert _mm_processed_fills_is_comparable(conn) is False


# ---------------------------------------------------------------------------
# End-to-end — the actual regression
# ---------------------------------------------------------------------------

def test_compare_last_n_days_does_not_raise_on_legacy_schema():
    """The bug: validator used to raise `OperationalError: no such column: side`
    on every run against prod. Guard must short-circuit the aggregator
    before SQL executes."""
    conn = _make_legacy_prod_schema()

    # Must not raise. Before the fix this was
    # sqlite3.OperationalError: no such column: side.
    report = compare_last_n_days(conn, n_days=7)

    assert report.ledger_contracts == 0
    assert report.reference_contracts == 0
    assert report.is_meaningful is False
    assert report.is_clean is True  # empty divergence list


def test_compare_last_n_days_report_is_informational_on_legacy_schema():
    conn = _make_legacy_prod_schema()
    report = compare_last_n_days(conn, n_days=7)

    text = format_report(report)
    assert "INFORMATIONAL" in text
    assert "run failed" not in text.lower()


def test_compare_last_n_days_runs_against_fresh_init():
    """Paranoia test: confirm the guard doesn't over-gate. A fresh
    init_db DB (which has the superset schema) must reach the real
    SQL path."""
    conn = _make_full_schema()
    report = compare_last_n_days(conn, n_days=7)
    # Empty tables → not meaningful but also not errored.
    assert report.ledger_contracts == 0
    assert report.reference_contracts == 0
