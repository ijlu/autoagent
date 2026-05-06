"""Unified calibration for ensemble probability estimates.

Single source of truth for:
  - fit_calibration(conn): learn a Platt curve (plus shadow isotonic + per-family
    overrides) from the `calibration` table.
  - apply_calibration(prob, curve, ticker=None): correct a raw probability using
    the fitted curve. Used by the ensemble at every emission site.

Curve shape (JSON-serializable)::

    {
        "method": "platt",           # applied at runtime
        "A": float,                  # global slope
        "B": float,                  # global bias
        "n_samples": int,
        "fit_ts": float,             # unix
        "buckets_debug": {           # human diagnostic, 0.1-wide buckets
            "0.3-0.4": {"n": .., "avg_est": .., "actual_rate": .., "bias": ..},
        },
        "isotonic_shadow": [[x, y], ...],   # monotone step-fn, shadow only
        "families": {                # per-family overrides (≥ min_family_samples each)
            "KXHIGHMIA": {"A": .., "B": .., "n_samples": ..},
        },
        "brier_before": float,
        "brier_after": float,
    }

When the curve is empty or under-sampled, ``apply_calibration`` returns the
input prob unchanged (modulo the 0.02–0.98 safety clamp that also lives in the
ensemble). A legacy flat ``{bucket: float}`` dict is detected and silently
routed to a no-op to keep old cached curves from crashing the trading loop.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Optional

from bot.db import db_write_ctx

# Global scope for per-run curve caching in a process (short-circuit for the
# legacy trade.py code path that reset a module-level curve each cycle). Not
# used by the daemon — the daemon reads from kv_cache per cycle.
_CURVE_CACHE: Optional[dict] = None


# ══════════════════════════════════════════════════════════════════════════════
# Tunables
# ══════════════════════════════════════════════════════════════════════════════

MIN_GLOBAL_SAMPLES = 30      # below this, return identity curve
MIN_FAMILY_SAMPLES = 30      # below this, family falls through to global
MIN_BUCKET_SAMPLES = 5       # for buckets_debug only, not for apply
PROB_CLAMP_LOW = 0.02
PROB_CLAMP_HIGH = 0.98
NEWTON_MAX_ITER = 60
NEWTON_TOL = 1e-6
L2_REG = 1e-3                # tiny ridge to keep Newton PSD when data is thin


# ══════════════════════════════════════════════════════════════════════════════
# Math helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _logit(p: float) -> float:
    p = max(1e-6, min(1.0 - 1e-6, p))
    return math.log(p / (1.0 - p))


def _prob_bucket_label(p: float) -> str:
    p = max(0.0, min(0.999999, p))
    lo = int(p * 10) / 10
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def family_key(ticker: Optional[str]) -> Optional[str]:
    """Short family prefix (first underscore-delimited token) or None."""
    if not ticker:
        return None
    # KXHIGHMIA-26APR16 → KXHIGHMIA
    base = ticker.split("-")[0]
    return base or None


# ══════════════════════════════════════════════════════════════════════════════
# Platt fit
# ══════════════════════════════════════════════════════════════════════════════

def _fit_platt(xs: list[float], ys: list[int]) -> tuple[float, float, int]:
    """Newton-Raphson Platt scaling on pre-logit features.

    Uses the Platt 1999 label-smoothing prior (t+ = (N++1)/(N++2),
    t- = 1/(N-+2)) to prevent overconfidence on tiny samples, plus an L2
    regularizer to keep the Hessian PSD. Returns (A, B, iterations).
    """
    n_pos = sum(ys)
    n_neg = len(ys) - n_pos
    t_pos = (n_pos + 1.0) / (n_pos + 2.0)
    t_neg = 1.0 / (n_neg + 2.0)
    targets = [t_pos if y else t_neg for y in ys]

    A, B = 1.0, 0.0
    for it in range(NEWTON_MAX_ITER):
        grad_A = grad_B = 0.0
        H_AA = H_AB = H_BB = 0.0
        for x, t in zip(xs, targets):
            p = _sigmoid(A * x + B)
            d = p - t
            grad_A += d * x
            grad_B += d
            w = max(1e-12, p * (1.0 - p))
            H_AA += w * x * x
            H_AB += w * x
            H_BB += w
        grad_A += L2_REG * A
        grad_B += L2_REG * B
        H_AA += L2_REG
        H_BB += L2_REG

        det = H_AA * H_BB - H_AB * H_AB
        if abs(det) < 1e-18:
            break
        dA = (H_BB * grad_A - H_AB * grad_B) / det
        dB = (H_AA * grad_B - H_AB * grad_A) / det
        A -= dA
        B -= dB
        if abs(dA) + abs(dB) < NEWTON_TOL:
            return A, B, it + 1
    return A, B, NEWTON_MAX_ITER


# ══════════════════════════════════════════════════════════════════════════════
# Isotonic (pool-adjacent-violators) — shadow only, not runtime-applied
# ══════════════════════════════════════════════════════════════════════════════

def _fit_isotonic(xs: list[float], ys: list[int]) -> list[list[float]]:
    """Monotone non-decreasing step function via PAV. Returns [[x_edge, y], ...]."""
    if not xs:
        return []
    paired = sorted(zip(xs, ys))
    # Initialize each point as its own block (sum, count, right_edge)
    blocks: list[list[float]] = [[float(y), 1.0, x] for x, y in paired]
    # Merge violations
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] / blocks[i][1] > blocks[i + 1][0] / blocks[i + 1][1]:
            blocks[i][0] += blocks[i + 1][0]
            blocks[i][1] += blocks[i + 1][1]
            blocks[i][2] = blocks[i + 1][2]
            del blocks[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    return [[b[2], b[0] / b[1]] for b in blocks]


# ══════════════════════════════════════════════════════════════════════════════
# Fit orchestration
# ══════════════════════════════════════════════════════════════════════════════

def _brier(ps: list[float], ys: list[int]) -> float:
    if not ps:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def _bucket_stats(ps: list[float], ys: list[int]) -> dict[str, dict]:
    buckets: dict[str, list[tuple[float, int]]] = {}
    for p, y in zip(ps, ys):
        buckets.setdefault(_prob_bucket_label(p), []).append((p, y))
    out = {}
    for b, items in sorted(buckets.items()):
        if len(items) < MIN_BUCKET_SAMPLES:
            continue
        avg_est = sum(p for p, _ in items) / len(items)
        actual = sum(y for _, y in items) / len(items)
        out[b] = {
            "n": len(items),
            "avg_est": round(avg_est, 4),
            "actual_rate": round(actual, 4),
            "bias": round(avg_est - actual, 4),
        }
    return out


def _fit_segment(ps: list[float], ys: list[int]) -> Optional[dict]:
    """Fit a single Platt segment, returning the shared {A,B,n_samples} sub-dict."""
    if len(ps) < MIN_GLOBAL_SAMPLES:
        return None
    xs = [_logit(p) for p in ps]
    A, B, iters = _fit_platt(xs, ys)
    return {
        "A": round(A, 6),
        "B": round(B, 6),
        "n_samples": len(ps),
        "iters": iters,
    }


def fit_calibration(conn, now_ts: Optional[float] = None) -> dict:
    """Fit a Platt curve from the ``calibration`` table. Always returns a curve dict.

    If sample count is below ``MIN_GLOBAL_SAMPLES``, returns an identity curve
    (``method="identity"``) — safe to pass to ``apply_calibration`` as a no-op.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    rows = conn.execute(
        "SELECT ticker, estimated_prob, actual_outcome "
        "FROM calibration "
        "WHERE estimated_prob IS NOT NULL AND actual_outcome IS NOT NULL"
    ).fetchall()

    ps_global: list[float] = []
    ys_global: list[int] = []
    by_family: dict[str, tuple[list[float], list[int]]] = {}
    for ticker, p, y in rows:
        try:
            pf = float(p)
            yi = int(y)
        except (TypeError, ValueError):
            continue
        if pf < 0 or pf > 1 or yi not in (0, 1):
            continue
        ps_global.append(pf)
        ys_global.append(yi)
        fam = family_key(ticker)
        if fam:
            bucket_ps, bucket_ys = by_family.setdefault(fam, ([], []))
            bucket_ps.append(pf)
            bucket_ys.append(yi)

    if len(ps_global) < MIN_GLOBAL_SAMPLES:
        curve = {
            "method": "identity",
            "A": 1.0,
            "B": 0.0,
            "n_samples": len(ps_global),
            "fit_ts": now_ts,
            "buckets_debug": _bucket_stats(ps_global, ys_global),
            "isotonic_shadow": [],
            "families": {},
            "brier_before": round(_brier(ps_global, ys_global), 6),
            "brier_after": round(_brier(ps_global, ys_global), 6),
            "reason": f"n={len(ps_global)} < MIN_GLOBAL_SAMPLES={MIN_GLOBAL_SAMPLES}",
        }
        return curve

    global_fit = _fit_segment(ps_global, ys_global)
    assert global_fit is not None  # guarded above

    A, B = global_fit["A"], global_fit["B"]
    applied_global = [_sigmoid(A * _logit(p) + B) for p in ps_global]

    # Per-family overrides — only where we have enough data to fit reliably
    families: dict[str, dict] = {}
    for fam, (fps, fys) in by_family.items():
        if len(fps) < MIN_FAMILY_SAMPLES:
            continue
        seg = _fit_segment(fps, fys)
        if seg is None:
            continue
        families[fam] = seg

    curve = {
        "method": "platt",
        "A": A,
        "B": B,
        "n_samples": len(ps_global),
        "fit_ts": now_ts,
        "buckets_debug": _bucket_stats(ps_global, ys_global),
        "isotonic_shadow": _fit_isotonic(ps_global, ys_global),
        "families": families,
        "brier_before": round(_brier(ps_global, ys_global), 6),
        "brier_after": round(_brier(applied_global, ys_global), 6),
    }
    return curve


