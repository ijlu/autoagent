"""Tests for bot/learning/shadow_promotion.py — step 9 promotion gate."""
from __future__ import annotations

import json
import time

import pytest

from bot.db import init_db
from bot.learning.directional_shadow import (
    LiveState,
    get_live_state,
    set_live_state,
)
from bot.learning.shadow_promotion import (
    DEFAULT_KILL_SWITCH,
    KillSwitchConfig,
    MIN_LIVE_SETTLED_FOR_DEMOTION,
    _rows_to_stats,
    evaluate_canary_graduation,
    evaluate_kill_switch,
    evaluate_promotion,
    manual_re_enable,
    run_promotion_sweep,
)


# ── Row seeders ─────────────────────────────────────────────────────────────
def _seed_shadow_row(
    conn,
    *,
    family: str,
    ts_decision_unix: float,
    our_p_yes: float,
    market_p_yes: float,
    won_yes: bool,
    pnl_cents: int,
    outcome: str = "shadow_only",
):
    """Insert one settled alpha_backtest row with the fields promotion code reads."""
    conn.execute(
        "INSERT INTO alpha_backtest "
        "(ts_decision, ts_decision_unix, ticker, family, decision_type, "
        " decision_outcome, side, price_cents, ensemble_p_yes, "
        " market_prob_yes, ts_settle_unix, won_yes, realized_pnl_cents) "
        "VALUES (?, ?, ?, ?, 'directional_shadow', ?, 'yes', 50, ?, ?, ?, ?, ?)",
        (
            "2026-04-17T00:00:00Z", ts_decision_unix,
            f"{family}-26APR17-T50", family, outcome,
            our_p_yes, market_p_yes,
            ts_decision_unix + 3600, int(bool(won_yes)), pnl_cents,
        ),
    )
    conn.commit()


def _seed_family(
    conn, family, n, *, our_p_yes=0.7, market_p_yes=0.5, wins=None,
    pnl_per=5, start_ts=None, outcome="shadow_only",
):
    """Seed N rows for a family with uniform p_yes. `wins` defaults to
    round(our_p_yes*n) so the family is well-calibrated to our model.

    Wins and losses are INTERLEAVED (Bresenham-style) rather than blocked
    so that the out-of-sample second-half slice has the same calibration
    as the first half. Blocked seeding would make OOS-gate tests vacuous.
    """
    if wins is None:
        wins = int(round(our_p_yes * n))
    if start_ts is None:
        start_ts = time.time() - 30 * 86400
    # Interleave wins uniformly: place a win when floor((i+1)*wins/n) increments.
    won_so_far = 0
    for i in range(n):
        target_wins = int(round((i + 1) * wins / n))
        is_win = target_wins > won_so_far
        if is_win:
            won_so_far += 1
        _seed_shadow_row(
            conn, family=family,
            ts_decision_unix=start_ts + i * 3600,
            our_p_yes=our_p_yes, market_p_yes=market_p_yes,
            won_yes=is_win,
            pnl_cents=pnl_per if is_win else -pnl_per,
            outcome=outcome,
        )


@pytest.fixture()
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


# ── FamilyStats aggregation ────────────────────────────────────────────────
class TestRowsToStats:
    def test_empty_rows(self):
        stats = _rows_to_stats([])
        assert stats.n == 0
        assert stats.edge_beat == 0.0


