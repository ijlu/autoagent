"""Unit tests for the unified calibration module (bot.learning.calibration).

Covers:
  - Math helpers: _sigmoid, _logit round-trip, _prob_bucket_label.
  - Platt fit (_fit_platt) on synthetic overconfident/underconfident data:
    verifies recovered A/B move Brier in the right direction.
  - Isotonic PAV monotonicity.
  - fit_calibration end-to-end: identity under MIN_GLOBAL_SAMPLES; per-family
    override fits once that family has ≥ MIN_FAMILY_SAMPLES; bucket_stats
    drops buckets below MIN_BUCKET_SAMPLES.
  - apply_calibration:
      * None / NaN / missing curve → pass-through (with clamp)
      * identity curve → pass-through
      * Platt curve → shifted toward family-specific or global A/B
      * legacy flat {bucket: float} dict → safe no-op (regression test for the
        shape-mismatch bug that would crash the trading loop once the
        calibration table had rows)
  - Persistence: fit_and_persist + load_curve round-trip through kv_cache,
    including TTL honored.
"""

from __future__ import annotations

import math
import random
import sqlite3
import time

import pytest

from bot.learning import calibration as cal


# ══════════════════════════════════════════════════════════════════════════════
# Math helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestMathHelpers:
    @pytest.mark.parametrize("p", [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99])
    def test_sigmoid_logit_roundtrip(self, p):
        assert cal._sigmoid(cal._logit(p)) == pytest.approx(p, abs=1e-6)

    def test_sigmoid_extreme_inputs_stable(self):
        # Very large magnitudes must not overflow
        assert cal._sigmoid(1000) == pytest.approx(1.0)
        assert cal._sigmoid(-1000) == pytest.approx(0.0)

    def test_logit_clamps_boundary(self):
        # Must not return -inf / +inf for p=0 or p=1
        assert math.isfinite(cal._logit(0.0))
        assert math.isfinite(cal._logit(1.0))

    @pytest.mark.parametrize("p,expected", [
        (0.05, "0.0-0.1"),
        (0.15, "0.1-0.2"),
        (0.5, "0.5-0.6"),
        (0.999, "0.9-1.0"),
        (0.0, "0.0-0.1"),
        (1.0, "0.9-1.0"),
    ])
    def test_prob_bucket_label(self, p, expected):
        assert cal._prob_bucket_label(p) == expected

    @pytest.mark.parametrize("ticker,expected", [
        ("KXHIGHMIA-26APR16", "KXHIGHMIA"),
        ("KXFED-26MAY", "KXFED"),
        ("KXBTC", "KXBTC"),
        ("", None),
        (None, None),
    ])
    def test_family_key(self, ticker, expected):
        assert cal.family_key(ticker) == expected


# ══════════════════════════════════════════════════════════════════════════════
# Platt fit on synthetic data
# ══════════════════════════════════════════════════════════════════════════════

def _synth_biased_dataset(n_per_side: int, true_rate_at_0_7: float, seed: int = 42):
    """Generate (raw_p, y) pairs where raw_p=0.7 predictions actually settle at
    true_rate_at_0_7. Mixes a handful of calibration-point clusters so Platt has
    signal at multiple logit slices."""
    rng = random.Random(seed)
    data = []
    clusters = [
        (0.3, 0.2),   # we say 30%, actually 20%
        (0.5, 0.4),   # we say 50%, actually 40%
        (0.7, true_rate_at_0_7),
        (0.85, 0.70),
    ]
    for raw_p, true_rate in clusters:
        for _ in range(n_per_side):
            y = 1 if rng.random() < true_rate else 0
            data.append((raw_p, y))
    return data


