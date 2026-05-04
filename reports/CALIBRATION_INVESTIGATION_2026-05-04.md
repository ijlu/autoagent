# Calibration Investigation — 2026-05-04 (start-here for next session)

**Status:** `CALIBRATION_ENABLED=false` since 2026-04-27. Re-enabling will degrade signal quality with the current fitter. Fix the fitter before flipping the flag.

This doc is the focused entry point for the calibration fix. Read `reports/WEATHER_ENSEMBLE_STATE_2026-05-04.md` first for the broader system context if you haven't.

## The problem in one paragraph

When our ensemble says "99% YES," reality says ~59%. When it says "5% YES," reality says ~14%. Calibration is the layer that corrects this gap. Platt scaling is the chosen calibration technique — it fits a smooth S-curve `corrected_prob = sigmoid(A · raw + B)` from past settled data. **For us, the Newton-Raphson fitter collapses to a step function** (A coefficients in the millions) because our raw probabilities are bimodal — they pile up near 0 or near 1, with very little in the middle. A step function is what MLE wants on bimodal data. When applied to live predictions, the step function maps "97% YES" to 0% on the wrong side of its threshold, producing Brier 22-73% worse than the raw uncalibrated predictions.

## Live evidence (run this first)

```bash
ssh root@45.55.79.193 'cd /home/kalshi/autoagent && python3 tools/platt_fit_inspect.py'
```

Output today (2026-05-04):

```
global: A=88,608,105.66  B=-4,531,079.07  n=35,662
global brier: before=0.274265 after=0.305479

family            n          A           B    br_raw    br_pf  delta  verdict
KXBTC           825    774,432.93   198,989.90  0.7882   0.8206 +0.032  PATHOLOGICAL
KXETH            33      6,270.08     1,611.09  0.9604   1.0000 +0.040  PATHOLOGICAL
KXHIGHAUS      2215  6,222,625.41   584,940.79  0.3038   0.3219 +0.018  PATHOLOGICAL
KXHIGHCHI      2815  6,215,271.45   780,894.81  0.4102   0.4494 +0.039  PATHOLOGICAL
KXHIGHDEN      9187 38,652,563.11 -7,630,907.73  0.2158  0.2512 +0.035  PATHOLOGICAL
KXHIGHLAX      4774 14,419,866.37 -1,945,938.86  0.2518  0.2807 +0.029  PATHOLOGICAL
KXHIGHMIA      2756  8,365,739.31  1,001,910.31  0.2251  0.2573 +0.032  PATHOLOGICAL
KXHIGHNY       5785 13,993,235.33  2,466,532.06  0.3619  0.3969 +0.035  PATHOLOGICAL
```

A in the millions = step function. Sane Platt fits have A in [1, 5].

## Why simple A-clipping doesn't work

We tried clamping A to [0.5, 5.0] post-fit. Result:

```
KXHIGHMIA     A_raw=+8.4M → A_clip=+5.0   raw=0.225 → clip=0.389  Δ=-72.9%
KXHIGHAUS     A_raw=+6.2M → A_clip=+5.0   raw=0.304 → clip=0.494  Δ=-62.7%
KXHIGHDEN     A_raw=+38.6M → A_clip=+5.0  raw=0.216 → clip=0.305  Δ=-41.2%
```

Worse, not better. The reason: **A and B were co-optimized**. With A in millions, B was also fit in the millions. Clamping A to 5 but leaving B at +1,000,000 gives `sigmoid(5·raw + 1M)` which is `1.0` for any reasonable raw input. Naive clipping doesn't preserve the curve's intent — it just shifts the broken curve in a different broken direction.

## Three viable fixes (pick one)

### Option A: Bounded-Newton Platt (~1h, simplest)

Change `_fit_platt` in `bot/learning/calibration.py:99` to clamp **both** A and B during each Newton iteration, not after:

```python
A = max(0.5, min(5.0, A))
B = max(-10.0, min(10.0, B))
```

Add inside the iteration loop after each `A -= dA; B -= dB`. This forces the fitter to find the best smooth S-curve within the bounded box rather than running to infinity.

**Pros:** smallest diff. Existing tests should pass. Easy to A/B against current.
**Cons:** if the truly optimal Platt fit is OUTSIDE [0.5, 5] × [-10, 10], we're forcing a sub-optimal solution. (For our data the optimal IS inside, so this is fine.)

**Validation:** re-run `tools/platt_fit_inspect.py` after the change; A should be in [0.5, 5], Brier_after should be < Brier_raw for at least the weather families.

### Option B: Per-family isotonic regression (~2h, more flexible)

Drop the sigmoid assumption. Build a piecewise-constant monotonic mapping directly from data. The pool-adjacent-violators (PAV) algorithm is already in `_fit_isotonic` (calibration.py:146) but it's only computed for the global pool and never applied. Wire it per-family.

Sketch:
1. In `fit_calibration`, for each family with ≥30 settled rows, fit isotonic via existing `_fit_isotonic(fps, fys)` instead of (or in addition to) Platt.
2. Persist as `families[fam]["isotonic"]` in the cached curve.
3. In `apply_calibration`, when family has isotonic, route through that. Use binary search to find the right step.

**Pros:** no shape assumption, captures bimodal data naturally.
**Cons:** more parameters (one per data point post-PAV), thin tails could be noisy. The earlier global isotonic_shadow degenerated to 3 buckets — need to check if per-family PAV does the same or actually finds structure.

