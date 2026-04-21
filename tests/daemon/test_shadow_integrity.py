"""Tests for bot.daemon.shadow_integrity.

Covers the three invariants the monitor enforces:
- Zero-price invariant (the exact Apr-17 bug signature)
- Blind-quote watchdog (posts but zero observed books)
- Stuck-book watchdog (many observations, frozen value)

Plus: clean-input returns no findings; alert fires only on critical;
window boundary; quiet series don't produce false positives.
"""
from __future__ import annotations

import sqlite3
import time
from unittest.mock import MagicMock

import pytest

from bot.daemon.shadow_integrity import (
    DEFAULT_WINDOW_S,
    MIN_ROWS_FOR_SIGNAL,
    IntegrityFinding,
    check_shadow_data_integrity,
    run_shadow_integrity_check,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_conn() -> sqlite3.Connection:
    """In-memory DB with just the columns the monitor reads.

    Kept intentionally minimal — monitor shouldn't care about columns it
    doesn't query. If it grows a dependency on more columns the test
    will fail fast at INSERT time.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE weather_mm_shadow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix INTEGER NOT NULL,
            series TEXT NOT NULL,
            gate_should_quote INTEGER,
            market_yes_bid INTEGER,
            market_yes_ask INTEGER
        )
    """)
    return conn


def _insert(conn, series, *, gate=1, bid=None, ask=None, ts=None, n=1):
    """Insert `n` rows for a series. `ts` defaults to now."""
    if ts is None:
        ts = int(time.time())
    for _ in range(n):
        conn.execute(
            "INSERT INTO weather_mm_shadow "
            "(ts_unix, series, gate_should_quote, market_yes_bid, market_yes_ask) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, series, gate, bid, ask),
        )
    conn.commit()


# ── Clean inputs return no findings ─────────────────────────────────────


class TestCleanInputs:
    def test_empty_table_returns_nothing(self):
        conn = _make_conn()
        assert check_shadow_data_integrity(conn) == []

    def test_healthy_series_no_findings(self):
        conn = _make_conn()
        # 30 rows with a diverse book — realistic post-fix shape.
        for i in range(30):
            _insert(
                conn, "KXHIGHCHI",
                gate=1, bid=45 + (i % 3), ask=46 + (i % 3),
            )
        assert check_shadow_data_integrity(conn) == []

    def test_quiet_series_below_signal_threshold_ignored(self):
        """Fewer than MIN_ROWS_FOR_SIGNAL book rows → no stuck-book fire.

        Real Kalshi behaviour: tiny obscure markets may genuinely quote
        the same book for several snapshots. We only warn when we have
        enough data for the signal to mean something.
        """
        conn = _make_conn()
        # 5 rows, one distinct bid. Below signal threshold → no find.
        _insert(conn, "KXHIGHAUS", gate=1, bid=50, ask=51, n=5)
        assert check_shadow_data_integrity(conn) == []


# ── Rule 1: zero-price invariant ────────────────────────────────────────


class TestZeroPriceInvariant:
    def test_zero_bid_triggers_critical(self):
        conn = _make_conn()
        _insert(conn, "KXHIGHCHI", gate=1, bid=0, ask=50)
        findings = check_shadow_data_integrity(conn)
        assert len(findings) == 1
        f = findings[0]
        assert f.level == "critical"
        assert f.kind == "zero_price"
        assert f.series == "KXHIGHCHI"
        assert f.metric == 1.0

    def test_zero_ask_triggers_critical(self):
        conn = _make_conn()
        _insert(conn, "KXHIGHNY", gate=1, bid=50, ask=0)
        findings = check_shadow_data_integrity(conn)
        assert len(findings) == 1
        assert findings[0].level == "critical"

    def test_counts_aggregate_per_series(self):
        """Metric reflects the magnitude of the invariant breach."""
        conn = _make_conn()
        _insert(conn, "KXHIGHCHI", gate=1, bid=0, ask=51, n=7)
        findings = check_shadow_data_integrity(conn)
        critical = [f for f in findings if f.level == "critical"]
        assert len(critical) == 1
        assert critical[0].metric == 7.0

    def test_zero_rows_outside_window_ignored(self):
        conn = _make_conn()
        # Insert a zero row with ts 2 hours ago — outside default 1h window.
        old = int(time.time()) - 2 * 3600
        _insert(conn, "KXHIGHCHI", gate=1, bid=0, ask=50, ts=old)
        assert check_shadow_data_integrity(conn, window_s=3600) == []


# ── Rule 2: blind-quote watchdog ────────────────────────────────────────


class TestBlindQuoteWatchdog:
    def test_all_null_books_with_posts_warns(self):
        conn = _make_conn()
        # 25 rows, all gate_should_quote=1, all NULL book → the exact
        # upstream-parser-regression signature.
        _insert(
            conn, "KXHIGHDEN",
            gate=1, bid=None, ask=None,
            n=MIN_ROWS_FOR_SIGNAL + 5,
        )
        findings = check_shadow_data_integrity(conn)
        blind = [f for f in findings if f.kind == "blind_quote"]
        assert len(blind) == 1
        assert blind[0].level == "warning"
        assert blind[0].series == "KXHIGHDEN"

    def test_some_books_present_suppresses_warning(self):
        """If any posts have a book, the upstream parser works — no fire."""
        conn = _make_conn()
        _insert(conn, "KXHIGHDEN", gate=1, bid=None, ask=None, n=24)
        _insert(conn, "KXHIGHDEN", gate=1, bid=47, ask=48, n=1)
        findings = check_shadow_data_integrity(conn)
        assert [f for f in findings if f.kind == "blind_quote"] == []

    def test_below_post_threshold_suppresses_warning(self):
        """<MIN_ROWS_FOR_SIGNAL posts → silent. Real thin markets."""
        conn = _make_conn()
        _insert(conn, "KXHIGHMIA", gate=1, bid=None, ask=None, n=5)
        findings = check_shadow_data_integrity(conn)
        assert [f for f in findings if f.kind == "blind_quote"] == []

    def test_gated_out_rows_dont_count_as_posts(self):
        """gate_should_quote=0 rows aren't posts and shouldn't trip the watchdog."""
        conn = _make_conn()
        # 30 rows, gate=0, all NULL book — we never intended to post,
        # this isn't a blind-quote scenario.
        _insert(conn, "KXHIGHLAX", gate=0, bid=None, ask=None, n=30)
        findings = check_shadow_data_integrity(conn)
        assert [f for f in findings if f.kind == "blind_quote"] == []


