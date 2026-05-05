# Post-fix re-assessment — major correction to PER_SOURCE_INVESTIGATION (2026-05-05)

**Status:** CORRECTION to [reports/PER_SOURCE_INVESTIGATION_2026-05-05.md](PER_SOURCE_INVESTIGATION_2026-05-05.md). Many recommendations in that doc are now superseded.
**Trigger:** Josh asked "is the lat/lon correct?" for the LAX hot bias. Investigation showed coords WERE wrong — and were FIXED by commit 8d043a8 on 2026-05-03 evening.
**Implication:** ~80% of the snapshot data the per-source investigation analyzed used PRE-FIX coords. Most "exclude X for LAX" / "correct ±N°F" recommendations were diagnosing a bug already shipped.

---

## 1. The fix that already happened

Commit `8d043a8` (2026-05-03 20:38 PDT, "Weather ensemble postmortem: per-city architecture + station-mapping fixes") moved every traded city's WEATHER_CITIES coords to the actual settlement station:

| City | Before | After | Move |
|---|---|---|---|
| NYC | (40.71, -74.01) ≈ Hoboken NJ | (40.78, -73.97) KNYC Central Park | ~5 mi NE |
| Chicago | (41.88, -87.63) Loop | (41.79, -87.75) KMDW | ~6 mi SW |
| Miami | (25.76, -80.19) downtown | (25.79, -80.29) KMIA | ~6 mi NW |
| Austin | (30.27, -97.74) downtown | (30.19, -97.67) KAUS | ~6 mi SE |
| **LAX** | **(34.05, -118.24) "Vernon, CA"** | **(33.94, -118.41) KLAX coast** | **~14 mi SW** |
| **Denver** | **(39.74, -104.99) "Glendale CO"** | **(39.86, -104.67) KDEN airport** | **~22 mi NE** |

The two biggest moves (LAX, Denver) are exactly the cities where I expected the largest bias correction. The fix did the work.

## 2. Pre-fix vs post-fix bias deltas (sanity check on the fix)

LAX HRRR forecast mean (across snapshots):
- Before fix: **73.7°F**
- After fix: **65.3°F**
- Δ = **-8.4°F**

Other LAX sources show similar magnitude shifts (-8°F for hrrr/weather/ecmwf/metno). **The fix is working.** Pre-fix forecasts were systematically biased ~8°F hot at LAX because they were sampling Vernon (inland heat island) instead of KLAX (coastal marine layer).

NY HRRR shifted +10°F post-fix vs pre-fix. That's confounded with seasonal warming (NY warmed ~7°F over the period per METAR), but the direction is consistent with moving from Hoboken to Central Park.

## 3. Updated post-fix scorecards (2-day samples)

I re-ran the scorecard for all 6 cities filtered to `--since 2026-05-04` (post-fix). **Sample size is small (2 settled days each)** but informative.

### Headline biases at peak window (post-fix)

| City | combined_v2 bias | Top NWP bias-offender | Verdict |
|---|---|---|---|
| NY | +1.1°F | weather +1.4°F, hrrr +1.3°F | **Mild hot bias on NWP. Acceptable.** |
| LAX | +2.1°F → -0.8°F (peak → post-peak) | ecmwf -1.8°F | **NWP near-neutral or slightly cold post-fix!** Pre-fix +3-7°F catastrophe is gone. |
| CHI | wide (small sample) | metno -4.1°F, ecmwf -3.2°F | Small sample; need more |
| AUS | wide | nws_5min +3.2°F, ecmwf -3.2°F | Small sample |
| MIA | combined_v2 +3.0°F | metar +2.5°F, metno +2.1°F | Mild hot bias |
| DEN | gem -5.5°F, ecmwf bias unknown | gem -5.5°F | Coord fix shifted DEN 22 mi — large change; need more data |

### What changed about combined_v2 σ-calibration

Pre-fix NY combined_v2 σ ratio at LST 21-23 was **3.84** (catastrophically tight σ). 
Post-fix NY combined_v2 σ ratio at LST 21-23 is **0.58** (slightly OVER-stated σ now).

**The post-peak σ-tightness problem may be resolved by the lat/lon fix alone.** The pre-fix sources were sampling biased coords with confident-but-wrong forecasts, which the precision-weighted combine then trusted. Post-fix, sources sample correct coords and produce better forecasts.

This is a much larger reframing than I expected.

## 4. What this means for the per-source investigation's recommendations

