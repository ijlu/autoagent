"""Tests for ``bot.learning.settlement_backfill``.

The poller drives the catalog-based back-fill of ``alpha_backtest`` and
``weather_mm_shadow`` so shadow-only tickers (no portfolio position) flow
into the learning loop. These tests mock ``bot.api.api_get`` — we do not
hit the Kalshi API in unit tests — and verify:

- no work when both tables are fully settled or empty
- only intersection (pending ∩ settled) is updated
- yes/no propagates correctly to ``alpha_backtest.won_yes`` and
  ``weather_mm_shadow.ticker_settled_yes``
- idempotent: a second invocation yields zero new writes
- non-yes/no catalog results are skipped cleanly
- scoped ``series_list`` override bypasses discovery
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from bot.db import init_db
from bot.learning import settlement_backfill as sb
from bot.learning.alpha_log import (
    DecisionOutcome,
    DecisionType,
    EnsembleSnapshot,
    MarketSnapshot,
    log_decision,
)


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _log_unsettled_alpha(conn, *, ticker: str, side: str = "yes",
                         price: int = 50, contracts: int = 10) -> int:
    """Insert one unsettled alpha_backtest row for ``ticker``."""
    ens = EnsembleSnapshot(
        p_yes=0.6, confidence=0.8, source_count=3,
        sources=["a", "b", "c"], source_estimates={"a": 0.6},
    )
    mkt = MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52, volume_fp=200)
    rid = log_decision(
        conn,
        ticker=ticker,
        decision_type=DecisionType.WEATHER_QUOTER_SHADOW,
        decision_outcome=DecisionOutcome.SHADOW_ONLY,
        ensemble=ens, market=mkt,
        side=side, price_cents=price, contracts=contracts,
    )
    assert rid is not None
    return rid


def _insert_unsettled_shadow(
    conn, *, ticker: str, series: str,
    proposed_bid: int = 40, proposed_ask: int = 60,
    market_bid: int = 45, market_ask: int = 55,
    ts_unix: float | None = None,
) -> int:
    if ts_unix is None:
        ts_unix = time.time() - 3600
    mid = (market_bid + market_ask) // 2
    cur = conn.execute(
        "INSERT INTO weather_mm_shadow "
        "(ts_unix, ts_iso, ticker, series, station, "
        " fair_value_cents, proposed_bid_cents, proposed_ask_cents, "
        " half_spread_cents, market_yes_bid, market_yes_ask, market_mid, "
        " gate_should_quote, live_mode) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(ts_unix), "t", ticker, series, "KJFK",
         (proposed_bid + proposed_ask) // 2, proposed_bid, proposed_ask,
         (proposed_ask - proposed_bid) // 2,
         market_bid, market_ask, mid, 1, 0),
    )
    conn.commit()
    return cur.lastrowid


def _make_api_fixture(payload: dict[str, list[dict[str, Any]]]):
    """Build a stand-in ``api_get`` that returns pre-baked markets per series.

    ``payload`` maps series ticker → list of market dicts. A single
    (non-paginated) response per call.
    """
    def _stub(path: str):
        # path is "/markets?series_ticker=KXHIGHNY&status=settled&limit=200"
        # Pull series_ticker out of the query string.
        if "series_ticker=" not in path:
            return {"markets": []}
        q = path.split("?", 1)[1]
        kv = dict(pair.split("=") for pair in q.split("&") if "=" in pair)
        series = kv.get("series_ticker", "")
        return {"markets": payload.get(series, []), "cursor": None}
    return _stub


# ══════════════════════════════════════════════════════════════════════════════
# _parse_close_ts
# ══════════════════════════════════════════════════════════════════════════════

class TestParseCloseTs:
    def test_parses_zulu(self):
        from datetime import datetime, timezone
        ts = sb._parse_close_ts("2026-04-21T20:00:00Z")
        assert ts is not None
        expected = datetime(2026, 4, 21, 20, 0, 0, tzinfo=timezone.utc).timestamp()
        assert ts == pytest.approx(expected, abs=1)

    @pytest.mark.parametrize("val", ["", None, "not-a-date", "2026/04/21"])
    def test_bad_input_returns_none(self, val):
        assert sb._parse_close_ts(val) is None


# ══════════════════════════════════════════════════════════════════════════════
# _distinct_unsettled_series — union across both tables
# ══════════════════════════════════════════════════════════════════════════════

class TestDistinctSeries:
    def test_empty(self, conn):
        assert sb._distinct_unsettled_series(conn) == set()

    def test_alpha_only(self, conn):
        _log_unsettled_alpha(conn, ticker="KXHIGHNY-26APR21-T75")
        assert sb._distinct_unsettled_series(conn) == {"KXHIGHNY"}

    def test_shadow_only(self, conn):
        _insert_unsettled_shadow(
            conn, ticker="KXHIGHMIA-26APR21-T90", series="KXHIGHMIA",
        )
        assert sb._distinct_unsettled_series(conn) == {"KXHIGHMIA"}

    def test_union(self, conn):
        _log_unsettled_alpha(conn, ticker="KXHIGHNY-26APR21-T75")
        _insert_unsettled_shadow(
            conn, ticker="KXHIGHMIA-26APR21-T90", series="KXHIGHMIA",
        )
        assert sb._distinct_unsettled_series(conn) == {"KXHIGHNY", "KXHIGHMIA"}

    def test_excludes_already_settled(self, conn):
        rid = _log_unsettled_alpha(conn, ticker="KXHIGHNY-26APR21-T75")
        conn.execute(
            "UPDATE alpha_backtest SET ts_settle_unix = ? WHERE id = ?",
            (time.time(), rid),
        )
        conn.commit()
        assert sb._distinct_unsettled_series(conn) == set()


# ══════════════════════════════════════════════════════════════════════════════
# backfill_from_catalog — end-to-end behaviour
# ══════════════════════════════════════════════════════════════════════════════

class TestBackfill:
    def test_no_unsettled_is_noop(self, conn):
        with patch.object(sb, "api_get") as mock_api:
            stats = sb.backfill_from_catalog(conn)
            mock_api.assert_not_called()
        assert stats["series_scanned"] == 0
        assert stats["tickers_settled"] == 0

    def test_fills_alpha_and_shadow_on_match(self, conn):
        rid_alpha = _log_unsettled_alpha(
            conn, ticker="KXHIGHNY-26APR21-T75", side="yes", price=52,
            contracts=10,
        )
        rid_shadow = _insert_unsettled_shadow(
            conn, ticker="KXHIGHNY-26APR21-T75", series="KXHIGHNY",
        )
        payload = {"KXHIGHNY": [{
            "ticker": "KXHIGHNY-26APR21-T75",
            "result": "yes",
            "close_time": "2026-04-21T20:00:00Z",
        }]}
        with patch.object(sb, "api_get", side_effect=_make_api_fixture(payload)):
            stats = sb.backfill_from_catalog(conn)

        assert stats["series_scanned"] == 1
        assert stats["tickers_settled"] == 1
        assert stats["alpha_rows_filled"] == 1
        assert stats["shadow_rows_annotated"] == 1

        # Alpha row: won_yes=1 (yes-side, yes result), realized_pnl > 0
        row = conn.execute(
            "SELECT settlement_result, won_yes, realized_pnl_cents, "
            "ts_settle_unix FROM alpha_backtest WHERE id=?",
            (rid_alpha,),
        ).fetchone()
        assert row[0] == "yes"
        assert row[1] == 1
        # contracts=10, price=52, won → 10*(100-52) = 480
        assert row[2] == 480
        assert row[3] is not None

        # Shadow row: ticker_settled_yes=1, ts_settle_unix populated
        row = conn.execute(
            "SELECT ticker_settled_yes, ts_settle_unix "
            "FROM weather_mm_shadow WHERE id=?",
            (rid_shadow,),
        ).fetchone()
        assert row[0] == 1
        assert row[1] is not None

    def test_no_result(self, conn):
        """Result=no → won_yes=0 for yes-side alpha, ticker_settled_yes=0
        for shadow."""
        rid_alpha = _log_unsettled_alpha(
            conn, ticker="KXHIGHNY-26APR21-T75", side="yes", price=52,
            contracts=10,
        )
        _insert_unsettled_shadow(
            conn, ticker="KXHIGHNY-26APR21-T75", series="KXHIGHNY",
        )
        payload = {"KXHIGHNY": [{
            "ticker": "KXHIGHNY-26APR21-T75",
            "result": "no",
            "close_time": "2026-04-21T20:00:00Z",
        }]}
        with patch.object(sb, "api_get", side_effect=_make_api_fixture(payload)):
            sb.backfill_from_catalog(conn)

        row = conn.execute(
            "SELECT won_yes, realized_pnl_cents FROM alpha_backtest "
            "WHERE id=?", (rid_alpha,),
        ).fetchone()
        assert row[0] == 0
        # yes-side, no result → lost: -10*52 = -520
        assert row[1] == -520

    def test_only_intersection_is_updated(self, conn):
        """A catalog entry for a ticker we never shadowed is ignored;
        an unsettled ticker not in the catalog stays unsettled."""
        rid = _log_unsettled_alpha(conn, ticker="KXHIGHNY-26APR21-T75")
        payload = {"KXHIGHNY": [
            {"ticker": "KXHIGHNY-UNRELATED", "result": "yes",
             "close_time": "2026-04-21T20:00:00Z"},
            # No entry for our ticker → stays unsettled.
        ]}
        with patch.object(sb, "api_get", side_effect=_make_api_fixture(payload)):
            stats = sb.backfill_from_catalog(conn)

        assert stats["tickers_settled"] == 0
        assert stats["alpha_rows_filled"] == 0
        row = conn.execute(
            "SELECT ts_settle_unix FROM alpha_backtest WHERE id=?", (rid,),
        ).fetchone()
        assert row[0] is None

    def test_idempotent_second_run(self, conn):
        _log_unsettled_alpha(conn, ticker="KXHIGHNY-26APR21-T75")
        _insert_unsettled_shadow(
            conn, ticker="KXHIGHNY-26APR21-T75", series="KXHIGHNY",
        )
        payload = {"KXHIGHNY": [{
            "ticker": "KXHIGHNY-26APR21-T75",
            "result": "yes",
            "close_time": "2026-04-21T20:00:00Z",
        }]}
        stub = _make_api_fixture(payload)
        with patch.object(sb, "api_get", side_effect=stub):
            first = sb.backfill_from_catalog(conn)
        # After first pass the rows are settled, so discovery finds nothing.
        with patch.object(sb, "api_get", side_effect=stub) as mock_api:
            second = sb.backfill_from_catalog(conn)

        assert first["alpha_rows_filled"] == 1
        assert second["series_scanned"] == 0
        assert second["alpha_rows_filled"] == 0
        assert second["shadow_rows_annotated"] == 0
        mock_api.assert_not_called()

    def test_skips_non_yesno_result(self, conn):
        """A 'void' or missing result entry is ignored."""
        rid = _log_unsettled_alpha(conn, ticker="KXHIGHNY-26APR21-T75")
        payload = {"KXHIGHNY": [
            {"ticker": "KXHIGHNY-26APR21-T75", "result": "void",
             "close_time": "2026-04-21T20:00:00Z"},
        ]}
        with patch.object(sb, "api_get", side_effect=_make_api_fixture(payload)):
            stats = sb.backfill_from_catalog(conn)
        assert stats["tickers_settled"] == 0
        row = conn.execute(
            "SELECT ts_settle_unix FROM alpha_backtest WHERE id=?", (rid,),
        ).fetchone()
        assert row[0] is None

    def test_series_list_override(self, conn):
        """Explicit series_list bypasses discovery; scans exactly that set.

        Even with no unsettled rows, it still scans — caller's choice."""
        # Pre-settle everything so discovery would otherwise find nothing.
        payload = {"KXFED": [
            {"ticker": "KXFED-26MAY-T425", "result": "yes",
             "close_time": "2026-04-21T20:00:00Z"},
        ]}
        with patch.object(sb, "api_get", side_effect=_make_api_fixture(payload)):
            stats = sb.backfill_from_catalog(conn, series_list=["KXFED"])
        assert stats["series_scanned"] == 1
        # Nothing matched (no rows) but the scan happened.
        assert stats["tickers_settled"] == 0

    def test_api_error_does_not_abort_other_series(self, conn):
        """One series raising on catalog fetch must not stop the sweep."""
        _log_unsettled_alpha(conn, ticker="KXHIGHNY-26APR21-T75")
        _log_unsettled_alpha(conn, ticker="KXHIGHMIA-26APR21-T90")
        good_payload = {"KXHIGHMIA": [{
            "ticker": "KXHIGHMIA-26APR21-T90", "result": "yes",
            "close_time": "2026-04-21T20:00:00Z",
        }]}

        def _raise_on_ny(path: str):
            if "KXHIGHNY" in path:
                raise RuntimeError("simulated API failure")
            return _make_api_fixture(good_payload)(path)

        with patch.object(sb, "api_get", side_effect=_raise_on_ny):
            stats = sb.backfill_from_catalog(conn)

        # NY errored but MIA still processed
        assert stats["series_scanned"] == 2
        assert stats["tickers_settled"] == 1
        assert stats["alpha_rows_filled"] == 1