# ── evaluate_promotion ─────────────────────────────────────────────────────
class TestEvaluatePromotion:
    def test_too_few_rows(self, conn):
        _seed_family(conn, "KXHIGHNY", 10)  # < default 50
        promote, reason, metrics = evaluate_promotion(conn, "KXHIGHNY")
        assert promote is False
        assert "insufficient_shadow_n=10<50" in reason
        assert metrics["n_shadow_settled"] == 10

    def test_passes_with_strong_signal(self, conn):
        # Our model predicts 0.7, market predicts 0.5, actual win rate 0.7.
        # Our Brier = 0.09; baseline = 0.25; edge_beat = 0.16 (well above 0.005).
        _seed_family(conn, "KXHIGHNY", 60, our_p_yes=0.7, market_p_yes=0.5)
        promote, reason, metrics = evaluate_promotion(conn, "KXHIGHNY")
        assert promote is True
        assert metrics["edge_beat"] > 0.005
        assert metrics["oos_edge_beat"] > 0.005
        assert metrics["realized_pnl_dollars"] >= 0.0

    def test_fails_when_edge_beat_insufficient(self, conn):
        # Our model = market, wins at market rate → edge_beat ≈ 0.
        _seed_family(conn, "KXHIGHNY", 60, our_p_yes=0.5, market_p_yes=0.5, wins=30)
        promote, reason, metrics = evaluate_promotion(conn, "KXHIGHNY")
        assert promote is False
        assert "edge_beat" in reason

    def test_fails_when_oos_slice_weak(self, conn):
        """First-half strong, second-half weak — OOS gate must catch this."""
        # Seed 30 strong rows then 30 weak rows.
        start = time.time() - 60 * 3600
        for i in range(30):
            _seed_shadow_row(conn, family="KXHIGHNY",
                             ts_decision_unix=start + i * 3600,
                             our_p_yes=0.7, market_p_yes=0.5,
                             won_yes=True, pnl_cents=5)
        for i in range(30):
            _seed_shadow_row(conn, family="KXHIGHNY",
                             ts_decision_unix=start + (30 + i) * 3600,
                             our_p_yes=0.7, market_p_yes=0.5,
                             won_yes=(i < 15),  # 50% WR → matches market
                             pnl_cents=5 if i < 15 else -5)
        promote, reason, metrics = evaluate_promotion(conn, "KXHIGHNY")
        assert promote is False
        assert "oos_edge_beat" in reason

    def test_fails_when_pnl_negative(self, conn):
        """Signal beats baseline on Brier but realized P&L is net negative."""
        # Interleaved wins (42/60) with asymmetric P&L: small wins, large losses.
        wins = 42
        won_so_far = 0
        start = time.time() - 60 * 3600
        for i in range(60):
            target = int(round((i + 1) * wins / 60))
            is_win = target > won_so_far
            if is_win:
                won_so_far += 1
            _seed_shadow_row(
                conn, family="KXHIGHNY",
                ts_decision_unix=start + i * 3600,
                our_p_yes=0.7, market_p_yes=0.5,
                won_yes=is_win,
                pnl_cents=5 if is_win else -20,  # wins small, losses big
            )
        promote, reason, metrics = evaluate_promotion(conn, "KXHIGHNY")
        assert promote is False
        assert "realized_pnl" in reason


# ── Canary graduation ──────────────────────────────────────────────────────
class TestEvaluateCanaryGraduation:
    def test_insufficient_canary_rows(self, conn):
        set_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_live_state(conn, "KXHIGHNY")
        # Seed only 10 canary-era live rows.
        _seed_family(conn, "KXHIGHNY", 10, start_ts=flag.since_ts_unix + 60,
                     outcome="posted")
        graduate, reason, metrics = evaluate_canary_graduation(conn, "KXHIGHNY", flag)
        assert graduate is False
        assert "canary_n=10" in reason

    def test_graduates_after_dwell(self, conn):
        set_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_live_state(conn, "KXHIGHNY")
        _seed_family(conn, "KXHIGHNY", 35, start_ts=flag.since_ts_unix + 60,
                     our_p_yes=0.7, outcome="posted")
        graduate, reason, metrics = evaluate_canary_graduation(conn, "KXHIGHNY", flag)
        assert graduate is True
        assert metrics["n_canary_settled"] == 35

    def test_blocks_graduation_on_negative_canary_pnl(self, conn):
        set_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_live_state(conn, "KXHIGHNY")
        # Losing canary: 35 rows, net negative P&L.
        for i in range(35):
            _seed_shadow_row(
                conn, family="KXHIGHNY",
                ts_decision_unix=flag.since_ts_unix + 60 + i * 60,
                our_p_yes=0.7, market_p_yes=0.5,
                won_yes=(i < 10),
                pnl_cents=5 if i < 10 else -5,
                outcome="posted",
            )
        graduate, reason, metrics = evaluate_canary_graduation(conn, "KXHIGHNY", flag)
        assert graduate is False
        assert "canary_pnl" in reason

    def test_pre_canary_rows_ignored(self, conn):
        """Live rows from BEFORE the canary flag was set must not count."""
        # Seed 50 rows from an earlier canary stint.
        _seed_family(conn, "KXHIGHNY", 50, start_ts=time.time() - 100 * 3600,
                     outcome="posted")
        # Now flip to canary fresh.
        set_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_live_state(conn, "KXHIGHNY")
        graduate, reason, metrics = evaluate_canary_graduation(conn, "KXHIGHNY", flag)
        assert graduate is False
        assert metrics["n_canary_settled"] == 0


