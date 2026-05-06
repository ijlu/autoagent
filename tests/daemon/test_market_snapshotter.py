"""Tests for bot.daemon.market_snapshotter_poller.

Covers:
- Schema regression (table + indexes exist after init_db)
- _decide_write rules: first-obs, change, heartbeat, payload sampling
- _build_row column ordering + payload presence/absence
- Allowlist filtering (categorize_market gate)
- DB_WRITE_LOCK held during writes
- cleanup_old_snapshots: no-op when ttl_days is None, deletes correctly when set
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from bot.db import init_db, DB_WRITE_LOCK
from bot.daemon.market_snapshotter_poller import (
    HEARTBEAT_INTERVAL_S,
    MarketSnapshotPoller,
    PAYLOAD_SAMPLE_INTERVAL_S,
    _COLUMNS,
    _LastSnapshot,
    _build_row,
    _decide_write,
    _dollars_to_cents,
    _iso_to_unix,
    _parse_fp,
    _price_to_cents,
    _series_from_event,
    cleanup_old_snapshots,
)


def _market(ticker="KXHIGHNY-26MAY06-B75", **overrides):
    """Build a minimal /markets-style dict for tests."""
    base = {
        "ticker": ticker,
        "event_ticker": "KXHIGHNY-26MAY06",
        "series_ticker": "KXHIGHNY",
        "title": "Highest temperature in NYC",
        "market_type": "binary",
        "status": "open",
        "strike_type": "between",
        "floor_strike": 70.0,
        "cap_strike": 75.0,
        "yes_bid": 40,
        "yes_ask": 45,
        "no_bid": 55,
        "no_ask": 60,
        "last_price": 42,
        "volume": 100,
        "liquidity": 5000,
    }
    base.update(overrides)
    return base


class TestParsers(unittest.TestCase):
    """Field-shape parsers — Kalshi's actual /markets response uses
    ``*_dollars`` strings and ``*_fp`` decimal strings, not ints."""

    def test_price_to_cents_dollar_strings(self):
        # Live shape: "0.0200" → 2¢
        assert _price_to_cents("0.0200") == 2
        assert _price_to_cents("0.4500") == 45
        assert _price_to_cents("0.99") == 99

    def test_price_to_cents_int_cents_passthrough(self):
        # Test/legacy shape: 2 (already cents) → 2¢
        assert _price_to_cents(40) == 40
        assert _price_to_cents("45") == 45

    def test_price_to_cents_zero_means_no_book(self):
        # Empty / zero → None so readers can distinguish "no resting bid"
        # from "price is 0¢" (Kalshi's minimum price is 1¢).
        assert _price_to_cents("0.0000") is None
        assert _price_to_cents(0) is None
        assert _price_to_cents("") is None
        assert _price_to_cents(None) is None

    def test_price_to_cents_invalid_returns_none(self):
        assert _price_to_cents("garbage") is None

    def test_dollars_to_cents_amounts(self):
        # Always multiplies by 100 — for liquidity / notional fields whose
        # dollar value can exceed $1.
        assert _dollars_to_cents("1.0000") == 100
        assert _dollars_to_cents("50.5000") == 5050
        assert _dollars_to_cents("0.0000") is None  # zero → None

    def test_parse_fp(self):
        assert _parse_fp("7717.41") == 7717
        # 0 is a legitimate fp value (zero volume traded, etc.) — keep as 0
        assert _parse_fp("0") == 0
        assert _parse_fp("") is None
        assert _parse_fp(None) is None

    def test_iso_to_unix(self):
        # 2026-05-07T05:59:00Z → known unix
        ts = _iso_to_unix("2026-05-07T05:59:00Z")
        assert ts is not None
        # Sanity: ~2026 timestamp range
        assert 1_770_000_000 < ts < 1_790_000_000
        assert _iso_to_unix(None) is None
        assert _iso_to_unix("") is None
        assert _iso_to_unix("not-a-date") is None

    def test_iso_to_unix_passes_through_int(self):
        assert _iso_to_unix(1_700_000_000) == 1_700_000_000

    def test_series_from_event(self):
        assert _series_from_event("KXHIGHAUS-26MAY06") == "KXHIGHAUS"
        assert _series_from_event("KXFED-27APR") == "KXFED"
        assert _series_from_event("") is None
        assert _series_from_event(None) is None


class TestKalshiLiveShape(unittest.TestCase):
    """End-to-end ingest with the actual Kalshi /markets payload shape.

    Asserts the snapshotter reads ``*_dollars`` price strings, ``*_fp``
    decimal strings, and ISO timestamps correctly — and derives
    ``series_ticker`` from ``event_ticker`` (Kalshi doesn't return it
    on individual market rows).
    """

    LIVE_SAMPLE = {
        "ticker": "KXHIGHAUS-26MAY06-B86.5",
        "event_ticker": "KXHIGHAUS-26MAY06",
        "title": "Highest temperature in Austin",
        "subtitle": "AUS",
        "market_type": "binary",
        "status": "active",
        "strike_type": "between",
        "floor_strike": 86,
        "cap_strike": 87,
        "yes_bid_dollars": "0.0100",
        "yes_ask_dollars": "0.0200",
        "no_bid_dollars": "0.9800",
        "no_ask_dollars": "0.9900",
        "last_price_dollars": "0.0200",
        "previous_yes_bid_dollars": "0.0100",
        "previous_yes_ask_dollars": "0.0200",
        "previous_price_dollars": "0.0100",
        "yes_bid_size_fp": "214.50",
        "yes_ask_size_fp": "183.94",
        "volume_fp": "7717.41",
        "volume_24h_fp": "7702.41",
        "liquidity_dollars": "0.0000",
        "notional_value_dollars": "1.0000",
        "open_interest_fp": "6688.31",
        "tick_size": 1,
        "open_time": "2026-05-05T14:00:00Z",
        "close_time": "2026-05-07T05:59:00Z",
        "expected_expiration_time": "2026-05-07T14:00:00Z",
        "expiration_time": "2026-05-13T14:00:00Z",
        "result": "",
    }

    def test_build_row_parses_all_live_fields(self):
        row = _build_row(self.LIVE_SAMPLE, now_ts=1_700_000_000, include_payload=False)
        col = {name: row[i] for i, name in enumerate(_COLUMNS)}
        assert col["ticker"] == "KXHIGHAUS-26MAY06-B86.5"
        assert col["event_ticker"] == "KXHIGHAUS-26MAY06"
        # Series derived from event_ticker (Kalshi doesn't return it directly)
        assert col["series_ticker"] == "KXHIGHAUS"
        assert col["status"] == "active"
        # Price fields converted dollars→cents
        assert col["yes_bid"] == 1
        assert col["yes_ask"] == 2
        assert col["no_bid"] == 98
        assert col["no_ask"] == 99
        assert col["last_price"] == 2
        assert col["previous_yes_bid"] == 1
        assert col["previous_yes_ask"] == 2
        assert col["previous_price"] == 1
        # Depth (yes-side only — Kalshi /markets gives top-of-book).
        # Python's banker's rounding: round(214.5) = 214, round(183.94) = 184.
        assert col["yes_bid_size"] == 214
        assert col["yes_ask_size"] == 184
        # Volume + open_interest from _fp
        assert col["volume"] == 7717
        assert col["volume_24h"] == 7702
        assert col["open_interest"] == 6688
        # Notional ($1 → 100¢); liquidity 0 → None
        assert col["notional_value"] == 100
        assert col["liquidity"] is None
        # Strikes (floor/cap come through as-is)
        assert col["floor_strike"] == 86.0
        assert col["cap_strike"] == 87.0
        # ISO timestamps → unix
        # 2026-05-07T05:59:00Z corresponds to a known unix epoch range
        assert col["close_time"] is not None
        assert 1_770_000_000 < col["close_time"] < 1_790_000_000
        # Settlement / result — pre-settle the result string is empty
        assert col["result"] is None  # empty string normalized to None
        # Payload not included
        assert col["payload"] is None

    def test_change_detection_works_on_live_shape(self):
        from bot.daemon.market_snapshotter_poller import _market_to_signature
        sig1 = _market_to_signature(self.LIVE_SAMPLE)
        # yes_bid moved 1¢ → 2¢
        moved = dict(self.LIVE_SAMPLE)
        moved["yes_bid_dollars"] = "0.0200"
        sig2 = _market_to_signature(moved)
        assert sig1 != sig2
        # Same data → identical signature
        sig3 = _market_to_signature(dict(self.LIVE_SAMPLE))
        assert sig1 == sig3


class TestSchema(unittest.TestCase):
    def test_table_and_indexes_created(self):
        conn = init_db(":memory:")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "kalshi_market_snapshots" in tables
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='kalshi_market_snapshots'"
        )}
        assert "idx_market_snap_ts" in idx
        assert "idx_market_snap_event_ts" in idx
        assert "idx_market_snap_series_ts" in idx

    def test_columns_match_insert_tuple(self):
        """Schema column set must contain every name in _COLUMNS."""
        conn = init_db(":memory:")
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(kalshi_market_snapshots)"
        )}
        for c in _COLUMNS:
            assert c in cols, f"missing column: {c}"


class TestDecideWrite(unittest.TestCase):
    def test_first_observation_writes_with_payload(self):
        write, payload = _decide_write(_market(), cache=None, now_ts=1000)
        assert write is True
        assert payload is True

    def test_no_change_no_heartbeat_skips(self):
        cache = _LastSnapshot(
            ts=1000,
            yes_bid=40, yes_ask=45, no_bid=55, no_ask=60,
            last_price=42, volume=100, status="open",
            last_payload_ts=1000,
        )
        # 60s later — under 300s heartbeat. Same quote.
        write, payload = _decide_write(_market(), cache, now_ts=1060)
        assert write is False
        assert payload is False

    def test_quote_change_writes_no_payload(self):
        cache = _LastSnapshot(
            ts=1000, yes_bid=40, yes_ask=45, no_bid=55, no_ask=60,
            last_price=42, volume=100, status="open",
            last_payload_ts=1000,
        )
        # yes_bid moved 40 -> 41
        write, payload = _decide_write(_market(yes_bid=41), cache, now_ts=1060)
        assert write is True
        # Payload only on first/status-change/hourly, not on every quote tick
        assert payload is False

    def test_heartbeat_due_writes_no_payload(self):
        cache = _LastSnapshot(
            ts=1000, yes_bid=40, yes_ask=45, no_bid=55, no_ask=60,
            last_price=42, volume=100, status="open",
            last_payload_ts=1000,
        )
        write, payload = _decide_write(
            _market(), cache, now_ts=1000 + HEARTBEAT_INTERVAL_S
        )
        assert write is True
        assert payload is False

    def test_status_transition_writes_with_payload(self):
        cache = _LastSnapshot(
            ts=1000, yes_bid=40, yes_ask=45, no_bid=55, no_ask=60,
            last_price=42, volume=100, status="open",
            last_payload_ts=1000,
        )
        write, payload = _decide_write(
            _market(status="closed"), cache, now_ts=1060,
        )
        assert write is True
        assert payload is True

    def test_hourly_payload_sample(self):
        # Quote changed AND payload-sample window elapsed → payload included
        cache = _LastSnapshot(
            ts=1000, yes_bid=40, yes_ask=45, no_bid=55, no_ask=60,
            last_price=42, volume=100, status="open",
            last_payload_ts=1000,
        )
        # 1h+1s later, quote moves
        write, payload = _decide_write(
            _market(yes_bid=41),
            cache,
            now_ts=1000 + PAYLOAD_SAMPLE_INTERVAL_S + 1,
        )
        assert write is True
        assert payload is True

    def test_heartbeat_after_payload_window_includes_payload(self):
        """Heartbeat after the payload window expires should include payload.

        Quiet markets without status transitions still need periodic full
        captures to track schema evolution."""
        cache = _LastSnapshot(
            ts=1000, yes_bid=40, yes_ask=45, no_bid=55, no_ask=60,
            last_price=42, volume=100, status="open",
            last_payload_ts=1000,
        )
        # 1h+1s later — heartbeat due, payload window expired
        write, payload = _decide_write(
            _market(), cache,
            now_ts=1000 + PAYLOAD_SAMPLE_INTERVAL_S + 1,
        )
        assert write is True
        assert payload is True


class TestBuildRow(unittest.TestCase):
    def test_row_length_matches_columns(self):
        row = _build_row(_market(), now_ts=1000, include_payload=False)
        assert len(row) == len(_COLUMNS)

    def test_payload_null_when_excluded(self):
        row = _build_row(_market(), now_ts=1000, include_payload=False)
        # payload is the last column
        assert row[-1] is None

    def test_payload_json_when_included(self):
        m = _market()
        row = _build_row(m, now_ts=1000, include_payload=True)
        payload = row[-1]
        assert payload is not None
        decoded = json.loads(payload)
        assert decoded["ticker"] == m["ticker"]
        assert decoded["yes_bid"] == 40

    def test_fixed_point_parsing_uses_round(self):
        """Per CLAUDE.md known bug pattern #5 — round, not int."""
        row = _build_row(
            _market(yes_bid="40.7", yes_ask="44.6"),
            now_ts=1000, include_payload=False,
        )
        # Index of yes_bid in _COLUMNS
        yb_idx = _COLUMNS.index("yes_bid")
        ya_idx = _COLUMNS.index("yes_ask")
        assert row[yb_idx] == 41   # round(40.7) = 41 (not int(40.7) = 40)
        assert row[ya_idx] == 45

    def test_null_inputs_pass_through(self):
        row = _build_row(_market(yes_bid=None, volume=None),
                         now_ts=1000, include_payload=False)
        yb_idx = _COLUMNS.index("yes_bid")
        v_idx = _COLUMNS.index("volume")
        assert row[yb_idx] is None
        assert row[v_idx] is None


