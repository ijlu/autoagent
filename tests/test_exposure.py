"""Per-family exposure cap accounting.

Covers the case that motivated the cap: Kelly-by-market on KXFED-26MAY,
KXFED-26JUN, KXFED-26JUL each looks independent, but they're a single
correlated rate-path bet. Without the cap the Apr 17 book ran 95% KXFED.

Tested surface:
  - family_from_ticker: KXFED-26MAY → KXFED
  - _position_exposure_cents: market_exposure, fallback, malformed
  - compute_family_exposures: bucketing + zero filter
  - family_headroom_cents: clamp + safe-closed on bad inputs
  - size_trade_against_family_cap: pass, reduce, reject, within-cycle accumulation
"""
from __future__ import annotations

import pytest

from bot.core.exposure import (
    _position_exposure_cents,
    compute_family_exposures,
    family_from_ticker,
    family_headroom_cents,
    size_trade_against_family_cap,
)


class TestFamilyFromTicker:
    def test_extracts_prefix_before_first_hyphen(self):
        assert family_from_ticker("KXFED-26MAY-T425") == "KXFED"
        assert family_from_ticker("KXHIGHMIA-26APR18-T75") == "KXHIGHMIA"

    def test_no_hyphen_is_full_ticker(self):
        assert family_from_ticker("KXWEIRD") == "KXWEIRD"

    def test_empty_returns_empty(self):
        assert family_from_ticker("") == ""


class TestPositionExposure:
    def test_uses_market_exposure_when_present(self):
        pos = {"market_exposure": 750, "position": 30, "average_price_paid": 25}
        # market_exposure wins even if pos*avg would compute differently
        assert _position_exposure_cents(pos) == 750

    def test_falls_back_to_position_times_avg_price(self):
        pos = {"position": 20, "average_price_paid": 45}
        assert _position_exposure_cents(pos) == 900

    def test_handles_fp_position_field(self):
        pos = {"position_fp": 15, "average_price_paid": 30}
        assert _position_exposure_cents(pos) == 450

    def test_zero_position_is_zero(self):
        pos = {"position": 0, "average_price_paid": 50}
        assert _position_exposure_cents(pos) == 0

    def test_malformed_fields_coerce_to_zero(self):
        assert _position_exposure_cents({"position": "oops", "average_price_paid": 50}) == 0
        assert _position_exposure_cents({"position": 10, "average_price_paid": None}) == 0


class TestComputeFamilyExposures:
    def test_buckets_by_family(self):
        positions = [
            {"ticker": "KXFED-26MAY-T425", "position": 10, "average_price_paid": 50},
            {"ticker": "KXFED-26JUN-T400", "position": 20, "average_price_paid": 40},
            {"ticker": "KXHIGHMIA-26APR18-T75", "position": 5, "average_price_paid": 60},
        ]
        exp = compute_family_exposures(positions)
        assert exp["KXFED"] == 500 + 800
        assert exp["KXHIGHMIA"] == 300

    def test_skips_zero_positions(self):
        positions = [
            {"ticker": "KXFED-26MAY-T425", "position": 0, "average_price_paid": 50},
            {"ticker": "KXFED-26JUN-T400", "position": 10, "average_price_paid": 40},
        ]
        exp = compute_family_exposures(positions)
        assert exp == {"KXFED": 400}

    def test_empty_input(self):
        assert compute_family_exposures([]) == {}
        assert compute_family_exposures(None) == {}  # type: ignore[arg-type]

    def test_tickers_without_family_are_ignored(self):
        positions = [
            {"ticker": "", "position": 10, "average_price_paid": 50},
        ]
        assert compute_family_exposures(positions) == {}


