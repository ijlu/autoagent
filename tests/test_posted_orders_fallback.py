"""Regression: fills_writer must recover ``client_order_id`` from the
``posted_orders`` ledger when Kalshi's ``/portfolio/fills`` response
omits it (2026-05-10+ format drift).

Background — second instance of the same Kalshi field-drift bug:

* 2026-05-03 (fix commit 6a39dd7): ``count`` → ``count_fp``,
  ``{side}_price`` → ``{side}_price_dollars``. Without dual-format
  parsing, every fill was rejected as malformed and 18 cross-bracket
  fills landed on Kalshi without ever entering the local ledger.
* 2026-05-10 (this fix): ``client_order_id`` removed from
  ``/portfolio/fills`` entirely. Without recovery, every fill tags as
  ``source='manual'`` and per-strategy attribution dies silently —
  ``weather_mm_shadow.live_pnl_cents`` back-fill matches zero rows,
  ``mm_promotion`` graduation never fires, and
  ``backtest_comprehensive.py``'s strategy slices are wrong.

Recovery contract:

  1. Every ``/portfolio/orders`` POST writes
     ``(order_id, client_order_id, source_hint)`` to ``posted_orders``
     via ``record_posted_order``.
  2. When ``ingest_page`` sees a fill with ``client_order_id`` missing
     from the Kalshi payload, it looks up by ``order_id`` in
     ``posted_orders`` and uses the recorded ``client_order_id``.
  3. If no row exists for that ``order_id`` (truly external fill or
     race), source falls through to ``manual`` — the correct default.

These tests pin all three rules.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from bot.db import init_db
from bot.daemon.fills_writer import (
    ALLOWED_SOURCES,
    FillsWriter,
    default_source_tagger,
    record_posted_order,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    return init_db(":memory:")


def _new_format_fill(
    *,
    trade_id: str = "trade-1",
    order_id: str,
    ticker: str = "KXHIGHNY-26MAY11-B62.5",
    side: str = "yes",
    yes_price: str = "0.0400",
    no_price: str = "0.9600",
    count: str = "1.00",
    created_time: str = "2026-05-11T22:58:53.000000Z",
) -> dict:
    """Kalshi's 2026-05-10+ /portfolio/fills payload shape.

    Notably: no ``client_order_id`` field. Confirmed against live API
    on 2026-05-12 — the only identity fields are ``trade_id``,
    ``fill_id``, ``order_id``, ``ticker``.
    """
    return {
        "action": "buy",
        "count_fp": count,
        "created_time": created_time,
        "fee_cost": "0.010000",
        "fill_id": trade_id,
        "is_taker": True,
        "market_ticker": ticker,
        "no_price_dollars": no_price,
        "order_id": order_id,
        "side": side,
        "subaccount_number": 0,
        "ticker": ticker,
        "trade_id": trade_id,
        "ts": 1778540333,
        "yes_price_dollars": yes_price,
    }


# ── Tagger contract ───────────────────────────────────────────────────────


def test_tagger_routes_cross_bracket_prefixes() -> None:
    """Both ``mm_xb_`` and ``mm_xb_exit_`` must route to their own
    source buckets, not collapse into ``legacy``. ``mm_xb_exit_`` must
    be checked first (prefix-superset of ``mm_xb_``).
    """
    assert default_source_tagger("mm_xb_KXHIGHNY-26MAY11_3_1234") == "cross_bracket"
    assert default_source_tagger(
        "mm_xb_exit_KXHIGHNY-26MAY11-B62_5_1234"
    ) == "cross_bracket_exit"


def test_allowed_sources_includes_cross_bracket() -> None:
    assert "cross_bracket" in ALLOWED_SOURCES
    assert "cross_bracket_exit" in ALLOWED_SOURCES


def test_tagger_returns_manual_for_empty() -> None:
    assert default_source_tagger(None) == "manual"
    assert default_source_tagger("") == "manual"


# ── Writer + lookup roundtrip ─────────────────────────────────────────────


def test_record_posted_order_inserts_row(conn) -> None:
    record_posted_order(
        conn,
        order_id="oid-1",
        client_order_id="mm_xb_KXHIGHNY-26MAY11_3_1234",
        ticker="KXHIGHNY-26MAY11-B62.5",
        side="no",
        action="buy",
        count=1,
        price_cents=84,
        source_hint="cross_bracket",
        live_mode=True,
    )
    row = conn.execute(
        "SELECT client_order_id, source_hint, live_mode "
        "FROM posted_orders WHERE order_id = ?", ("oid-1",),
    ).fetchone()
    assert row is not None
    assert row[0] == "mm_xb_KXHIGHNY-26MAY11_3_1234"
    assert row[1] == "cross_bracket"
    assert row[2] == 1


def test_record_posted_order_is_idempotent(conn) -> None:
    """A retry/replay of the same order_id must not raise or duplicate."""
    for _ in range(3):
        record_posted_order(
            conn, order_id="oid-dup", client_order_id="mm_xb_1",
            ticker="T", side="no", action="buy", count=1, price_cents=50,
            source_hint="cross_bracket", live_mode=True,
        )
    n = conn.execute(
        "SELECT COUNT(*) FROM posted_orders WHERE order_id = ?",
        ("oid-dup",),
    ).fetchone()[0]
    assert n == 1


def test_record_posted_order_refuses_partial_row(conn) -> None:
    """Without both order_id and client_order_id, attribution recovery
    is broken anyway — refusing to insert is preferable to a useless row."""
    record_posted_order(
        conn, order_id="", client_order_id="mm_xb_1",
        ticker="T", side="no", action="buy", count=1, price_cents=50,
        source_hint="cross_bracket", live_mode=True,
    )
    record_posted_order(
        conn, order_id="oid-x", client_order_id="",
        ticker="T", side="no", action="buy", count=1, price_cents=50,
        source_hint="cross_bracket", live_mode=True,
    )
    n = conn.execute("SELECT COUNT(*) FROM posted_orders").fetchone()[0]
    assert n == 0


# ── End-to-end: ingest a new-format fill ──────────────────────────────────


def test_fill_without_client_order_id_recovers_via_posted_orders(conn) -> None:
    """Kalshi /portfolio/fills payload has no client_order_id. The
    writer must recover the right source by joining on order_id."""
    record_posted_order(
        conn,
        order_id="kalshi-order-A",
        client_order_id="mm_xb_KXHIGHNY-26MAY11_3_1778540000",
        ticker="KXHIGHNY-26MAY11-B62.5",
        side="no", action="buy", count=1, price_cents=84,
        source_hint="cross_bracket", live_mode=True,
    )

    writer = FillsWriter(conn)
    fill = _new_format_fill(
        trade_id="trade-A", order_id="kalshi-order-A",
        ticker="KXHIGHNY-26MAY11-B62.5", side="no",
        yes_price="0.1600", no_price="0.8400",
    )
    n = writer.ingest_page([fill], live_mode=True)
    assert n == 1

    row = conn.execute(
        "SELECT source, client_order_id FROM fills_ledger "
        "WHERE trade_id = ?", ("trade-A",),
    ).fetchone()
    assert row is not None, "fill should have landed in fills_ledger"
    assert row[0] == "cross_bracket", (
        f"expected source='cross_bracket' from posted_orders lookup, "
        f"got {row[0]!r}"
    )
    assert row[1] == "mm_xb_KXHIGHNY-26MAY11_3_1778540000"


def test_fill_without_client_order_id_and_no_posted_row_is_manual(conn) -> None:
    """A fill we have no record of (truly external — Josh placed it
    via the Kalshi UI) correctly falls through to ``manual``."""
    writer = FillsWriter(conn)
    fill = _new_format_fill(
        trade_id="trade-ext", order_id="unknown-order",
        ticker="KXNBATOTAL-26MAY10-220", side="yes",
    )
    n = writer.ingest_page([fill], live_mode=False)
    assert n == 1
    src = conn.execute(
        "SELECT source FROM fills_ledger WHERE trade_id = ?",
        ("trade-ext",),
    ).fetchone()[0]
    assert src == "manual"


def test_fill_with_client_order_id_still_works(conn) -> None:
    """Defense against a future Kalshi format reversal: if they ever
    re-add ``client_order_id`` to the fill payload, the writer must
    prefer it over the lookup (faster path)."""
    # No posted_orders row — to prove the writer used the payload field.
    writer = FillsWriter(conn)
    fill = _new_format_fill(
        trade_id="trade-direct", order_id="oid-direct",
    )
    fill["client_order_id"] = "mm_xb_exit_T_1234"  # legacy-format echoback
    writer.ingest_page([fill], live_mode=True)
    src = conn.execute(
        "SELECT source FROM fills_ledger WHERE trade_id = ?",
        ("trade-direct",),
    ).fetchone()[0]
    assert src == "cross_bracket_exit"


# ── Drift alert ───────────────────────────────────────────────────────────


def _insert_posted_decision(conn, *, ticker, side, ts_decision_unix):
    """Insert an alpha_backtest decision row simulating a recently-posted
    cross_bracket_live order on this ticker."""
    from datetime import datetime, timezone
    ts_iso = datetime.fromtimestamp(
        ts_decision_unix, tz=timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        """INSERT INTO alpha_backtest
        (ts_decision, ts_decision_unix, ticker, family, decision_type,
         decision_outcome, side, ensemble_p_yes)
        VALUES (?, ?, ?, 'KXHIGHAUS', 'cross_bracket_live', 'posted',
                ?, 0.05)""",
        (ts_iso, ts_decision_unix, ticker, side),
    )


def test_drift_alert_fires_when_manual_fill_follows_posted_decision(
    conn, caplog,
) -> None:
    """REGRESSION (2026-05-12 audit Phase A.4): a fill tagged 'manual'
    on a ticker where the bot just posted a decision is the
    fingerprint of Kalshi response-shape drift. The writer must log
    a loud warning so the next instance of the same bug class
    surfaces immediately instead of silently mis-attributing fills
    for days.
    """
    import logging
    from datetime import datetime, timezone

    # Posted decision at fill_ts - 60s
    fill_ts_unix = datetime.fromisoformat(
        "2026-05-11T22:58:53.000000+00:00"
    ).timestamp()
    _insert_posted_decision(
        conn, ticker="KXHIGHAUS-26MAY11-B81.5", side="no",
        ts_decision_unix=fill_ts_unix - 60,
    )
    conn.commit()

    writer = FillsWriter(conn)
    fill = _new_format_fill(
        trade_id="t-drift", order_id="oid-drift",
        ticker="KXHIGHAUS-26MAY11-B81.5", side="no",
    )  # no client_order_id, no posted_orders row → tags as 'manual'

    with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
        writer.ingest_page([fill], live_mode=True)

    assert any("DRIFT ALERT" in rec.message for rec in caplog.records), (
        "Expected a DRIFT ALERT warning when a manual fill matches a "
        "recently-posted decision"
    )


def test_drift_alert_silent_for_genuine_external_fill(conn, caplog) -> None:
    """A manual fill with NO matching posted decision is just an
    external trade (Josh's UI activity, an old order finally filling,
    etc.). No alert — would be noise.
    """
    import logging
    writer = FillsWriter(conn)
    # No alpha_backtest decision inserted.
    fill = _new_format_fill(
        trade_id="t-ext", order_id="oid-ext",
        ticker="KXNBATOTAL-26MAY10-220", side="yes",
    )
    with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
        writer.ingest_page([fill], live_mode=False)
    assert not any("DRIFT ALERT" in rec.message for rec in caplog.records)


def test_drift_alert_window_excludes_old_decisions(conn, caplog) -> None:
    """Only decisions within the last ~10 min count. A 2-hour-old
    decision matching by ticker is just a coincidence, not drift.
    """
    import logging
    from datetime import datetime
    fill_ts_unix = datetime.fromisoformat(
        "2026-05-11T22:58:53+00:00"
    ).timestamp()
    _insert_posted_decision(
        conn, ticker="KXHIGHAUS-26MAY11-B81.5", side="no",
        ts_decision_unix=fill_ts_unix - 7200,  # 2 hours earlier
    )
    conn.commit()

    writer = FillsWriter(conn)
    fill = _new_format_fill(
        trade_id="t-old", order_id="oid-old",
        ticker="KXHIGHAUS-26MAY11-B81.5", side="no",
    )
    with caplog.at_level(logging.WARNING, logger="bot.daemon.fills_writer"):
        writer.ingest_page([fill], live_mode=True)
    assert not any("DRIFT ALERT" in rec.message for rec in caplog.records)
