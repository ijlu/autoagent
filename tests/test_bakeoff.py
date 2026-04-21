"""Tests for bot/learning/bakeoff.py — T2 strategy comparison.

Scenarios:
  - Per-strategy rollup produces Brier beat, implied vs realized P&L, and
    a sensible realization ratio when rows are present.
  - Clean-market-slice filter excludes wide_mid / one_side rows.
  - Paired-ticker comparison picks the right winner when both strategies
    fire on the same ticker.
  - Empty inputs / no-overlap return stable empty results instead of errors.
  - Report formatter produces markdown with the expected sections.
"""
from __future__ import annotations

import time

import pytest

from bot.db import init_db
from bot.learning import alpha_log
from bot.learning.bakeoff import (
    compute_bakeoff,
    compute_paired_tickers,
    format_report,
    render_bakeoff_report,
)


def _log(conn, *, ticker, strategy, side, price, contracts, ens_p, market,
         settle_result, ts_dec=None):
    """Insert a settled alpha_backtest row inline."""
    ts_dec = ts_dec or time.time()
    rid = alpha_log.log_decision(
        conn,
        ticker=ticker,
        decision_type=strategy,
        decision_outcome=alpha_log.DecisionOutcome.SHADOW_ONLY,
        ensemble=alpha_log.EnsembleSnapshot(p_yes=ens_p),
        market=market,
        side=side,
        price_cents=price,
        contracts=contracts,
        ts_decision_unix=ts_dec,
    )
    alpha_log.fill_settlement_for_ticker(
        conn, ticker=ticker,
        settlement_result=settle_result,
        ts_settle_unix=ts_dec + 3600,
    )
    return rid


def _tight_market(yes_bid=48, yes_ask=52):
    return alpha_log.MarketSnapshot(
        yes_bid_cents=yes_bid, yes_ask_cents=yes_ask, volume_fp=1000
    )


def _wide_market(yes_bid=30, yes_ask=70):
    # Spread > MAX_TIGHT_SPREAD_CENTS → resolves to wide_mid.
    return alpha_log.MarketSnapshot(
        yes_bid_cents=yes_bid, yes_ask_cents=yes_ask, volume_fp=1000
    )


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def test_compute_bakeoff_basic_rollup(conn):
    """Two MM rows (one winner, one loser) produce a correct rollup."""
    # MM quote: bought YES at 48¢ x 10, market settled YES → +520¢
    _log(conn, ticker="KXHIGHNY-26APR20-B7476", strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.60,
         market=_tight_market(), settle_result="yes")
    # MM quote: bought YES at 48¢ x 10, market settled NO → -480¢
    _log(conn, ticker="KXHIGHNY-26APR20-B7678", strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.60,
         market=_tight_market(), settle_result="no")

    out = compute_bakeoff(conn)
    assert len(out) == 1
    r = out[0]
    assert r.strategy == "mm_quote"
    assert r.family == "KXHIGHNY"
    assert r.n_settled == 2
    # Brier ensemble: ens_p=0.60, outcomes 1 and 0 → (0.4^2 + 0.6^2) / 2 = 0.26
    assert r.brier_ensemble == pytest.approx((0.4**2 + 0.6**2) / 2, abs=1e-6)
    # Brier market: market_prob = 0.50, outcomes 1 and 0 → 0.25 both → 0.25
    assert r.brier_market == pytest.approx(0.25, abs=1e-6)
    # Ensemble was worse than market here (thesis-consistent — ensemble=0.60 is further from both outcomes on average than 0.50).
    assert r.brier_beat == pytest.approx(0.25 - r.brier_ensemble, abs=1e-6)
    # Realized: +520 + -480 = 40 cents.
    assert r.realized_pnl_cents_sum == 40
    # Implied P&L per row: contracts * (p_win*100 - price). Both rows side=yes,
    # market_prob=0.50 → p_win=0.5, price=48 → 10*(50-48)=20 per row × 2 = 40.
    assert r.implied_pnl_cents_sum == 40
    assert r.realization_ratio == pytest.approx(1.0)


def test_clean_slice_filter_excludes_wide_mid(conn):
    """Wide-spread rows resolve to 'wide_mid' and are excluded by default."""
    _log(conn, ticker="KXHIGHNY-26APR20-T75", strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.60,
         market=_wide_market(), settle_result="yes")

    clean = compute_bakeoff(conn, clean_market_slice=True)
    assert clean == []  # filtered out

    wide = compute_bakeoff(conn, clean_market_slice=False)
    assert len(wide) == 1
    assert wide[0].n_settled == 1


