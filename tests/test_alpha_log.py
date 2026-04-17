"""Tests for bot.learning.alpha_log — the decision-time alpha_backtest logger.

Covers:
- family_from_ticker extraction for weather/econ/crypto/company families
- _parse_kalshi_cents coercing all the Kalshi field shapes
- market_snapshot_from_dict tolerating field-name variants
- resolve_market_prob resolution ladder (mid / last / wide_mid / one_side / none)
- log_decision round-trip against a fresh alpha_backtest schema
- log_decision never raises even on malformed inputs
- fill_settlement idempotency and won_yes translation
"""

from __future__ import annotations

import pytest

from bot.db import init_db
from bot.learning.alpha_log import (
    DecisionOutcome,
    DecisionType,
    EnsembleSnapshot,
    MarketProbSource,
    MarketSnapshot,
    _parse_kalshi_cents,
    family_from_ticker,
    fill_settlement,
    log_decision,
    market_snapshot_from_dict,
    resolve_market_prob,
)


# ══════════════════════════════════════════════════════════════════════════════
# family_from_ticker
# ══════════════════════════════════════════════════════════════════════════════

class TestFamilyFromTicker:
    @pytest.mark.parametrize("ticker,family", [
        ("KXHIGHMIA-26APR18-T75", "KXHIGHMIA"),
        ("KXFED-26MAY-T425", "KXFED"),
        ("KXBTC-26APR30-B35000", "KXBTC"),
        ("KXETH-26MAY15-B2500", "KXETH"),
        ("KXHIGHNY-26APR18-B65_70", "KXHIGHNY"),
        ("SIMPLE", "SIMPLE"),
        ("", ""),
    ])
    def test_extracts_prefix(self, ticker, family):
        assert family_from_ticker(ticker) == family


# ══════════════════════════════════════════════════════════════════════════════
# _parse_kalshi_cents
# ══════════════════════════════════════════════════════════════════════════════

class TestParseKalshiCents:
    @pytest.mark.parametrize("raw,expected", [
        (None, None),
        ("", None),
        (0, None),
        ("0", None),
        ("0.47", 47),
        (0.47, 47),
        (47, 47),
        (47.0, 47),
        ("47", 47),
        ("0.995", 100),     # rounds up
        ("not-a-number", None),
        (-5, None),
        ("-0.50", None),
    ])
    def test_coerce(self, raw, expected):
        assert _parse_kalshi_cents(raw) == expected

    def test_off_by_one_safe(self):
        # The Kalshi _dollars string "0.47" in float is 0.46999...; int() would
        # yield 46. round() yields 47. This is the CLAUDE.md bug pattern #5 test.
        val = _parse_kalshi_cents("0.47")
        assert val == 47


# ══════════════════════════════════════════════════════════════════════════════
# market_snapshot_from_dict
# ══════════════════════════════════════════════════════════════════════════════

class TestMarketSnapshotFromDict:
    def test_standard_fields(self):
        m = {"yes_bid": 0.48, "yes_ask": 0.52, "last_price": 0.50, "volume": 125}
        snap = market_snapshot_from_dict(m)
        assert snap.yes_bid_cents == 48
        assert snap.yes_ask_cents == 52
        assert snap.yes_last_cents == 50
        assert snap.volume_fp == 125
        assert snap.last_trade_age_s is None  # not yet tracked

    def test_dollars_variant(self):
        m = {"yes_bid_dollars": "0.48", "yes_ask_dollars": "0.52"}
        snap = market_snapshot_from_dict(m)
        assert snap.yes_bid_cents == 48
        assert snap.yes_ask_cents == 52

    def test_cents_variant(self):
        m = {"yes_bid_cents": 48, "yes_ask_cents": 52}
        snap = market_snapshot_from_dict(m)
        assert snap.yes_bid_cents == 48
        assert snap.yes_ask_cents == 52

    def test_missing_fields(self):
        snap = market_snapshot_from_dict({})
        assert snap.yes_bid_cents is None
        assert snap.yes_ask_cents is None
        assert snap.yes_last_cents is None
        assert snap.volume_fp is None

    def test_malformed_input(self):
        # Must not raise — returns empty snapshot
        snap = market_snapshot_from_dict("not a dict")
        assert snap.yes_bid_cents is None
        assert snap.yes_ask_cents is None