# ── Kill switch ────────────────────────────────────────────────────────────
class TestEvaluateKillSwitch:
    def test_insufficient_live_rows_does_not_trip(self, conn):
        _seed_family(conn, "KXHIGHNY", 5, outcome="posted")
        trip, reason, metrics = evaluate_kill_switch(conn, "KXHIGHNY", 982.0)
        assert trip is False
        assert "insufficient_live_n" in reason

    def test_single_trade_hard_stop_fires_early(self, conn):
        # Even at N=1, a −6% equity loss trips the hard stop.
        _seed_shadow_row(
            conn, family="KXHIGHNY", ts_decision_unix=time.time(),
            our_p_yes=0.5, market_p_yes=0.5,
            won_yes=False, pnl_cents=-6000,  # −$60 on $1000 equity = 6%
            outcome="posted",
        )
        trip, reason, metrics = evaluate_kill_switch(conn, "KXHIGHNY", 1000.0)
        assert trip is True
        assert "single_trade_loss" in reason

    def test_pnl_floor_trips_on_bleed(self, conn):
        # 30 live rows, net -$40 (4% of $1000). Floor is max($30, 3% * $1000 = $30).
        # So floor = -$30, bleed = -$40 → trip.
        for i in range(30):
            _seed_shadow_row(
                conn, family="KXHIGHNY",
                ts_decision_unix=time.time() - (30 - i) * 3600,
                our_p_yes=0.7, market_p_yes=0.5,
                won_yes=(i < 10),
                pnl_cents=100 if i < 10 else -250,  # 10 wins × $1 - 20 losses × $2.50 = -$40
                outcome="posted",
            )
        trip, reason, metrics = evaluate_kill_switch(conn, "KXHIGHNY", 1000.0)
        assert trip is True
        assert "live_pnl" in reason

    def test_brier_regime_shift_trips(self, conn):
        # Shadow baseline: calibrated (Brier ~0.09).
        # Live: anti-calibrated (Brier ~0.49) → delta >> 0.03.
        _seed_family(conn, "KXHIGHNY", 50, our_p_yes=0.7, market_p_yes=0.5,
                     outcome="shadow_only")
        # Seed 30 live rows where we predict 0.7 but win only 10% — Brier ≈ 0.42.
        for i in range(30):
            _seed_shadow_row(
                conn, family="KXHIGHNY",
                ts_decision_unix=time.time() - (30 - i) * 60,
                our_p_yes=0.7, market_p_yes=0.5,
                won_yes=(i < 3),
                pnl_cents=1 if i < 3 else -1,  # small P&L so only Brier trips
                outcome="posted",
            )
        trip, reason, metrics = evaluate_kill_switch(conn, "KXHIGHNY", 1000.0)
        assert trip is True
        # Either brier or pnl could fire; assert that at least one trigger fired.
        assert ("live_brier" in reason or "live_pnl" in reason
                or "single_trade_loss" in reason)

    def test_clean_live_does_not_trip(self, conn):
        _seed_family(conn, "KXHIGHNY", 50, our_p_yes=0.7, market_p_yes=0.5,
                     outcome="shadow_only")
        _seed_family(conn, "KXHIGHNY", 30, our_p_yes=0.7, market_p_yes=0.5,
                     start_ts=time.time() - 30 * 60, outcome="posted")
        trip, reason, _ = evaluate_kill_switch(conn, "KXHIGHNY", 1000.0)
        assert trip is False
        assert reason == "kill_switch_clear"

    def test_n_floor_is_20_not_default_pnl_window(self, conn):
        # 15 live rows, all losing. Below N=20 floor → no trip except hard stop.
        for i in range(15):
            _seed_shadow_row(
                conn, family="KXHIGHNY",
                ts_decision_unix=time.time() - (15 - i) * 3600,
                our_p_yes=0.7, market_p_yes=0.5,
                won_yes=False, pnl_cents=-200,  # small per-trade, no hard stop
                outcome="posted",
            )
        trip, reason, _ = evaluate_kill_switch(conn, "KXHIGHNY", 1000.0)
        assert trip is False
        assert "insufficient_live_n" in reason


