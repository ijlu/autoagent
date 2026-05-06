"""Tests for tools/weather_markout_analysis.py.

Keep these narrow: parsing, v1 reconstruction, direction audit, markout
math, bootstrap determinism, and the same-ticker self-join. The tool is
read-only, so no DB write fixtures needed — we build in-memory DBs from
scratch for each test.
"""

from __future__ import annotations

import math
import random
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.weather_markout_analysis import (  # noqa: E402
    MarketSpec,
    MarkoutSample,
    _signed_markout,
    aggregate,
    audit_threshold_directions,
    bootstrap_mean_ci,
    compute_markouts,
    parse_ticker,
    v1_fair_value_cents,
)


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------


class TestParseTicker:
    def test_threshold_default_is_above_true(self):
        spec = parse_ticker("KXHIGHNY-26APR24-T75")
        assert spec is not None
        assert spec.is_bracket is False
        assert spec.is_above is True
        assert spec.threshold == 75.0

    def test_bracket_has_2deg_cap(self):
        spec = parse_ticker("KXHIGHMIA-26APR24-B82")
        assert spec is not None
        assert spec.is_bracket is True
        assert spec.bracket_floor == 82.0
        assert spec.bracket_cap == 84.0

    def test_ticker_without_trailing_T_or_B_returns_none(self):
        # parser is shape-based; upstream caller filters on weather table.
        assert parse_ticker("KXSOMETHING-26MAY") is None

    def test_negative_threshold(self):
        spec = parse_ticker("KXHIGHXYZ-26JAN01-T-10")
        assert spec is not None
        assert spec.threshold == -10.0


# ---------------------------------------------------------------------------
# v1 FV reconstruction
# ---------------------------------------------------------------------------


class TestV1FairValue:
    def test_running_high_at_or_above_threshold_is_confident(self):
        spec = MarketSpec(
            is_bracket=False, is_above=True, threshold=75.0,
            bracket_floor=None, bracket_cap=None,
        )
        # margin=3.0 → triggers the >=3 branch → 0.98 → 98¢
        fv = v1_fair_value_cents(spec, running_high_f=78.0, forecast_high_f=76.0, hours_left=4.0)
        assert fv == 98

    def test_running_high_far_above_threshold_clamps_at_98(self):
        spec = MarketSpec(
            is_bracket=False, is_above=True, threshold=75.0,
            bracket_floor=None, bracket_cap=None,
        )
        fv = v1_fair_value_cents(spec, running_high_f=90.0, forecast_high_f=90.0, hours_left=2.0)
        assert fv == 98

    def test_below_threshold_is_above_false_flips(self):
        # For the same inputs, is_above=True vs False should sum to ~100
        kwargs = dict(running_high_f=70.0, forecast_high_f=74.0, hours_left=6.0)
        above = v1_fair_value_cents(
            MarketSpec(False, True, 75.0, None, None), **kwargs,
        )
        below = v1_fair_value_cents(
            MarketSpec(False, False, 75.0, None, None), **kwargs,
        )
        assert above is not None and below is not None
        # Rounding can lose a cent; tolerance of 1.
        assert abs((above + below) - 100) <= 1

    def test_bracket_running_high_above_cap_snaps_to_two_cents(self):
        spec = MarketSpec(
            is_bracket=True, is_above=None, threshold=None,
            bracket_floor=80.0, bracket_cap=82.0,
        )
        fv = v1_fair_value_cents(spec, running_high_f=85.0, forecast_high_f=85.0, hours_left=2.0)
        assert fv == 2

    def test_bracket_in_window_probability_is_above_2_below_98(self):
        spec = MarketSpec(
            is_bracket=True, is_above=None, threshold=None,
            bracket_floor=80.0, bracket_cap=82.0,
        )
        fv = v1_fair_value_cents(spec, running_high_f=79.0, forecast_high_f=81.0, hours_left=4.0)
        assert 2 <= fv <= 98


