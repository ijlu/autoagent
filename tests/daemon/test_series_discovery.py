"""Tests for bot/daemon/series_discovery.py.

Two behaviours we want to lock:

1. The routable-prefix filter only flags series we'd plausibly trade —
   nothing in `KXMVE*`, sports, music charts, etc.
2. The "alert once per series" rule — a series newly discovered today
   triggers an alert; running the sweep again the next day doesn't.

We mock `api_get` and `send_alert` so the test stays hermetic. Real
network access in unit tests has bitten us before (April shadow
corruption postmortem).
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

from bot.daemon import series_discovery
from bot.db import init_db


def _mk_conn():
    return init_db(":memory:")


def _events_response(series_tickers, cursor=None):
    """Build a fake `/events?status=open` response."""
    return {
        "events": [
            {
                "series_ticker": s,
                "event_ticker": f"{s}-EV01",
                "markets": [{"ticker": f"{s}-T1"}, {"ticker": f"{s}-T2"}],
            }
            for s in series_tickers
        ],
        "cursor": cursor,
    }


def test_routable_prefix_filter():
    # Routable
    assert series_discovery._routable_prefix("KXHIGHNY") == "KXHIGH"
    assert series_discovery._routable_prefix("KXFED") == "KXFED"
    assert series_discovery._routable_prefix("KXJOB") == "KXJOB"
    assert series_discovery._routable_prefix("KXBTC") == "KXBTC"
    # Not routable
    assert series_discovery._routable_prefix("KXMVECROSSCATEGORY") is None
    assert series_discovery._routable_prefix("KXMLBHIT") is None
    assert series_discovery._routable_prefix("KXNFLWINS") is None
    assert series_discovery._routable_prefix("") is None
    assert series_discovery._routable_prefix(None) is None


def test_first_run_alerts_then_idempotent():
    conn = _mk_conn()

    # Day 1: KXHIGHHOU is brand new (not in allowlist), KXHIGHNY is in
    # allowlist (so should NOT alert), KXMVECROSSCATEGORY is parlay (filtered).
    page1 = _events_response(
        ["KXHIGHHOU", "KXHIGHNY", "KXMVECROSSCATEGORY"], cursor=None,
    )

    with patch.object(series_discovery, "api_get", return_value=page1), \
         patch.object(series_discovery, "send_alert", return_value=True) as alert:
        summary = series_discovery.run_discovery(conn)

    # Discovery alerted on KXHIGHHOU only.
    assert summary["new_routable"] == 1
    assert alert.call_count == 1
    msg = alert.call_args[0][0]
    assert "KXHIGHHOU" in msg
    assert "KXHIGHNY" not in msg          # already in allowlist
    assert "KXMVECROSSCATEGORY" not in msg  # not routable

    # Row exists with alert_sent_unix populated.
    row = conn.execute(
        "SELECT family_prefix, alert_sent_unix FROM discovered_series "
        "WHERE series_ticker='KXHIGHHOU'"
    ).fetchone()
    assert row[0] == "KXHIGH"
    assert row[1] is not None

    # Day 2: same response → no new alert.
    with patch.object(series_discovery, "api_get", return_value=page1), \
         patch.object(series_discovery, "send_alert", return_value=True) as alert:
        summary2 = series_discovery.run_discovery(conn)
    assert summary2["new_routable"] == 0
    assert alert.call_count == 0


def test_alert_failure_does_not_block_upsert():
    """If Telegram is down, we still want the discovered row recorded so
    the next sweep doesn't re-alert."""
    conn = _mk_conn()
    page = _events_response(["KXHIGHHOU"], cursor=None)
    # Boom on send_alert — but the row should still exist (no alert_sent yet
    # though, so a future successful run alerts once).
    with patch.object(series_discovery, "api_get", return_value=page), \
         patch.object(series_discovery, "send_alert",
                      side_effect=RuntimeError("telegram down")):
        summary = series_discovery.run_discovery(conn)
    assert summary["new_routable"] == 1
    row = conn.execute(
        "SELECT alert_sent_unix FROM discovered_series WHERE series_ticker=?",
        ("KXHIGHHOU",),
    ).fetchone()
    assert row is not None
    assert row[0] is None  # alert never confirmed — retry next sweep
