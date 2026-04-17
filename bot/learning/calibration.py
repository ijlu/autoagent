"""Calibration correction for ensemble probability estimates.

Builds a calibration curve from settlement data that corrects systematic bias
(e.g., if we estimate 70% but actual outcomes are 55%, we adjust down).
"""

from __future__ import annotations

_CALIBRATION_CURVE = None  # cached per run


def _prob_bucket(p):
    """Assign a probability to a calibration bucket (0.0-0.1, 0.1-0.2, ..., 0.9-1.0)."""
    if p is None:
        return None
    bucket = int(p * 10) / 10  # floor to nearest 0.1
    return f"{bucket:.1f}-{bucket+0.1:.1f}"


def compute_calibration_correction(conn):
    """Build a calibration curve that corrects systematic bias in our estimates.
    If we estimate 70% but actual outcomes are 55%, we should adjust down.
    Returns a dict of {bucket: correction_offset} to apply to ensemble output."""
    global _CALIBRATION_CURVE
    if _CALIBRATION_CURVE is not None:
        return _CALIBRATION_CURVE

    MIN_CAL_SAMPLES = 5  # per bucket

    rows = conn.execute(
        "SELECT bucket, estimated_prob, actual_outcome FROM calibration WHERE bucket IS NOT NULL"
    ).fetchall()

    if len(rows) < 20:
        _CALIBRATION_CURVE = {}
        return _CALIBRATION_CURVE

    buckets = {}
    for bucket, est, actual in rows:
        buckets.setdefault(bucket, []).append((est, actual))

    corrections = {}
    for bucket, entries in sorted(buckets.items()):
        if len(entries) < MIN_CAL_SAMPLES:
            continue
        avg_est = sum(e for e, _ in entries) / len(entries)
        actual_rate = sum(a for _, a in entries) / len(entries)
        bias = avg_est - actual_rate

        # Only correct if bias is significant (>5%) and we have enough data
        if abs(bias) > 0.05 and len(entries) >= MIN_CAL_SAMPLES:
            # Apply partial correction (50% of observed bias) to be conservative
            # Full correction would overfit to small samples
            corrections[bucket] = -bias * 0.5
            direction = "overconfident" if bias > 0 else "underconfident"
            print(f"[calibration] {bucket}: {direction} by {abs(bias):.1%} "
                  f"(est={avg_est:.2f} vs actual={actual_rate:.2f}, n={len(entries)}) "
                  f"→ correction={corrections[bucket]:+.3f}")

    _CALIBRATION_CURVE = corrections
    return corrections


def apply_calibration_correction(ensemble_prob, calibration_corrections):
    """Apply learned calibration correction to an ensemble probability estimate."""
    if not calibration_corrections or ensemble_prob is None:
        return ensemble_prob
    bucket = _prob_bucket(ensemble_prob)
    correction = calibration_corrections.get(bucket, 0)
    if correction == 0:
        return ensemble_prob
    corrected = max(0.02, min(0.98, ensemble_prob + correction))
    return corrected
