"""Calibration bake-off — evaluate candidate calibrators on held-out data.

Methods compared, per family:
  - identity            (no calibration)
  - platt               (bounded Newton, current production)
  - platt_wide          (same fit with PLATT_A_MIN lowered to 0.1)
  - isotonic            (pool-adjacent-violators)
  - beta                (3-param: σ(a·log(p) + b·log(1-p) + c))
  - histogram           (10 equal-frequency bins, Laplace-smoothed mean)

Train/holdout split:
  - eligible window: 2026-05-01 ≤ recorded_at < 2026-05-04 14:00:00 UTC
    (post v2-stabilization, pre today's bounded-calibrator deploy)
  - train = 2026-05-01 to 2026-05-03 inclusive
  - holdout = 2026-05-04 (00:00–14:00 UTC)

Metrics on the holdout:
  - Brier score (mean squared error of probability)
  - Log-loss (cross-entropy)
  - ECE (10-bin equal-frequency expected calibration error)

Output: per-family ranked table by holdout Brier, plus a recommended
per-family choice with delta vs identity.

Read-only — does NOT persist anything. Safe to run while the daemon is up.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from collections import defaultdict
from typing import Callable, Optional

# ── splits ─────────────────────────────────────────────────────────────────────

TRAIN_START = "2026-05-01"
TRAIN_END = "2026-05-04"             # exclusive
HOLDOUT_START = "2026-05-04"
HOLDOUT_END = "2026-05-04T14:08:00"  # exclusive — bounded fitter went live ~14:08 UTC

WEATHER_FAMILIES = (
    "KXHIGHNY", "KXHIGHMIA", "KXHIGHCHI",
    "KXHIGHLAX", "KXHIGHAUS", "KXHIGHDEN",
)

MIN_FAMILY_TRAIN = 200    # below this we don't bother fitting most methods
EPS = 1e-6


# ── numerics ───────────────────────────────────────────────────────────────────

def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _log_sigmoid(z: float) -> float:
    return -max(0.0, -z) - math.log1p(math.exp(-abs(z)))


def _logit(p: float) -> float:
    p = max(EPS, min(1.0 - EPS, p))
    return math.log(p / (1.0 - p))


def _clip01(p: float) -> float:
    return max(EPS, min(1.0 - EPS, p))


# ── methods ────────────────────────────────────────────────────────────────────

def fit_identity(_ps, _ys):
    return ("identity", None)


def apply_identity(_model, p):
    return p


def _fit_platt_bounded(ps, ys, a_min, a_max, b_min=-10.0, b_max=10.0,
                       max_iter=60, tol=1e-6, l2=1e-3):
    """Bounded-Newton Platt with backtracking line search. Mirrors the
    production fitter but takes the bounds as parameters so we can sweep."""
    n_pos = sum(ys)
    n_neg = len(ys) - n_pos
    if n_pos == 0 or n_neg == 0:
        return (1.0, 0.0)
    t_pos = (n_pos + 1.0) / (n_pos + 2.0)
    t_neg = 1.0 / (n_neg + 2.0)
    targets = [t_pos if y else t_neg for y in ys]
    xs = [_logit(p) for p in ps]

    def loss(A, B):
        s = 0.0
        for x, t in zip(xs, targets):
            z = A * x + B
            s -= t * _log_sigmoid(z) + (1 - t) * _log_sigmoid(-z)
        s += 0.5 * l2 * (A * A + B * B)
        return s

    A, B = 1.0, 0.0
    L = loss(A, B)
    for _ in range(max_iter):
        gA = gB = HAA = HAB = HBB = 0.0
        for x, t in zip(xs, targets):
            p = _sigmoid(A * x + B)
            d = p - t
            gA += d * x
            gB += d
            w = max(1e-12, p * (1 - p))
            HAA += w * x * x
            HAB += w * x
            HBB += w
        gA += l2 * A
        gB += l2 * B
        HAA += l2
        HBB += l2
        det = HAA * HBB - HAB * HAB
        if abs(det) < 1e-18:
            break
        dA = (HBB * gA - HAB * gB) / det
        dB = (HAA * gB - HAB * gA) / det
        step = 1.0
        for _ls in range(20):
            At = max(a_min, min(a_max, A - step * dA))
            Bt = max(b_min, min(b_max, B - step * dB))
            Lt = loss(At, Bt)
            if Lt < L:
                break
            step *= 0.5
        else:
            return (A, B)
        if abs(At - A) + abs(Bt - B) < tol:
            return (At, Bt)
        A, B, L = At, Bt, Lt
    return (A, B)


def fit_platt(ps, ys):
    A, B = _fit_platt_bounded(ps, ys, a_min=0.5, a_max=5.0)
    return ("platt", (A, B))


def apply_platt(model, p):
    A, B = model
    return _clip01(_sigmoid(A * _logit(p) + B))


def fit_platt_wide(ps, ys):
    A, B = _fit_platt_bounded(ps, ys, a_min=0.1, a_max=5.0)
    return ("platt_wide", (A, B))


def apply_platt_wide(model, p):
    return apply_platt(model, p)


def fit_isotonic(ps, ys):
    """Pool-adjacent-violators on (p, y), returning monotone step function as
    a sorted list of (x_right_edge, y_value). Lookup interpolates between
    block midpoints (linear) for smoother predictions on holdout."""
    if not ps:
        return ("isotonic", [])
    pairs = sorted(zip(ps, ys))
    blocks = [[float(y), 1.0, x] for x, y in pairs]
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
    # Convert blocks to (right_edge, mean_y) pairs for binary-search lookup.
    return ("isotonic", [(b[2], b[0] / b[1]) for b in blocks])


def apply_isotonic(model, p):
    """Return the step value at the smallest x_right_edge >= p. If p exceeds
    all edges, return last block's y."""
    if not model:
        return p
    lo, hi = 0, len(model) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if model[mid][0] >= p:
            hi = mid
        else:
            lo = mid + 1
    return _clip01(model[lo][1])


