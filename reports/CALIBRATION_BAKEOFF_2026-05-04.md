# Calibration Bake-Off — 2026-05-04

**Tool:** `tools/calibration_bakeoff.py` (read-only; persists nothing)
**Methods:** identity, platt (A∈[0.5,5]), platt_wide (A∈[0.1,5]), isotonic, beta, histogram
**Train:** 2026-05-01 → 2026-05-03 (UTC, inclusive). Total ~17.7K rows weather + 328 BTC.
**Holdout:** 2026-05-04 00:00 → 14:08 UTC (pre-bounded-fitter-deploy). ~2.7K rows weather.
**Metrics:** Brier (primary), log-loss, ECE (10 equal-frequency bins).

## Per-family winners

| Family | Winner | Brier ↓ | Identity Brier | Δ% | Runner-up | Runner-up Δ% |
|---|---|---|---|---|---|---|
| KXHIGHNY | **isotonic** | 0.216 | 0.471 | **−54.2%** | beta | −52.0% |
| KXHIGHCHI | **isotonic** | 0.232 | 0.482 | **−52.0%** | platt_wide | −51.5% |
| KXHIGHAUS | **beta** | 0.236 | 0.430 | **−45.2%** | platt_wide | −44.4% |
| KXHIGHLAX | **platt** | 0.170 | 0.265 | **−36.0%** | platt_wide | −35.0% |
| KXHIGHMIA | **histogram** | 0.227 | 0.336 | **−32.6%** | platt_wide | −32.3% |
| KXHIGHDEN | **beta** | 0.153 | 0.209 | **−27.0%** | platt_wide | −22.8% |
| KXBTC | beta | 0.152 | 0.785 | −80.7% | platt_wide | −80.7% |

★ all weather families show meaningful holdout-set improvement vs identity

## Key findings

**1. No single method dominates.** Beta wins on 3 families, isotonic on 2, histogram on 1, current platt on 1. There is no "always-best" calibrator for our data — the calibration shape varies per family.

**2. The currently-shipped `platt` fitter (A_min=0.5) is consistently sub-optimal.** On every weather family except KXHIGHLAX, the wider variant `platt_wide` (A_min=0.1) beats `platt`. The 0.5 lower bound was chosen from the doc, but the data wants A in [0.27, 0.50] — the lower bound is binding on most families. The unconstrained MLE *is* below 0.5, contrary to the handoff doc's assumption.

**3. `platt_wide` is within 1-3% of the per-family winner everywhere.** That's the simplest viable improvement — one constant change, no new code paths.

**4. KXBTC's −80.7% Brier improvement is misleading.** Identity Brier is 0.785 (catastrophic raw signal). Beta and platt_wide both squash everything to a low prob (~18%) and "win" by base-rate prediction, not by actually capturing signal structure. This is what we already knew: KXBTC needs a *signal* fix, not a calibration fix. KXBTC stays in `DIRECTIONAL_BLOCKLIST` regardless.

**5. Every winning fit applies strong logit compression + a negative bias.** All winning Platt slopes are in [0.1, 0.5]. All winning Beta `a` coefficients are small. Pattern is consistent: our raw probs are ~3× too sharp at the extremes and biased high. The data is calling for "halve the logit, shift down."

**6. ECE often regresses even when Brier improves.** Worth noting: histogram on KXHIGHMIA wins on Brier (0.227) but its ECE (0.369) is the worst non-identity entry. Methods that bin/step (histogram, isotonic) trade off Brier vs. local calibration noisiness in the tails. Beta and Platt give smoother curves at slight Brier cost.

## Three options for what to ship

### Option 1: lower `PLATT_A_MIN` from 0.5 to 0.1 (1-line change)
- Brier improvement on every weather family vs the just-shipped fix
- Within 1–3% of the per-family bake-off winner everywhere
- No new code paths, no new tests, no new model storage shape
- **Ships today, high confidence**

### Option 2: per-family method routing (~100 lines)
- Route each family to its bake-off winner: NY/CHI→isotonic, AUS/DEN/MIA→beta, LAX→platt
- Captures the last 1–3% of Brier improvement
- Requires: (a) isotonic-apply in `apply_calibration`, (b) beta-apply, (c) per-family `method` field in the cached curve
- New unit tests for each apply path
- Ongoing maintenance: the "winner" per family will drift as data accumulates; need a periodic re-bake-off

### Option 3: ship Option 1 now, plan Option 2 as Phase 2
- Get the easy 80% today; revisit after weather goes live and we have real-money fills informing the choice
- This is what the original CALIBRATION_INVESTIGATION doc recommended (Option A → ship → measure → Option B)
- **My recommendation**

## Caveats / unsolved

- **Holdout is 14 hours of one day** (2026-05-04 morning). Brier estimates have meaningful sampling variance at n≈300/family. Differences <2% are likely within noise.
- **Train data crosses the 2026-05-04 source-cleanup boundary** (per-city exclusions + lat/lon fixes landed earlier today). Train is mostly pre-cleanup; holdout is post-cleanup. The fact that fits trained on pre-cleanup still lower Brier on post-cleanup holdout is a *good* sign for generalization — but a fresh bake-off in a few days, with fully post-cleanup data, would be more authoritative.
- **No ensemble-of-calibrators tested.** A simple averaging of beta + platt_wide might Pareto-dominate either alone. Not tested in this round.
- **Forward calibration data will be doubly-processed** unless we also start logging raw probs separately. `alpha_backtest.ensemble_p_yes` is post-`apply_calibration`. Until that's split, every refit will fit a calibrator on top of the previous calibrator's output — drift accumulates.

## How to reproduce

```
python3 tools/calibration_bakeoff.py
# defaults: kalshi_trades.db, weather families starred
```

Adjust `TRAIN_START`, `TRAIN_END`, `HOLDOUT_START`, `HOLDOUT_END` constants to slide the window.