class TestFamilyHeadroom:
    def test_headroom_is_cap_minus_current(self):
        # 25% of $1000 = $250 = 25_000 cents
        h = family_headroom_cents(
            family="KXFED", current_family_exposure_cents=10_000,
            total_equity_cents=100_000, max_family_ratio=0.25,
        )
        assert h == 15_000

    def test_headroom_zero_when_over_cap(self):
        h = family_headroom_cents(
            family="KXFED", current_family_exposure_cents=30_000,
            total_equity_cents=100_000, max_family_ratio=0.25,
        )
        assert h == 0

    def test_safe_closed_on_bad_ratio_or_equity(self):
        assert family_headroom_cents(
            family="KXFED", current_family_exposure_cents=0,
            total_equity_cents=100_000, max_family_ratio=0,
        ) == 0
        assert family_headroom_cents(
            family="KXFED", current_family_exposure_cents=0,
            total_equity_cents=0, max_family_ratio=0.25,
        ) == 0


class TestSizeTradeAgainstFamilyCap:
    def _args(self, **kw):
        base = dict(
            ticker="KXFED-26MAY-T425",
            proposed_contracts=10,
            price_cents=50,
            family_exposures={},
            total_equity_cents=100_000,
            max_family_ratio=0.25,
        )
        base.update(kw)
        return base

    def test_trade_fits_untouched(self):
        n, skip = size_trade_against_family_cap(**self._args())
        assert n == 10
        assert skip is None

    def test_reduces_when_partial_headroom(self):
        # cap = $250, existing KXFED = $240 → headroom $10 = 1000 cents
        # proposed 10 @ 50c = $5 = 500 cents — fits inside headroom, untouched
        n, skip = size_trade_against_family_cap(
            **self._args(family_exposures={"KXFED": 24_000}, proposed_contracts=10)
        )
        assert n == 10
        assert skip is None

        # Now push past headroom: 40 @ 50c = $20 = 2000c > 1000c headroom
        n, skip = size_trade_against_family_cap(
            **self._args(family_exposures={"KXFED": 24_000}, proposed_contracts=40)
        )
        # 1000 / 50 = 20 contracts fit
        assert n == 20
        assert skip is None

    def test_rejects_when_cap_exhausted(self):
        n, skip = size_trade_against_family_cap(
            **self._args(family_exposures={"KXFED": 30_000})
        )
        assert n == 0
        assert skip == "family_cap_exhausted:KXFED"

    def test_different_families_do_not_cross_contaminate(self):
        # KXFED is already full, but we're trading KXHIGHMIA
        n, skip = size_trade_against_family_cap(
            **self._args(
                ticker="KXHIGHMIA-26APR18-T75",
                family_exposures={"KXFED": 30_000},
            )
        )
        assert n == 10
        assert skip is None

    def test_within_cycle_accumulation_is_on_caller(self):
        """Caller must mutate family_exposures after a successful accept."""
        exposures = {"KXFED": 0}
        # First trade: 100 @ 50c = 5000c allowed (headroom 25000c)
        n1, skip1 = size_trade_against_family_cap(
            **self._args(family_exposures=exposures, proposed_contracts=100)
        )
        assert n1 == 100 and skip1 is None
        exposures["KXFED"] = exposures.get("KXFED", 0) + n1 * 50  # caller updates

        # Second trade on same family: 500 @ 50c = 25000c, but only 20000c left
        n2, skip2 = size_trade_against_family_cap(
            **self._args(family_exposures=exposures, proposed_contracts=500)
        )
        assert n2 == 400  # 20000 / 50
        assert skip2 is None

    def test_invalid_input_returns_zero(self):
        n, skip = size_trade_against_family_cap(**self._args(proposed_contracts=0))
        assert n == 0 and skip == "invalid_input"
        n, skip = size_trade_against_family_cap(**self._args(price_cents=0))
        assert n == 0 and skip == "invalid_input"

    def test_empty_family_returns_proposed(self):
        """A ticker with no family prefix (edge case) bypasses the cap."""
        n, skip = size_trade_against_family_cap(**self._args(ticker=""))
        assert n == 10
        assert skip is None