def fit_beta(ps, ys, max_iter=60, tol=1e-6, l2=1e-3,
             bounds=(-10.0, 10.0)):
    """Beta calibration (Kull et al.): σ(a·log(p) + b·log(1-p) + c).

    Reduces to logistic regression on the 2-feature [log p, log(1-p)] space
    with intercept. Newton + backtracking line search, three params bounded
    in [-10, 10] to mirror Platt-style numerical safety."""
    n_pos = sum(ys)
    n_neg = len(ys) - n_pos
    if n_pos == 0 or n_neg == 0:
        return ("beta", (0.0, 0.0, 0.0))
    t_pos = (n_pos + 1.0) / (n_pos + 2.0)
    t_neg = 1.0 / (n_neg + 2.0)
    targets = [t_pos if y else t_neg for y in ys]
    feats = [(math.log(_clip01(p)), math.log(1.0 - _clip01(p))) for p in ps]
    lo, hi = bounds

    def loss(a, b, c):
        s = 0.0
        for (lp, l1mp), t in zip(feats, targets):
            z = a * lp + b * l1mp + c
            s -= t * _log_sigmoid(z) + (1 - t) * _log_sigmoid(-z)
        s += 0.5 * l2 * (a * a + b * b + c * c)
        return s

    a, b, c = 1.0, -1.0, 0.0   # init: identity-ish (a=1, b=-1 gives σ(logit(p))=p)
    L = loss(a, b, c)
    for _ in range(max_iter):
        # Gradient and Hessian of cross-entropy w/ Bernoulli logits:
        # grad_θ = Σ (σ(z) - t) · x
        # H_θθ = Σ σ(z)(1-σ(z)) · x x^T
        g = [0.0, 0.0, 0.0]
        H = [[0.0]*3 for _ in range(3)]
        for (lp, l1mp), t in zip(feats, targets):
            z = a * lp + b * l1mp + c
            p = _sigmoid(z)
            d = p - t
            x = (lp, l1mp, 1.0)
            for i in range(3):
                g[i] += d * x[i]
            w = max(1e-12, p * (1 - p))
            for i in range(3):
                for j in range(3):
                    H[i][j] += w * x[i] * x[j]
        for i in range(3):
            g[i] += l2 * (a, b, c)[i]
            H[i][i] += l2
        # Solve H · δ = g via 3x3 inverse (Cramer's rule for clarity).
        try:
            det = (H[0][0] * (H[1][1] * H[2][2] - H[1][2] * H[2][1])
                   - H[0][1] * (H[1][0] * H[2][2] - H[1][2] * H[2][0])
                   + H[0][2] * (H[1][0] * H[2][1] - H[1][1] * H[2][0]))
            if abs(det) < 1e-18:
                break
            inv = [[0.0]*3 for _ in range(3)]
            inv[0][0] = (H[1][1] * H[2][2] - H[1][2] * H[2][1]) / det
            inv[0][1] = (H[0][2] * H[2][1] - H[0][1] * H[2][2]) / det
            inv[0][2] = (H[0][1] * H[1][2] - H[0][2] * H[1][1]) / det
            inv[1][0] = (H[1][2] * H[2][0] - H[1][0] * H[2][2]) / det
            inv[1][1] = (H[0][0] * H[2][2] - H[0][2] * H[2][0]) / det
            inv[1][2] = (H[0][2] * H[1][0] - H[0][0] * H[1][2]) / det
            inv[2][0] = (H[1][0] * H[2][1] - H[1][1] * H[2][0]) / det
            inv[2][1] = (H[0][1] * H[2][0] - H[0][0] * H[2][1]) / det
            inv[2][2] = (H[0][0] * H[1][1] - H[0][1] * H[1][0]) / det
            d = [sum(inv[i][j] * g[j] for j in range(3)) for i in range(3)]
        except ZeroDivisionError:
            break
        step = 1.0
        for _ls in range(20):
            at = max(lo, min(hi, a - step * d[0]))
            bt = max(lo, min(hi, b - step * d[1]))
            ct = max(lo, min(hi, c - step * d[2]))
            Lt = loss(at, bt, ct)
            if Lt < L:
                break
            step *= 0.5
        else:
            return ("beta", (a, b, c))
        if abs(at - a) + abs(bt - b) + abs(ct - c) < tol:
            return ("beta", (at, bt, ct))
        a, b, c, L = at, bt, ct, Lt
    return ("beta", (a, b, c))


