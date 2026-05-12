"""Tests for bot.daemon.fills_writer (T3.1).

Covers the Step 2 skeleton: schema shape, source-tagger prefix table,
row-construction helper, row-count warning. The ingest_page / sync_since
implementations are Step 3 — their bodies raise NotImplementedError here
and are tested once they land.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from bot.daemon.fills_writer import (
    ALLOWED_SOURCES,
    FillsWriter,
    ROW_COUNT_WARNING_THRESHOLD,
    default_source_tagger,
)
from bot.db import init_db


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------

class TestFillsLedgerSchema:
    """init_db should create fills_ledger with exactly the columns the T3
    scoping doc §4 lists, plus all four indexes."""

    def test_table_exists(self):
        conn = init_db(":memory:")
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='fills_ledger'"
        ).fetchone()
        assert row is not None

    def test_all_required_columns_present(self):
        conn = init_db(":memory:")
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(fills_ledger)"
        ).fetchall()}
        required = {
            "trade_id", "order_id", "client_order_id",
            "ticker", "series", "family",
            "side", "action", "contracts",
            "yes_price_cents", "no_price_cents",
            "is_taker", "fee_cents",
            "fill_ts_iso", "fill_ts_unix", "ingested_ts_unix",
            "live_mode", "source",
        }
        missing = required - cols
        assert not missing, f"fills_ledger missing columns: {missing}"

    def test_no_cycle_id_column(self):
        """Q7: cycle_id deliberately dropped from schema. Readers that
        want cycle attribution must derive it from timestamps or the
        client_order_id, not from a (always-null) column."""
        conn = init_db(":memory:")
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(fills_ledger)"
        ).fetchall()}
        assert "cycle_id" not in cols

    def test_primary_key_is_trade_id(self):
        conn = init_db(":memory:")
        pk_cols = [
            row[1] for row in conn.execute(
                "PRAGMA table_info(fills_ledger)"
            ).fetchall() if row[5]  # pk column
        ]
        assert pk_cols == ["trade_id"]

    def test_duplicate_trade_id_rejected(self):
        """INSERT OR IGNORE semantics are the writer's job; the schema
        must enforce uniqueness so a bare INSERT would fail."""
        conn = init_db(":memory:")
        row = _valid_row(trade_id="t1")
        _insert_raw(conn, row)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw(conn, row)

    def test_source_not_null(self):
        """source is NOT NULL by design (Q4): writer must always
        classify. Schema enforces the invariant so a bug can't silently
        leave it empty."""
        conn = init_db(":memory:")
        row = _valid_row()
        row["source"] = None
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw(conn, row)

    def test_all_indexes_created(self):
        conn = init_db(":memory:")
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='fills_ledger'"
        ).fetchall()}
        for idx in (
            "idx_fills_ticker_ts",
            "idx_fills_order_id",
            "idx_fills_series_ts",
            "idx_fills_family_ts",
        ):
            assert idx in indexes, f"Missing index: {idx}"

    def test_ticker_ts_index_is_usable(self):
        """EXPLAIN QUERY PLAN should show the index being consulted for
        the per-ticker per-time query pattern that kill-switch P&L and
        shadow-row joins use."""
        conn = init_db(":memory:")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM fills_ledger "
            "WHERE ticker = ? AND fill_ts_unix > ?",
            ("KXHIGHNY-26APR20-B7476", 0.0),
        ).fetchall()
        # Plan rows: (id, parent, notused, detail)
        detail = " ".join(str(row[3]) for row in plan)
        assert "idx_fills_ticker_ts" in detail, (
            f"EXPLAIN did not use ticker_ts index: {detail}"
        )


# ---------------------------------------------------------------------------
# Source tagger (Q4 prefix table)
# ---------------------------------------------------------------------------

class TestDefaultSourceTagger:
    @pytest.mark.parametrize("cid,expected", [
        ("mm_wx_KXHIGHNY_26APR20_1745e8a1", "mm_quote"),
        ("mm_sc_KXFED_27APR_T2_00_1745678900", "safe_compounder"),
        ("mm_exit_KXHIGHMIA_26APR18_T75_1745678900", "exit"),
        ("mm_dir_KXBTC_26APR20_B95000_1745678900", "directional"),
        ("mm_v1_something_ancient", "legacy"),
        ("mm_", "legacy"),  # bare mm_ with no sub-prefix
        ("", "manual"),
        (None, "manual"),
        ("totally_external_id", "manual"),
        ("user_placed_via_ui", "manual"),
    ])
    def test_prefix_mapping(self, cid, expected):
        assert default_source_tagger(cid) == expected

    def test_every_output_is_in_allowed_set(self):
        """Invariant: default_source_tagger never returns an unknown
        value. Check a spread of inputs."""
        for cid in [
            None, "", "mm_", "mm_wx_x", "mm_sc_x", "mm_exit_x", "mm_dir_x",
            "mm_mystery_x", "not_mm_at_all", "random",
        ]:
            assert default_source_tagger(cid) in ALLOWED_SOURCES

    def test_sub_prefix_priority_over_bare_mm(self):
        """mm_exit_* must NOT be classified as legacy just because it
        starts with mm_. Sub-prefixes win."""
        assert default_source_tagger("mm_exit_foo_123") == "exit"
        assert default_source_tagger("mm_dir_foo_123") == "directional"


# ---------------------------------------------------------------------------
# FillsWriter — instantiation + helpers
# ---------------------------------------------------------------------------

class TestFillsWriter:
    def test_constructor_accepts_real_db(self):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        assert w.conn is conn
        assert w.source_tagger is default_source_tagger

    def test_custom_source_tagger_is_used(self):
        conn = init_db(":memory:")
        custom_called: list[str] = []

        def tagger(cid):
            custom_called.append(cid or "<none>")
            return "manual"

        w = FillsWriter(conn, source_tagger=tagger)
        row = w._fill_to_row(_valid_fill_dict(), live_mode=True)
        assert row is not None
        assert custom_called == ["mm_wx_KXHIGHNY_t1"]
        assert row["source"] == "manual"

# ---------------------------------------------------------------------------
# _fill_to_row (pure helper, no I/O)
# ---------------------------------------------------------------------------

class TestFillToRow:
    def test_happy_path_weather_mm(self):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        fill = _valid_fill_dict(
            trade_id="t_happy",
            ticker="KXHIGHNY-26APR20-B7476",
            client_order_id="mm_wx_KXHIGHNY_26APR20_B7476_1745",
            side="yes", action="buy", count=10,
            yes_price=47, no_price=53,
            is_taker=False,
        )
        row = w._fill_to_row(fill, live_mode=True)
        assert row is not None
        assert row["trade_id"] == "t_happy"
        assert row["series"] == "KXHIGHNY-26APR20"
        assert row["family"] == "KXHIGHNY"
        assert row["source"] == "mm_quote"
        assert row["is_taker"] == 0
        assert row["live_mode"] == 1
        assert row["yes_price_cents"] == 47
        assert row["no_price_cents"] == 53
        # Maker fee for 10 contracts at 47¢ should be > 0
        assert row["fee_cents"] > 0

    def test_taker_fee_is_larger_than_maker_fee(self):
        """Sanity: same ticker/price/contracts, only is_taker differs.
        Fee must be higher when is_taker=True."""
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        base = _valid_fill_dict(side="yes", yes_price=47, no_price=53, count=10)
        maker = w._fill_to_row({**base, "is_taker": False}, live_mode=True)
        taker = w._fill_to_row({**base, "is_taker": True, "trade_id": "t_taker"},
                               live_mode=True)
        assert taker["fee_cents"] > maker["fee_cents"]

    def test_no_side_uses_no_price_for_fee(self):
        """Fee math: for a NO buy, our price is no_price, not yes_price.
        This catches a classic bug where fee gets computed at the wrong
        side's price."""
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        fill = _valid_fill_dict(
            side="no", action="buy", count=10,
            yes_price=5, no_price=95, is_taker=False,
        )
        row = w._fill_to_row(fill, live_mode=True)
        # Maker fee at 95¢ is much smaller than at 5¢ (Kalshi's fee
        # curve is round-trip symmetric). If the code accidentally
        # used yes_price (5¢), we'd get a very different number.
        from bot.core.money import kalshi_maker_fee
        assert row["fee_cents"] == kalshi_maker_fee(10, 95)

    def test_malformed_fill_returns_none_not_raise(self):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        bad = {"trade_id": "oops", "ticker": "KXX"}  # most fields missing
        assert w._fill_to_row(bad, live_mode=False) is None

    def test_tagger_returning_invalid_source_falls_back_to_manual(self):
        """If a custom source_tagger returns something outside
        ALLOWED_SOURCES, the writer logs and falls back to 'manual'
        rather than writing a corrupt value."""
        conn = init_db(":memory:")
        w = FillsWriter(conn, source_tagger=lambda _: "gibberish")
        row = w._fill_to_row(_valid_fill_dict(), live_mode=True)
        assert row["source"] == "manual"

    def test_iso_timestamp_parses_correctly(self):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        fill = _valid_fill_dict(created_time="2026-04-20T18:23:11.402Z")
        row = w._fill_to_row(fill, live_mode=False)
        # 2026-04-20T18:23:11.402Z → 1776709391.402
        assert 1_776_709_390 < row["fill_ts_unix"] < 1_776_709_393
        assert row["fill_ts_iso"] == "2026-04-20T18:23:11.402Z"


