"""Tests for bot/learning/mm_promotion.py — Thompson-sized MM gate."""
from __future__ import annotations

import random
import time

import pytest

from bot.config import MM_ORDER_SIZE
from bot.db import init_db
from bot.learning.directional_shadow import LiveState
from bot.learning.mm_promotion import (
    DEFAULT_MM_KILL_SWITCH,
    MMKillSwitchConfig,
    _compute_mm_stats,
    _pnl_for_side_fill,
    _sample_mm_multiplier,
    annotate_shadow_pnl,
    evaluate_mm_graduation,
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


@pytest.fixture(autouse=True)
def _empty_mm_blocklist(monkeypatch):
    """Clear MM_BLOCKED_SERIES so default-blocked series can be exercised.

    Production default holds back KXHIGHCHI/NY/DEN for cohort-2 staggering
    (see bot/config.py). The unit tests here exercise the gate logic itself
    on KXHIGHNY for historical reasons; clearing the blocklist isolates the
    promotion mechanics from the cohort-rollout decision.
    """
    empty: frozenset[str] = frozenset()
    monkeypatch.setattr(
        "bot.learning.mm_promotion.MM_BLOCKED_SERIES", empty,
    )


def _insert_shadow_row(
    conn,
    *,
    ticker: str,
    series: str,
    ts_unix: float,
    proposed_bid: int,
    proposed_ask: int,
    market_yes_bid: int | None,
    market_yes_ask: int | None,
    gate_should_quote: int = 1,
    live_mode: int = 0,
    live_order_id_bid: str | None = None,
    live_order_id_ask: str | None = None,
) -> int:
    mid = None
    if market_yes_bid is not None and market_yes_ask is not None:
        mid = (market_yes_bid + market_yes_ask) // 2
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
         market_yes_bid, market_yes_ask, mid,
         gate_should_quote, live_mode,
         live_order_id_bid, live_order_id_ask),
    )
    conn.commit()
    return cur.lastrowid


def _seed_settled_shadow_rows(
    conn, series: str, n: int, *,
    pnl_per_row: int = 10, fill_pattern: str = "both",
    live_mode: int = 0, start_ts: float | None = None,
):
    """Seed `n` settled shadow rows with the given P&L and fill pattern."""
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


# ── Per-series state API ────────────────────────────────────────────────
class TestMMLiveState:
    def test_default_shadow(self, conn):
        flag = get_mm_live_state(conn, "KXHIGHNY")
        assert flag.state == LiveState.SHADOW

    def test_shadow_multiplier_is_zero(self, conn):
        assert get_mm_order_size_multiplier(conn, "KXHIGHNY") == 0.0
        assert is_mm_live(conn, "KXHIGHNY") is False

    def test_canary_multiplier_is_fixed_one_contract(self, conn):
        # LIVE_CANARY returns 1 / MM_ORDER_SIZE so consumer rounds the
        # effective order_size to exactly 1 contract regardless of equity.
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        assert is_mm_live(conn, "KXHIGHNY") is True
        mult = get_mm_order_size_multiplier(conn, "KXHIGHNY")
        assert mult == pytest.approx(1.0 / max(1, MM_ORDER_SIZE))
        # Effective order size after rounding is ≥1.
        assert max(1, int(round(MM_ORDER_SIZE * mult))) == 1

    def test_canary_multiplier_ignores_shadow_data(self, conn):
        # Even with rich shadow history, CANARY stays at 1 contract.
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=40, pnl_per_row=8, fill_pattern="bid",
        )
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        mult = get_mm_order_size_multiplier(
            conn, "KXHIGHNY", force_resample=True,
        )
        assert mult == pytest.approx(1.0 / max(1, MM_ORDER_SIZE))

    def test_live_without_data_returns_zero(self, conn):
        # LIVE_FULL but <min_n settled fills → Thompson returns 0.
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
        assert is_mm_live(conn, "KXHIGHNY") is True
        assert get_mm_order_size_multiplier(
            conn, "KXHIGHNY", force_resample=True,
        ) == 0.0

    def test_live_with_good_data_returns_positive(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=30, pnl_per_row=5, fill_pattern="bid",
        )
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
        # With mean=5¢ and target=2¢, the Thompson draw centered at 5/2=2.5
        # clamps to cap=1.0 almost always. Take the average of many samples.
        mults = []
        for _ in range(20):
            m = get_mm_order_size_multiplier(
                conn, "KXHIGHNY", force_resample=True,
            )
            mults.append(m)
        assert max(mults) > 0.5
        assert sum(mults) / len(mults) > 0.5

    def test_case_insensitive_key(self, conn):
        set_mm_live_state(conn, "kxhighny", LiveState.LIVE_FULL)
        assert is_mm_live(conn, "KXHIGHNY") is True

    def test_invalid_state_raises(self, conn):
        with pytest.raises(ValueError):
            set_mm_live_state(conn, "KXHIGHNY", "nonsense")

    def test_multiplier_is_cached_between_calls(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=30, pnl_per_row=5, fill_pattern="bid",
        )
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
        m1 = get_mm_order_size_multiplier(conn, "KXHIGHNY")
        m2 = get_mm_order_size_multiplier(conn, "KXHIGHNY")
        assert m1 == m2  # cached


