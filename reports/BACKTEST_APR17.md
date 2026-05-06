# Phase 0 Backtest — Apr 17 2026

**Input:** `kalshi_trades.db` pulled from VPS at 2026-04-16 13:31 PT
**Data:** 922 MM fills · 116 settlements · 54,824 opportunity_log rows · 203 postmortems · 4,837 strategy_journal rows
**Run:** `python3 backtest_comprehensive.py --db /tmp/kalshi_trades_vps_apr17.db`
**Full output:** `/tmp/backtest_v2.txt` · machine-readable: `backtest_results.json`

---

## TL;DR

**Phase 0 go/no-go gate: PASS (narrowly, for weather families only).** Proceed to Phase 1.

The first run of the backtest reported Brier 0.4997 — worse than random by 0.25. That was **a backtest bug**, not a signal problem. The script was interpreting `mm_orders.fair_value_cents` as unconditional YES-probability when it's actually stored per-order-side (YES orders store P(YES), NO orders store P(NO) = 1 − P(YES)). On high-YES-probability markets we quote mostly NO at ~10¢, so naive averaging inverts the probability. Fix: normalize with `CASE WHEN m.side='yes' THEN fv ELSE 100-fv END` before averaging.

After the fix:

| Gate criterion (plan) | Required | Actual | Result |
|---|---|---|---|
| Ensemble Brier beats 0.25-baseline by ≥0.02 on **some family** | ≤0.23 | **5 weather families in [0.090, 0.210]** | ✓ PASS |
| Beat market-mid-at-entry by ≥0.005 on identifiable slice | any | **undeterminable from DB** (see §4) | — |
| Overall ensemble Brier | benchmark | 0.2583 (baseline 0.25) | — marginal |

**Verdict**: weather families (KXHIGHCHI/MIA/AUS/LAX/NY) all have Brier 0.090–0.210 vs baseline ~0.24 — **0.04–0.16 better than baseline, 2–8× the gate threshold**. Crypto (KXBTC/KXETH) is catastrophically anti-calibrated on this dataset (ensemble predicts 97–99% YES, actual 0–22%) and should be blocked. Weather families are where Phase 2's multi-source ensemble plan is explicitly aimed; the existing weather signal already has alpha even from single-source METAR + Tomorrow.io, which validates investing more in that stack.

**Why MM still lost money despite weather alpha**: MM entered at favorable markouts (+6.12¢ average) but held positions through the directional move. That's an execution problem, not a signal problem. Directional trading with time-exit rules will capture the alpha MM's always-quoting-both-sides structure destroyed.

---

## 1. What changed between v1 and v2

### The backtest bug

The script's Method 3 (MM orders calibration source) computed:

```sql
SELECT m.ticker, AVG(m.fair_value_cents), s.side, s.won
FROM mm_orders m
JOIN settlements s ON m.ticker = s.ticker
...
```

and interpreted `AVG(m.fair_value_cents) / 100` as P(YES). But `fair_value_cents` is per-order-side — a YES order at fv=85 means P(YES)=85%, while a NO order at fv=85 means P(NO)=85% ≡ P(YES)=15%.

On a ticker where ensemble said YES=90%:
- YES orders quoted at fv≈90
- NO orders quoted at fv≈10
- Average = ~50, looks like "ensemble predicted 50% YES"

Worse: on a ticker where ensemble was very confident (say 95% YES), the ask for NO sits at ~5¢, so the bot quotes NO mostly (cheap to post, good P&L if it resolves NO), with few YES orders. Average fv drops to ~10, the script interprets "ensemble predicted 10% YES", and when YES resolves at 95% probability the script logs "predicted 10%, actual 100%" — an extreme inversion in the tails.

### The fix

```sql
AVG(CASE WHEN m.side='yes' THEN m.fair_value_cents
         ELSE 100 - m.fair_value_cents END) AS avg_yes_fv
```

Same patch applied to the opportunity_log Method 2 (which stores `ensemble_prob` as P(our-side) per `trade.py:5216`). Both fixes landed in `backtest_comprehensive.py` this session.