class TestPlattFit:
    def test_recovers_slope_on_overconfident_data(self):
        # We systematically over-predict. Platt should pull logits in (A<1).
        data = _synth_biased_dataset(n_per_side=200, true_rate_at_0_7=0.50, seed=1)
        xs = [cal._logit(p) for p, _ in data]
        ys = [y for _, y in data]
        A, B, iters = cal._fit_platt(xs, ys)
        assert 0 < A < 1.0, f"expected slope compression, got A={A}"
        assert iters < cal.NEWTON_MAX_ITER

    def test_recovers_slope_on_underconfident_data(self):
        # We systematically under-predict: predictions span [0.3, 0.85] but
        # actual rates span [0.1, 0.97] (stretched further from 0.5). A clean
        # Platt fit should respond with A>1 (sharper logits).
        rng = random.Random(2)
        data = []
        for raw_p, true_rate in [(0.3, 0.10), (0.5, 0.50), (0.7, 0.90), (0.85, 0.97)]:
            for _ in range(300):
                y = 1 if rng.random() < true_rate else 0
                data.append((raw_p, y))
        xs = [cal._logit(p) for p, _ in data]
        ys = [y for _, y in data]
        A, _, _ = cal._fit_platt(xs, ys)
        assert A > 1.2, f"expected slope stretch (A>1.2), got A={A}"

    def test_lowers_brier_on_biased_data(self):
        """The whole point: applying the fitted Platt curve should reduce Brier."""
        data = _synth_biased_dataset(n_per_side=300, true_rate_at_0_7=0.50, seed=3)
        ps = [p for p, _ in data]
        ys = [y for _, y in data]
        xs = [cal._logit(p) for p in ps]
        A, B, _ = cal._fit_platt(xs, ys)
        brier_before = cal._brier(ps, ys)
        calibrated = [cal._sigmoid(A * x + B) for x in xs]
        brier_after = cal._brier(calibrated, ys)
        assert brier_after < brier_before - 0.005, (
            f"Platt did not lower Brier enough: {brier_before:.4f} -> {brier_after:.4f}"
        )

    def test_degenerate_all_same_label_survives(self):
        """All-positive or all-negative data shouldn't crash — label smoothing
        prevents the logit blowup, L2 keeps Hessian PSD."""
        xs = [cal._logit(p) for p in [0.3, 0.5, 0.7] * 20]
        ys = [1] * 60
        A, B, _ = cal._fit_platt(xs, ys)
        assert math.isfinite(A) and math.isfinite(B)

    def test_bounds_respected_on_bimodal_overconfident_data(self):
        """Regression for the step-function pathology (CALIBRATION_INVESTIGATION
        2026-05-04): on bimodal raw probabilities clustered near 0 and 1 with
        actual rates that don't match the extremes, the unconstrained MLE chases
        A → millions. Bounded Newton must keep A in [PLATT_A_MIN, PLATT_A_MAX]
        and B in [PLATT_B_MIN, PLATT_B_MAX]."""
        rng = random.Random(2026)
        data = []
        # cluster near 0 — raw says ~5%, reality is ~14%
        for _ in range(2000):
            raw_p = 0.02 + rng.random() * 0.06
            y = 1 if rng.random() < 0.14 else 0
            data.append((raw_p, y))
        # cluster near 1 — raw says ~99%, reality is ~59%
        for _ in range(2000):
            raw_p = 0.95 + rng.random() * 0.04
            y = 1 if rng.random() < 0.59 else 0
            data.append((raw_p, y))
        xs = [cal._logit(p) for p, _ in data]
        ys = [y for _, y in data]
        A, B, _ = cal._fit_platt(xs, ys)
        assert cal.PLATT_A_MIN <= A <= cal.PLATT_A_MAX, f"A={A} out of bounds"
        assert cal.PLATT_B_MIN <= B <= cal.PLATT_B_MAX, f"B={B} out of bounds"

    def test_brier_improves_on_bimodal_overconfident_data(self):
        """Bounded Newton on bimodal overconfident data must produce a fit that
        REDUCES Brier vs raw — that's the whole point of running the calibrator
        on the live weather pipeline."""
        rng = random.Random(2027)
        data = []
        for _ in range(2000):
            raw_p = 0.02 + rng.random() * 0.06
            y = 1 if rng.random() < 0.14 else 0
            data.append((raw_p, y))
        for _ in range(2000):
            raw_p = 0.95 + rng.random() * 0.04
            y = 1 if rng.random() < 0.59 else 0
            data.append((raw_p, y))
        ps = [p for p, _ in data]
        ys = [y for _, y in data]
        xs = [cal._logit(p) for p in ps]
        A, B, _ = cal._fit_platt(xs, ys)
        brier_before = cal._brier(ps, ys)
        calibrated = [cal._sigmoid(A * x + B) for x in xs]
        brier_after = cal._brier(calibrated, ys)
        assert brier_after < brier_before - 0.01, (
            f"bounded Platt did not improve Brier on bimodal data: "
            f"{brier_before:.4f} -> {brier_after:.4f}"
        )

    def test_no_step_function_on_separable_data(self):
        """Pin: even on near-perfectly-separable data (the regime where
        unconstrained MLE wants A → ∞), bounded Newton must NOT collapse to a
        step function. A must stay ≤ PLATT_A_MAX."""
        rng = random.Random(2028)
        data = []
        for _ in range(500):
            raw_p = 0.02 + rng.random() * 0.06
            y = 1 if rng.random() < 0.05 else 0  # nearly always 0
            data.append((raw_p, y))
        for _ in range(500):
            raw_p = 0.95 + rng.random() * 0.04
            y = 1 if rng.random() < 0.95 else 0  # nearly always 1
            data.append((raw_p, y))
        xs = [cal._logit(p) for p, _ in data]
        ys = [y for _, y in data]
        A, B, _ = cal._fit_platt(xs, ys)
        assert A <= cal.PLATT_A_MAX, f"A={A} exceeds PLATT_A_MAX"
        assert math.isfinite(A) and math.isfinite(B)


