"""Tests for the cross-bracket live-trading safety gates.

The defaults must keep us in shadow mode. Live trading requires:
  1. CROSS_BRACKET_LIVE env truthy
  2. Per-family kv key truthy
  3. TTE in (CROSS_BRACKET_MIN_TTE_HOURS, CROSS_BRACKET_MAX_TTE_HOURS)
  4. Per-leg edge >= CROSS_BRACKET_LIVE_MIN_EDGE
  5. Adding leg cost wouldn't blow the daily exposure cap

Each gate has its own test below. Tests do NOT actually post orders —
they verify the gates short-circuit before reaching the order-placement
path.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from bot.db import init_db, kv_set
from bot.daemon import cross_bracket_shadow as cb


@pytest.fixture
def memdb():
    """In-memory DB with the schema initialized."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


# ── Helper: stub decisions matching the bracket_portfolio.BracketDecision shape ──


class _FakeDecision:
    """Minimal stand-in for ``bot.scoring.bracket_portfolio.BracketDecision``.

    Only the fields the live-execution path reads — keeps the test
    independent of the scorer's evolution."""
    def __init__(self, *, ticker, action="buy_yes", side="yes",
                  price_cents=42, p_yes=0.5,
                  edge_yes=0.15, edge_no=None):
        self.ticker = ticker
        self.action = action
        self.side = side
        self.price_cents = price_cents
        self.p_yes = p_yes
        self.edge_yes = edge_yes
        self.edge_no = edge_no
        # Other fields the logger touches but doesn't read for gate logic
        self.is_bracket = True
        self.bracket_lo = 60.0
        self.bracket_hi = 62.0
        self.threshold = None
        self.is_above = True
        self.market_yes_bid = 30
        self.market_yes_ask = 42
        self.market_yes_mid = 36.0
        self.skip_reason = None


# ── Family-live kv switch ──────────────────────────────────────────────


def test_family_live_default_off(memdb):
    """No env, no kv key → not live. Belt-and-suspenders default."""
    assert cb._is_family_live(memdb, "KXHIGHNY") is False


def test_family_live_requires_global_env_too(memdb):
    """If env is OFF, even a kv key set to True doesn't enable live.

    Both gates must pass — env is the kill-switch. Lets us instantly
    disable cross-bracket live across all families via a deploy +
    env restart, regardless of kv state.
    """
    kv_set(memdb, "cross_bracket_live:KXHIGHNY", True, ttl_seconds=86400)
    # CROSS_BRACKET_LIVE defaults to False; without monkeypatching it
    # stays False at module level. Verify the helper still says False.
    assert cb._is_family_live(memdb, "KXHIGHNY") is False


def test_family_live_requires_kv_too(memdb, monkeypatch):
    """If env is on but no kv key for the family → still off.

    Lets us turn on cross-bracket live for ONE family at a time
    (canary mode) without enabling all 6.
    """
    monkeypatch.setattr(cb, "CROSS_BRACKET_LIVE", True)
    assert cb._is_family_live(memdb, "KXHIGHNY") is False


def test_family_live_both_truthy_enables(memdb, monkeypatch):
    """Env true + kv true → live. The expected canary-on path."""
    monkeypatch.setattr(cb, "CROSS_BRACKET_LIVE", True)
    kv_set(memdb, "cross_bracket_live:KXHIGHNY", True, ttl_seconds=86400)
    assert cb._is_family_live(memdb, "KXHIGHNY") is True


def test_family_live_kv_dict_form(memdb, monkeypatch):
    """kv payload may also be a dict with ``enabled: True``.

    The kv_cache layer wraps simple values so dict payloads are common.
    """
    monkeypatch.setattr(cb, "CROSS_BRACKET_LIVE", True)
    kv_set(memdb, "cross_bracket_live:KXHIGHNY",
           {"enabled": True, "set_at": "2026-04-30"}, ttl_seconds=86400)
    assert cb._is_family_live(memdb, "KXHIGHNY") is True


# ── TTE window gate ────────────────────────────────────────────────────


def test_tte_gate_rejects_pre_window():
    """Settlement 12+ hours away → out of band, no live trading."""
    # Settlement key for next month
    far_key = "KXHIGHNY-26AUG30"
    in_window, hrs = cb._is_live_eligible_window(far_key)
    assert in_window is False
    assert hrs > 24  # well beyond max TTE


def test_tte_gate_rejects_post_settlement():
    """Settlement already in the past → out of band."""
    past_key = "KXHIGHNY-25JAN01"
    in_window, hrs = cb._is_live_eligible_window(past_key)
    assert in_window is False
    assert hrs < 0