# ══════════════════════════════════════════════════════════════════════════════
# Apply
# ══════════════════════════════════════════════════════════════════════════════

def _looks_like_legacy_flat(curve: Any) -> bool:
    """True if curve is an old {bucket_str: float_offset} dict from pre-unification."""
    if not isinstance(curve, dict):
        return False
    if "method" in curve or "A" in curve:
        return False
    for k, v in curve.items():
        if isinstance(k, str) and "-" in k and isinstance(v, (int, float)):
            return True
        break
    return False


def _clamp(p: float) -> float:
    return max(PROB_CLAMP_LOW, min(PROB_CLAMP_HIGH, p))


def apply_calibration(
    prob: Optional[float],
    curve: Optional[dict],
    ticker: Optional[str] = None,
) -> Optional[float]:
    """Apply the fitted curve to a raw probability.

    Always clamps output to [0.02, 0.98]. Returns ``prob`` unchanged (modulo
    clamp) when the curve is missing, identity, or a legacy shape.

    GLOBAL DISABLE (2026-04-27): the persisted Platt curve was fit
    overwhelmingly on weather rows from the broken v1 ensemble path
    (8509/8815 rows = 96.5%). Those rows had raw=0.98 + actual=16% in the
    top bucket because the broken v1 was producing clamped extremes
    regardless of input. The Platt fit captured that pattern (A=22M,
    B=-6.1M → essentially a step function at 0.28) and now applies it to
    every category, destroying signal in non-weather predictions too.
    Until we accumulate enough clean v2-era data to refit per category,
    apply_calibration is gated behind ``CALIBRATION_ENABLED`` (default
    false) and returns the raw prob (clamped) for any category.

    The fitter (``fit_calibration``) keeps running so data continues to
    accumulate. When per-category sample counts cross a threshold, we'll
    enable per-family Platt fits and flip the flag.
    """
    if prob is None:
        return None
    if not isinstance(prob, (int, float)) or math.isnan(prob):
        return prob

    # Global gate — see docstring. Default is false; override by setting
    # CALIBRATION_ENABLED=true in the env once the fit is trustworthy.
    try:
        from bot.config import CALIBRATION_ENABLED
    except ImportError:
        CALIBRATION_ENABLED = False
    if not CALIBRATION_ENABLED:
        return _clamp(float(prob))

    if not curve or _looks_like_legacy_flat(curve):
        return _clamp(float(prob))

    method = curve.get("method", "identity")
    if method == "identity":
        return _clamp(float(prob))

    # Prefer family-specific segment if available, otherwise global.
    A = curve.get("A", 1.0)
    B = curve.get("B", 0.0)
    fam = family_key(ticker)
    if fam and fam in curve.get("families", {}):
        seg = curve["families"][fam]
        A = seg.get("A", A)
        B = seg.get("B", B)

    try:
        z = A * _logit(float(prob)) + B
        return _clamp(_sigmoid(z))
    except (ValueError, OverflowError):
        return _clamp(float(prob))