def apply_beta(model, p):
    a, b, c = model
    pp = _clip01(p)
    z = a * math.log(pp) + b * math.log(1 - pp) + c
    return _clip01(_sigmoid(z))


def fit_histogram(ps, ys, n_bins=10):
    """Equal-frequency histogram binning with Laplace smoothing."""
    if not ps:
        return ("histogram", [])
    pairs = sorted(zip(ps, ys))
    n = len(pairs)
    bin_size = max(1, n // n_bins)
    bins = []
    for k in range(n_bins):
        lo = k * bin_size
        hi = n if k == n_bins - 1 else (k + 1) * bin_size
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        bin_lo_p = chunk[0][0]
        bin_hi_p = chunk[-1][0]
        n_in = len(chunk)
        n_pos_in = sum(y for _, y in chunk)
        # Laplace-smoothed rate
        rate = (n_pos_in + 1.0) / (n_in + 2.0)
        bins.append((bin_lo_p, bin_hi_p, rate))
    return ("histogram", bins)


def apply_histogram(model, p):
    if not model:
        return p
    for lo, hi, rate in model:
        if lo <= p <= hi:
            return _clip01(rate)
    # outside any bin: fall back to nearest extreme bin's rate
    if p < model[0][0]:
        return _clip01(model[0][2])
    return _clip01(model[-1][2])


METHODS = [
    ("identity",   fit_identity,   apply_identity),
    ("platt",      fit_platt,      apply_platt),
    ("platt_wide", fit_platt_wide, apply_platt_wide),
    ("isotonic",   fit_isotonic,   apply_isotonic),
    ("beta",       fit_beta,       apply_beta),
    ("histogram",  fit_histogram,  apply_histogram),
]


# ── metrics ────────────────────────────────────────────────────────────────────

def brier(ps, ys):
    if not ps:
        return float("nan")
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def logloss(ps, ys):
    if not ps:
        return float("nan")
    s = 0.0
    for p, y in zip(ps, ys):
        pp = _clip01(p)
        s -= y * math.log(pp) + (1 - y) * math.log(1 - pp)
    return s / len(ps)


def ece(ps, ys, n_bins=10):
    """Equal-frequency ECE: Σ (n_bin / N) · |avg_p − avg_y|."""
    if not ps:
        return float("nan")
    pairs = sorted(zip(ps, ys))
    n = len(pairs)
    bin_size = max(1, n // n_bins)
    s = 0.0
    for k in range(n_bins):
        lo = k * bin_size
        hi = n if k == n_bins - 1 else (k + 1) * bin_size
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        avg_p = sum(p for p, _ in chunk) / len(chunk)
        avg_y = sum(y for _, y in chunk) / len(chunk)
        s += (len(chunk) / n) * abs(avg_p - avg_y)
    return s


# ── data loading ───────────────────────────────────────────────────────────────

def family_key(ticker: Optional[str]) -> Optional[str]:
    if not ticker:
        return None
    return ticker.split("-")[0] or None


def load_split(conn) -> tuple[dict, dict]:
    """Returns (train, holdout) dicts: family -> (ps, ys)."""
    train = defaultdict(lambda: ([], []))
    holdout = defaultdict(lambda: ([], []))

    rows = conn.execute(
        """SELECT recorded_at, ticker, estimated_prob, actual_outcome
           FROM calibration
           WHERE estimated_prob IS NOT NULL AND actual_outcome IS NOT NULL
             AND recorded_at >= ? AND recorded_at < ?
        """,
        (TRAIN_START, HOLDOUT_END),
    ).fetchall()

    for ts, ticker, p, y in rows:
        try:
            pf = float(p)
            yi = int(y)
        except (TypeError, ValueError):
            continue
        if not (0 <= pf <= 1) or yi not in (0, 1):
            continue
        fam = family_key(ticker)
        if fam is None:
            continue
        if ts < TRAIN_END:
            train[fam][0].append(pf)
            train[fam][1].append(yi)
        elif ts >= HOLDOUT_START:
            holdout[fam][0].append(pf)
            holdout[fam][1].append(yi)

    return train, holdout


# ── main ───────────────────────────────────────────────────────────────────────

def evaluate_family(fam, train_ps, train_ys, holdout_ps, holdout_ys, n_bins=10):
    """Returns list of (method_name, brier, logloss, ece, n_train, n_holdout, model)."""
    results = []
    for name, fit, apply in METHODS:
        if name != "identity" and len(train_ps) < MIN_FAMILY_TRAIN:
            results.append((name, float("nan"), float("nan"), float("nan"),
                           len(train_ps), len(holdout_ps), None))
            continue
        try:
            _, model = fit(train_ps, train_ys)
        except Exception as e:
            print(f"  [{fam}/{name}] fit failed: {e}")
            results.append((name, float("nan"), float("nan"), float("nan"),
                           len(train_ps), len(holdout_ps), None))
            continue
        if not holdout_ps:
            results.append((name, float("nan"), float("nan"), float("nan"),
                           len(train_ps), 0, model))
            continue
        applied = [apply(model, p) for p in holdout_ps]
        results.append((
            name,
            brier(applied, holdout_ys),
            logloss(applied, holdout_ys),
            ece(applied, holdout_ys, n_bins),
            len(train_ps),
            len(holdout_ps),
            model,
        ))
    return results


def fmt_model(name, model):
    if model is None:
        return ""
    if name == "identity":
        return ""
    if name in ("platt", "platt_wide"):
        A, B = model
        return f"A={A:+.3f} B={B:+.3f}"
    if name == "beta":
        a, b, c = model
        return f"a={a:+.3f} b={b:+.3f} c={c:+.3f}"
    if name == "isotonic":
        return f"{len(model)} blocks"
    if name == "histogram":
        return f"{len(model)} bins"
    return ""


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "kalshi_trades.db"
    conn = sqlite3.connect(db)
    train, holdout = load_split(conn)
    fams = sorted(set(train) | set(holdout))
    fams = [f for f in fams if f in WEATHER_FAMILIES] + \
           [f for f in fams if f not in WEATHER_FAMILIES]

    print(f"Train window: {TRAIN_START} → {TRAIN_END}")
    print(f"Holdout window: {HOLDOUT_START} → {HOLDOUT_END}")
    print()

    summary_rows = []  # (fam, best_method, best_brier, identity_brier, delta_pct)

    for fam in fams:
        tps, tys = train.get(fam, ([], []))
        hps, hys = holdout.get(fam, ([], []))
        is_weather = fam in WEATHER_FAMILIES
        marker = "★" if is_weather else " "
        print(f"{marker} {fam}  train={len(tps)}  holdout={len(hps)}  "
              f"holdout_pos_rate={(sum(hys)/len(hys)) if hys else 0:.3f}")
        if len(tps) < MIN_FAMILY_TRAIN:
            print(f"    skip: train below MIN_FAMILY_TRAIN={MIN_FAMILY_TRAIN}")
            print()
            continue
        if not hps:
            print(f"    skip: empty holdout")
            print()
            continue

        results = evaluate_family(fam, tps, tys, hps, hys)
        # baseline = identity (raw)
        ident = next((r for r in results if r[0] == "identity"), None)
        baseline_brier = ident[1] if ident else float("nan")

        # sort by holdout brier ascending, NaN last
        ordered = sorted(
            results,
            key=lambda r: (math.isnan(r[1]), r[1] if not math.isnan(r[1]) else 0),
        )
        print(f"    {'method':<11} {'brier':>8} {'Δ%':>6} {'logloss':>8} "
              f"{'ECE':>6}  params")
        for name, br, ll, ec, nt, nh, model in ordered:
            if math.isnan(br):
                continue
            delta = (br - baseline_brier) / baseline_brier * 100 if baseline_brier else 0
            print(f"    {name:<11} {br:>8.4f} {delta:>+6.1f} {ll:>8.4f} "
                  f"{ec:>6.4f}  {fmt_model(name, model)}")
        # winner = best non-identity that beats identity by ≥ 1% relative
        non_ident = [r for r in ordered if r[0] != "identity" and not math.isnan(r[1])]
        winner = None
        if non_ident:
            best = non_ident[0]
            if (baseline_brier - best[1]) / baseline_brier >= 0.01:
                winner = best
        if winner:
            wd = (winner[1] - baseline_brier) / baseline_brier * 100
            print(f"    → recommended: {winner[0]} ({wd:+.1f}% Brier vs identity)")
            summary_rows.append((fam, winner[0], winner[1], baseline_brier, wd))
        else:
            print(f"    → recommended: identity (no method beats raw by ≥1%)")
            summary_rows.append((fam, "identity", baseline_brier, baseline_brier, 0.0))
        print()

    print("=" * 64)
    print("RECOMMENDATIONS (ranked by Brier improvement vs identity):")
    print(f"{'family':<12} {'method':<11} {'brier':>8} {'identity':>9} {'Δ%':>6}")
    summary_rows.sort(key=lambda r: r[4])  # most negative (biggest improvement) first
    for fam, m, br, ib, d in summary_rows:
        flag = "★" if fam in WEATHER_FAMILIES else " "
        print(f"{flag} {fam:<10} {m:<11} {br:>8.4f} {ib:>9.4f} {d:>+6.1f}")


if __name__ == "__main__":
    main()