# ---------------------------------------------------------------------------
# Direction audit (needs a real SQLite DB)
# ---------------------------------------------------------------------------


def _make_shadow_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE weather_mm_shadow (
                id INTEGER PRIMARY KEY,
                ts_iso TEXT, ts_unix INTEGER, ticker TEXT, series TEXT,
                running_high_f REAL, forecast_high_f REAL, hours_left REAL,
                fair_value_cents INTEGER,
                market_yes_bid INTEGER, market_yes_ask INTEGER
            )"""
    )
    conn.executemany(
        "INSERT INTO weather_mm_shadow (ts_iso, ts_unix, ticker, series, "
        "running_high_f, forecast_high_f, hours_left, fair_value_cents, "
        "market_yes_bid, market_yes_ask) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


class TestAuditThresholdDirections:
    def test_is_above_true_ticker_not_flipped(self):
        # Synthesize pre-flip rows where stored FV matches is_above=True
        spec = MarketSpec(False, True, 75.0, None, None)
        pre = "2026-04-20T00:00:00+00:00"
        rows = []
        for rh, fh, hl in [(70.0, 74.0, 6.0), (72.0, 76.0, 3.0), (73.0, 75.0, 1.5)]:
            fv = v1_fair_value_cents(spec, rh, fh, hl)
            rows.append((pre, 0, "KXHIGHNY-26APR24-T75", "KXHIGHNY", rh, fh, hl, fv, None, None))
        conn = _make_shadow_db(rows)
        overrides = audit_threshold_directions(conn, "2026-04-24T00:00:00+00:00")
        assert overrides["KXHIGHNY-26APR24-T75"] is True

    def test_is_above_false_ticker_flipped(self):
        spec_false = MarketSpec(False, False, 75.0, None, None)
        pre = "2026-04-20T00:00:00+00:00"
        rows = []
        for rh, fh, hl in [(70.0, 74.0, 6.0), (72.0, 76.0, 3.0), (68.0, 72.0, 10.0)]:
            fv = v1_fair_value_cents(spec_false, rh, fh, hl)
            rows.append((pre, 0, "KXHIGHNY-26APR24-T75", "KXHIGHNY", rh, fh, hl, fv, None, None))
        conn = _make_shadow_db(rows)
        overrides = audit_threshold_directions(conn, "2026-04-24T00:00:00+00:00")
        assert overrides["KXHIGHNY-26APR24-T75"] is False


# ---------------------------------------------------------------------------
# Markout math
# ---------------------------------------------------------------------------


class TestSignedMarkout:
    def test_fv_above_mid_and_mid_rises_is_positive(self):
        # fv=70, mid_t=50, mid_t+=55: market moved +5 toward us → +5
        assert _signed_markout(70, 50.0, 55.0) == pytest.approx(5.0)

    def test_fv_below_mid_and_mid_falls_is_positive(self):
        # fv=30, mid_t=50, mid_t+=45: market moved -5 toward us → +5
        assert _signed_markout(30, 50.0, 45.0) == pytest.approx(5.0)

    def test_fv_equal_mid_is_zero(self):
        assert _signed_markout(50, 50.0, 55.0) == 0.0

    def test_adverse_move_is_negative(self):
        # fv=70 (we think YES), but mid falls → adverse
        assert _signed_markout(70, 50.0, 40.0) == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# compute_markouts self-join
# ---------------------------------------------------------------------------


class TestComputeMarkouts:
    def _seed_pair(self, ts_a, ts_b, bid_a, ask_a, bid_b, ask_b, fv=50, rh=70.0, fh=76.0, hl=6.0):
        ticker = "KXHIGHNY-26APR24-T75"
        return [
            (_iso(ts_a), ts_a, ticker, "KXHIGHNY", rh, fh, hl, fv, bid_a, ask_a),
            (_iso(ts_b), ts_b, ticker, "KXHIGHNY", rh, fh, hl, fv, bid_b, ask_b),
        ]

    def test_pair_within_window_emits_sample(self):
        # Post-flip so we skip the v1-reconstruction-vs-stored guard.
        rows = self._seed_pair(1000, 1300, 40, 60, 50, 70)
        conn = _make_shadow_db(rows)
        samples = compute_markouts(
            conn, since_iso="1970-01-01T00:00:00+00:00",
            delta_seconds=300, max_spread_c=30,
            flip_iso="1970-01-01T00:00:00+00:00", direction_overrides={},
        )
        assert len(samples) == 1
        assert samples[0].mid_t == 50.0 and samples[0].mid_t_plus == 60.0

    def test_partner_beyond_2delta_drops_sample(self):
        rows = self._seed_pair(1000, 2000, 40, 60, 50, 70)  # gap=1000s > 2*300
        conn = _make_shadow_db(rows)
        samples = compute_markouts(
            conn, "1970-01-01T00:00:00+00:00",
            delta_seconds=300, max_spread_c=30,
            flip_iso="1970-01-01T00:00:00+00:00", direction_overrides={},
        )
        assert samples == []

    def test_wide_spread_row_filtered(self):
        rows = self._seed_pair(1000, 1300, 10, 90, 50, 70)  # spread_a=80 > 30
        conn = _make_shadow_db(rows)
        samples = compute_markouts(
            conn, "1970-01-01T00:00:00+00:00",
            delta_seconds=300, max_spread_c=30,
            flip_iso="1970-01-01T00:00:00+00:00", direction_overrides={},
        )
        assert samples == []

    def test_v2_populated_for_post_flip_rows(self):
        rows = self._seed_pair(1000, 1300, 40, 60, 50, 70, fv=55)
        conn = _make_shadow_db(rows)
        samples = compute_markouts(
            conn, "1970-01-01T00:00:00+00:00",
            delta_seconds=300, max_spread_c=30,
            flip_iso="1970-01-01T00:00:00+00:00",  # flip before everything
            direction_overrides={},
        )
        assert len(samples) == 1
        assert samples[0].v2_fv == 55
        # v1 and v2 may differ — that's the whole point.
        assert samples[0].v1_markout is not None
        assert samples[0].v2_markout is not None


def _iso(ts_unix):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Bootstrap + aggregate
# ---------------------------------------------------------------------------


class TestBootstrapMeanCI:
    def test_empty_returns_nan(self):
        m, lo, hi = bootstrap_mean_ci([])
        assert math.isnan(m) and math.isnan(lo) and math.isnan(hi)

    def test_deterministic_under_fixed_seed(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0] * 20
        a = bootstrap_mean_ci(vals, n_boot=500, rng=random.Random(42))
        b = bootstrap_mean_ci(vals, n_boot=500, rng=random.Random(42))
        assert a == b

    def test_mean_is_sample_mean(self):
        vals = [1.0, 3.0, 5.0]
        m, _, _ = bootstrap_mean_ci(vals, n_boot=100)
        assert m == pytest.approx(3.0)

    def test_ci_brackets_mean(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        m, lo, hi = bootstrap_mean_ci(vals, n_boot=500)
        assert lo <= m <= hi


class TestAggregate:
    def test_groups_by_series_and_skips_none(self):
        s1 = MarkoutSample("t1", "A", 0, 1.0, 5, 50, 55, 60, 58,  5.0, 3.0)
        s2 = MarkoutSample("t2", "A", 0, 1.0, 5, 50, 45, 40, None, 5.0, None)
        s3 = MarkoutSample("t3", "B", 0, 1.0, 5, 50, 55, 60, 58,  5.0, 3.0)
        out = aggregate([s1, s2, s3], "series")
        assert out["A"]["v1"] == [5.0, 5.0]
        assert out["A"]["v2"] == [3.0]
        assert out["B"]["v1"] == [5.0] and out["B"]["v2"] == [3.0]