# ══════════════════════════════════════════════════════════════════════════════
# Persistence helpers (kv_cache-backed, used by daemon + oneshot)
# ══════════════════════════════════════════════════════════════════════════════

KV_KEY = "calibration_curve_v2"
KV_TTL_SECONDS = 3600  # refit once an hour at most


def load_curve(conn) -> Optional[dict]:
    """Load the cached curve from kv_cache. Returns None if missing or unparseable."""
    try:
        row = conn.execute(
            "SELECT value FROM kv_cache WHERE key=? AND (expires_at IS NULL OR expires_at > ?)",
            (KV_KEY, int(time.time())),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def save_curve(conn, curve: dict, ttl: int = KV_TTL_SECONDS) -> None:
    """Persist the curve to kv_cache. Uses the bot.db.DB_WRITE_LOCK lock via
    caller's responsibility — this function is a bare UPSERT to stay
    orchestration-layer-agnostic."""
    expires = int(time.time() + ttl)
    payload = json.dumps(curve, separators=(",", ":"))
    conn.execute(
        "INSERT INTO kv_cache(key, value, expires_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
        (KV_KEY, payload, expires),
    )


def fit_and_persist(conn) -> dict:
    """Fit the curve, persist to kv_cache, return the curve."""
    curve = fit_calibration(conn)
    try:
        with db_write_ctx(conn):
            save_curve(conn, curve)
    except Exception as e:
        print(f"[calibration] save_curve failed: {e}")
    return curve


# ══════════════════════════════════════════════════════════════════════════════
# Back-compat shims (kept as thin wrappers so legacy imports don't break while
# we migrate callers). Remove once trade.py + market_scorer.py are re-wired.
# ══════════════════════════════════════════════════════════════════════════════

def compute_calibration_correction(conn):
    """Legacy alias → returns the Platt curve dict. Callers that treated the
    return value as a flat {bucket: offset} dict will fall through to a no-op
    in apply_calibration (the curve is detected as non-legacy by its ``method``
    key). New callers should use fit_calibration() directly."""
    global _CURVE_CACHE
    if _CURVE_CACHE is not None:
        return _CURVE_CACHE
    _CURVE_CACHE = fit_calibration(conn)
    return _CURVE_CACHE


def apply_calibration_correction(prob, curve):
    """Legacy alias → delegates to apply_calibration without a ticker hint.
    Family-specific curves won't apply via this alias; new callers should use
    apply_calibration(prob, curve, ticker=...) directly."""
    return apply_calibration(prob, curve, ticker=None)


def reset_cache() -> None:
    """Drop the in-process curve cache. Called at the top of each cycle."""
    global _CURVE_CACHE
    _CURVE_CACHE = None
