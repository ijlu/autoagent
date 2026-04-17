"""Tests for bot/learning/mm_promotion.py (Phase 1 step 10 — option A.7)."""
from __future__ import annotations

import time

import pytest

from bot.db import init_db
from bot.learning.directional_shadow import LiveState
from bot.learning.mm_promotion import (
    DEFAULT_MM_KILL_SWITCH,
    DEFAULT_MM_PROMOTION,
    MMKillSwitchConfig,
    MMPromotionConfig,
    _compute_mm_stats,
    _pnl_for_side_fill,
    annotate_shadow_pnl,
    evaluate_mm_canary_graduation,
    evaluate_mm_kill_switch,
    evaluate_mm_promotion,
    get_mm_live_state,
    get_mm_order_size_multiplier,
    is_mm_live,
    match_shadow_fills,
    run_mm_promotion_sweep,
    set_mm_live_state,
)


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture()
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _insert_shadow_row(
    conn,
    *,
    ticker: str,
    series: str,
    ts_unix: float,
    proposed_bid: int,
    proposed_ask: int,
    market_yes_bid: int,
    market_yes_ask: int,
    gate_should_quote: int = 1,
    live_mode: int = 0,
    live_order_id_bid: str | None = None,
    live_order_id_ask: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO weather_mm_shadow "
        "(ts_unix, ts_iso, ticker, series, station, "
        " fair_value_cents, proposed_bid_cents, proposed_ask_cents, "
        " half_spread_cents, market_yes_bid, market_yes_ask, market_mid, "
        " gate_should_quote, live_mode, "
        " live_order_id_bid, live_order_id_ask) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(ts_unix), "t", ticker, series, "KJFK",
         (proposed_bid + proposed_ask) // 2, proposed_bid, proposed_ask,
         (proposed_ask - proposed_bid) // 2,
         market_yes_bid, market_yes_ask, (market_yes_bid + market_yes_ask) // 2,
         gate_should_quote, live_mode,
         live_order_id_bid, live_order_id_ask),
    )
    conn.commit()
    return cur.lastrowid


# ── Per-series state API ────────────────────────────────────────────────
class TestMMLiveState:
    def test_default_shadow(self, conn):
        flag = get_mm_live_state(conn, "KXHIGHNY")
        assert flag.state == LiveState.SHADOW

    def test_promote_to_canary(self, conn):
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        assert is_mm_live(conn, "KXHIGHNY") is True
        assert get_mm_order_size_multiplier(conn, "KXHIGHNY") == 0.5

    def test_promote_to_full(self, conn):
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
        assert get_mm_order_size_multiplier(conn, "KXHIGHNY") == 1.0

    def test_shadow_multiplier_is_zero(self, conn):
        assert get_mm_order_size_multiplier(conn, "KXHIGHNY") == 0.0
        assert is_mm_live(conn, "KXHIGHNY") is False

    def test_case_insensitive_key(self, conn):
        set_mm_live_state(conn, "kxhighny", LiveState.LIVE_FULL)
        assert is_mm_live(conn, "KXHIGHNY") is True

    def test_invalid_state_raises(self, conn):
        with pytest.raises(ValueError):
            set_mm_live_state(conn, "KXHIGHNY", "nonsense")


# ── P&L helper ──────────────────────────────────────────────────────────
class TestPnLPerSideFill:
    def test_yes_wins_at_40c_gains_60c(self):
        # Bought YES @ 40, settles YES → gross = 60c/contract * 10 = 600c
        # Fee on 10 contracts at 40c: tiny compared to 600.
        pnl = _pnl_for_side_fill("yes", 40, contracts=10, won=True)
        assert pnl > 0
        assert pnl <= 600

    def test_yes_loses_at_40c_loses_40c(self):
        pnl = _pnl_for_side_fill("yes", 40, contracts=10, won=False)
        assert pnl < 0
        # Gross = -40c * 10 = -400c; fees make it worse.
        assert pnl <= -400

    def test_no_bought_at_30c_wins_gains_70c(self):
        # NO side: we "bought NO" at 30c ⇒ if NO wins, +70c per contract.
        pnl = _pnl_for_side_fill("no", 30, contracts=5, won=True)
        assert pnl > 0