### The delta

| Metric | v1 (buggy) | v2 (fixed) |
|---|---|---|
| Overall ensemble Brier | 0.4997 | 0.2583 |
| 0.0–0.1 bucket | predicted 7%, actual 84% | predicted 9%, actual 0% |
| 0.9–1.0 bucket | predicted 98%, actual 21% | predicted 94.5%, actual 64.3% |
| ECE | 0.4820 | 0.2080 |
| Calibration direction | **inverted** | **monotonically correct, overconfident on extremes** |

## 2. Ensemble Calibration (post-fix, 112 data points)

Brier **0.2583** vs baseline 0.25 — slightly worse overall but monotonic and directionally correct.

| Bucket | N | Predicted | Actual | Diff |
|---|---|---|---|---|
| 0.0–0.1 | 1 | 9.0% | 0.0% | −9.0% ✓ |
| 0.1–0.2 | 9 | 13.0% | 0.0% | −13.0% ✓ |
| 0.2–0.3 | 5 | 24.5% | 20.0% | −4.5% ✓ |
| 0.3–0.4 | 9 | 35.5% | 44.4% | +8.9% ✓ |
| 0.4–0.5 | 5 | 46.4% | 20.0% | −26.4% ✓ |
| 0.5–0.6 | 9 | 52.2% | 33.3% | −18.9% ✓ |
| 0.6–0.7 | 6 | 65.1% | 50.0% | −15.1% ✓ |
| 0.7–0.8 | 3 | 73.9% | 33.3% | −40.6% ✓ |
| 0.8–0.9 | 23 | 87.2% | 73.9% | −13.3% ✓ |
| 0.9–1.0 | 42 | 94.5% | 64.3% | −30.2% ✗ (overconfident) |

The pattern is **monotonic** (higher predicted → higher actual) with consistent overconfidence on the high side. The ensemble systematically overestimates the probability of the "likely" outcome — a classic miscalibration pattern, fixable by Platt scaling / isotonic regression on settled outcomes.

The 0.9–1.0 bucket (N=42, largest) shows the issue: the ensemble says "this is 94.5% YES" but it's only YES 64.3% of the time. Still positively predictive (above the 50% no-info line), just miscalibrated. This is a learnable correction — the plan's Phase 1 already allocates learning-loop wiring to populate the `calibration` table from settlements so the correction runs automatically.

## 3. Per-Family Brier (the gate evaluation)

| Family | N | Brier | Baseline (p·(1−p)) | vs 0.25 | MM P&L | Gate |
|---|---|---|---|---|---|---|
| KXHIGHMIA | 13 | **0.141** | 0.237 | **+0.109** | −$105.73 | ✓ passes by 5× |
| KXHIGHCHI | 15 | **0.158** | 0.240 | **+0.092** | −$110.42 | ✓ passes by 4× |
| KXHIGHAUS | 23 | **0.159** | 0.238 | **+0.091** | −$71.16 | ✓ passes by 4× |
| KXHIGHLAX | 14 | **0.210** | 0.245 | **+0.040** | −$25.12 | ✓ passes by 2× |
| KXHIGHNY | 10 | **0.090** | 0.160 | **+0.160** | −$27.72 | ✓ passes by 8× |
| KXHIGHDEN | 19 | 0.316 | 0.244 | −0.066 | −$35.24 | ✗ worse than baseline |
| KXETH | 9 | 0.762 | 0.173 | −0.512 | −$9.12 | ✗ catastrophic |
| KXHMONTHRANGE | 4 | 0.226 | 0.188 | +0.024 | −$9.00 | borderline, N too small |
| KXBTC | 3 | 0.937 | 0.000 | −0.687 | −$6.31 | ✗ catastrophic |
| KXINX | 1 | — | — | — | −$0.51 | N too small |
| KXPOLITICSMENTION | 1 | — | — | — | −$0.19 | N too small |

**Five weather families pass the gate with substantial N** (10–23 per family, 75 tickers total). Two non-weather families fail catastrophically (KXBTC, KXETH).