# ---------------------------------------------------------------------------
# Row-count warning (Q1)
# ---------------------------------------------------------------------------

class TestRowCountWarning:
    def test_no_warning_below_threshold(self, caplog):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
            w._check_row_count()
        assert not any(
            "threshold" in rec.getMessage() for rec in caplog.records
        )

    def test_warning_fires_once_at_threshold(self, caplog, monkeypatch):
        """Patch the threshold to 2 so we don't need to insert 2M rows.
        Two rows present → warning fires on first _check_row_count call,
        subsequent calls are silent (one-shot latch)."""
        monkeypatch.setattr(
            "bot.daemon.fills_writer.ROW_COUNT_WARNING_THRESHOLD", 2,
        )
        conn = init_db(":memory:")
        _insert_raw(conn, _valid_row(trade_id="t1"))
        _insert_raw(conn, _valid_row(trade_id="t2"))

        w = FillsWriter(conn)

        with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
            w._check_row_count()
            w._check_row_count()  # second call must be silent
            w._check_row_count()

        warnings = [
            rec for rec in caplog.records
            if "ledger has" in rec.getMessage()
        ]
        assert len(warnings) == 1, (
            f"Expected exactly one warning, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}"
        )


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_COLUMNS = [
    "trade_id", "order_id", "client_order_id",
    "ticker", "series", "family",
    "side", "action", "contracts",
    "yes_price_cents", "no_price_cents",
    "is_taker", "fee_cents",
    "fill_ts_iso", "fill_ts_unix", "ingested_ts_unix",
    "live_mode", "source",
]