def test_tte_gate_accepts_in_band(monkeypatch):
    """A settle ~5 hours in the future falls inside the default 3-7h window."""
    # Patch _settlement_unix_from_key to return now + 5h regardless of key.
    monkeypatch.setattr(
        cb, "_settlement_unix_from_key",
        lambda key: int(time.time()) + 5 * 3600,
    )
    in_window, hrs = cb._is_live_eligible_window("KXHIGHNY-26MAY01")
    assert in_window is True
    assert 4.5 < hrs < 5.5


def test_tte_gate_failclose_on_unparseable_key():
    """Garbage settlement key → fail closed (no live trade)."""
    in_window, hrs = cb._is_live_eligible_window("NOT-A-VALID-KEY")
    assert in_window is False


# ── client_order_id format ─────────────────────────────────────────────


def test_client_order_id_starts_with_mm():
    """Regression: client_order_id must start with 'mm_' or Kalshi rejects.
    See CLAUDE.md regression watchlist #1."""
    coid = cb._safe_client_order_id("KXHIGHNY-26APR30", 0)
    assert coid.startswith("mm_")


def test_client_order_id_no_periods():
    """Regression: client_order_id must NOT contain periods (Kalshi 400)."""
    coid = cb._safe_client_order_id("KXHIGHNY.26APR30", 0)  # ticker w/ period
    assert "." not in coid


def test_client_order_id_unique_within_session():
    """Two calls in quick succession must produce different IDs (timestamp suffix).
    Otherwise the second order would collide with the first and be rejected."""
    a = cb._safe_client_order_id("KXHIGHNY-26APR30", 0)
    time.sleep(0.002)  # ensure ms tick
    b = cb._safe_client_order_id("KXHIGHNY-26APR30", 0)
    assert a != b


# ── Daily exposure tracker ─────────────────────────────────────────────


def test_daily_exposure_starts_at_zero(memdb):
    assert cb._get_daily_exposure_cents(memdb) == 0


def test_daily_exposure_accumulates(memdb):
    cb._bump_daily_exposure_cents(memdb, 42)
    cb._bump_daily_exposure_cents(memdb, 100)
    assert cb._get_daily_exposure_cents(memdb) == 142


# ── Settlement key parser ──────────────────────────────────────────────


def test_settlement_key_to_unix():
    """Round-trip: key parses to a finite unix timestamp.

    KXHIGHNY-26APR30 settles at 23:59 LST (NY = UTC-5), which is
    04:59 UTC on the NEXT calendar day (May 1). The exact UTC
    boundary depends on the family's LST offset.
    """
    ts = cb._settlement_unix_from_key("KXHIGHNY-26APR30")
    assert ts is not None
    assert ts > 0
    # Should be in 2026 — the LST→UTC shift never moves us out of year.
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    assert dt.year == 2026
    # NY 23:59 LST = 04:59 UTC on May 1
    assert dt.month == 5
    assert dt.day == 1
    # Western cities have larger offsets so settle later in UTC
    ts_lax = cb._settlement_unix_from_key("KXHIGHLAX-26APR30")
    assert ts_lax is not None and ts_lax > ts  # LAX (UTC-8) settles after NY (UTC-5)


def test_settlement_key_invalid_returns_none():
    assert cb._settlement_unix_from_key("BAD-KEY") is None
    assert cb._settlement_unix_from_key("") is None
    assert cb._settlement_unix_from_key("KXHIGHNY-NOTADATE") is None


# ── Family parser ──────────────────────────────────────────────────────


def test_family_from_key():
    assert cb._family_from_settlement_key("KXHIGHNY-26APR30") == "KXHIGHNY"
    assert cb._family_from_settlement_key("KXHIGHMIA-26MAY15") == "KXHIGHMIA"
    assert cb._family_from_settlement_key("KXHIGHLAX-26JUN01") == "KXHIGHLAX"


# ── End-to-end: default config keeps everything in shadow ─────────────


def test_process_decisions_default_keeps_shadow_only(memdb):
    """With default env (CROSS_BRACKET_LIVE=false), no orders should be
    posted regardless of how good the decisions look. This is the
    CRITICAL safety test: a misconfigured deploy must NOT accidentally
    fire live orders.
    """
    decisions = [
        _FakeDecision(ticker="KXHIGHNY-26APR30-B62", action="buy_yes",
                      side="yes", price_cents=30, p_yes=0.55, edge_yes=0.20),
    ]
    market = {"ticker": "KXHIGHNY-26APR30-B62", "yes_bid": 28, "yes_ask": 30}
    stats = {
        "live_orders_posted": 0, "live_orders_failed": 0,
        "live_skipped_tte": 0, "live_skipped_edge": 0,
        "live_skipped_exposure_cap": 0, "live_skipped_leg_cap": 0,
        "live_skipped_family_off": 0,
    }

    # Patch api_post to fail loudly if called — we want to assert
    # that the default path NEVER reaches order placement.
    with patch("bot.api.api_post",
                side_effect=AssertionError("api_post must not be called in default mode")):
        cb._process_decisions(memdb, "KXHIGHNY-26APR30", [market], decisions, stats)

    # Live counters all zero except the one for "family not enabled".
    assert stats["live_orders_posted"] == 0
    assert stats["live_orders_failed"] == 0
    assert stats["live_skipped_family_off"] >= 1