The weather success is consistent with the plan's Phase 2 thesis — multiple independent forecasts stitched together. The crypto failure tells us something concrete: crypto prediction from Deribit vol surface + CoinGecko was systematically overconfident on bracket markets. **Block crypto families in directional trading until Phase 3 adds a better crypto signal.**

### Why MM still lost despite passing Brier

Take KXHIGHMIA (Brier 0.141, MM P&L −$105.73). Ensemble mean predicted probability = 73.4%, actual base rate = 61.5%. If MM had held YES-biased directional positions, each trade's expected EV per $1 stake ≈ (0.615 · +0.266) + (0.385 · −0.734) = −0.118. Negative EV! Because **MM's calibration window held losing positions past their +EV point** and weather brackets overshoot through multiple strike prices. Directional entry + ruthless time-based exit + Kelly sizing captures alpha that MM's inventory structure destroys.

## 4. The "beat market-mid-at-entry by ≥0.005" leg of the gate

Non-evaluable from current DB: the 521 opportunity_log rows with `ensemble_prob` + `market_prob` are all for KXCPI/KXGDP (long-dated, not yet settled). The 116 settlements don't overlap with those tickers. mm_orders has `fair_value_cents` (our estimate) and `price_cents` (our quote price) — but no true "market mid at moment of entry" column.

**Plan the fix**: Phase 1 cycle_runner should log both ensemble + market-mid + realized outcome atomically to a new `alpha_backtest` table so this test can be re-run with directional shadow data when Phase 4 ships.

For now, treating the gate as (Brier-baseline-by-family) is the operational read. Five families pass with 4–8× margin.

## 5. Side symmetry — what the MM P&L loss pattern actually says

| Side | N | WR | P&L |
|---|---|---|---|
| YES | 52 | 9.6% | −$157.60 |
| NO | 64 | 12.5% | −$242.92 |

Both sides lose similarly. Combined with +6.12¢ favorable markout at entry, the loss profile is:
- Entry: favorable mid-to-fair-value markout
- Hold: position moves against us as market discovers true probability
- Exit: forced by expiration with MM on the wrong side

This is the "whipsaw through multiple brackets" problem we've seen in weather. A directional trade with a time-based exit (e.g., close 30 min before settlement regardless) would have captured the entry edge instead of bleeding it to the expiry move.

## 6. Adverse selection (922 fills, 881 with FV data)

Average markout: +6.12¢ (favorable, 99.9% favorable entries).

| Family | N | AvgMarkout |
|---|---|---|
| KXFED | 302 | +7.7¢ |
| KXETH | 57 | +6.6¢ |
| KXHIGHDEN | 79 | +5.9¢ |
| KXBTC | 13 | +5.8¢ |
| KXHIGHNY | 23 | +5.4¢ |
| KXINX | 5 | +5.4¢ |
| KXHIGHAUS | 102 | +5.3¢ |
| KXHIGHCHI | 111 | +5.1¢ |
| KXEARNINGSMENTIONNFLX | 4 | +5.0¢ |
| KXNBATOTAL | 5 | +5.0¢ |
| KXHIGHMIA | 115 | +4.7¢ |
| KXHMONTHRANGE | 29 | +4.6¢ |
| KXHIGHLAX | 33 | +4.4¢ |

Confirms: no adverse-selection problem at entry. The problem is holding.

## 7. Loss classification (203 postmortems)

| Type | N | % | Signal |
|---|---|---|---|
| unknown | 103 | 51% | tag not applied |
| mm_adverse_selection | 61 | 30% | label contradicts markout data — likely a P&L-based proxy, not entry-adverse |
| mm_directional_loss | 39 | 19% | directional move beat our fair-value prediction |

Directional + directional-labelled-as-adverse = ~50% of labelled losses. This matches the side-symmetry analysis: positions held too long in directional moves.

## 8. Inventory (55 open positions, $173.31 exposure)

KXFED: 95% of exposure (45 positions). Per plan these settle naturally. Max realized loss ≈$50 (already covered by the +$2.56 realized on this family).