def _valid_row(**overrides) -> dict:
    """Return a dict suitable for raw INSERT into fills_ledger. Every
    NOT NULL column populated. Caller can override any field."""
    base = {
        "trade_id": "trade_default",
        "order_id": "order_default",
        "client_order_id": "mm_wx_KXHIGHNY_t1",
        "ticker": "KXHIGHNY-26APR20-B7476",
        "series": "KXHIGHNY-26APR20",
        "family": "KXHIGHNY",
        "side": "yes",
        "action": "buy",
        "contracts": 10,
        "yes_price_cents": 47,
        "no_price_cents": 53,
        "is_taker": 0,
        "fee_cents": 5,
        "fill_ts_iso": "2026-04-20T18:23:11.402Z",
        "fill_ts_unix": 1_776_104_591.402,
        "ingested_ts_unix": 1_776_104_592.0,
        "live_mode": 0,
        "source": "mm_quote",
    }
    base.update(overrides)
    return base


def _insert_raw(conn: sqlite3.Connection, row: dict) -> None:
    placeholders = ", ".join("?" * len(_COLUMNS))
    conn.execute(
        f"INSERT INTO fills_ledger ({', '.join(_COLUMNS)}) "
        f"VALUES ({placeholders})",
        tuple(row[c] for c in _COLUMNS),
    )
    conn.commit()