# ══════════════════════════════════════════════════════════════════════════════
# Isotonic PAV
# ══════════════════════════════════════════════════════════════════════════════

class TestIsotonic:
    def test_monotone_non_decreasing(self):
        random.seed(7)
        xs = [random.random() for _ in range(200)]
        ys = [1 if random.random() < x else 0 for x in xs]  # calibrated Bernoulli
        segments = cal._fit_isotonic(xs, ys)
        y_prev = -1.0
        for _, y in segments:
            assert y >= y_prev - 1e-12
            y_prev = y

    def test_empty_returns_empty(self):
        assert cal._fit_isotonic([], []) == []

    def test_perfectly_calibrated_input_produces_increasing_steps(self):
        xs = [0.1] * 10 + [0.3] * 10 + [0.6] * 10 + [0.9] * 10
        ys = [0] * 9 + [1] + [0] * 7 + [1] * 3 + [0] * 4 + [1] * 6 + [0] * 1 + [1] * 9
        segs = cal._fit_isotonic(xs, ys)
        assert all(segs[i][1] <= segs[i + 1][1] + 1e-12 for i in range(len(segs) - 1))


# ══════════════════════════════════════════════════════════════════════════════
# fit_calibration + DB integration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def conn(tmp_path):
    from bot.db import init_db
    c = init_db(str(tmp_path / "test_cal.db"))
    yield c
    c.close()


def _insert_cal_rows(conn, rows):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.executemany(
        "INSERT INTO calibration(recorded_at, ticker, estimated_prob, actual_outcome, "
        "source_desc, n_sources, bucket) VALUES(?,?,?,?,?,?,?)",
        [(ts, t, p, y, "test", 1, cal._prob_bucket_label(p)) for t, p, y in rows],
    )
    conn.commit()


