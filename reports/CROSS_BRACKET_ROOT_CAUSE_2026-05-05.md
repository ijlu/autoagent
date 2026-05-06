# Cross-Bracket Loss Root Cause — Investigation Findings (2026-05-05)

**Status:** FINDINGS for review. Root cause identified.
**Trigger:** Morning audit showed 0% win rate, -$8.43 net over 5 settlements since 2026-05-03 go-live; diagnostic flagged "shadow significantly overstating, pause expansion."
**Hypotheses tested:** H1 forecast bias, H2 decision-vs-settle drift, H3 side-selection bug.

> **2026-05-05 update (post-Josh feedback):**
> - **Live cross-bracket stays running.** Bleeding is small ($5/day cap) and we want live data. Not pausing.
> - **TTE-as-axis is the wrong frame.** What actually matters is **local solar time relative to the diurnal peak**. "Long TTE" in this analysis means "decisions made before the day's diurnal cycle even occurs" — that's a meaningful state. "Short TTE" means "decisions after peak when the high is already nearly determined" — also meaningful. But raw hours-to-expiry conflates these because settlement time is fixed at end-of-night while peak time varies by city.
> - The fix path below has been re-framed in LST/diurnal-phase terms. The old TTE-gate language is preserved only where it describes existing code; new fixes are LST-aware.
> - This document is now superseded for Track A planning by [reports/ENSEMBLE_DEEP_DIVE_2026-05-05.md](ENSEMBLE_DEEP_DIVE_2026-05-05.md) — per-market, per-source first-principles analysis.

---

## TL;DR