# ── P&L helper ──────────────────────────────────────────────────────────
class TestPnLPerSideFill:
    def test_yes_wins_at_40c_gains_60c(self):
        pnl = _pnl_for_side_fill("yes", 40, contracts=10, won=True)
        assert pnl > 0
        assert pnl <= 600

    def test_yes_loses_at_40c_loses_40c(self):
        pnl = _pnl_for_side_fill("yes", 40, contracts=10, won=False)
        assert pnl < 0
        assert pnl <= -400

    def test_no_bought_at_30c_wins_gains_70c(self):
        pnl = _pnl_for_side_fill("no", 30, contracts=5, won=True)
        assert pnl > 0


# ── Shadow fill matcher ─────────────────────────────────────────────────
class TestMatchShadowFills:
    def test_bid_fills_when_market_ask_drops(self, conn):
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-X", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=50, market_yes_ask=60,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-X", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=48, market_yes_ask=54,
        )
        summary = match_shadow_fills(conn, lifetime_s=600)
        assert summary["bid_fills"] >= 1

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
            market_yes_bid=56, market_yes_ask=62,
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

    def test_gate_rejected_marks_unfilled(self, conn):
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-G", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=40, market_yes_ask=50,
            gate_should_quote=0,
        )
        match_shadow_fills(conn, lifetime_s=600)
        row = conn.execute(
            "SELECT shadow_bid_filled, shadow_ask_filled "
            "FROM weather_mm_shadow WHERE ts_unix=?", (int(t0),)
        ).fetchone()
        assert row[0] == 0 and row[1] == 0

    def test_idempotent(self, conn):
        # Two candidate rows (old enough that their lifetime window is
        # closed) plus a recent observation row that seeds the window.
        now = time.time()
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-I", series="KXHIGHNY", ts_unix=now - 1000,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=40, market_yes_ask=50,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-I", series="KXHIGHNY", ts_unix=now - 900,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=40, market_yes_ask=50,
        )
        # Recent row inside both candidates' 600s windows but outside the
        # matcher's ts_unix < now - 600 scan filter — acts as pure data.
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-I", series="KXHIGHNY", ts_unix=now - 500,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=40, market_yes_ask=50,
        )
        s1 = match_shadow_fills(conn, lifetime_s=600)
        s2 = match_shadow_fills(conn, lifetime_s=600)
        assert s1["checked"] >= 2
        assert s2["checked"] == 0

    def test_zero_book_observations_do_not_produce_fills(self, conn):
        # Regression for the 2026-04-17 data-corruption episode: when the
        # book snapshot is all zeros (which _safe_cents used to store for
        # missing sides), the matcher must not treat zero as a crossing
        # price. Otherwise every row's bid "fills" at 0¢.
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-Z0", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=0, market_yes_ask=0,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-Z0", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=0, market_yes_ask=0,
        )
        summary = match_shadow_fills(conn, lifetime_s=600)
        assert summary["bid_fills"] == 0
        assert summary["ask_fills"] == 0

    def test_no_observations_leaves_row_unmatched(self, conn):
        # If the lifetime window saw no valid book observation on either
        # side, the matcher must *not* UPDATE — leaving shadow_bid_filled
        # as NULL so a later run with better data can still resolve it.
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-ZN", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=None, market_yes_ask=None,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-ZN", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=55, proposed_ask=60,
            market_yes_bid=0, market_yes_ask=0,
        )
        match_shadow_fills(conn, lifetime_s=600)
        row = conn.execute(
            "SELECT shadow_bid_filled, shadow_ask_filled "
            "FROM weather_mm_shadow WHERE ts_unix=?", (int(t0),),
        ).fetchone()
        assert row[0] is None and row[1] is None

    def test_partial_observation_does_match(self, conn):
        # If only the ask side has real observations (bid is None/0), the
        # matcher should still resolve the bid-fill question and mark the
        # row (ask remains 0 because no real bid obs means no ask-fill).
        t0 = time.time() - 1000
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-P", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=55, proposed_ask=70,
            market_yes_bid=0, market_yes_ask=60,
        )
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-P", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=55, proposed_ask=70,
            market_yes_bid=0, market_yes_ask=50,  # crosses bid=55
        )
        match_shadow_fills(conn, lifetime_s=600)
        row = conn.execute(
            "SELECT shadow_bid_filled, shadow_ask_filled "
            "FROM weather_mm_shadow WHERE ts_unix=?", (int(t0),),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == 0


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
        assert row[0] > 0
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


# ── live_pnl_cents wiring via fills_ledger join (T3.3) ──────────────────
def _insert_fill(
    conn,
    *,
    trade_id: str,
    ticker: str,
    side: str,
    yes_price: int,
    no_price: int,
    contracts: int,
    fee_cents: int,
    fill_ts_unix: float,
    source: str = "mm_quote",
    live_mode: int = 1,
    action: str = "buy",
    is_taker: int = 0,
    client_order_id: str = "mm_wx_abc",
) -> None:
    """Insert one fills_ledger row. Mirrors FillsWriter schema exactly —
    tests that need to drive the live P&L annotator forge rows here
    rather than running the real writer, which would require network.
    """
    from bot.core.categorization import _get_series_prefix
    from bot.learning.alpha_log import family_from_ticker
    series, _ = _get_series_prefix(ticker)
    family = family_from_ticker(ticker)
    conn.execute(
        "INSERT INTO fills_ledger "
        "(trade_id, order_id, client_order_id, ticker, series, family, "
        " side, action, contracts, yes_price_cents, no_price_cents, "
        " is_taker, fee_cents, fill_ts_iso, fill_ts_unix, "
        " ingested_ts_unix, live_mode, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, "ord_" + trade_id, client_order_id, ticker, series, family,
         side, action, contracts, yes_price, no_price,
         is_taker, fee_cents, "t", float(fill_ts_unix),
         float(fill_ts_unix), live_mode, source),
    )
    conn.commit()