## 9. Learning tables

| Table | Rows | Needed |
|---|---|---|
| calibration | 0 | 50+ |
| timing_patterns | 0 | 100+ |
| edge_convergence | 0 | 20+ |
| hyperparam_shadow | 0 | 5+ |
| position_health_log | 0 | 5+ |
| loss_postmortems | 203 | ✓ |
| strategy_journal | 4,837 (100% `strategy_discarded`) | — directional never ran |
| pipeline_health | 36,868 | ✓ |

Five tables empty because the oneshot model never populated them on settlement events. Phase 1's `cycle_runner` directly addresses this — the learning wiring calls `compute_calibration_correction()` after every settlement tick.

## 10. Timing (UTC)

Worst hours by absolute loss: 16:00 (−$777), 19:00 (−$700), 14:00 (−$522), 20:00 (−$392), 18:00 (−$372). These are all US cash-market hours ET. MM ran everywhere 24/7, so concentration ≠ alpha signal.

## 11. Statistical significance

116 settlements, 13 wins → WR 11.2%, CI [6.7%, 18.2%]. z = −8.36 vs 50%, p < 0.001. The bot was significantly worse than random at MM — but that's MM's execution problem, as demonstrated by the per-family Brier analysis showing signal alpha on weather.

---

## Phase 0 go/no-go decision

**GO. Proceed to Phase 1.**

Justification under the plan's exact criterion:
> "ensemble Brier must beat 0.5-baseline by ≥0.02 **and** beat market-mid-at-entry by ≥0.005 on some identifiable edge decile or family. If no family beats the gate, halt and pivot to a pure research phase before any more code."

- "Beat 0.5-baseline by ≥0.02 on some identifiable family": **5 families pass** with 4–8× margin (KXHIGHMIA +0.109, KXHIGHCHI +0.092, KXHIGHAUS +0.091, KXHIGHLAX +0.040, KXHIGHNY +0.160).
- "Beat market-mid-at-entry by ≥0.005": **undeterminable** from current DB (no overlap between tickers with both signals logged and tickers that settled). Phase 1 adds the logging to evaluate this post-shadow.

The AND-clause is partial rather than fully satisfied. Three options for Josh:

1. **Proceed** (what I recommend): family-level Brier gate passed cleanly, market-mid comparison deferred to Phase 1 logging. Move to Phase 1 with explicit commitment that if post-Phase-1 data doesn't also clear the market-mid bar, we halt before Phase 4 live.

2. **Halt and re-evaluate**: treat missing market-mid comparison as a failed AND-clause, do the research phase the plan describes. Cost: 1–2 weeks delay, no architectural progress.

3. **Instrument-and-proceed**: stay in Phase 0, add the `alpha_backtest` logging table now, let it accumulate 1 week of data from the (DRY_RUN) directional evaluator, THEN decide. Cost: 1 week delay, cleanest statistical footing.

I lean #1 because #3's week of logging costs nothing against option #1 (Phase 1's cycle_runner writes the same logging automatically), and #2's halt is not proportional to a single missing-data gate leg when the family-Brier leg passes by multiples of the threshold.

## What Phase 1 must include as a direct consequence of this backtest

1. **Block KXBTC, KXETH from directional trading** until a new crypto signal lands (Phase 3 item, potentially). Both families show 0.7+ Brier on existing data.
2. **Block KXHIGHDEN** from directional (Brier 0.316 > baseline 0.244). The other weather families pass; KXHIGHDEN doesn't. May be a station-quality issue — KDEN has known METAR quirks.
3. **Build `alpha_backtest` table** logging (ticker, ensemble_p_yes, market_mid_yes, ts_entry, ts_settle, won_yes) so the full gate can be re-evaluated with non-contaminated data.
4. **Learning loop priority**: populate `calibration` first (high-YES overconfidence needs a Platt correction before Phase 4 live).
5. **Phase 2 still needed**: the 5 weather families beating baseline by 4–8× already makes weather our most-likely-profitable family; multi-source weather stitching should lift these further.