def test_process_decisions_with_live_on_but_tte_outside(memdb, monkeypatch):
    """Even with env + kv both on, an out-of-window TTE must skip live.

    This is the second-most-critical safety test: the TTE window is
    where the backtest measured the alpha. Outside it, we have no
    evidence of edge and must stay shadow.
    """
    monkeypatch.setattr(cb, "CROSS_BRACKET_LIVE", True)
    kv_set(memdb, "cross_bracket_live:KXHIGHNY", True, ttl_seconds=86400)
    # KXHIGHNY-26AUG30 is months away → out of TTE window.
    decisions = [
        _FakeDecision(ticker="KXHIGHNY-26AUG30-B62", action="buy_yes",
                      side="yes", price_cents=30, p_yes=0.55, edge_yes=0.20),
    ]
    market = {"ticker": "KXHIGHNY-26AUG30-B62"}
    stats = {
        "live_orders_posted": 0, "live_orders_failed": 0,
        "live_skipped_tte": 0, "live_skipped_edge": 0,
        "live_skipped_exposure_cap": 0, "live_skipped_leg_cap": 0,
        "live_skipped_family_off": 0,
    }

    with patch("bot.api.api_post",
                side_effect=AssertionError("api_post must not be called for out-of-TTE leg")):
        cb._process_decisions(memdb, "KXHIGHNY-26AUG30", [market], decisions, stats)

    assert stats["live_orders_posted"] == 0
    assert stats["live_skipped_tte"] >= 1


# ── Layer 1+2: slippage protection in _post_live_order ────────────────


def _ob(yes_bids: list[tuple[int, int]], no_bids: list[tuple[int, int]]) -> dict:
    """Build a Kalshi-shape orderbook payload from human-readable bids."""
    return {"orderbook": {
        "yes": [[p, q] for p, q in yes_bids],
        "no":  [[p, q] for p, q in no_bids],
    }}


def test_best_ask_for_buy_yes_picks_lowest_implied_ask():
    """no_bids = [(95, 50), (93, 30)] → best YES ask is 100-95=5¢, size 50."""
    book = _ob(yes_bids=[(3, 10)], no_bids=[(95, 50), (93, 30)])
    with patch("bot.api.api_get", return_value=book):
        ask, size = cb._best_ask_for_buy("KXHIGHNY-26MAY03-B61.5", "yes")
    assert ask == 5
    assert size == 50


def test_best_ask_for_buy_no_picks_lowest_implied_ask():
    """yes_bids = [(93, 20), (90, 100)] → best NO ask is 100-93=7¢, size 20."""
    book = _ob(yes_bids=[(93, 20), (90, 100)], no_bids=[(2, 5)])
    with patch("bot.api.api_get", return_value=book):
        ask, size = cb._best_ask_for_buy("KXHIGHNY-26MAY03-B59.5", "no")
    assert ask == 7
    assert size == 20


def test_best_ask_handles_dollar_string_format():
    """Some Kalshi endpoints return [price_string_dollars, qty_string]."""
    book = {"orderbook": {
        "no": [["0.95", "50"], ["0.93", "30"]],
        "yes": [],
    }}
    with patch("bot.api.api_get", return_value=book):
        ask, size = cb._best_ask_for_buy("KXHIGHNY-26MAY03-B60", "yes")
    assert ask == 5
    assert size == 50


def test_best_ask_returns_none_on_empty_book():
    book = _ob(yes_bids=[], no_bids=[])
    with patch("bot.api.api_get", return_value=book):
        ask, size = cb._best_ask_for_buy("KXHIGHNY-26MAY03-B60", "yes")
    assert ask is None
    assert size is None


def test_best_ask_returns_none_on_api_error():
    with patch("bot.api.api_get",
                side_effect=ConnectionError("network down")):
        ask, size = cb._best_ask_for_buy("KXHIGHNY-26MAY03-B60", "yes")
    assert ask is None
    assert size is None