# ══════════════════════════════════════════════════════════════════════════════
# resolve_market_prob — the layered fallback ladder
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveMarketProb:
    def test_tight_mid(self):
        # Bid 48, Ask 52 → spread 4¢ (<=5) → 'mid' at 50¢
        snap = MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52)
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.MID
        assert p == pytest.approx(0.50)
        assert spread == 4

    def test_wide_mid_falls_back_to_last_when_fresh(self):
        # Spread 20¢ (>5) + fresh last at 55¢ → 'last'
        snap = MarketSnapshot(
            yes_bid_cents=40, yes_ask_cents=60,
            yes_last_cents=55, last_trade_age_s=60.0,
        )
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.LAST
        assert p == pytest.approx(0.55)
        assert spread == 20

    def test_wide_mid_when_last_stale(self):
        # Spread 20¢ + last 55 but age unknown (None) → 'wide_mid'
        snap = MarketSnapshot(
            yes_bid_cents=40, yes_ask_cents=60,
            yes_last_cents=55, last_trade_age_s=None,
        )
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.WIDE_MID
        assert p == pytest.approx(0.50)
        assert spread == 20

    def test_only_bid(self):
        snap = MarketSnapshot(yes_bid_cents=30)
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.ONE_SIDE
        assert p == pytest.approx(0.30)
        assert spread is None

    def test_only_ask(self):
        snap = MarketSnapshot(yes_ask_cents=75)
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.ONE_SIDE
        assert p == pytest.approx(0.75)

    def test_only_stale_last(self):
        snap = MarketSnapshot(yes_last_cents=42, last_trade_age_s=None)
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.ONE_SIDE  # stale last bucketed here
        assert p == pytest.approx(0.42)

    def test_none(self):
        snap = MarketSnapshot()
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.NONE
        assert p is None
        assert spread is None

    def test_fresh_last_only(self):
        snap = MarketSnapshot(yes_last_cents=33, last_trade_age_s=30.0)
        p, src, spread = resolve_market_prob(snap)
        assert src == MarketProbSource.LAST
        assert p == pytest.approx(0.33)


# ══════════════════════════════════════════════════════════════════════════════
# log_decision — end-to-end insert
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def conn():
    return init_db(":memory:")