def _valid_fill_dict(**overrides) -> dict:
    """Return a Kalshi-shaped /fills page dict (what the API returns
    per fill), for testing _fill_to_row.

    Uses the LEGACY cents-int format. The dollar-string format is
    exercised separately in TestDualFormatPayloads."""
    base = {
        "trade_id": "t1",
        "order_id": "o1",
        "ticker": "KXHIGHNY-26APR20-B7476",
        "side": "yes",
        "action": "buy",
        "count": 10,
        "yes_price": 47,
        "no_price": 53,
        "is_taker": False,
        "created_time": "2026-04-20T18:23:11.402Z",
        "client_order_id": "mm_wx_KXHIGHNY_t1",
    }
    base.update(overrides)
    return base


def _dollar_string_fill_dict(**overrides) -> dict:
    """Return a fill dict using Kalshi's NEW dollar-string format.

    This is the actual response shape from /fills as of May 2026, and
    the format that broke fills_writer in the 2026-05-03 cross-bracket
    canary (18 fills silently dropped). Capturing the live response
    shape verbatim so future Kalshi format drift is caught."""
    base = {
        "trade_id": "76372d92-f9b7-78fc-fa05-f61f83575422",
        "order_id": "59b0c838-b6c4-4e32-9699-b133e4bdc794",
        "fill_id": "76372d92-f9b7-78fc-fa05-f61f83575422",
        "ticker": "KXHIGHNY-26MAY03-B59.5",
        "market_ticker": "KXHIGHNY-26MAY03-B59.5",
        "side": "no",
        "action": "buy",
        "count_fp": "1.00",
        "yes_price_dollars": "0.9100",
        "no_price_dollars": "0.0900",
        "fee_cost": "0.010000",
        "is_taker": True,
        "created_time": "2026-05-03T23:07:15.646414Z",
        "ts": 1777849635,
        "subaccount_number": 0,
        "client_order_id": "mm_xb_KXHIGHNY-26MAY03_5_1777849635011",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Dual-format parsing (cents-int + dollar-string)
# ---------------------------------------------------------------------------


class TestDualFormatPayloads:
    """Ensures fills_writer handles BOTH the legacy cents-int format
    and the dollar-string format Kalshi switched to. Postmortem:
    2026-05-03 cross-bracket canary lost track of 18 live fills
    because fills_writer only knew the legacy keys."""

    def test_dollar_string_format_parses_cleanly(self):
        """The exact response shape from a real cross-bracket fill on
        2026-05-03 must produce a valid row, not a malformed-fill skip."""
        from bot.daemon.fills_writer import FillsWriter
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        fill = _dollar_string_fill_dict()
        row = w._fill_to_row(fill, live_mode=True)
        assert row is not None, (
            "dollar-string format must produce a valid row — pre-fix "
            "this returned None and 18 production fills were dropped"
        )
        assert row["contracts"] == 1
        # 0.0900 dollars = 9 cents
        assert row["no_price_cents"] == 9
        # 0.9100 dollars = 91 cents
        assert row["yes_price_cents"] == 91
        assert row["is_taker"] == 1
        # Source tagged from client_order_id ``mm_xb_*`` → routes to
        # ``cross_bracket`` (was ``legacy`` before T3.2 added a
        # cross-bracket arm to default_source_tagger).
        assert row["source"] == "cross_bracket"

    def test_legacy_cents_int_format_still_parses(self):
        """The original cents-int format must still work after the
        defensive dual-format refactor."""
        from bot.daemon.fills_writer import FillsWriter
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        row = w._fill_to_row(_valid_fill_dict(), live_mode=False)
        assert row is not None
        assert row["contracts"] == 10
        assert row["yes_price_cents"] == 47
        assert row["no_price_cents"] == 53

    def test_parse_count_handles_both_shapes(self):
        from bot.daemon.fills_writer import _parse_count
        assert _parse_count({"count": 5}) == 5
        assert _parse_count({"count_fp": "5.00"}) == 5
        # Old field wins when both are present (defensive — newer
        # libraries might emit both during migration).
        assert _parse_count({"count": 3, "count_fp": "999.0"}) == 3
        # Missing → None
        assert _parse_count({}) is None
        assert _parse_count({"count": None, "count_fp": None}) is None
        # Garbage → None (don't crash)
        assert _parse_count({"count_fp": "not a number"}) is None

    def test_parse_price_cents_handles_both_shapes(self):
        from bot.daemon.fills_writer import _parse_price_cents
        # Cents int
        assert _parse_price_cents({"yes_price": 91}, "yes") == 91
        assert _parse_price_cents({"no_price": 9}, "no") == 9
        # Dollar string
        assert _parse_price_cents({"yes_price_dollars": "0.9100"}, "yes") == 91
        assert _parse_price_cents({"no_price_dollars": "0.0900"}, "no") == 9
        # Old field wins when both present
        assert _parse_price_cents(
            {"yes_price": 50, "yes_price_dollars": "0.99"}, "yes",
        ) == 50
        # Floating-point boundary (0.0900 must NOT round to 8)
        assert _parse_price_cents({"no_price_dollars": "0.0900"}, "no") == 9
        # Missing → None
        assert _parse_price_cents({}, "yes") is None
        # Garbage → None
        assert _parse_price_cents(
            {"yes_price_dollars": "junk"}, "yes",
        ) is None

    def test_warning_includes_raw_keys_when_malformed(self, caplog):
        """When a fill is rejected, the log should include the actual
        keys present so a future Kalshi-format drift is diagnosable
        without re-instrumenting code."""
        import logging
        from bot.daemon.fills_writer import FillsWriter
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        bad_fill = {
            "trade_id": "tx",
            "order_id": "ox",
            "ticker": "KXHIGHNY-26MAY03-B59.5",
            # Missing every other required field.
        }
        with caplog.at_level(logging.WARNING):
            row = w._fill_to_row(bad_fill, live_mode=False)
        assert row is None
        # The log message includes "raw_keys" so a Kalshi shape drift
        # surfaces the unknown key set.
        assert any(
            "raw_keys" in (rec.getMessage() + str(rec.args))
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# ingest_page
# ---------------------------------------------------------------------------

class TestIngestPage:
    def test_empty_page_returns_zero(self):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        assert w.ingest_page([], live_mode=False) == 0

    def test_inserts_new_rows_and_returns_count(self):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        fills = [
            _valid_fill_dict(trade_id="t1", order_id="o1"),
            _valid_fill_dict(trade_id="t2", order_id="o2"),
            _valid_fill_dict(trade_id="t3", order_id="o3"),
        ]
        inserted = w.ingest_page(fills, live_mode=True)
        assert inserted == 3

        rows = conn.execute(
            "SELECT trade_id, live_mode FROM fills_ledger ORDER BY trade_id"
        ).fetchall()
        assert [r[0] for r in rows] == ["t1", "t2", "t3"]
        # live_mode flag propagates
        assert all(r[1] == 1 for r in rows)

    def test_idempotent_on_same_page(self):
        """Re-ingesting an already-present trade_id is a no-op."""
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        fills = [_valid_fill_dict(trade_id="t_dup")]

        first = w.ingest_page(fills, live_mode=False)
        second = w.ingest_page(fills, live_mode=False)
        third = w.ingest_page(fills, live_mode=False)
        assert (first, second, third) == (1, 0, 0)

        count = conn.execute(
            "SELECT COUNT(*) FROM fills_ledger WHERE trade_id='t_dup'"
        ).fetchone()[0]
        assert count == 1

    def test_partial_overlap_inserts_only_new(self):
        """Page with some already-seen and some new trade_ids returns the
        delta, not the full size."""
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        w.ingest_page(
            [_valid_fill_dict(trade_id="t1"), _valid_fill_dict(trade_id="t2")],
            live_mode=False,
        )
        mixed = [
            _valid_fill_dict(trade_id="t1"),  # dup
            _valid_fill_dict(trade_id="t2"),  # dup
            _valid_fill_dict(trade_id="t3"),  # new
            _valid_fill_dict(trade_id="t4"),  # new
        ]
        inserted = w.ingest_page(mixed, live_mode=False)
        assert inserted == 2
        total = conn.execute(
            "SELECT COUNT(*) FROM fills_ledger"
        ).fetchone()[0]
        assert total == 4

    def test_malformed_fills_skipped_not_raised(self, caplog):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        good = _valid_fill_dict(trade_id="t_ok")
        malformed = {"trade_id": "t_bad"}  # missing every other field
        with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
            inserted = w.ingest_page([good, malformed], live_mode=False)
        # Only the good one lands; malformed logged and skipped.
        assert inserted == 1
        assert any("malformed fill" in r.getMessage() for r in caplog.records)

    def test_ingested_ts_preserved_across_retry(self):
        """Q6: INSERT OR IGNORE preserves the original ingested_ts_unix
        even if ingest_page is retried with the same fill later."""
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        fill = _valid_fill_dict(trade_id="t_retry")

        import time as _time
        t_before = _time.time()
        w.ingest_page([fill], live_mode=False)
        first_ingested = conn.execute(
            "SELECT ingested_ts_unix FROM fills_ledger WHERE trade_id='t_retry'"
        ).fetchone()[0]
        _time.sleep(0.05)
        w.ingest_page([fill], live_mode=False)
        second_ingested = conn.execute(
            "SELECT ingested_ts_unix FROM fills_ledger WHERE trade_id='t_retry'"
        ).fetchone()[0]
        assert first_ingested == second_ingested
        assert first_ingested >= t_before

    def test_source_derived_from_client_order_id(self):
        conn = init_db(":memory:")
        w = FillsWriter(conn)
        w.ingest_page([
            _valid_fill_dict(trade_id="t_wx", client_order_id="mm_wx_x_1"),
            _valid_fill_dict(trade_id="t_ex", client_order_id="mm_exit_x_1"),
            _valid_fill_dict(trade_id="t_di", client_order_id="mm_dir_x_1"),
            _valid_fill_dict(trade_id="t_mn", client_order_id=None),
        ], live_mode=False)
        rows = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT trade_id, source FROM fills_ledger"
            ).fetchall()
        }
        assert rows == {
            "t_wx": "mm_quote",
            "t_ex": "exit",
            "t_di": "directional",
            "t_mn": "manual",
        }