class TestAnnotateShadowPnlLivePaired:
    """T3.3 — live_pnl_cents populated from fills_ledger at settlement."""

    def test_shadow_only_row_leaves_live_pnl_null(self, conn):
        # live_mode=0 row: no live order was posted, so the column must
        # stay NULL. evaluate_mm_graduation filters on `live_pnl_cents
        # IS NOT NULL` and would incorrectly include shadow-only rows if
        # we wrote 0 here.
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-SH", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=0,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()
        annotate_shadow_pnl(
            conn, "KXHIGHNY-SH", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        row = conn.execute(
            "SELECT shadow_pnl_cents, live_pnl_cents "
            "FROM weather_mm_shadow WHERE id=?", (rid,),
        ).fetchone()
        assert row[0] > 0           # shadow still credits the fill
        assert row[1] is None       # live column untouched

    def test_live_row_without_fills_gets_zero(self, conn):
        # The drift case graduation exists to catch: shadow predicted a
        # fill but the live order never crossed. live_pnl_cents must be
        # 0, not NULL, so the graduation gate counts this as a paired
        # row pulling the realization ratio down.
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-L0", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=1,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()
        annotate_shadow_pnl(
            conn, "KXHIGHNY-L0", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        row = conn.execute(
            "SELECT shadow_pnl_cents, live_pnl_cents "
            "FROM weather_mm_shadow WHERE id=?", (rid,),
        ).fetchone()
        assert row[0] > 0           # shadow optimistic
        assert row[1] == 0          # live realized zero — paired drift

    def test_live_row_with_matching_fill_accumulates_realized_pnl(self, conn):
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-LP", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=1,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()
        # Live fill inside the shadow row's 300s lifetime window: bought
        # YES @ 40¢, 5 contracts, 2¢ maker fee. YES settles → gross
        # payout = (100-40)*5 = 300; net = 300 - 2 = 298.
        _insert_fill(
            conn, trade_id="t1", ticker="KXHIGHNY-LP",
            side="yes", yes_price=40, no_price=60,
            contracts=5, fee_cents=2, fill_ts_unix=t0 + 30,
        )
        annotate_shadow_pnl(
            conn, "KXHIGHNY-LP", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        row = conn.execute(
            "SELECT live_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid,),
        ).fetchone()
        assert row[0] == 298

    def test_no_side_fill_on_ticker_yields_negative_on_yes_win(self, conn):
        # ask-side live fill: bought NO @ (100-proposed_ask)=30¢. If YES
        # wins, NO pays 0 → P&L = (0-30)*contracts - fee.
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-NO", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=1,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_ask_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()
        _insert_fill(
            conn, trade_id="t2", ticker="KXHIGHNY-NO",
            side="no", yes_price=70, no_price=30,
            contracts=5, fee_cents=2, fill_ts_unix=t0 + 30,
        )
        annotate_shadow_pnl(
            conn, "KXHIGHNY-NO", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        row = conn.execute(
            "SELECT live_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid,),
        ).fetchone()
        # (0 - 30) * 5 - 2 = -152
        assert row[0] == -152

    def test_fills_outside_lifetime_are_orphaned(self, conn):
        t0 = time.time() - 10_000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-ORF", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=1,
        )
        conn.commit()
        # Fill happens 1 hour after the shadow row — well past the 300s
        # lifetime. Live order would have been cancelled by a later
        # requote before this; don't attribute.
        _insert_fill(
            conn, trade_id="t3", ticker="KXHIGHNY-ORF",
            side="yes", yes_price=40, no_price=60,
            contracts=5, fee_cents=2, fill_ts_unix=t0 + 3600,
        )
        annotate_shadow_pnl(
            conn, "KXHIGHNY-ORF", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        row = conn.execute(
            "SELECT live_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid,),
        ).fetchone()
        # Row is live_mode=1 but orphan-fill → live_pnl_cents = 0
        assert row[0] == 0

    def test_fills_attributed_to_latest_preceding_shadow_row(self, conn):
        # Two live shadow rows at t0 and t0+60. A fill at t0+90 belongs
        # to the second row (the later one that was resting at the time).
        t0 = time.time() - 5000
        rid_old = _insert_shadow_row(
            conn, ticker="KXHIGHNY-AT", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=1,
        )
        rid_new = _insert_shadow_row(
            conn, ticker="KXHIGHNY-AT", series="KXHIGHNY", ts_unix=t0 + 60,
            proposed_bid=42, proposed_ask=72,
            market_yes_bid=42, market_yes_ask=72,
            live_mode=1,
        )
        conn.commit()
        _insert_fill(
            conn, trade_id="t4", ticker="KXHIGHNY-AT",
            side="yes", yes_price=42, no_price=58,
            contracts=1, fee_cents=0, fill_ts_unix=t0 + 90,
        )
        annotate_shadow_pnl(
            conn, "KXHIGHNY-AT", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        old_pnl = conn.execute(
            "SELECT live_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid_old,),
        ).fetchone()[0]
        new_pnl = conn.execute(
            "SELECT live_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid_new,),
        ).fetchone()[0]
        assert old_pnl == 0                # fill did not belong to it
        assert new_pnl == (100 - 42)       # fill belonged to the newer row

    def test_non_mm_quote_source_is_ignored(self, conn):
        # Directional or exit fills must not pollute MM paired data.
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-EX", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=1,
        )
        conn.commit()
        _insert_fill(
            conn, trade_id="t5", ticker="KXHIGHNY-EX",
            side="yes", yes_price=40, no_price=60,
            contracts=5, fee_cents=2, fill_ts_unix=t0 + 30,
            source="exit", client_order_id="mm_exit_xyz",
        )
        _insert_fill(
            conn, trade_id="t6", ticker="KXHIGHNY-EX",
            side="yes", yes_price=40, no_price=60,
            contracts=5, fee_cents=2, fill_ts_unix=t0 + 40,
            source="directional", client_order_id="mm_dir_abc",
        )
        annotate_shadow_pnl(
            conn, "KXHIGHNY-EX", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        live_pnl = conn.execute(
            "SELECT live_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid,),
        ).fetchone()[0]
        assert live_pnl == 0        # neither fill was source='mm_quote'

    def test_live_mode_zero_fill_is_ignored(self, conn):
        # A live fill ingested before WEATHER_MM_LIVE was set (live_mode=0
        # on the fills_ledger row) must not count toward live_pnl for a
        # later live-mode shadow row. Paired data are strictly live×live.
        t0 = time.time() - 5000
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-SM", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=70,
            market_yes_bid=40, market_yes_ask=70,
            live_mode=1,
        )
        conn.commit()
        _insert_fill(
            conn, trade_id="t7", ticker="KXHIGHNY-SM",
            side="yes", yes_price=40, no_price=60,
            contracts=5, fee_cents=2, fill_ts_unix=t0 + 30,
            live_mode=0,
        )
        annotate_shadow_pnl(
            conn, "KXHIGHNY-SM", won_yes=True, ts_settle_unix=t0 + 7200,
        )
        live_pnl = conn.execute(
            "SELECT live_pnl_cents FROM weather_mm_shadow WHERE id=?",
            (rid,),
        ).fetchone()[0]
        assert live_pnl == 0


# ── SHADOW → CANARY promotion gate (B+D rewrite) ────────────────────────
class TestEvaluatePromotion:
    def test_no_fills_fails(self, conn):
        ok, reason, _ = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert not ok
        assert "insufficient_fills" in reason

    def test_few_fills_fails(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=3, pnl_per_row=10, fill_pattern="bid",
        )
        ok, reason, _ = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert not ok
        assert "insufficient_fills" in reason

    def test_negative_pnl_blocks_promotion(self, conn):
        # B+D rewrite: negative realized shadow P&L must block promotion
        # (was previously accepted on N-floor alone; the 2026-04-17
        # _safe_cents bug proved that rule dangerous). Seed n above
        # MM_SIZING_MIN_N (15) so the unprofitable_shadow gate is reached.
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=-5, fill_pattern="bid",
        )
        ok, reason, metrics = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert not ok
        assert "unprofitable_shadow" in reason
        assert metrics["pnl_per_fill_cents"] == pytest.approx(-5.0)

    def test_breakeven_pnl_blocks_promotion(self, conn):
        # Zero P&L per fill (the Apr-17 contamination signature) must also
        # fail the >= 2¢ floor.
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=0, fill_pattern="bid",
        )
        ok, reason, _ = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert not ok
        assert "unprofitable_shadow" in reason

    def test_positive_pnl_passes_gate(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=5, fill_pattern="bid",
        )
        ok, reason, metrics = evaluate_mm_promotion(conn, "KXHIGHNY")
        assert ok, reason
        assert "canary_gate_passed" in reason
        assert metrics["n_fills"] == 20
        assert metrics["pnl_per_fill_cents"] == pytest.approx(5.0)


# ── CANARY → FULL graduation gate ───────────────────────────────────────
def _insert_paired_row(
    conn, *, series: str, ticker: str, ts_unix: float,
    shadow_pnl: int, live_pnl: int,
) -> int:
    """Insert a settled, paired (shadow + live) row at ts_unix."""
    rid = _insert_shadow_row(
        conn, ticker=ticker, series=series, ts_unix=ts_unix,
        proposed_bid=40, proposed_ask=60,
        market_yes_bid=45, market_yes_ask=55,
        live_mode=1,
    )
    conn.execute(
        "UPDATE weather_mm_shadow SET shadow_bid_filled=1, "
        "shadow_ask_filled=0, shadow_pnl_cents=?, "
        "live_pnl_cents=?, ts_settle_unix=? WHERE id=?",
        (shadow_pnl, live_pnl, ts_unix + 3600, rid),
    )
    conn.commit()
    return rid


class TestEvaluateGraduation:
    def test_insufficient_paired_fails(self, conn):
        # Below MM_GRADUATION_MIN_PAIRED_N (default 8 post-2026-04-26 relax).
        t0 = time.time() - 86400
        for i in range(5):
            _insert_paired_row(
                conn, series="KXHIGHNY", ticker=f"KXHIGHNY-{i}",
                ts_unix=t0 + i * 3600,
                shadow_pnl=10, live_pnl=8,
            )
        ok, reason, metrics = evaluate_mm_graduation(
            conn, "KXHIGHNY", since_ts_unix=t0 - 60,
        )
        assert not ok
        assert "insufficient_paired" in reason
        assert metrics["n_paired"] == 5

    def test_shadow_nonpositive_blocks_graduation(self, conn):
        # Enough pairs, but shadow sum is 0 → can't divide, refuse.
        t0 = time.time() - 86400
        for i in range(35):
            _insert_paired_row(
                conn, series="KXHIGHNY", ticker=f"KXHIGHNY-{i}",
                ts_unix=t0 + i * 3600,
                shadow_pnl=-5, live_pnl=-5,
            )
        ok, reason, _ = evaluate_mm_graduation(
            conn, "KXHIGHNY", since_ts_unix=t0 - 60,
        )
        assert not ok
        assert "shadow_nonpositive" in reason

    def test_ratio_below_floor_blocks(self, conn):
        # Shadow predicts $1 profit per row but live captures only 20% of it.
        t0 = time.time() - 86400
        for i in range(35):
            _insert_paired_row(
                conn, series="KXHIGHNY", ticker=f"KXHIGHNY-{i}",
                ts_unix=t0 + i * 3600,
                shadow_pnl=100, live_pnl=20,
            )
        ok, reason, metrics = evaluate_mm_graduation(
            conn, "KXHIGHNY", since_ts_unix=t0 - 60,
        )
        assert not ok
        assert "ratio_below_floor" in reason
        assert metrics["live_over_shadow_ratio"] == pytest.approx(0.2)

    def test_all_gates_pass_graduates(self, conn):
        t0 = time.time() - 86400
        for i in range(35):
            _insert_paired_row(
                conn, series="KXHIGHNY", ticker=f"KXHIGHNY-{i}",
                ts_unix=t0 + i * 3600,
                shadow_pnl=100, live_pnl=80,
            )
        ok, reason, metrics = evaluate_mm_graduation(
            conn, "KXHIGHNY", since_ts_unix=t0 - 60,
        )
        assert ok, reason
        assert "graduated" in reason
        assert metrics["n_paired"] == 35
        assert metrics["live_over_shadow_ratio"] == pytest.approx(0.8)

    def test_since_ts_excludes_older_rows(self, conn):
        # Rows older than since_ts_unix must not count toward graduation.
        # Canary floor is 30 paired rows; plant 35 old rows + 3 new ones.
        t_old = time.time() - 30 * 86400
        for i in range(35):
            _insert_paired_row(
                conn, series="KXHIGHNY", ticker=f"KXHIGHNY-old-{i}",
                ts_unix=t_old + i * 3600,
                shadow_pnl=100, live_pnl=80,
            )
        t_canary = time.time() - 3600
        for i in range(3):
            _insert_paired_row(
                conn, series="KXHIGHNY", ticker=f"KXHIGHNY-new-{i}",
                ts_unix=t_canary + i * 60,
                shadow_pnl=100, live_pnl=80,
            )
        ok, reason, metrics = evaluate_mm_graduation(
            conn, "KXHIGHNY", since_ts_unix=t_canary - 60,
        )
        assert not ok
        assert "insufficient_paired" in reason
        assert metrics["n_paired"] == 3


# ── Thompson sampling integration ───────────────────────────────────────
class TestSampleMMMultiplier:
    def test_empty_series_returns_zero(self, conn):
        decision = _sample_mm_multiplier(conn, "KXHIGHNY")
        assert decision.multiplier == 0.0
        assert decision.reason == "insufficient_n"

    def test_zero_fill_rows_ignored(self, conn):
        # 20 settled rows but none filled → n=0 into Thompson.
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=0, fill_pattern="none",
        )
        decision = _sample_mm_multiplier(conn, "KXHIGHNY")
        assert decision.multiplier == 0.0
        assert decision.n == 0

    def test_positive_pnl_samples_positive(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=40, pnl_per_row=8, fill_pattern="bid",
        )
        decision = _sample_mm_multiplier(conn, "KXHIGHNY")
        # Mean = 8¢ / target = 2¢ → posterior mean ≈ 4.0, clamped to cap 1.0.
        assert decision.mean_cents == 8.0
        assert decision.multiplier == 1.0
        assert decision.reason == "degenerate_variance"

    def test_negative_pnl_samples_zero(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=40, pnl_per_row=-10, fill_pattern="bid",
        )
        decision = _sample_mm_multiplier(conn, "KXHIGHNY")
        assert decision.multiplier == 0.0


# ── Kill switch ─────────────────────────────────────────────────────────
class TestKillSwitch:
    def test_single_large_loss_trips(self, conn):
        t0 = time.time() - 3600
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-K1", series="KXHIGHNY", ts_unix=t0,
            proposed_bid=40, proposed_ask=60,
            market_yes_bid=45, market_yes_ask=55, live_mode=1,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1, "
            "shadow_pnl_cents=?, ts_settle_unix=? WHERE id=?",
            (-10000, t0 + 60, rid),
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
    def test_gate_promotes_to_canary_not_full(self, conn):
        # B+D rewrite: promotion lands on LIVE_CANARY; FULL requires
        # graduation evidence the sweep can't fabricate on first pass.
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=6, fill_pattern="bid",
        )
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        promoted = [p for p in summary["promoted"]
                    if p["series"] == "KXHIGHNY"]
        assert len(promoted) == 1
        assert promoted[0]["to"] == LiveState.LIVE_CANARY
        assert (get_mm_live_state(conn, "KXHIGHNY").state
                == LiveState.LIVE_CANARY)
        # Canary does not Thompson-sample, so no "multiplier" key on entry.
        assert "multiplier" not in promoted[0]

    def test_negative_pnl_blocks_promotion(self, conn):
        # Even with n >= min_n, bad shadow P&L must keep us in SHADOW.
        # This is the regression guard against the 2026-04-17 corruption
        # that produced near-zero P&L across hundreds of rows.
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=-5, fill_pattern="bid",
        )
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        assert len(summary["promoted"]) == 0
        assert (get_mm_live_state(conn, "KXHIGHNY").state
                == LiveState.SHADOW)

    def test_summary_has_graduated_key(self, conn):
        # Sanity: the sweep summary exposes the new "graduated" key.
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        assert "graduated" in summary
        assert summary["graduated"] == []

    def test_canary_graduates_to_full_with_paired_evidence(self, conn):
        # Set CANARY, seed enough paired rows after since_ts to clear
        # MM_GRADUATION_MIN_PAIRED_N (8 post-2026-04-26), expect graduation to LIVE_FULL
        # and a Thompson resample.
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        flag = get_mm_live_state(conn, "KXHIGHNY")
        # Insert paired rows strictly after the CANARY since_ts_unix.
        for i in range(35):
            _insert_paired_row(
                conn, series="KXHIGHNY", ticker=f"KXHIGHNY-g{i}",
                ts_unix=flag.since_ts_unix + 60 + i * 60,
                shadow_pnl=100, live_pnl=80,
            )
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        graduated = [g["series"] for g in summary["graduated"]]
        assert "KXHIGHNY" in graduated
        assert (get_mm_live_state(conn, "KXHIGHNY").state
                == LiveState.LIVE_FULL)

    def test_canary_unchanged_without_paired_evidence(self, conn):
        # CANARY with no paired data stays CANARY.
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        assert len(summary["graduated"]) == 0
        assert (get_mm_live_state(conn, "KXHIGHNY").state
                == LiveState.LIVE_CANARY)

    def test_kill_switch_demotes_canary_to_shadow(self, conn):
        # Kill-switch fires on any LIVE state and demotes straight to
        # SHADOW (no intermediate stop).
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_CANARY)
        t0 = time.time() - 3600
        rid = _insert_shadow_row(
            conn, ticker="KXHIGHNY-K3", series="KXHIGHNY", ts_unix=t0,
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
        assert "KXHIGHNY" in [d["series"] for d in summary["demoted"]]
        assert (get_mm_live_state(conn, "KXHIGHNY").state
                == LiveState.SHADOW)

    def test_kill_switch_demotes_full_to_shadow(self, conn):
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
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
        assert "KXHIGHNY" in [d["series"] for d in summary["demoted"]]
        assert (get_mm_live_state(conn, "KXHIGHNY").state
                == LiveState.SHADOW)

    def test_resampled_section_populated_for_existing_full(self, conn):
        set_mm_live_state(conn, "KXHIGHNY", LiveState.LIVE_FULL)
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=20, pnl_per_row=4, fill_pattern="bid",
        )
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        resampled_series = [r["series"] for r in summary["resampled"]]
        assert "KXHIGHNY" in resampled_series
        assert "KXHIGHNY" not in [p["series"] for p in summary["promoted"]]

    def test_unchanged_when_below_n_floor(self, conn):
        _seed_settled_shadow_rows(
            conn, "KXHIGHNY", n=2, pnl_per_row=5, fill_pattern="bid",
        )
        summary = run_mm_promotion_sweep(conn, equity_dollars=1000.0)
        assert len(summary["promoted"]) == 0
        assert (get_mm_live_state(conn, "KXHIGHNY").state
                == LiveState.SHADOW)