# ── Orchestration ──────────────────────────────────────────────────────────
class TestRunPromotionSweep:
    def test_shadow_to_canary_promotion(self, conn):
        _seed_family(conn, "KXHIGHNY", 60, our_p_yes=0.7, market_p_yes=0.5)
        summary = run_promotion_sweep(conn, equity_dollars=1000.0)
        assert len(summary["promoted"]) == 1
        assert summary["promoted"][0]["family"] == "KXHIGHNY"
        assert get_live_state(conn, "KXHIGHNY").state == LiveState.LIVE_CANARY

    def test_blocked_family_skipped(self, conn):
        # KXBTC is hard-blocked; even with great shadow data, stay in shadow.
        _seed_family(conn, "KXBTC", 60, our_p_yes=0.7, market_p_yes=0.5)
        summary = run_promotion_sweep(conn, equity_dollars=1000.0)
        # candidate_families filters out KXBTC, so it's not even "checked".
        assert summary["checked"] == 0

    def test_kill_switch_demotes_full_to_canary(self, conn):
        set_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
        _seed_shadow_row(
            conn, family="KXHIGHNY", ts_decision_unix=time.time(),
            our_p_yes=0.5, market_p_yes=0.5,
            won_yes=False, pnl_cents=-8000,  # 8% equity loss = hard stop
            outcome="posted",
        )
        summary = run_promotion_sweep(conn, equity_dollars=1000.0)
        assert len(summary["demoted"]) == 1
        assert summary["demoted"][0]["to"] == LiveState.LIVE_CANARY
        assert get_live_state(conn, "KXHIGHNY").state == LiveState.LIVE_CANARY

    def test_kill_switch_demotes_canary_to_shadow(self, conn):
        set_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        _seed_shadow_row(
            conn, family="KXHIGHNY", ts_decision_unix=time.time(),
            our_p_yes=0.5, market_p_yes=0.5,
            won_yes=False, pnl_cents=-8000,
            outcome="posted",
        )
        summary = run_promotion_sweep(conn, equity_dollars=1000.0)
        assert summary["demoted"][0]["to"] == LiveState.SHADOW
        assert get_live_state(conn, "KXHIGHNY").state == LiveState.SHADOW

    def test_canary_graduation_to_full(self, conn):
        set_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_live_state(conn, "KXHIGHNY")
        _seed_family(conn, "KXHIGHNY", 35, start_ts=flag.since_ts_unix + 60,
                     our_p_yes=0.7, outcome="posted")
        summary = run_promotion_sweep(conn, equity_dollars=1000.0)
        assert len(summary["graduated"]) == 1
        assert get_live_state(conn, "KXHIGHNY").state == LiveState.LIVE_FULL

    def test_writes_promotion_event_row(self, conn):
        _seed_family(conn, "KXHIGHNY", 60, our_p_yes=0.7, market_p_yes=0.5)
        run_promotion_sweep(conn, equity_dollars=1000.0)
        rows = conn.execute(
            "SELECT old_state, new_state, trigger, metrics_json "
            "FROM promotion_events WHERE family='KXHIGHNY'"
        ).fetchall()
        assert len(rows) == 1
        old, new, trig, metrics_json = rows[0]
        assert old == LiveState.SHADOW
        assert new == LiveState.LIVE_CANARY
        assert trig == "shadow_promotion"
        metrics = json.loads(metrics_json)
        assert metrics["n_shadow_settled"] == 60


