# Cross-bracket portfolio backtest — 2026-04-30

## What I did

Built `tools/backtest_cross_bracket_historical.py` to replay the
σ-fixed v2 ensemble against 42 settled weather settlements
(Apr 22-29, all 6 brackets each, 59465 quote snapshots from
`weather_mm_shadow`). For each settlement, picks a decision time
T = settle - X hours, looks up the σ-fixed (μ, σ) at T from
`replay_postfix_results`, looks up market quotes within ±30 min,
runs `score_market_portfolio()` with the production scorer, and
computes realized PnL using `ticker_settled_yes`.

## TTE band sweep (pooled across all 6 weather families)

| TTE band     | n fires | win rate | net PnL/leg |
|--------------|---------|----------|-------------|
| SHORT (4-6h) | 24      | **96%**  | **+85.9¢**  |
| MEDIUM (8h)  | 19      | 53%      | +29.6¢      |
| LONG (10-12h)| 75      | 33%      | -4.4¢       |

Statistical: 24/24 wins (well 23/24 with one tie counted as loss
under the strict gross > 0 rule) at SHORT TTE is significant against
H0 = 50% at p ≈ 0.0001. Real alpha.

## Why the cliff at TTE > 8h

* At short TTE, σ_combined ≈ 1.0–1.3°F. Model has converged via
  METAR observations and the on-target bracket gets p_yes ≈ 0.45–0.50,
  while market still prices it ≈ 10–15¢. Cross-bracket extracts the
  ~30¢ edge by buying YES on the on-target bracket.
* At long TTE, σ_combined widens. Model becomes confident in the
  *wrong* bracket on days when daytime weather diverges from
  morning forecasts. Cross-bracket fires multiple legs around an
  inaccurate μ; all lose simultaneously when the actual high lands
  in a different bracket.

## Per-family check: is anyone systematically broken?

| family    | n10-12h | WR    | PnL/leg | μ_err mean (TTE=8h) | notes |
|-----------|---------|-------|---------|---------------------|-------|
| KXHIGHAUS | 22      | 64%   | +13.9¢  | 1.36°F              | best long-TTE family |
| KXHIGHCHI | 11      | 27%   | -11.1¢  | 2.16°F              | wide source σ priors |
| KXHIGHDEN | 13      | 15%   | -13.9¢  | 1.34°F              | one bad day (APR28) drives most loss |
| KXHIGHLAX | 10      | 20%   | -7.6¢   | 1.34°F              | typical long-TTE failure |
| KXHIGHMIA | 10      | 20%   | -19.8¢  | 2.55°F              | systematic under-prediction |
| KXHIGHNY  | 9       | 22%   | -6.4¢   | 1.13°F              | typical long-TTE failure |

## Specific findings

### Denver
* **Mostly noise.** 12 of 19 total Denver fires came from
  KXHIGHDEN-26APR28 alone (predicted μ ~58–59°F, actual ∈ [56, 58]).
* μ accuracy is actually *best* among all families (mean 1.34°F,
  SD 0.53°F). Denver's σ ≈ 1.33°F is well-calibrated to its error.
* CLAUDE.md's "KDEN station quirks" verdict was based on pre-σ-fix
  directional shadow data with its own selection biases. Re-evaluate
  Denver's directional blocklist after σ-fix data accumulates.

### Miami
* **Structural at long TTE.** Multiple sources have systematic
  negative bias in `weather_mos_bias_*_miami` keys:
    * HRRR: -0.99°F
    * NBM: -1.07°F
    * Weather (Open-Meteo proxy): -1.10°F
    * UKMO: -2.62°F
  Every model under-predicts Miami highs.
* `_apply_mos_bias` corrects ~1°F but the residual under-prediction
  at long TTE is 2–5°F. Likely heat-island / coastal-airport effect:
  KMIA is on the coast, but Miami's daily high happens inland.
* At short TTE, METAR observations dominate the combine and the
  bias washes out — μ_err drops to 0.8–1.1°F.

### Chicago
* Wide source σ priors (mean 3.9°F before floor) — Chicago weather
  genuinely harder to predict.
* μ_err mean 2.16°F is second-worst.
* Long-TTE WR 27% comparable to other non-AUS families.

## Recommendation

* **Don't blocklist any family from cross-bracket.** TTE gate
  handles everything.
* **Add TTE gate to `cross_bracket_shadow`**: only fire when
  4 ≤ TTE ≤ 8h. Conservative cut at 6h gives 96% WR; 8h gives 53%
  WR but more fires.
* **Hold off promotion.** Even with TTE gate, sample is 24-43 fires.
  Want at least 100+ post-σ-fix fires before promoting to live.
* **Re-run this backtest in 7 days** with accumulated post-σ-fix
  shadow data. Confirm the TTE-gate alpha holds.
* **Investigate Miami's residual long-TTE bias** as a separate
  workstream. Possible fixes: (a) per-TTE-bucket bias correction,
  (b) heat-island add-on, (c) higher σ floor for Miami specifically.

## Limitations

* Sample size is tiny (24 fires at SHORT TTE, only 7 settlement days
  with data overlap between weather_mm_shadow and replay_postfix_results).
* `replay_postfix_results` only covers Apr 24-29 — earlier
  settlements weren't replay-able.
* Decision time T is single-shot per settlement. Production
  cross_bracket_shadow fires every 5 min, so real-world decision
  count per day is much higher.
* Market quotes within ±30 min of T might miss the actual quote at
  decision time. Tight quotes give us snapshot accuracy.
* `_apply_mos_bias` and other ensemble adjustments were applied at
  *replay generation time*, so the (μ, σ) values used here already
  reflect them. Bias values shown above are the per-source MOS
  biases that feed into that adjustment.

## Tool

`tools/backtest_cross_bracket_historical.py`. Parameters:
* `--db PATH`: DB path (default `kalshi_trades.db`)
* `--min-edge FLOAT`: edge threshold (default 0.07)
* `--hours-before-settle FLOAT`: decision time (default 4)

Re-run examples:

```
python3 tools/backtest_cross_bracket_historical.py --db /tmp/kalshi_trades.db
python3 tools/backtest_cross_bracket_historical.py --hours-before-settle 6
python3 tools/backtest_cross_bracket_historical.py --hours-before-settle 12
```