# ── Regression: record_settlements() annotates shadow rows for
#    shadow-only tickers (no bot position on the ticker). ────────────────
# This guards the 2026-04-22 structural bug where annotate_shadow_pnl was
# placed after the "this ticker wasn't traded by the bot" early-`continue`
# gate, making it unreachable for weather tickers under WEATHER_MM_LIVE=false.
class TestRecordSettlementsShadowAnnotation:
    def _canned_settlement(self, ticker: str, market_result: str = "yes") -> dict:
        return {
            "ticker": ticker,
            "market_result": market_result,
            "revenue": 0,
            "yes_count_fp": "0",
            "no_count_fp": "0",
            "yes_total_cost_dollars": "0",
            "no_total_cost_dollars": "0",
            "fee_cost": 0,
        }

    def test_shadow_only_ticker_gets_annotated(self, conn, monkeypatch):
        import trade  # imported lazily so sqlite/bot.db path resolves first

        ticker = "KXHIGHNY-26APR22-T64"
        rid = _insert_shadow_row(
            conn, ticker=ticker, series="KXHIGHNY",
            ts_unix=time.time() - 3600,
            proposed_bid=45, proposed_ask=55,
            market_yes_bid=47, market_yes_ask=53,
        )
        # simulate bid side filled during the lifetime window
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()

        # No mm_orders, no trades, no safe_compounder_orders rows for this
        # ticker — this is the "shadow-only" case. Pre-fix, record_settlements
        # would hit `continue` at the bot-position gate and never annotate.
        monkeypatch.setattr(
            trade, "api_get",
            lambda path: {"settlements": [self._canned_settlement(ticker, "yes")]}
            if path.startswith("/portfolio/settlements") else {},
        )

        recorded = trade.record_settlements(conn)

        # The settlement itself won't be inserted (skipped_notours), but
        # the shadow row MUST be annotated.
        assert recorded == 0, (
            "settlement should be skipped as not-ours — this test is about "
            "annotation, not settlement recording"
        )
        row = conn.execute(
            "SELECT ticker_settled_yes, ts_settle_unix, shadow_pnl_cents "
            "FROM weather_mm_shadow WHERE id=?",
            (rid,),
        ).fetchone()
        assert row is not None
        settled_yes, ts_settle, shadow_pnl = row
        assert settled_yes == 1, "settled_yes should reflect market_result=yes"
        assert ts_settle is not None, (
            "ts_settle_unix must be non-null — this is the column the "
            "promotion gate filters on; NULL here = shadow data invisible "
            "to evaluate_mm_promotion"
        )
        # bid filled @ 45c, YES wins → +55c per contract × MM_ORDER_SIZE
        assert shadow_pnl is not None
        assert shadow_pnl > 0, (
            f"expected positive shadow P&L for filled-bid-wins-yes, "
            f"got {shadow_pnl}"
        )

    def test_no_side_fill_still_stamps_settle_ts(self, conn, monkeypatch):
        """Unfilled rows also need ts_settle_unix so the graduation gate
        can count them in denominators (fill-rate, gate-pass rate)."""
        import trade

        ticker = "KXHIGHCHI-26APR22-T58"
        rid = _insert_shadow_row(
            conn, ticker=ticker, series="KXHIGHCHI",
            ts_unix=time.time() - 3600,
            proposed_bid=30, proposed_ask=40,
            market_yes_bid=32, market_yes_ask=38,
        )
        # no fills — bid_f=0, ask_f=0
        monkeypatch.setattr(
            trade, "api_get",
            lambda path: {"settlements": [self._canned_settlement(ticker, "no")]}
            if path.startswith("/portfolio/settlements") else {},
        )

        trade.record_settlements(conn)

        ts_settle = conn.execute(
            "SELECT ts_settle_unix FROM weather_mm_shadow WHERE id=?", (rid,),
        ).fetchone()[0]
        assert ts_settle is not None

    def test_idempotent_across_sweeps(self, conn, monkeypatch):
        """Kalshi's /portfolio/settlements returns the same rows for hours
        until they roll off. Re-annotating must be a no-op, not a
        double-write — annotate_shadow_pnl guards this with
        WHERE ts_settle_unix IS NULL."""
        import trade

        ticker = "KXHIGHMIA-26APR22-T81"
        rid = _insert_shadow_row(
            conn, ticker=ticker, series="KXHIGHMIA",
            ts_unix=time.time() - 3600,
            proposed_bid=50, proposed_ask=60,
            market_yes_bid=52, market_yes_ask=58,
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=1 WHERE id=?",
            (rid,),
        )
        conn.commit()
        monkeypatch.setattr(
            trade, "api_get",
            lambda path: {"settlements": [self._canned_settlement(ticker, "yes")]}
            if path.startswith("/portfolio/settlements") else {},
        )

        trade.record_settlements(conn)
        first_ts = conn.execute(
            "SELECT ts_settle_unix FROM weather_mm_shadow WHERE id=?", (rid,),
        ).fetchone()[0]
        assert first_ts is not None

        # Re-run — ts_settle_unix must NOT change.
        trade.record_settlements(conn)
        second_ts = conn.execute(
            "SELECT ts_settle_unix FROM weather_mm_shadow WHERE id=?", (rid,),
        ).fetchone()[0]
        assert first_ts == second_ts, (
            "second sweep rewrote ts_settle_unix — the "
            "`WHERE ts_settle_unix IS NULL` guard is not holding"
        )

    def test_personal_trade_skipped_before_annotation(self, conn, monkeypatch):
        """Personal prefixes (NBA/NCAA/NFL/etc) bail out before annotation —
        no shadow row would exist for them anyway, but make sure we don't
        insert spurious ones or raise."""
        import trade

        monkeypatch.setattr(
            trade, "api_get",
            lambda path: {"settlements": [
                self._canned_settlement("KXNBAGAME-26APR22-LAL", "yes")
            ]} if path.startswith("/portfolio/settlements") else {},
        )
        # Should not raise, should not print annotate-fail.
        trade.record_settlements(conn)
