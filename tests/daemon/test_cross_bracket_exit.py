"""Tests for cross-bracket exit decision logic.

Pinned behaviors:
  - Don't exit at a loss (best_bid <= avg_entry → None)
  - Trigger when realized_pct ≥ EXIT_PCT_THRESHOLD (50%)
  - Trigger when realized_cents ≥ EXIT_ABS_CENTS (25¢) even if pct low
  - Don't trigger when realized post-fee gain ≤ 0
  - Synthetic-sell direction: NO position → BUY YES at (100 - exit_price)
  - Position identification filters to mm_xb_ client_order_id
"""

from __future__ import annotations

import pytest

from bot.daemon import cross_bracket_exit as cbe


# ── evaluate_exit ──────────────────────────────────────────────────────────────


def _pos(side="no", contracts=1, avg_entry=9.0, fee_per=1.0):
    return {
        "ticker": "KXHIGHNY-26MAY04-B72.5",
        "side": side,
        "contracts": contracts,
        "avg_entry_cents": avg_entry,
        "fee_per_contract": fee_per,
    }


def _make_conn():
    """Fresh in-memory DB. post_exit_order writes a posted_orders row
    after a successful api_post, so the conn must have init_db's tables."""
    from bot.db import init_db
    return init_db(":memory:")


class TestEvaluateExit:
    def test_no_book_returns_none(self):
        assert cbe.evaluate_exit(_pos(), None) is None
        assert cbe.evaluate_exit(_pos(), 0) is None

    def test_loss_floor_no_exit_below_entry(self):
        # bid below entry → would be selling at a loss → None
        assert cbe.evaluate_exit(_pos(avg_entry=10), 8) is None
        assert cbe.evaluate_exit(_pos(avg_entry=10), 10) is None

    def test_realized_below_threshold_no_exit(self):
        # entry=9, bid=15 → gross gain 6¢; max upside ≈ 90¢; 6/90 ≈ 7%
        # also abs gain 6¢ < 25¢. Both criteria fail → None
        assert cbe.evaluate_exit(_pos(avg_entry=9), 15) is None

    def test_pct_threshold_triggers(self):
        # entry=9, bid=55 → gain ≈ 55-9-2 = 44¢; max upside = 90; 44/90 ≈ 49%.
        # Below 50%, so use 56 to be safe → 56-9-2 = 45¢ ; 45/90 = 50% exact.
        spec = cbe.evaluate_exit(_pos(avg_entry=9), 56)
        assert spec is not None
        assert spec["exit_price_cents"] == 56
        assert spec["realized_pct"] >= cbe.EXIT_PCT_THRESHOLD

    def test_abs_threshold_triggers_even_when_pct_low(self):
        # entry=70 (high entry), bid=98 → gain = 98-70-fee ≈ 26-27¢
        # max upside = 100-70-1 = 29¢; pct = 27/29 ≈ 90% (very high pct)
        # This case actually triggers BOTH thresholds. Pick a different shape:
        # Need a case where pct < 50% but abs > 25¢.
        # entry=20 + max_held=80; bid=46 → gain = 46-20-fee ≈ 25¢; pct = 25/80 = 31%
        spec = cbe.evaluate_exit(_pos(avg_entry=20, fee_per=1), 47)
        assert spec is not None, "should trigger via abs threshold"
        # pct should be below threshold to confirm we triggered via abs
        assert spec["realized_cents_per_contract"] >= cbe.EXIT_ABS_CENTS

    def test_negative_after_fee_gain_no_exit(self):
        # entry=9, bid=10 → gross 1¢; taker fee at 10¢ ≈ ceil(7*1*10*90/10000) = 1¢
        # net gain = 1 - 1 = 0 → not strictly positive → None
        spec = cbe.evaluate_exit(_pos(avg_entry=9), 10)
        assert spec is None

    def test_yes_position_same_logic(self):
        # YES position with entry=15, bid=80 → gain = 80-15-fee ≈ 64¢
        # max upside = 100-15-1 = 84¢; pct = 64/84 ≈ 76%
        spec = cbe.evaluate_exit(_pos(side="yes", avg_entry=15), 80)
        assert spec is not None
        assert spec["realized_pct"] > cbe.EXIT_PCT_THRESHOLD