def test_paired_tickers_picks_winner(conn):
    """Same ticker, both strategies fire → paired cell shows correct winner."""
    T = "KXHIGHNY-26APR20-B7476"
    # MM won big: side=yes, price=40, contracts=10, settled yes → +600
    _log(conn, ticker=T, strategy="mm_quote",
         side="yes", price=40, contracts=10, ens_p=0.70,
         market=_tight_market(), settle_result="yes")
    # Directional broke even: side=yes, price=50, contracts=10, settled yes → +500
    _log(conn, ticker=T, strategy="directional_shadow",
         side="yes", price=50, contracts=10, ens_p=0.70,
         market=_tight_market(), settle_result="yes")

    paired = compute_paired_tickers(conn)
    assert len(paired) == 1
    p = paired[0]
    assert p.ticker == T
    assert p.mm_realized_cents == 600
    assert p.directional_realized_cents == 500
    assert p.winner == "mm"


def test_paired_requires_both_legs(conn):
    """If only one strategy fired on a ticker, it does not pair."""
    _log(conn, ticker="KXHIGHNY-26APR20-B7476", strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.60,
         market=_tight_market(), settle_result="yes")
    assert compute_paired_tickers(conn) == []


def test_realization_ratio_none_when_implied_zero(conn):
    """If implied_pnl_sum == 0 (no rows had market_prob) the ratio is None,
    not a division-by-zero crash."""
    # price == market_prob*100 → implied pnl per row is 0, and sum stays 0.
    # side=yes, market_prob=0.50, price=50 → 10 * (50 - 50) = 0.
    _log(conn, ticker="KXHIGHNY-26APR20-B7476", strategy="mm_quote",
         side="yes", price=50, contracts=10, ens_p=0.50,
         market=_tight_market(), settle_result="yes")
    out = compute_bakeoff(conn)
    assert len(out) == 1
    assert out[0].implied_pnl_cents_sum == 0
    assert out[0].realization_ratio is None


def test_min_n_drops_noisy_rollups(conn):
    """min_n filter suppresses small-sample rows."""
    _log(conn, ticker="KXHIGHNY-26APR20-B7476", strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.60,
         market=_tight_market(), settle_result="yes")
    # min_n=1 keeps it
    assert len(compute_bakeoff(conn, min_n=1)) == 1
    # min_n=5 drops it
    assert compute_bakeoff(conn, min_n=5) == []


def test_family_filter(conn):
    """Passing family=... restricts to just that family."""
    _log(conn, ticker="KXHIGHNY-26APR20-B7476", strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.60,
         market=_tight_market(), settle_result="yes")
    _log(conn, ticker="KXHIGHMIA-26APR20-B8588", strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.60,
         market=_tight_market(), settle_result="yes")
    ny = compute_bakeoff(conn, family="KXHIGHNY")
    assert len(ny) == 1
    assert ny[0].family == "KXHIGHNY"


def test_empty_conn_renders_empty_report(conn):
    """No rows → report still renders, no crash."""
    out = render_bakeoff_report(conn)
    assert "No settled rows" in out
    assert "# Strategy bakeoff" in out


def test_format_report_contains_both_sections(conn):
    T = "KXHIGHNY-26APR20-B7476"
    _log(conn, ticker=T, strategy="mm_quote",
         side="yes", price=40, contracts=10, ens_p=0.70,
         market=_tight_market(), settle_result="yes")
    _log(conn, ticker=T, strategy="directional_shadow",
         side="yes", price=50, contracts=10, ens_p=0.70,
         market=_tight_market(), settle_result="yes")
    out = render_bakeoff_report(conn)
    assert "Per-strategy × family rollup" in out
    assert "Paired-ticker head-to-head" in out
    assert "mm_quote" in out
    assert "directional_shadow" in out
    # Winner row rendered
    assert "| mm |" in out


def test_only_clean_slice_rows_pair(conn):
    """Paired comparison also respects the clean-market-slice filter."""
    T = "KXHIGHNY-26APR20-B7476"
    _log(conn, ticker=T, strategy="mm_quote",
         side="yes", price=48, contracts=10, ens_p=0.70,
         market=_wide_market(), settle_result="yes")
    _log(conn, ticker=T, strategy="directional_shadow",
         side="yes", price=48, contracts=10, ens_p=0.70,
         market=_wide_market(), settle_result="yes")
    # Clean: both filtered, no pair
    assert compute_paired_tickers(conn, clean_market_slice=True) == []
    # Non-clean: pair appears
    assert len(compute_paired_tickers(conn, clean_market_slice=False)) == 1