class TestLogDecision:
    def test_roundtrip(self, conn):
        ens = EnsembleSnapshot(
            p_yes=0.62, confidence=0.8, source_count=4,
            sources=["weather", "metar", "open_meteo", "tomorrow"],
            source_estimates={"weather": 0.6, "metar": 0.63},
        )
        mkt = MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52, volume_fp=200)
        rid = log_decision(
            conn,
            ticker="KXHIGHMIA-26APR18-T75",
            decision_type=DecisionType.WEATHER_QUOTER_SHADOW,
            decision_outcome=DecisionOutcome.SHADOW_ONLY,
            ensemble=ens, market=mkt,
            side="yes", price_cents=52, contracts=10,
            cycle_id="c42", notes="unit test",
        )
        assert rid is not None

        row = conn.execute(
            "SELECT ticker, family, decision_type, decision_outcome, "
            "ensemble_p_yes, source_count, yes_bid_cents, yes_ask_cents, "
            "market_prob_yes, market_prob_source, spread_cents, ts_settle_unix "
            "FROM alpha_backtest WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == "KXHIGHMIA-26APR18-T75"
        assert row[1] == "KXHIGHMIA"
        assert row[2] == "weather_quoter_shadow"
        assert row[3] == "shadow_only"
        assert row[4] == pytest.approx(0.62)
        assert row[5] == 4
        assert row[6] == 48
        assert row[7] == 52
        assert row[8] == pytest.approx(0.50)  # mid
        assert row[9] == "mid"
        assert row[10] == 4
        assert row[11] is None  # settlement not yet

    def test_never_raises_on_bad_input(self, conn):
        # Bad ensemble (None p_yes) should not raise; returns None on failure.
        # We construct via a dataclass with p_yes type allowed.
        class _BadEns:
            p_yes = None
            confidence = None
            source_count = None
            sources = None
            source_estimates = None

        rid = log_decision(
            conn, ticker="", decision_type="bogus", decision_outcome="bogus",
            ensemble=_BadEns(), market=MarketSnapshot(),
        )
        # Doesn't matter whether it inserted garbage or returned None — the
        # trading loop must not crash.
        assert rid is None or isinstance(rid, int)

    def test_persists_market_prob_source_tag(self, conn):
        # Wide spread + no last → wide_mid tag preserved in the row
        rid = log_decision(
            conn,
            ticker="KXFED-26MAY-T425",
            decision_type=DecisionType.DIRECTIONAL_SHADOW,
            decision_outcome=DecisionOutcome.DISCARDED,
            ensemble=EnsembleSnapshot(p_yes=0.70, source_count=3),
            market=MarketSnapshot(yes_bid_cents=40, yes_ask_cents=60),
            side="yes", skip_reason="kelly_zero",
        )
        assert rid is not None
        src = conn.execute(
            "SELECT market_prob_source, spread_cents FROM alpha_backtest WHERE id=?",
            (rid,),
        ).fetchone()
        assert src[0] == "wide_mid"
        assert src[1] == 20


# ══════════════════════════════════════════════════════════════════════════════
# fill_settlement — idempotency + won_yes translation
# ══════════════════════════════════════════════════════════════════════════════

class TestFillSettlement:
    def _seed_row(self, conn, ticker, side):
        return log_decision(
            conn, ticker=ticker,
            decision_type=DecisionType.DIRECTIONAL_SHADOW,
            decision_outcome=DecisionOutcome.SHADOW_ONLY,
            ensemble=EnsembleSnapshot(p_yes=0.6, source_count=3),
            market=MarketSnapshot(yes_bid_cents=48, yes_ask_cents=52),
            side=side, price_cents=52, contracts=5,
        )

    def test_yes_side_yes_result_wins(self, conn):
        rid = self._seed_row(conn, "KXFED-26MAY-T425", "yes")
        n = fill_settlement(
            conn, ticker="KXFED-26MAY-T425", side="yes",
            settlement_result="yes", realized_pnl_cents=240,
        )
        assert n == 1
        row = conn.execute(
            "SELECT won_yes, realized_pnl_cents, settlement_result FROM alpha_backtest WHERE id=?",
            (rid,),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == 240
        assert row[2] == "yes"

    def test_yes_side_no_result_loses(self, conn):
        self._seed_row(conn, "KXFED-26MAY-T425", "yes")
        fill_settlement(conn, ticker="KXFED-26MAY-T425", side="yes",
                        settlement_result="no", realized_pnl_cents=-260)
        won = conn.execute(
            "SELECT won_yes FROM alpha_backtest WHERE ticker=?",
            ("KXFED-26MAY-T425",),
        ).fetchone()[0]
        assert won == 0

    def test_no_side_no_result_wins(self, conn):
        self._seed_row(conn, "KXHIGHMIA-26APR18-T75", "no")
        fill_settlement(conn, ticker="KXHIGHMIA-26APR18-T75", side="no",
                        settlement_result="no", realized_pnl_cents=300)
        won = conn.execute(
            "SELECT won_yes FROM alpha_backtest WHERE ticker=?",
            ("KXHIGHMIA-26APR18-T75",),
        ).fetchone()[0]
        assert won == 1  # NO side wins on NO result → won_yes=1 (we won)

    def test_idempotent(self, conn):
        self._seed_row(conn, "KXBTC-26APR30-B35000", "yes")
        n1 = fill_settlement(conn, ticker="KXBTC-26APR30-B35000", side="yes",
                             settlement_result="yes", realized_pnl_cents=100)
        n2 = fill_settlement(conn, ticker="KXBTC-26APR30-B35000", side="yes",
                             settlement_result="yes", realized_pnl_cents=999)
        assert n1 == 1
        assert n2 == 0  # second call is no-op
        pnl = conn.execute(
            "SELECT realized_pnl_cents FROM alpha_backtest WHERE ticker=?",
            ("KXBTC-26APR30-B35000",),
        ).fetchone()[0]
        assert pnl == 100  # first value preserved

    def test_only_updates_matching_side(self, conn):
        self._seed_row(conn, "KXETH-26MAY15-B2500", "yes")
        self._seed_row(conn, "KXETH-26MAY15-B2500", "no")
        n = fill_settlement(conn, ticker="KXETH-26MAY15-B2500", side="yes",
                            settlement_result="yes", realized_pnl_cents=50)
        assert n == 1
        # NO-side row still open
        open_rows = conn.execute(
            "SELECT COUNT(*) FROM alpha_backtest WHERE ticker=? AND ts_settle_unix IS NULL",
            ("KXETH-26MAY15-B2500",),
        ).fetchone()[0]
        assert open_rows == 1
