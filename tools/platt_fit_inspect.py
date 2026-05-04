"""Inspect persisted per-family Platt fits + simulate per-family Brier
before deciding whether to flip CALIBRATION_ENABLED."""
import math
from collections import defaultdict
from bot.db import init_db
from bot.learning.calibration import (
    load_curve, _logit, _sigmoid, _brier, family_key,
)

conn = init_db("kalshi_trades.db")
curve = load_curve(conn)
if not curve:
    print("no curve cached")
    raise SystemExit

print(f"global: A={curve['A']:.4f} B={curve['B']:.4f} n={curve['n_samples']}")
print(f"global brier: before={curve['brier_before']} after={curve['brier_after']}")
print()

fams = curve.get("families", {})
print(f"per-family fits in cached curve: {len(fams)}")
print()

# Pull recent settled predictions per family — focus on weather and
# only use post-2026-04-27 data so we're testing on clean v2-era rows.
rows = conn.execute(
    "SELECT ticker, estimated_prob, actual_outcome, recorded_at "
    "FROM calibration "
    "WHERE estimated_prob IS NOT NULL AND actual_outcome IS NOT NULL "
    "AND recorded_at >= '2026-04-27'"
).fetchall()

by_fam: dict[str, tuple[list[float], list[int]]] = defaultdict(lambda: ([], []))
for tk, p, y, _ in rows:
    fam = family_key(tk)
    if not fam:
        continue
    try:
        pf = float(p); yi = int(y)
        if 0 <= pf <= 1 and yi in (0, 1):
            by_fam[fam][0].append(pf)
            by_fam[fam][1].append(yi)
    except (TypeError, ValueError):
        pass

# Cap A coefficient to flag pathological / step-function fits
PATHOLOGICAL_A = 50.0  # well above any sensible Platt — anything > 5 is suspicious

print(f"{'family':<14} {'n':>6} {'A':>14} {'B':>14}  "
      f"{'br_raw':>8} {'br_pf':>8} {'delta':>7}  verdict")
safe_to_enable = []
for fam in sorted(by_fam):
    ps, ys = by_fam[fam]
    if len(ps) < 30:
        continue
    seg = fams.get(fam)
    if seg is None:
        # No family-specific fit; would fall back to global
        verdict = "no fit"
        print(f"  {fam:<12} {len(ps):>6} {'-':>14} {'-':>14}  "
              f"{_brier(ps, ys):>8.4f} {'?':>8}  {verdict}")
        continue
    A, B = seg["A"], seg["B"]
    corrected = [_sigmoid(A * _logit(p) + B) for p in ps]
    br_raw = _brier(ps, ys)
    br_pf = _brier(corrected, ys)
    delta = br_pf - br_raw
    if abs(A) > PATHOLOGICAL_A:
        verdict = "PATHOLOGICAL — skip"
    elif br_pf < br_raw:
        verdict = "BETTER — safe to enable"
        safe_to_enable.append(fam)
    else:
        verdict = "regresses — skip"
    print(f"  {fam:<12} {len(ps):>6} {A:>14.4f} {B:>14.4f}  "
          f"{br_raw:>8.4f} {br_pf:>8.4f} {delta:>+7.4f}  {verdict}")

print()
print(f"Families safe to enable: {safe_to_enable}")

# What if we clamp A to [0.5, 5.0] before applying?
print()
print(f"=== With A-clip [0.5, 5.0] ===")
for fam in sorted(by_fam):
    ps, ys = by_fam[fam]
    if len(ps) < 30:
        continue
    seg = fams.get(fam)
    if seg is None:
        continue
    A_raw = seg["A"]
    B_raw = seg["B"]
    A = max(0.5, min(5.0, A_raw))
    # Rescale B proportionally so the curve passes through similar
    # midpoint; simplest: keep B as-is and just clamp A.
    B = B_raw
    corrected = [_sigmoid(A * _logit(p) + B) for p in ps]
    br_raw = _brier(ps, ys)
    br_pf_clip = _brier(corrected, ys)
    delta_pct = -100 * (br_pf_clip - br_raw) / br_raw if br_raw else 0
    print(f"  {fam:<14} A_raw={A_raw:>+12.4f} → A_clip={A:>+5.2f}  "
          f"raw={br_raw:.4f} → clip={br_pf_clip:.4f}  Δ%={delta_pct:>+5.1f}%")