# ── Manual re-enable ──────────────────────────────────────────────────────
class TestManualReEnable:
    def test_rejects_when_ratcheted_gate_fails(self, conn):
        # Signal beats baseline by 0.005 but ratchet demands 0.010.
        # Use our_p=0.56, market_p=0.50, wins=56/100 → calibrated.
        # Brier(our) = 0.56*(1-0.56)^2 + 0.44*(0.56)^2 = 0.56*0.1936 + 0.44*0.3136 ≈ 0.246
        # Baseline = 0.25. edge_beat ≈ 0.004 < 0.010 ratcheted.
        _seed_family(conn, "KXHIGHNY", 100, our_p_yes=0.56, market_p_yes=0.50, wins=56)
        enabled, reason = manual_re_enable(conn, "KXHIGHNY")
        assert enabled is False
        assert "ratcheted_gate_failed" in reason

    def test_accepts_strong_signal(self, conn):
        _seed_family(conn, "KXHIGHNY", 60, our_p_yes=0.75, market_p_yes=0.50)
        enabled, reason = manual_re_enable(conn, "KXHIGHNY")
        assert enabled is True
        flag = get_live_state(conn, "KXHIGHNY")
        assert flag.state == LiveState.LIVE_CANARY
        assert flag.manual is True

    def test_rejects_invalid_target(self, conn):
        with pytest.raises(ValueError):
            manual_re_enable(conn, "KXHIGHNY", target_state=LiveState.SHADOW)

    def test_writes_manual_event(self, conn):
        _seed_family(conn, "KXHIGHNY", 60, our_p_yes=0.75, market_p_yes=0.50)
        manual_re_enable(conn, "KXHIGHNY")
        row = conn.execute(
            "SELECT manual, trigger FROM promotion_events WHERE family='KXHIGHNY'"
        ).fetchone()
        assert row[0] == 1
        assert row[1] == "manual"


# ── Kill-switch config ────────────────────────────────────────────────────
class TestKillSwitchConfig:
    def test_default_thresholds(self):
        assert DEFAULT_KILL_SWITCH.pnl_window_n == 30
        assert DEFAULT_KILL_SWITCH.pnl_floor_dollars == 30.0
        assert DEFAULT_KILL_SWITCH.pnl_floor_equity_pct == 0.03
        assert DEFAULT_KILL_SWITCH.single_loss_equity_pct == 0.05
        assert MIN_LIVE_SETTLED_FOR_DEMOTION == 20

    def test_custom_config_applies(self, conn):
        # Tight config trips on $20 loss where default wouldn't.
        tight = KillSwitchConfig(pnl_floor_dollars=5.0, pnl_floor_equity_pct=0.0)
        for i in range(30):
            _seed_shadow_row(
                conn, family="KXHIGHNY",
                ts_decision_unix=time.time() - (30 - i) * 3600,
                our_p_yes=0.5, market_p_yes=0.5,
                won_yes=False, pnl_cents=-100,  # -$30 total
                outcome="posted",
            )
        trip_tight, _, _ = evaluate_kill_switch(conn, "KXHIGHNY", 1000.0, cfg=tight)
        assert trip_tight is True