# ---------------------------------------------------------------------------
# sync_since
# ---------------------------------------------------------------------------

class _FakeApi:
    """Scripted api_get: returns each response in ``pages`` on successive
    calls, then raises if called again. Records each received path."""

    def __init__(self, pages: list[dict], *, raise_on_call: int | None = None):
        self.pages = pages
        self.paths: list[str] = []
        self.raise_on_call = raise_on_call

    def __call__(self, path: str) -> dict:
        self.paths.append(path)
        idx = len(self.paths) - 1
        if self.raise_on_call is not None and idx == self.raise_on_call:
            raise RuntimeError("simulated API failure")
        if idx >= len(self.pages):
            raise AssertionError(f"unexpected extra call #{idx}: {path}")
        return self.pages[idx]


class TestSyncSince:
    def test_single_page_no_cursor(self):
        api = _FakeApi(pages=[
            {"fills": [
                _valid_fill_dict(trade_id="t1"),
                _valid_fill_dict(trade_id="t2"),
            ], "cursor": ""},
        ])
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        inserted = w.sync_since(1_776_000_000.0, live_mode=False)
        assert inserted == 2
        assert len(api.paths) == 1
        assert "min_ts=1776000000" in api.paths[0]
        assert "/portfolio/fills" in api.paths[0]
        assert "cursor=" not in api.paths[0]

    def test_multi_page_pagination(self):
        api = _FakeApi(pages=[
            {"fills": [_valid_fill_dict(trade_id="t1")], "cursor": "c1"},
            {"fills": [_valid_fill_dict(trade_id="t2")], "cursor": "c2"},
            {"fills": [_valid_fill_dict(trade_id="t3")], "cursor": ""},
        ])
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        inserted = w.sync_since(0.0, live_mode=True)
        assert inserted == 3
        assert len(api.paths) == 3
        assert "cursor=c1" in api.paths[1]
        assert "cursor=c2" in api.paths[2]

    def test_api_failure_returns_zero(self, caplog):
        api = _FakeApi(pages=[], raise_on_call=0)
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
            inserted = w.sync_since(0.0, live_mode=False)
        assert inserted == 0
        count = conn.execute("SELECT COUNT(*) FROM fills_ledger").fetchone()[0]
        assert count == 0
        assert any(
            "/portfolio/fills fetch failed" in r.getMessage()
            for r in caplog.records
        )

    def test_partial_pagination_failure_returns_zero(self, caplog):
        """If page 2 of 3 raises, the entire sync aborts and inserts nothing.
        Partial ingestion would break the 'scan forward from max(fill_ts_unix)'
        invariant next tick."""
        api = _FakeApi(
            pages=[
                {"fills": [_valid_fill_dict(trade_id="t1")], "cursor": "c1"},
            ],
            raise_on_call=1,  # second call raises
        )
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
            inserted = w.sync_since(0.0, live_mode=False)
        assert inserted == 0
        # Nothing persisted — partial batches are not flushed.
        count = conn.execute("SELECT COUNT(*) FROM fills_ledger").fetchone()[0]
        assert count == 0

    def test_empty_response_returns_zero(self):
        api = _FakeApi(pages=[{"fills": [], "cursor": ""}])
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        assert w.sync_since(0.0, live_mode=False) == 0

    def test_missing_fills_key_treated_as_empty(self):
        """Defensive: a malformed API response with no 'fills' key must
        not crash — treat as empty page."""
        api = _FakeApi(pages=[{"cursor": ""}])
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        assert w.sync_since(0.0, live_mode=False) == 0

    def test_since_unix_floored_to_int(self):
        api = _FakeApi(pages=[{"fills": [], "cursor": ""}])
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        w.sync_since(1_776_000_123.987, live_mode=False)
        # Fractional seconds should be dropped — Kalshi min_ts is unix secs.
        assert "min_ts=1776000123" in api.paths[0]

    def test_max_pages_truncation(self, monkeypatch, caplog):
        """Infinite-cursor bug protection: we cap at MAX_PAGES and warn."""
        # Build a fake that always returns a non-empty cursor.
        class _InfApi:
            def __init__(self):
                self.count = 0
            def __call__(self, path):
                self.count += 1
                return {
                    "fills": [_valid_fill_dict(trade_id=f"t_{self.count}")],
                    "cursor": "never_empty",
                }
        api = _InfApi()
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)

        with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
            inserted = w.sync_since(0.0, live_mode=False)

        # 100 pages × 1 fill each, all distinct.
        assert inserted == 100
        assert api.count == 100
        assert any(
            "hit MAX_PAGES" in r.getMessage() for r in caplog.records
        )

    def test_check_row_count_called(self, monkeypatch):
        """sync_since must invoke the Q1 row-count warning check so we
        notice ledger growth even in normal operation."""
        api = _FakeApi(pages=[{"fills": [], "cursor": ""}])
        conn = init_db(":memory:")
        w = FillsWriter(conn, api_get=api)
        calls = []
        monkeypatch.setattr(
            w, "_check_row_count", lambda: calls.append(None)
        )
        w.sync_since(0.0, live_mode=False)
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Scheduler integration: _run_fills_sync
# ---------------------------------------------------------------------------