class TestPollerIngest(unittest.TestCase):
    """End-to-end ingest behavior on an in-memory DB."""

    def setUp(self):
        self.conn = init_db(":memory:")
        self.api_get = MagicMock()
        self.poller = MarketSnapshotPoller(
            conn=self.conn,
            series=("KXHIGHNY",),
            api_get_fn=self.api_get,
        )

    def _set_response(self, markets, cursor=None):
        self.api_get.return_value = {"markets": markets, "cursor": cursor}

    def _count_rows(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM kalshi_market_snapshots"
        ).fetchone()[0]

    def test_first_poll_writes_one_row_per_ticker(self):
        self._set_response([_market(), _market(ticker="KXHIGHNY-26MAY06-B80")])
        result = self.poller._poll_once()
        assert result["rows_written"] == 2
        assert result["rows_with_payload"] == 2
        assert self._count_rows() == 2

    def test_unchanged_quote_no_heartbeat_skips(self):
        m = _market()
        with patch("bot.daemon.market_snapshotter_poller.time.time",
                   return_value=1_700_000_000):
            self._set_response([m])
            self.poller._poll_once()
        assert self._count_rows() == 1
        # 60s later — under 300s heartbeat. Same quote.
        with patch("bot.daemon.market_snapshotter_poller.time.time",
                   return_value=1_700_000_060):
            self._set_response([m])
            self.poller._poll_once()
        assert self._count_rows() == 1  # still one — second was skipped

    def test_quote_change_writes_second_row(self):
        m = _market()
        with patch("bot.daemon.market_snapshotter_poller.time.time",
                   return_value=1_700_000_000):
            self._set_response([m])
            self.poller._poll_once()
        with patch("bot.daemon.market_snapshotter_poller.time.time",
                   return_value=1_700_000_060):
            self._set_response([_market(yes_bid=41)])
            self.poller._poll_once()
        assert self._count_rows() == 2
        rows = self.conn.execute(
            "SELECT payload FROM kalshi_market_snapshots ORDER BY ts"
        ).fetchall()
        assert rows[0][0] is not None  # first-obs has payload
        assert rows[1][0] is None      # quote-change does not

    def test_status_transition_includes_payload(self):
        m = _market()
        with patch("bot.daemon.market_snapshotter_poller.time.time",
                   return_value=1_700_000_000):
            self._set_response([m])
            self.poller._poll_once()
        with patch("bot.daemon.market_snapshotter_poller.time.time",
                   return_value=1_700_000_060):
            self._set_response([_market(status="closed")])
            self.poller._poll_once()
        rows = self.conn.execute(
            "SELECT status, payload FROM kalshi_market_snapshots ORDER BY ts"
        ).fetchall()
        assert rows[0][0] == "open"
        assert rows[0][1] is not None
        assert rows[1][0] == "closed"
        assert rows[1][1] is not None  # status transition → payload

    def test_non_weather_filtered_out(self):
        # Title + ticker that shouldn't categorize as weather
        weird = {
            "ticker": "KXFAKE-001",
            "title": "Some unrelated thing",
            "series_ticker": "KXHIGHNY",  # accidentally in our allowlist
            "status": "open",
            "yes_bid": 10, "yes_ask": 90, "no_bid": 90, "no_ask": 10,
        }
        self._set_response([weird, _market()])
        self.poller._poll_once()
        # Only the legitimate weather market gets stored.
        assert self._count_rows() == 1
        assert self.poller._non_weather_skipped == 1

    def test_pagination_walks_cursor(self):
        """Multi-page response should be fully consumed."""
        # Two pages: 200 markets + 1 market.
        page1 = [_market(ticker=f"KXHIGHNY-26MAY06-B{i}") for i in range(200)]
        page2 = [_market(ticker="KXHIGHNY-26MAY06-EXTRA")]

        responses = [
            {"markets": page1, "cursor": "next"},
            {"markets": page2, "cursor": None},
        ]
        self.api_get.side_effect = responses

        self.poller._poll_once()
        assert self._count_rows() == 201

    def test_one_failed_series_doesnt_kill_poll(self):
        """If /markets fails for one series, others still get persisted."""
        poller = MarketSnapshotPoller(
            conn=self.conn,
            series=("KXHIGHNY", "KXHIGHCHI"),
            api_get_fn=MagicMock(),
        )

        def side_effect(url):
            if "KXHIGHNY" in url:
                raise RuntimeError("HTTP 500")
            return {"markets": [_market(
                ticker="KXHIGHCHI-26MAY06-B75",
                series_ticker="KXHIGHCHI",
                title="Highest temperature in Chicago",
            )], "cursor": None}

        poller._api_get.side_effect = side_effect
        result = poller._poll_once()
        # CHI persisted; NY skipped
        assert result["rows_written"] == 1
        rows = self.conn.execute(
            "SELECT series_ticker FROM kalshi_market_snapshots"
        ).fetchall()
        assert rows == [("KXHIGHCHI",)]

    def test_persist_holds_db_write_lock(self):
        """Sanity: snapshotter writes go through db_write_ctx, which holds
        DB_WRITE_LOCK. Verify by trying to acquire it from another thread
        while a write is in progress."""
        # We can't easily intercept inside db_write_ctx; instead, hold the
        # lock from another thread and confirm writes block.
        DB_WRITE_LOCK.acquire()
        try:
            self._set_response([_market()])
            done = threading.Event()
            err: list[Exception] = []

            def _run():
                try:
                    self.poller._poll_once()
                except Exception as e:
                    err.append(e)
                finally:
                    done.set()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            # Should still be blocked on the lock
            blocked = not done.wait(timeout=0.3)
            assert blocked, "snapshotter wrote without holding DB_WRITE_LOCK"
        finally:
            DB_WRITE_LOCK.release()
        # Now it should complete
        assert done.wait(timeout=2.0)
        assert not err, f"unexpected errors: {err}"


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        # Seed rows at known timestamps.
        from bot.daemon.market_snapshotter_poller import _build_row
        rows = [
            _build_row(_market(ticker=f"T{i}"), now_ts=ts, include_payload=False)
            for i, ts in enumerate([1000, 2000, 3000, 4000])
        ]
        from bot.db import db_write_ctx
        from bot.daemon.market_snapshotter_poller import _INSERT_SQL
        with db_write_ctx(self.conn):
            self.conn.executemany(_INSERT_SQL, rows)

    def test_no_op_when_ttl_none(self):
        deleted = cleanup_old_snapshots(self.conn, ttl_days=None, now_ts=10_000)
        assert deleted == 0
        assert self.conn.execute(
            "SELECT COUNT(*) FROM kalshi_market_snapshots"
        ).fetchone()[0] == 4

    def test_no_op_when_ttl_zero(self):
        deleted = cleanup_old_snapshots(self.conn, ttl_days=0, now_ts=10_000)
        assert deleted == 0

    def test_deletes_older_than_cutoff(self):
        # Cutoff = now (5000) - 1 day (86400) = -81400. All rows newer.
        # Pick a now_ts where some rows are stale:
        # ts=1000 corresponds to 1970; cutoff: now - 86400 → if now=86401+1000=87401,
        # cutoff = 1001, so ts=1000 is deleted, ts=2000+ kept.
        deleted = cleanup_old_snapshots(self.conn, ttl_days=1, now_ts=87_401)
        assert deleted == 1
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM kalshi_market_snapshots"
        ).fetchone()[0]
        assert remaining == 3

    def test_batched_delete_finishes(self):
        """Force batch_rows=2 to confirm the loop terminates correctly."""
        deleted = cleanup_old_snapshots(
            self.conn, ttl_days=1, now_ts=10_000_000, batch_rows=2,
        )
        assert deleted == 4
        assert self.conn.execute(
            "SELECT COUNT(*) FROM kalshi_market_snapshots"
        ).fetchone()[0] == 0


if __name__ == "__main__":
    unittest.main()