class TestFitCalibration:
    def test_identity_under_min_samples(self, conn):
        _insert_cal_rows(conn, [("KXHIGHMIA", 0.6, 1)] * 5)
        curve = cal.fit_calibration(conn)
        assert curve["method"] == "identity"
        assert curve["A"] == 1.0
        assert curve["B"] == 0.0
        assert curve["n_samples"] == 5
        assert curve["families"] == {}

    def test_platt_fits_once_threshold_crossed(self, conn):
        # Seed with overconfident global data
        rng = random.Random(11)
        rows = []
        for raw_p, true_rate in [(0.3, 0.2), (0.5, 0.4), (0.7, 0.5), (0.85, 0.7)]:
            for _ in range(20):  # 80 total, > MIN_GLOBAL_SAMPLES=30
                y = 1 if rng.random() < true_rate else 0
                rows.append(("KXGENERIC", raw_p, y))
        _insert_cal_rows(conn, rows)

        curve = cal.fit_calibration(conn)
        assert curve["method"] == "platt"
        assert curve["n_samples"] == 80
        assert curve["brier_after"] <= curve["brier_before"] + 1e-6
        assert len(curve["buckets_debug"]) >= 3
        assert curve["isotonic_shadow"]  # non-empty
        # Single family — not enough per-family samples yet because they'd all
        # be under the shared ticker "KXGENERIC". Assert that family got fit
        # (80 samples >= MIN_FAMILY_SAMPLES=30).
        assert "KXGENERIC" in curve["families"]

    def test_per_family_override_requires_min_samples(self, conn):
        # KXHIGHMIA: 40 rows (qualifies); KXHIGHNY: 10 rows (doesn't)
        rng = random.Random(22)
        rows = []
        for _ in range(40):
            p = rng.choice([0.3, 0.5, 0.7, 0.85])
            y = 1 if rng.random() < p * 0.7 else 0  # overconfident
            rows.append(("KXHIGHMIA", p, y))
        for _ in range(10):
            rows.append(("KXHIGHNY", 0.5, 1))
        _insert_cal_rows(conn, rows)

        curve = cal.fit_calibration(conn)
        assert "KXHIGHMIA" in curve["families"]
        assert "KXHIGHNY" not in curve["families"]

    def test_skips_malformed_rows(self, conn):
        # Inject garbage that must be silently filtered.
        _insert_cal_rows(conn, [
            ("KX", 0.5, 1), ("KX", 0.6, 0),
        ])
        conn.execute("INSERT INTO calibration(recorded_at, ticker, estimated_prob, "
                     "actual_outcome, source_desc, n_sources, bucket) "
                     "VALUES('t','KX',NULL,1,'test',1,NULL)")
        conn.execute("INSERT INTO calibration(recorded_at, ticker, estimated_prob, "
                     "actual_outcome, source_desc, n_sources, bucket) "
                     "VALUES('t','KX',0.5,NULL,'test',1,NULL)")
        conn.execute("INSERT INTO calibration(recorded_at, ticker, estimated_prob, "
                     "actual_outcome, source_desc, n_sources, bucket) "
                     "VALUES('t','KX',1.5,1,'test',1,NULL)")  # out-of-range
        conn.commit()
        curve = cal.fit_calibration(conn)
        assert curve["n_samples"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# apply_calibration
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyCalibration:
    """The math tests below monkey-patch ``CALIBRATION_ENABLED=True`` so we
    can verify the Platt formula. Production default is False — see
    ``test_calibration_disabled_by_default_returns_raw``. When the flag is
    flipped on after per-category fits land, the math tests still cover
    the formula."""

    @pytest.fixture(autouse=True)
    def _enable_for_math_tests(self, monkeypatch):
        import bot.config as _config
        monkeypatch.setattr(_config, "CALIBRATION_ENABLED", True)

    def test_none_input_returns_none(self):
        assert cal.apply_calibration(None, {"method": "platt", "A": 1, "B": 0}) is None

    def test_missing_curve_returns_clamped_prob(self):
        assert cal.apply_calibration(0.5, None) == 0.5
        # clamp boundaries
        assert cal.apply_calibration(0.001, None) == 0.02
        assert cal.apply_calibration(0.999, None) == 0.98

    def test_identity_curve_is_noop_mod_clamp(self):
        curve = {"method": "identity", "A": 1.0, "B": 0.0, "families": {}}
        for p in [0.1, 0.3, 0.5, 0.8]:
            assert cal.apply_calibration(p, curve) == pytest.approx(p, abs=1e-9)

    def test_platt_curve_applies_global(self):
        # A=0.5 compresses toward 0.5; B=0 keeps symmetry
        curve = {"method": "platt", "A": 0.5, "B": 0.0, "families": {}}
        out = cal.apply_calibration(0.8, curve)
        # σ(0.5 * logit(0.8)) = σ(0.5 * 1.3863) = σ(0.693) ≈ 0.667
        assert out == pytest.approx(0.667, abs=0.01)

    def test_platt_curve_applies_family_override_when_available(self):
        curve = {
            "method": "platt",
            "A": 1.0, "B": 0.0,
            "families": {"KXHIGHMIA": {"A": 0.2, "B": -0.5}},
        }
        out_global = cal.apply_calibration(0.8, curve, ticker="KXUNKNOWN-X")
        out_family = cal.apply_calibration(0.8, curve, ticker="KXHIGHMIA-26APR16")
        # Family curve should pull harder than the identity global
        assert out_global == pytest.approx(0.8, abs=1e-6)
        assert out_family < 0.6

    def test_legacy_flat_dict_is_no_op(self):
        """Regression: the old shape was {bucket_str: float_offset}. Passing it
        into apply_calibration must not crash — the curve should be detected
        as legacy and the input returned unchanged."""
        legacy = {"0.6-0.7": 0.05, "0.7-0.8": -0.03}
        out = cal.apply_calibration(0.65, legacy)
        assert out == pytest.approx(0.65, abs=1e-9)

    def test_nan_input_passes_through(self):
        assert math.isnan(cal.apply_calibration(float("nan"), None))

    def test_clamp_applied_even_on_extreme_fit(self):
        # A huge positive bias would push toward 1.0; clamp must hold
        curve = {"method": "platt", "A": 5.0, "B": 20.0, "families": {}}
        assert cal.apply_calibration(0.8, curve) == 0.98


class TestCalibrationGate:
    """The CALIBRATION_ENABLED feature gate — production default is False."""

    def test_calibration_disabled_by_default_returns_raw(self, monkeypatch):
        """With the default-off flag, even a sharp Platt curve must be
        bypassed and the raw probability returned (clamped)."""
        import bot.config as _config
        monkeypatch.setattr(_config, "CALIBRATION_ENABLED", False)
        # The Platt curve below would map 0.8 → ~0.667 if applied. With the
        # gate off it must return 0.8 verbatim.
        curve = {"method": "platt", "A": 0.5, "B": 0.0, "families": {}}
        assert cal.apply_calibration(0.8, curve) == pytest.approx(0.8, abs=1e-9)

    def test_calibration_disabled_still_clamps(self, monkeypatch):
        """Even with the gate off, output still goes through the [0.02, 0.98]
        clamp (no caller should ever see a literal 0 or 1)."""
        import bot.config as _config
        monkeypatch.setattr(_config, "CALIBRATION_ENABLED", False)
        assert cal.apply_calibration(0.001, {"method": "platt", "A": 1, "B": 0}) == 0.02
        assert cal.apply_calibration(0.999, {"method": "platt", "A": 1, "B": 0}) == 0.98

    def test_calibration_disabled_handles_none_input(self, monkeypatch):
        """The early-return for None must still work with gate off."""
        import bot.config as _config
        monkeypatch.setattr(_config, "CALIBRATION_ENABLED", False)
        assert cal.apply_calibration(None, None) is None

    def test_step_function_curve_neutralized_when_disabled(self, monkeypatch):
        """The actual broken curve we found in production has A=22M, B=-6.1M
        — a step function at p=0.28. With the gate off, all inputs return
        raw (clamped) regardless of the curve's shape."""
        import bot.config as _config
        monkeypatch.setattr(_config, "CALIBRATION_ENABLED", False)
        broken_curve = {"method": "platt", "A": 21883020.0, "B": -6124319.0,
                         "families": {}}
        # Without the gate, raw 0.65 would have been pushed to 0.98 by this
        # curve. With it, we get 0.65 back.
        assert cal.apply_calibration(0.65, broken_curve) == pytest.approx(0.65, abs=1e-9)
        assert cal.apply_calibration(0.20, broken_curve) == pytest.approx(0.20, abs=1e-9)


# ══════════════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════════════

class TestPersistence:
    def test_save_load_roundtrip(self, conn):
        curve = {
            "method": "platt", "A": 0.9, "B": -0.1,
            "n_samples": 100, "fit_ts": time.time(),
            "buckets_debug": {"0.5-0.6": {"n": 20}},
            "isotonic_shadow": [[0.1, 0.1], [0.5, 0.4]],
            "families": {},
            "brier_before": 0.25, "brier_after": 0.22,
        }
        cal.save_curve(conn, curve)
        conn.commit()
        loaded = cal.load_curve(conn)
        assert loaded["method"] == "platt"
        assert loaded["A"] == pytest.approx(0.9)
        assert loaded["families"] == {}

    def test_load_missing_returns_none(self, conn):
        assert cal.load_curve(conn) is None

    def test_load_respects_ttl(self, conn):
        curve = {"method": "identity", "A": 1, "B": 0}
        cal.save_curve(conn, curve, ttl=-1)
        conn.commit()
        # already expired
        assert cal.load_curve(conn) is None

    def test_fit_and_persist_returns_curve(self, conn):
        # Empty DB → identity curve, still persisted
        curve = cal.fit_and_persist(conn)
        assert curve["method"] == "identity"
        loaded = cal.load_curve(conn)
        assert loaded["method"] == "identity"


# ══════════════════════════════════════════════════════════════════════════════
# Legacy shim compat (trade.py + scoring still import these names)
# ══════════════════════════════════════════════════════════════════════════════

class TestLegacyShims:
    def test_compute_calibration_correction_returns_curve_dict(self, conn):
        # Even under-sampled, shim must return a dict (not crash)
        cal.reset_cache()
        curve = cal.compute_calibration_correction(conn)
        assert isinstance(curve, dict)
        assert "method" in curve

    def test_apply_calibration_correction_preserves_legacy_signature(self, monkeypatch):
        # Legacy signature (prob, corrections) without ticker must still work.
        # Enable the calibration gate for this math-verification test.
        import bot.config as _config
        monkeypatch.setattr(_config, "CALIBRATION_ENABLED", True)
        curve = {"method": "platt", "A": 0.5, "B": 0.0, "families": {}}
        out = cal.apply_calibration_correction(0.8, curve)
        assert 0 < out < 1
        assert abs(out - 0.667) < 0.01

    def test_reset_cache_clears_in_process_curve(self, conn):
        cal.reset_cache()
        assert cal._CURVE_CACHE is None
        _ = cal.compute_calibration_correction(conn)
        assert cal._CURVE_CACHE is not None
        cal.reset_cache()
        assert cal._CURVE_CACHE is None