### RETRACT (or significantly soften):
- ❌ "EXCLUDE-PHASE(LAX, peak+post_peak) for hrrr/weather/icon/ukmo/nws_point" — pre-fix evidence. Post-fix, NWP at LAX is near-neutral. **Don't exclude.**
- ❌ "CORRECT(LAX, all, -3.5°F) for HRRR" — pre-fix evidence. Post-fix HRRR is +0 to +0.1°F at LAX peak. **Don't correct.**
- ❌ "CORRECT(LAX, peak, -1.5°F) for METAR" — pre-fix evidence. Post-fix METAR LAX peak is +2.1°F. Borderline; revisit with more data.
- ⚠️ All "σ-INFLATE(NY, ×0.6)" type recommendations — based on pre-fix σ ratios. Post-fix ratios look very different. **Revisit with more data.**
- ⚠️ All per-(city, phase) bias correction values — based on pre-fix data. **Revisit.**

### KEEP:
- ✅ **Drop `weather` from GAUSSIAN_COMBINE_SOURCES.** Still 0.99-1.00 correlated with `hrrr` post-fix (correlation isn't bias-dependent). Clean win.
- ✅ **Per-city LST cross-bracket gate** based on METAR-derived empirical peak. METAR data isn't affected by NWP coord fix (it's local KNYC/KLAX hourly observations).
- ✅ **Empirical phase boundaries from METAR** (LST 13 NY peak, LST 11 LAX peak, LST 14 CHI, LST 15 AUS, LST 12 MIA, LST 13 DEN) — based on 100 days of METAR backfill, fix-independent.

## 5. Revised Phase 3 plan — much more conservative

### Phase 3a — Ship today (safe wins, fix-independent)

1. **Drop `weather` from `GAUSSIAN_COMBINE_SOURCES`** in `bot/signals/weather_sources.py`.
   - Justification: 0.994-1.000 correlated with `hrrr`. Both are Open-Meteo (`gfs_hrrr` model vs default blend). Removing eliminates double-counting at zero information loss.
   - LOC: ~5 + a regression test.

2. **Add dynamic per-city LST cross-bracket gate.**
   - Replace the hardcoded `CROSS_BRACKET_MIN_TTE_HOURS=3, MAX=7` with a per-city LST window derived from `tools/per_city_source_scorecard.py::empirical_phase_boundaries`.
   - Run the scorecard nightly; persist per-city `(min_lst_hour_to_fire, max_lst_hour_to_fire)` to `kv_cache`. Daemon reads at decision time.
   - Default fallback if cache empty: `(15, 23)` (post-peak window).
   - LOC: ~150 (scorecard hook + kv_cache writer + daemon reader + tests).

3. **Apply the same LST gate to shadow logging.** Currently shadow logs everything across TTE 5-45h. Filter shadow to the same LST window so the diagnostic compares apples to apples.
   - LOC: ~30.

### Phase 3b — Wait 7-14 days, then re-evaluate

Don't ship per-(city, phase) σ corrections, bias corrections, or new exclusions until we have:
- ≥7 days of post-fix snapshot data per city (~15 settled days total per city).
- A re-run of the scorecard with ≥7 days post-fix.
- A re-run of the cross-bracket diagnostic with ≥3 settled cycles post-fix.

If post-fix shadow markout converges to realized P&L within ±20%, **we don't need Phase 3b at all** — the lat/lon fix may have addressed everything.

### Phase 3c — Investigation: did the fix make cross-bracket profitable?

While waiting for Phase 3b data, run one analytical check: re-run `tools/backtest_cross_bracket_historical.py` against the 5 settled losses, but using only post-fix forecasts (i.e., what would the cross-bracket scorer have decided with the corrected coords). If the strategy would have skipped or sized down those trades, **the strategy itself is fine and the losses were 100% coord-bug-driven**.

## 6. Open questions for Josh — revised

1. **Do you accept the more conservative Phase 3a plan?** (Drop `weather`, dynamic LST gate, monitor.)
2. **Is Phase 3c (post-fix backtest of the 5 losses) worth the cycles?** It would validate the "lat/lon fix is sufficient" hypothesis. ~1 session of work.
3. **How long do you want to wait before Phase 3b?** Suggested 7-14 days. Could be more aggressive (3-5 days) if combined with Phase 3c showing the fix is sufficient.
4. **Should I keep running the per-city scorecards daily** as data accumulates, so we have a moving picture of post-fix bias evolution? ~5 min/day to run; would build up the dataset for Phase 3b.

## 7. Acknowledgment

The per-source investigation work wasn't wasted — it built the analytical infrastructure (scorecard tool, LST utility, methodology) that we'll need for Phase 3b. But the substantive recommendations in §3 of [PER_SOURCE_INVESTIGATION_2026-05-05.md](PER_SOURCE_INVESTIGATION_2026-05-05.md) need to be regenerated with post-fix data before being implemented.

This is exactly the "trace before you summarize" rule from the user-level CLAUDE.md: I should have checked the data's vintage before drawing conclusions. Got there in the end via Josh's coord question — won't make this mistake again.