**The model is dramatically overconfident before the diurnal cycle has occurred, and predictions swing wildly between pre-peak and post-peak phases.** The cross-bracket strategy is taking large positions during the pre-peak window (when the day's high is genuinely uncertain) using σ that's 3-4× too small. When the model corrects after peak (μ shifts 4-7°F, σ triples to reflect actual NWP error at that horizon), the early positions are stranded on the wrong side of the truth.

H1 confirmed. H2 confirmed (and is the same fish as H1 — symptom of underconfident-σ-during-pre-peak-phase). H3 disproved (math is right), but uncovered a related issue: **the TTE gate only applies to live trades, not shadow logging** — meaning the shadow-vs-realized P&L gap is partly inflated by pre-peak-phase shadow noise that would never have fired live.

---

## The data

Audit covers 5 settled cross-bracket positions since 2026-05-03 (cross-bracket go-live):

| Ticker | Settled | Settlement | Bot bet | Result |
|---|---|---|---|---|
| KXHIGHNY-26MAY03-B59.5 | high in [59,61) | YES | NO at avg 43¢ | Lost (NY high was 60°F) |
| KXHIGHNY-26MAY03-B61.5 | high NOT in [61,63) | NO | NO at avg 73¢ | **Won** (one of two)|
| KXHIGHNY-26MAY04-B72.5 | high in [72,74) | YES | NO at avg 45¢ | Lost (NY high was 73°F) |
| KXHIGHAUS-26MAY04-B82.5 | high in [82,84) | YES | NO at avg 70¢ | Lost (AUS high was 83°F) |
| KXHIGHLAX-26MAY04-B68.5 | high in [68,70) | YES | NO at avg 73¢ | Lost (LAX high was 69°F) |

**4 of 5 losses are the same pattern:** bot took NO at high price on the bracket that contained the actual high. In every case, the model assigned <10% probability to that bracket.

---

## H1 — Forecast bias (CONFIRMED, but not the way I expected)

The combined v2 ensemble's μ at decision time near settlement was actually **close to the truth**:

| City May 4 | Model μ (T-5h) | Model σ (T-5h) | Actual high | Implied P(actual bracket) |
|---|---|---|---|---|
| NY | 72.2°F | 2.38 | ~73°F | ~31% |
| AUS | 82.4°F | 4.06 | ~83°F | ~19% |
| LAX | 67.7°F | 3.61 | ~69°F | ~20% |

**At T-5h the model knew. Implied per-bracket P(YES) for the actual settled bracket was 19-31%** — not great, but not the catastrophic <10% the bot logged.

So why did the bot log p_yes ~5-7% on these brackets? **Because most of the bot's decisions came from MUCH earlier in the day, when the model was confidently wrong.**

### Smoking gun — KXHIGHNY-26MAY04-B72.5 forecast evolution

| Time | TTE | μ (°F) | σ | Implied P([72,74)) | What this means |
|---|---|---|---|---|---|
| 2026-05-03 14:55 | 38h | **64.52** | **1.37** | ~0% | Model: "99% confident high will be 62-67°F" |
| 2026-05-03 15:06 | 38h | 66.67 | 1.37 | ~0.1% | Same |
| 2026-05-03 15:53 | 37h | 69.65 | 1.31 | ~4% | Drifting up, σ still tiny |
| 2026-05-03 16:19 | 37h | 66.22 | 2.83 | ~1.5% | σ doubled, μ dropped |
| 2026-05-04 18:02 | **12h** | **73.94** | **5.33** | **~30%** | Model corrects: μ jumps 7°F, σ quadruples |
| Actual settlement | 0h | — | — | — | High was 73°F |

The model's σ at TTE 38h was **1.37°F** — claiming 99% confidence in a 5°F window. The actual realized error was **~9°F** (predicted 64.5, actual 73). That's 6.5σ outside the model's "99% confidence" interval. The model's σ at long TTE is not just wrong, it's catastrophically under-calibrated — likely a 3-5× underestimate.

**The mechanism of loss:** at TTE 38h with σ=1.37, the model assigns essentially 0% probability to brackets more than 3°F from μ. The cross-bracket strategy looks at a market quoting these "0% brackets" at 30-40¢ and sees a 60-70¢ edge on buying NO. It buys NO at 65¢. Then the model corrects and the actual high lands in one of those "0%" brackets. Catastrophic loss.

---

## H2 — Decision-vs-settle drift (CONFIRMED, same root cause as H1)

The May 4 NY example above is itself the H2 evidence: μ swung from 64.52 → 73.94 between T-38h and T-12h, a 7°F move with σ also moving from 1.37 → 5.33.

This is not "drift" in the sense of the world changing. It's the model **finally getting good information** as the forecast horizon shrinks (HRRR runs every hour out to 18h; ECMWF/GFS update every 6h with longer ranges; METAR observations only become useful at TTE < 8h). At long TTE, the model is essentially regressing to a poor prior and reporting tiny σ as if that prior were certain.

**This is exactly what σ should be modeling — and it isn't.**

---

## H3 — Side-selection bug (DISPROVED, but uncovered something else)

The math in `bracket_portfolio.py::_decide_leg` ([line 90-145](../bot/scoring/bracket_portfolio.py#L90)) is correct:
- buy YES if `p_yes - yes_ask/100 ≥ min_edge`
- buy NO if `(1 - p_yes) - (100 - yes_bid)/100 ≥ min_edge`

No flip, no off-by-one. Side selection is fine.

But while verifying, I found a different issue: **the TTE gate only applies to LIVE decisions, not SHADOW logging.**

[bot/daemon/cross_bracket_shadow.py:373](../bot/daemon/cross_bracket_shadow.py#L373) gates LIVE on `CROSS_BRACKET_MIN_TTE_HOURS=3, MAX=7`. But the alpha_backtest table shows shadow decisions logged at TTE 5h all the way to 45h:

```
TTE bucket | n decisions | avg p_yes
   5       |     6       | 0.230
  10       |   117       | 0.216
  20       |   160       | 0.087
  30       |   206       | 0.152
  40       |    34       | 0.056
```

**~85% of cross-bracket shadow decisions are logged outside the live-eligible window.** This means:
1. The diagnostic's shadow-vs-realized comparison is comparing apples (live actually-fillable decisions in the 3-7h window) to oranges (shadow logging across the entire 5-45h range, where forecasts are much worse).
2. The "shadow predicted +$12.92, realized -$8.43" gap likely overstates the problem on real live decisions — but the underlying root cause (under-calibrated σ at long TTE) still bites the live window too.

### Counter-question: why are some live trades happening?

Looking at the 1-contract NY-B72.5 fill at 2¢: that price implies yes ~98¢ at fill time, which only happens very late when the high is almost determined. This is consistent with either a defensive add or a synthetic-sell exit, not a fresh entry. Worth verifying in fills_ledger which path each of the 5 live fills came from.

---

## Root cause synthesis

The cross-bracket strategy as designed assumes the v2 ensemble's σ is well-calibrated. **It is not.** At TTE > ~8-10h, the combined v2 σ is severely under-estimated (1-2°F when reality is 4-6°F). This causes:

1. The model assigns near-zero probability to brackets just a few °F from its (often-wrong) μ.
2. Cross-bracket sees these near-zero brackets quoted by the market at 25-40¢, computes huge edge_no, and buys NO at 60-75¢.
3. When the model corrects later (μ moves 4-7°F as forecasts firm up), the actual high lands in one of those "near-zero" brackets.
4. We lose 60-75¢ per contract.

**This is not an MIA-specific bug, an LST-clamp issue, or a side-selection bug. It is a fundamental calibration failure of the v2 σ at long TTE.**

The today's session "Fix calibration layer issues" — was probably touching the right problem but addressing it via Platt calibration (which fits per-bracket-probability calibration after the Gaussian projection). That can't fix this. The bug is upstream: the **Gaussian σ itself is wrong**, so even a perfectly Platt-calibrated bracket probability can't recover. Garbage in, calibrated-garbage out.

---

## Fix path — superseded

The original Tier 1/2/3 plan below is preserved for record but **superseded** by the LST-/diurnal-phase-framed plan in [reports/ENSEMBLE_DEEP_DIVE_2026-05-05.md](ENSEMBLE_DEEP_DIVE_2026-05-05.md). The new plan re-frames every "TTE-bucketed σ inflation" idea as "diurnal-phase-bucketed σ inflation per city" because:
- A 12h-TTE decision in NY at 17:00 LST (post-peak) is fundamentally different from a 12h-TTE decision in AUS at 07:00 LST (pre-peak), even though TTE is identical.
- Settlement time is anchored to end-of-night LST; the day's high is determined by mid-afternoon LST. So *time-since-peak* is the natural axis, not *time-to-settle*.

Per Josh's call: live posting continues, no pause. The data is worth $5/day in tuition.

### Original Tier 1-4 (preserved for reference, do not execute as written)

<details>
<summary>Click to expand</summary>

**Tier 1.1:** Tighten live TTE gate to 2-5h. → Replaced by LST window in deep-dive plan.

**Tier 1.2:** Cap NO leg price at 50¢. → Still useful as a guardrail; carried into deep-dive plan.

**Tier 1.3:** Pause cross-bracket live until σ recalibration ships. → **Rejected by Josh.** Keep live for the data.

**Tier 2.1:** Per-source σ-vs-TTE inflation. → Reframed as per-source σ-vs-LST-hour in deep-dive plan.

**Tier 2.2:** Combined σ post-combine inflation by TTE. → Reframed as by diurnal-phase.

**Tier 2.3:** TTE-aware σ floor. → Reframed as LST-phase-aware floor.

**Tier 3.1:** Apply TTE gate to shadow logging. → Carry-over: apply LST window to shadow too, OR add `lst_phase` column to alpha_backtest for clean filtering.

</details>

---

## What changed about the city expansion plan

**Track B (city expansion) stays paused until σ recalibration ships.** Adding cities to a strategy with broken σ is just losing money in more markets.

**Track A's known-issue list is now obsolete:**
- "MIA past-peak clamp" — minor compared to σ calibration
- "σ floor too generous" — wrong direction; σ floor is too LOW at long TTE, not too high anywhere
- "fills_writer client_order_id NULL" — still worth fixing for attribution but unrelated to losses

**The new Track A is:** Tier 1 (stop bleeding) + Tier 2 (fix σ calibration). This becomes the gating work for everything else.

---

## Open questions for Josh

1. **Pause live cross-bracket?** I recommend yes (Fix 1.3). Risk-of-doing-nothing is small ($5/day cap), but the strategy is producing biased data. 7 more days of live = -$8 more net + tainted shadow data. Better to pause, fix σ, re-validate.
2. **σ recalibration scope.** Tier 2 is real engineering work — backfill required for per-source σ-vs-TTE empirical fit, ensemble code change, regression test on historical data. Estimate 2-3 sessions. Sound right, or want a smaller-scope version (e.g., apply a TTE-dependent floor without per-source fitting)?
3. **Do you want the today's "Fix calibration layer issues" work paused?** If it was Platt calibration on per-bracket probabilities, it doesn't address the upstream σ bug — and may even mask it (Platt will dampen overconfident probabilities, which makes the symptom less visible without fixing the cause). Worth knowing what that session did before deciding whether to revert it.
4. **Confirm the 5 live fills' provenance.** I want to verify in fills_ledger whether the 5 settled positions came from cross-bracket entry, exit (synthetic sell), or some other path. Knowing this changes whether Tier 1 fixes are sufficient or if exit logic also needs work.

Nothing executes until you read this and respond.