# ── Rule 3: stuck-book watchdog ─────────────────────────────────────────


class TestStuckBookWatchdog:
    def test_many_rows_one_distinct_book_flags_info(self):
        conn = _make_conn()
        # 25 rows, all bid=50, ask=51 → only 1 distinct bid value.
        _insert(
            conn, "KXHIGHNY", gate=1, bid=50, ask=51,
            n=MIN_ROWS_FOR_SIGNAL + 5,
        )
        findings = check_shadow_data_integrity(conn)
        stuck = [f for f in findings if f.kind == "stuck_book"]
        assert len(stuck) == 1
        assert stuck[0].level == "info"
        assert stuck[0].metric == 1.0

    def test_diverse_books_dont_flag(self):
        conn = _make_conn()
        for i in range(25):
            _insert(conn, "KXHIGHNY", gate=1, bid=45 + i % 5, ask=46 + i % 5)
        findings = check_shadow_data_integrity(conn)
        assert [f for f in findings if f.kind == "stuck_book"] == []

    def test_low_volume_series_not_checked(self):
        conn = _make_conn()
        # 10 rows, all the same book. Too few to warn on.
        _insert(conn, "KXHIGHMIA", gate=1, bid=50, ask=51, n=10)
        findings = check_shadow_data_integrity(conn)
        assert [f for f in findings if f.kind == "stuck_book"] == []


# ── Alerting behaviour ──────────────────────────────────────────────────


class TestRunShadowIntegrityCheck:
    def test_clean_run_no_alert(self):
        conn = _make_conn()
        # Diverse book so we don't accidentally trip stuck_book.
        for i in range(30):
            _insert(conn, "KXHIGHCHI", gate=1, bid=45 + i % 4, ask=46 + i % 4)
        alert = MagicMock()
        findings = run_shadow_integrity_check(conn, alert_fn=alert)
        assert findings == []
        alert.assert_not_called()

    def test_critical_fires_alert(self):
        conn = _make_conn()
        _insert(conn, "KXHIGHCHI", gate=1, bid=0, ask=51)
        alert = MagicMock()
        findings = run_shadow_integrity_check(conn, alert_fn=alert)
        assert any(f.level == "critical" for f in findings)
        alert.assert_called_once()
        # Alert is called with level=critical
        _, kwargs = alert.call_args
        assert kwargs.get("level") == "critical"

    def test_warning_does_not_alert(self):
        """Warnings are informational only — no Telegram buzz."""
        conn = _make_conn()
        _insert(
            conn, "KXHIGHDEN", gate=1, bid=None, ask=None,
            n=MIN_ROWS_FOR_SIGNAL + 5,
        )
        alert = MagicMock()
        findings = run_shadow_integrity_check(conn, alert_fn=alert)
        assert any(f.level == "warning" for f in findings)
        alert.assert_not_called()

    def test_info_does_not_alert(self):
        conn = _make_conn()
        _insert(
            conn, "KXHIGHMIA", gate=1, bid=50, ask=51,
            n=MIN_ROWS_FOR_SIGNAL + 5,
        )
        alert = MagicMock()
        findings = run_shadow_integrity_check(conn, alert_fn=alert)
        assert any(f.level == "info" for f in findings)
        alert.assert_not_called()

    def test_alert_failure_does_not_raise(self):
        """Alert transport errors must not kill the monitor."""
        conn = _make_conn()
        _insert(conn, "KXHIGHCHI", gate=1, bid=0, ask=51)
        alert = MagicMock(side_effect=RuntimeError("telegram down"))
        # Should complete without raising.
        findings = run_shadow_integrity_check(conn, alert_fn=alert)
        assert any(f.level == "critical" for f in findings)

    def test_run_handles_db_error_gracefully(self):
        """A broken table must not propagate — monitor is best-effort."""
        conn = sqlite3.connect(":memory:")
        # No weather_mm_shadow table at all.
        alert = MagicMock()
        findings = run_shadow_integrity_check(conn, alert_fn=alert)
        assert findings == []
        alert.assert_not_called()


# ── Regression: the Apr-17 signature ────────────────────────────────────


class TestApr17Regression:
    """Simulate the exact shape of the Apr-17 → Apr-21 corruption.

    If this test fails we've re-introduced the bug class the monitor
    exists to catch.
    """
    def test_fabricated_zero_books_fire_critical(self):
        conn = _make_conn()
        # 6 series, 100 rows each, all with bid=0 ask=0 — the shape on
        # disk during the incident.
        for series in [
            "KXHIGHCHI", "KXHIGHNY", "KXHIGHDEN",
            "KXHIGHLAX", "KXHIGHMIA", "KXHIGHAUS",
        ]:
            _insert(conn, series, gate=1, bid=0, ask=0, n=100)
        alert = MagicMock()
        findings = run_shadow_integrity_check(conn, alert_fn=alert)
        critical = [f for f in findings if f.level == "critical"]
        # One critical per series.
        assert len(critical) == 6
        # Alert fires per critical.
        assert alert.call_count == 6