def test_post_live_order_layer1_caps_limit_price():
    """Best YES ask = 5¢, our FV bid = 25¢, slip tolerance = 2¢ →
    posted limit = min(25, 5+2) = 7¢. We never offer to pay more than
    +2¢ above best ask, even when our model thinks the leg is worth 25¢.
    """
    book = _ob(yes_bids=[(3, 1)], no_bids=[(95, 50)])  # best yes_ask = 5¢
    decision = _FakeDecision(ticker="KXHIGHNY-26MAY03-B61.5",
                              action="buy_yes", side="yes", price_cents=25)
    captured: dict = {}

    def fake_post(path, body):
        captured.update(body)
        return {"order": {"order_id": "ord_test_123"}}

    with patch("bot.api.api_get", return_value=book), \
         patch("bot.api.api_post", side_effect=fake_post):
        ok, oid = cb._post_live_order(None, "KXHIGHNY-26MAY03", 0, decision, 1)

    assert ok is True
    assert oid == "ord_test_123"
    assert captured["yes_price"] == 7  # 5¢ best ask + 2¢ slip tolerance
    assert "no_price" not in captured  # null fields stripped
    assert captured["post_only"] is False  # cross-bracket needs to cross


def test_post_live_order_layer2_caps_count_to_top_of_book():
    """Best YES ask = 5¢ with size 1. We requested count=4. Layer 2
    drops it to 1 so we never walk the book."""
    book = _ob(yes_bids=[], no_bids=[(95, 1), (90, 100)])  # 5¢ ask sz 1, 10¢ ask sz 100
    decision = _FakeDecision(ticker="KXHIGHNY-26MAY03-B61.5",
                              action="buy_yes", side="yes", price_cents=20)
    captured: dict = {}

    def fake_post(path, body):
        captured.update(body)
        return {"order": {"order_id": "ord_test"}}

    with patch("bot.api.api_get", return_value=book), \
         patch("bot.api.api_post", side_effect=fake_post):
        ok, _ = cb._post_live_order(None, "KXHIGHNY-26MAY03", 0, decision, 4)

    assert ok is True
    assert captured["count"] == 1  # capped from 4 to top-of-book size


def test_post_live_order_aborts_when_no_edge():
    """If our FV is BELOW best ask, there's no edge to capture — abort
    instead of posting a non-marketable order that will never fill."""
    book = _ob(yes_bids=[], no_bids=[(70, 50)])  # best yes_ask = 30¢
    decision = _FakeDecision(ticker="KXHIGHNY-26MAY03-B61.5",
                              action="buy_yes", side="yes", price_cents=10)

    with patch("bot.api.api_get", return_value=book), \
         patch("bot.api.api_post",
                side_effect=AssertionError("must not POST when fv < ask")):
        ok, reason = cb._post_live_order(None, "KXHIGHNY-26MAY03", 0, decision, 1)

    assert ok is False
    assert reason.startswith("no_edge:")


def test_post_live_order_aborts_on_unfetchable_book():
    """No book = no slippage visibility = no order. Fail closed."""
    decision = _FakeDecision(ticker="KXHIGHNY-26MAY03-B61.5",
                              action="buy_yes", side="yes", price_cents=20)

    with patch("bot.api.api_get", side_effect=ConnectionError("net")), \
         patch("bot.api.api_post",
                side_effect=AssertionError("must not POST without book")):
        ok, reason = cb._post_live_order(None, "KXHIGHNY-26MAY03", 0, decision, 1)

    assert ok is False
    assert "orderbook_unavailable" in reason


def test_post_live_order_post_only_is_false():
    """Regression: the very bug that broke the 2026-05-03 canary —
    post_only=True caused every cross-bracket POST to fail with
    'post only cross' because cross-bracket FAIR VALUES are by design
    on the wrong side of the spread (that's where the alpha is). The
    body sent to Kalshi must have post_only=False."""
    book = _ob(yes_bids=[(3, 10)], no_bids=[(95, 50)])
    decision = _FakeDecision(ticker="KXHIGHNY-26MAY03-B61.5",
                              action="buy_yes", side="yes", price_cents=20)
    captured: dict = {}

    def fake_post(path, body):
        captured.update(body)
        return {"order": {"order_id": "ord_test"}}

    with patch("bot.api.api_get", return_value=book), \
         patch("bot.api.api_post", side_effect=fake_post):
        cb._post_live_order(None, "KXHIGHNY-26MAY03", 0, decision, 1)

    assert captured["post_only"] is False, (
        "post_only must be False for cross-bracket — Layers 1+2 bound "
        "slippage instead. See 2026-05-03 canary postmortem in this "
        "file's docstring."
    )