**Validation:** brier improvement per-family AND monotonic curve (PAV guarantees this; just sanity-check).

### Option C: Beta-distribution calibration (~3h, mathematically right)

Beta calibration fits a 2-parameter Beta CDF `corrected = BetaCDF(raw; alpha, beta)`. Specifically tailored for bimodal probability data — handles "clusters at extremes" without degenerating.

Math:
```
corrected = sigmoid(a · log(raw) + b · log(1-raw) + c)
```
(equivalent reparameterization). Three params instead of two. Fit via gradient descent or Newton (existing infrastructure).

**Pros:** literature-backed for bimodal calibration; nice analytic form.
**Cons:** new code path; 3 params makes the fitting more sensitive; need to validate against synthetic bimodal data first.

**Validation:** same as Platt — verify Brier improves per-family, sanity-check the curve shape isn't pathological.

## My recommendation: do Option A first, Option B as a follow-up

Reasoning:
- Option A is a **20-line diff** to a well-tested function. Lowest risk, fastest path to a non-broken Platt.
- If A produces meaningful improvement (Brier −5% or better per-family on weather), ship it as v1.
- Then implement B (isotonic) and A/B them against each other on the same evaluation set. If isotonic wins by another 5%+, ship that as v2.
- C (Beta) only if A and B both underperform expectations.

## Files to touch

| Path | Change |
|---|---|
| `bot/learning/calibration.py:99` (`_fit_platt`) | Add A,B clamping inside Newton loop (Option A) |
| `bot/learning/calibration.py:267` (`fit_calibration`) | If Option B: also produce per-family isotonic |
| `bot/learning/calibration.py:357` (`apply_calibration`) | If Option B: route through isotonic when present |
| `bot/config.py:CALIBRATION_ENABLED` | Flip to true once fits look sane (don't ship before verification) |
| `tests/test_calibration.py` | New: verify A is in bounds; new: verify Brier improvement; new: bimodal synthetic regression |
| `tools/platt_fit_inspect.py` | Already in repo; re-run as the verification step |

## Schema reference

`calibration` table (where the fitter reads from):
```sql
CREATE TABLE calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT,
    ticker TEXT,
    estimated_prob REAL,
    actual_outcome INTEGER,
    source_desc TEXT,
    n_sources INTEGER,
    bucket TEXT,
    alpha_id INTEGER
);
```

Total rows as of today: 35,662. Split:
- pre-2026-04-27 (v1 era): 7,271 — well-calibrated by accident (avg_prob 0.16, avg_actual 0.17, Brier 0.19)
- post-2026-04-27 (v2 era): 28,391 — overconfident at extremes (avg_prob 0.61, avg_actual 0.41, Brier 0.29)

The investigation should fit on the v2 era only — pre-fix data is from a broken pipeline and shouldn't influence the calibrator.

`kv_cache` key for the persisted curve: `calibration_curve_v2`. JSON shape:
```json
{
    "method": "platt",
    "A": float, "B": float,
    "n_samples": int, "fit_ts": float,
    "buckets_debug": {bucket_str: {n, avg_est, actual_rate, bias}},
    "isotonic_shadow": [[x_edge, y], ...],
    "families": {family: {"A": float, "B": float, "n_samples": int}},
    "brier_before": float, "brier_after": float
}
```

## Pinning tests for the next session to add

When you ship the fix, please add tests pinning:

1. **A and B bounds** — `_fit_platt` returns A in [0.5, 5] and B in [-10, 10] regardless of input data, including pathological bimodal.
2. **Brier improvement on weather families** — refit on the v2 era data, assert per-family Brier_after < Brier_raw with margin > 5%.
3. **No NaN/Inf in calibrated outputs** — synthetic test with extreme A,B inputs.
4. **Step-function regression** — synthetic bimodal input, assert the fit doesn't collapse to A > 50.

## How the broken-fitter slipped past

The original Platt fitter shipped 2026-04-15 against simulated probabilities that were uniform across [0, 1]. On uniform data the Newton fit converges to sane A,B values. The bimodality emerged organically as the v2 ensemble tightened σ — predictions clustered at extremes naturally, but no test caught the regime shift. The 2026-04-27 audit noted the global Platt curve had A=22M and disabled the application; the per-family path was never re-enabled.

## When you're done

1. Re-run `tools/platt_fit_inspect.py` — expect A in [0.5, 5], Brier_pf < Brier_raw per family
2. Set `CALIBRATION_ENABLED=true` in `.env` ONLY for weather families (gate by ticker prefix in `apply_calibration` if needed) — don't enable for KXBTC/KXETH (Brier 0.78/0.96 means uncalibratable)
3. Commit + deploy
4. Watch one cycle of live predictions; verify combined_v2 probabilities aren't getting catastrophically clamped
5. Update `reports/WEATHER_ENSEMBLE_STATE_*.md` to mark calibration as live

## Backlog adjacent to this work

- The `decision_log` and `alpha_backtest` tables capture per-source contributions; might be useful for fitting per-(family, source) calibration in the future
- The `weather_mm_shadow` table's `live_pnl_cents` is what we want calibration to ultimately improve
- Per-family Brier improvement target: 5-10% reduction would be a real win and would justify enabling for live trading