# ── Shadow fill matcher ─────────────────────────────────────────────────
class TestMatchShadowFills:
    def test_bid_fills_when_market_ask_drops(self, conn):
        t0 = time.time() - 1000
        # Posted BID at 55¢. Later snapshot shows market_yes_ask=54 → fill.
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-X", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=50, market_yes_ask=60,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-X", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=48, market_yes_ask=54,  # ask dropped below our bid
        )
        summary = match_shadow_fills(conn, lifetime_s=600)
        assert summary["bid_fills"] >= 1
        row = conn.execute(
            "SELECT shadow_bid_filled, shadow_ask_filled "
            "FROM weather_mm_shadow WHERE ts_unix=?", (int(t0),)
        ).fetchone()
        assert row[0] == 1

    def test_ask_fills_when_market_bid_rises(self, conn):
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-Y", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=55,
            market_yes_bid=40, market_yes_ask=55,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-Y", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=40, proposed_ask=55,
            market_yes_bid=56, market_yes_ask=62,  # bid rose above our ask
        )
        match_shadow_fills(conn, lifetime_s=600)
        row = conn.execute(
            "SELECT shadow_ask_filled FROM weather_mm_shadow "
            "WHERE ts_unix=?", (int(t0),)
        ).fetchone()
        assert row[0] == 1

    def test_no_fill_when_market_never_crosses(self, conn):
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-Z", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=60,
            market_yes_bid=45, market_yes_ask=55,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-Z", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=40, proposed_ask=60,
            market_yes_bid=46, market_yes_ask=56,
        )
        match_shadow_fills(conn, lifetime_s=600)
        row = conn.execute(
            "SELECT shadow_bid_filled, shadow_ask_filled "
            "FROM weather_mm_shadow WHERE ts_unix=?", (int(t0),)
        ).fetchone()
        assert row[0] == 0 and row[1] == 0

    def test_gate_rejected_marks_unfilled_and_moves_on(self, conn):
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-G", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=40, market_yes_ask=50,
            gate_should_quote=0,  # gate said no
        )
        match_shadow_fills(conn, lifetime_s=600)
        row = conn.execute(
            "SELECT shadow_bid_filled, shadow_ask_filled "
            "FROM weather_mm_shadow WHERE ts_unix=?", (int(t0),)
        ).fetchone()
        assert row[0] == 0 and row[1] == 0

    def test_idempotent(self, conn):
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-I", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=40, market_yes_ask=50,
        )
        s1 = match_shadow_fills(conn, lifetime_s=600)
        s2 = match_shadow_fills(conn, lifetime_s=600)
        assert s1["checked"] >= 1
        # second pass re-checks nothing (already matched rows filtered)
        assert s2["checked"] == 0