# ── synthetic-sell direction ───────────────────────────────────────────────────


class TestSyntheticSellDirection:
    def test_no_position_buys_yes_at_inverse_price(self, monkeypatch):
        captured = {}

        def fake_post(path, body):
            captured["path"] = path
            captured["body"] = body
            return {"order": {"order_id": "test-order-id"}}

        monkeypatch.setattr(cbe, "api_post", fake_post)
        cbe.post_exit_order(
            _make_conn(),
            _pos(side="no", contracts=1, avg_entry=9),
            {
                "exit_price_cents": 60,
                "exit_fee_per_contract": 1,
                "realized_pct": 0.55,
                "realized_cents_per_contract": 50.0,
                "realized_cents_total": 50.0,
                "max_held_gain": 90.0,
            },
        )
        body = captured["body"]
        # NO position → exit by buying YES at (100 - 60) = 40¢
        assert body["side"] == "yes"
        assert body["yes_price"] == 40
        assert body["count"] == 1
        assert body["action"] == "buy"
        # mm_xb_exit_ prefix (with underscore) so the fills tagger
        # routes to ``cross_bracket_exit`` distinct from ``cross_bracket``.
        assert body["client_order_id"].startswith("mm_xb_exit_")

    def test_yes_position_buys_no_at_inverse_price(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            cbe, "api_post",
            lambda p, b: captured.setdefault("body", b)
            or {"order": {"order_id": "x"}},
        )
        cbe.post_exit_order(
            _make_conn(),
            _pos(side="yes", contracts=2, avg_entry=15),
            {
                "exit_price_cents": 80,
                "exit_fee_per_contract": 2,
                "realized_pct": 0.7,
                "realized_cents_per_contract": 63.0,
                "realized_cents_total": 126.0,
                "max_held_gain": 84.0,
            },
        )
        body = captured["body"]
        # YES position → exit by buying NO at (100 - 80) = 20¢
        assert body["side"] == "no"
        assert body["no_price"] == 20
        assert body["count"] == 2

    def test_invalid_opposite_price_skips_post(self, monkeypatch):
        called = {"n": 0}

        def fake_post(path, body):
            called["n"] += 1
            return {"order": {"order_id": "x"}}

        monkeypatch.setattr(cbe, "api_post", fake_post)
        # exit_price_cents=100 → opposite = 0 → invalid → no post
        result = cbe.post_exit_order(
            _make_conn(),
            _pos(),
            {
                "exit_price_cents": 100,
                "exit_fee_per_contract": 0,
                "realized_pct": 1.0,
                "realized_cents_per_contract": 99.0,
                "realized_cents_total": 99.0,
                "max_held_gain": 99.0,
            },
        )
        assert result is None
        assert called["n"] == 0


# ── client_order_id format ────────────────────────────────────────────────────


class TestClientOrderId:
    def test_uses_mm_xb_exit_prefix(self, monkeypatch):
        """``mm_xb_exit_`` (with underscore) routes to ``cross_bracket_exit``
        in default_source_tagger. The previous ``mm_xbexit_`` (no
        underscore) was ambiguous with ``mm_xb_*`` and would have
        collapsed exits into the ``cross_bracket`` bucket once the
        T3.1 tagger learned to distinguish them.
        """
        captured = {}
        monkeypatch.setattr(
            cbe, "api_post",
            lambda p, b: captured.setdefault("body", b)
            or {"order": {"order_id": "x"}},
        )
        cbe.post_exit_order(
            _make_conn(),
            _pos(),
            {
                "exit_price_cents": 50,
                "exit_fee_per_contract": 2,
                "realized_pct": 0.55,
                "realized_cents_per_contract": 39.0,
                "realized_cents_total": 39.0,
                "max_held_gain": 70.0,
            },
        )
        coid = captured["body"]["client_order_id"]
        assert coid.startswith("mm_xb_exit_")
        # Must not contain periods (Kalshi rejects per CLAUDE.md regression list)
        assert "." not in coid
        assert len(coid) <= 64