class TestRunFillsSync:
    """Exercises the wrapper the scheduler actually invokes. Verifies the
    since_unix derivation (daemon-start for empty ledger, max(fill_ts_unix)
    minus overlap otherwise) and the never-raises contract."""

    def _writer_with_pages(self, pages):
        api = _FakeApi(pages=pages)
        conn = init_db(":memory:")
        writer = FillsWriter(conn, api_get=api)
        return writer, conn, api

    def test_empty_ledger_uses_daemon_start(self):
        from bot.daemon.main import _run_fills_sync, FILLS_SYNC_OVERLAP_S
        writer, conn, api = self._writer_with_pages([
            {"fills": [_valid_fill_dict(trade_id="t1")], "cursor": ""},
        ])
        daemon_start = 1_776_700_000.0
        _run_fills_sync(writer, conn, daemon_start)

        # Expected since = daemon_start - overlap
        expected_since = int(daemon_start - FILLS_SYNC_OVERLAP_S)
        assert f"min_ts={expected_since}" in api.paths[0]
        # Fill landed.
        count = conn.execute(
            "SELECT COUNT(*) FROM fills_ledger"
        ).fetchone()[0]
        assert count == 1

    def test_populated_ledger_uses_max_ts_minus_overlap(self):
        from bot.daemon.main import _run_fills_sync, FILLS_SYNC_OVERLAP_S
        writer, conn, api = self._writer_with_pages([
            {"fills": [], "cursor": ""},
        ])
        # Pre-populate with a fill at a known ts.
        ledger_max = 1_776_800_000.0
        _insert_raw(conn, _valid_row(
            trade_id="existing", fill_ts_unix=ledger_max,
        ))
        # Daemon start is earlier than ledger_max — ledger_max should win.
        _run_fills_sync(writer, conn, daemon_start_unix=1_776_700_000.0)
        expected_since = int(ledger_max - FILLS_SYNC_OVERLAP_S)
        assert f"min_ts={expected_since}" in api.paths[0]

    def test_propagates_db_error_to_scheduler(self):
        """The wrapper must NOT swallow DB errors. The scheduler's own
        try/except around ``task.fn()`` handles logging and increments
        ``error_count``; a defensive wrapper here would make the
        scheduler's per-task health counter lie — the same silent-failure
        shape that hid the 2026-04-22 fills_validator schema bug."""
        from bot.daemon.main import _run_fills_sync

        class _BoomConn:
            def execute(self, *a, **kw):
                raise RuntimeError("db gone")

        # Writer constructed against a real conn (so init wiring works)
        # but the sync function gets the broken one.
        real_conn = init_db(":memory:")
        api = _FakeApi(pages=[])
        writer = FillsWriter(real_conn, api_get=api)
        with pytest.raises(RuntimeError, match="db gone"):
            _run_fills_sync(writer, _BoomConn(), 1_776_700_000.0)
        # API was never reached because the max(fill_ts) read failed.
        assert api.paths == []

    def test_live_mode_env_flag_propagates(self, monkeypatch):
        """``WEATHER_MM_LIVE`` at import time of bot.daemon.main determines
        the live_mode column on every fill written by this sync."""
        import bot.daemon.main as main_module
        writer, conn, api = self._writer_with_pages([
            {"fills": [_valid_fill_dict(trade_id="t_live")], "cursor": ""},
        ])
        monkeypatch.setattr(main_module, "WEATHER_MM_LIVE", True)
        main_module._run_fills_sync(writer, conn, 1_776_700_000.0)
        live = conn.execute(
            "SELECT live_mode FROM fills_ledger WHERE trade_id='t_live'"
        ).fetchone()[0]
        assert live == 1