# ── Settlement annotator ────────────────────────────────────────────────
class TestAnnotateShadowPnl:
    def test_yes_settlement_credits_filled_bid(self, conn):
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-S1", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1, "
            "shadow_ask_filled=0 WHERE id=?", (rid,),
        )
        conn.commit()
        annotate_shadow_pnl(
            conn, "KXHIGHNY-S1", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        row = conn.execute(
            "SELECT shadow_pnl_cents, ticker_settled_yes "
            "FROM weather_mm_shadow WHERE id=?", (rid,),
        ).fetchone()
        assert row[0] > 0  # bought YES at 40, YES wins ⇒ positive
        assert row[1] == 1

    def test_no_settlement_punishes_filled_bid(self, conn):
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-S2", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=70, proposed_ask=90,
            market_yes_bid=70, market_yes_ask=90,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()
        annotate_shadow_pnl(
            conn, "KXHIGHNY-S2", won_yes=False, ts_settle_unix=t0 + 7200,
        )
        row = conn.execute(
            "SELECT shadow_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid,),
        ).fetchone()
        assert row[0] < 0

    def test_idempotent_via_settle_ts(self, conn):
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-S3", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=60,
            market_yes_bid=40, market_yes_ask=60,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()
        n1 = annotate_shadow_pnl(
            conn, "KXHIGHNY-S3", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        n2 = annotate_shadow_pnl(
            conn, "KXHIGHNY-S3", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        assert n1 == 1
        assert n2 == 0


# ── Evaluators ──────────────────────────────────────────────────────────
def _seed_settled_shadow_rows(
    conn, series: str, n: int, *,
    pnl_per_row: int = 10, fill_pattern: str = "both",
    live_mode: int = 0, start_ts: float | None = None,
):
    """Seed `n` settled shadow rows for one series, each with the given P&L.

    fill_pattern ∈ {both, bid, ask, none}.
    """
    if start_ts is None:
        start_ts = time.time() - 14 * 86400
    for i in range(n):
        rid = _insert_shadow_row(
            conn, ticker=f"{series}-{i}", series=series,
            ts_unix=start_ts + i * 3600,
            proposed_bid=40, proposed_ask=60,
            market_yes_bid=45, market_yes_ask=55,
            live_mode=live_mode,
        )
        bid_f = 1 if fill_pattern in ("both", "bid") else 0
        ask_f = 1 if fill_pattern in ("both", "ask") else 0
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=?, "
            "shadow_ask_filled=?, shadow_pnl_cents=?, ts_settle_unix=? "
            "WHERE id=?",
            (bid_f, ask_f, pnl_per_row, start_ts + (i + 1) * 3600, rid),
        )
    conn.commit()


class TestEvaluatePromotion:
    def test_insufficient_settled_fails(self, conn):
        _seed_settled_shadow_rows(conn, "KXHIGHNY", n=10, pnl_per_row=10)
        ok, reason, _ = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert not ok
        assert "insufficient_shadow_n" in reason

    def test_negative_pnl_fails(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=100, pnl_per_row=-5, fill_pattern="bid",
        )
        ok, reason, _ = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert not ok

    def test_passing_promotes(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=100, pnl_per_row=10, fill_pattern="bid",
        )
        ok, reason, metrics = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert ok, reason
        assert metrics["n_settled"] == 100
        assert metrics["n_fills"] == 100

    def test_oos_slice_blocks_late_regression(self, conn):
        # First half +10, second half 0 → overall clears per-fill gate
        # (mean = 5¢ > 1¢), but OOS slice (0¢) fails the OOS gate (0.5¢).
        start = time.time() - 20 * 86400
        # Need >= min_shadow_settled=80 and enough total P&L. 120 rows,
        # first half +15, second half 0 → total $9, mean 7.5¢, OOS 0¢.
        n = 120
        for i in range(n):
            pnl = 15 if i < n // 2 else 0
            rid = _insert_shadow_row(
                conn, ticker=f"KXHIGHNY-O{i}", series="KXHIGHNY",
                ts_unix=start + i * 3600,
                proposed_bid=40, proposed_ask=60,
                market_yes_bid=45, market_yes_ask=55,
            )
            conn.execute(
                "UPDATE weather_mm_shadow SET shadow_bid_filled=1, "
                "shadow_ask_filled=0, shadow_pnl_cents=?, ts_settle_unix=? "
                "WHERE id=?", (pnl, start + (i + 1) * 3600, rid),
            )
        conn.commit()
        ok, reason, _ = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert not ok
        assert "oos" in reason


class TestEvaluateCanaryGraduation:
    def test_not_enough_canary_data_fails(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=10, pnl_per_row=10, live_mode=1,
        )
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_mm_live_state(conn, "KXHIGHNY")
        ok, reason, _ = evaluate_mm_canary_graduation(conn, "KXHIGHNY", flag)
        assert not ok

    def test_positive_canary_graduates(self, conn):
        # Seed *before* flipping canary so rows' ts_unix > flag.since_ts_unix
        # (here they will — since we seed in the past).
        start = time.time() - 10 * 86400
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_mm_live_state(conn, "KXHIGHNY")
        # Seed 80 live-mode rows *after* flag.since_ts_unix (i.e. "now")
        for i in range(80):
            rid = _insert_shadow_row(
                conn, ticker=f"KXHIGHNY-C{i}", series="KXHIGHNY",
                ts_unix=flag.since_ts_unix + (i + 1) * 60,
                proposed_bid=40, proposed_ask=60,
                market_yes_bid=45, market_yes_ask=55,
                live_mode=1,
            )
            conn.execute(
                "UPDATE weather_mm_shadow SET shadow_bid_filled=1, "
                "shadow_pnl_cents=?, ts_settle_unix=? WHERE id=?",
                (10, flag.since_ts_unix + (i + 2) * 60, rid),
            )
        conn.commit()
        ok, reason, _ = evaluate_mm_canary_graduation(conn, "KXHIGHNY", flag)
        assert ok, reason


class TestKillSwitch:
    def test_single_large_loss_trips(self, conn):
        # Seed a single catastrophic live row.
        t0 = time.time() - 3600
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-K1", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=60,
            market_yes_bid=45, market_yes_ask=55, live_mode=1,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1, "
            "shadow_pnl_cents=?, ts_settle_unix=? WHERE id=?",
            (-10000, t0 + 60, rid),  # -$100
        )
        conn.commit()
        tripped, reason, _ = evaluate_mm_kill_switch(
            conn, "KXHIGHNY", equity_dollars=1000.0,
        )
        assert tripped
        assert "single_trade_loss" in reason

    def test_clear_when_small_sample(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=5, pnl_per_row=5, live_mode=1,
        )
        tripped, reason, _ = evaluate_mm_kill_switch(
            conn, "KXHIGHNY", equity_dollars=1000.0,
        )
        assert not tripped
        assert "insufficient_live_n" in reason

    def test_rolling_pnl_floor_trips(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=40, pnl_per_row=-200, live_mode=1,
            fill_pattern="bid",
        )
        tripped, reason, _ = evaluate_mm_kill_switch(
            conn, "KXHIGHNY", equity_dollars=1000.0,
        )
        assert tripped
        assert "live_pnl=" in reason


# ── Sweep orchestration ─────────────────────────────────────────────────
class TestRunMMPromotionSweep:
    def test_shadow_to_canary_path(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=120, pnl_per_row=15, fill_pattern="bid",
        )
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        assert "KXHIGHNY" in [p["series"] for p in summary["promoted"]]
        assert get_mm_live_state(conn, "KXHIGHNY").state == LiveState.LIVE_CANARY

    def test_kill_switch_demotes(self, conn):
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
        # Seed a single catastrophic live row to trigger the single-loss rule.
        t0 = time.time() - 3600
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-K2", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=60,
            market_yes_bid=45, market_yes_ask=55, live_mode=1,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1, "
            "shadow_pnl_cents=?, ts_settle_unix=? WHERE id=?",
            (-10000, t0 + 60, rid),
        )
        conn.commit()
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        demoted_series = [d["series"] for d in summary["demoted"]]
        assert "KXHIGHNY" in demoted_series
        # Should demote FULL → CANARY (not all the way to shadow)
        assert get_mm_live_state(conn, "KXHIGHNY").state == LiveState.LIVE_CANARY

    def test_unchanged_when_below_threshold(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=5, fill_pattern="bid",
        )
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        assert len(summary["promoted"]) == 0
        assert get_mm_live_state(conn, "KXHIGHNY").state == LiveState.SHADOW
