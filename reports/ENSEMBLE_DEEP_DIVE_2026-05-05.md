# Per-Market Ensemble Deep Dive — Plan (FOR REVIEW)

**Status:** DRAFT plan. Does not execute until Josh signs off.
**Trigger:** Cross-bracket loss root cause ([reports/CROSS_BRACKET_ROOT_CAUSE_2026-05-05.md](CROSS_BRACKET_ROOT_CAUSE_2026-05-05.md)) showed v2 ensemble σ is broken at the pre-peak diurnal phase. Fix-by-tinkering won't work — we need a per-market, per-source first-principles audit before touching the combiner.
**Companion to:** [reports/CITY_EXPANSION_PLAN_2026-05-05.md](CITY_EXPANSION_PLAN_2026-05-05.md). City expansion remains paused; this work is the prerequisite.

---

## 0. Reframing — LST/diurnal phase, not TTE

The cross-bracket investigation conflated "long TTE" with "high uncertainty." That's wrong. What actually drives uncertainty is **time relative to the day's diurnal peak**. Settlement is fixed at end-of-night LST; peak temperature happens mid-afternoon LST and varies by city (NY ~16:00 LST, MIA ~14:00 LST, LAX ~13-15:00 LST depending on marine layer, AUS ~16-18:00 LST, DEN ~14-16:00 LST, CHI ~16:00 LST).

The right axis is **diurnal phase**, defined per-city:
- **Pre-peak** (LST 06:00 → 12:00): day hasn't happened. Forecast σ should be wide (3-6°F). NWP models dominate; METAR running-high is meaningless (still climbing).
- **Peak window** (LST 12:00 → 18:00): day's high is being realized. NWP is ground truth as it lands; METAR running-high becomes meaningful.
- **Post-peak** (LST 18:00 → settlement): high is essentially set. METAR running-high is near-truth; σ should be tight (0.3-1.0°F). NWP forecasts of *higher* values are noise.

These map roughly to TTE bands but with significant city offsets (LAX peak is 3-5h earlier in LST than AUS), so any per-TTE rule mis-calibrates somewhere.

The ensemble currently runs the same source weights and σ priors across all phases. **First-principles claim: it shouldn't.** Pre-peak should weight NWP heavily and ignore METAR; post-peak should weight METAR heavily and ignore NWP.

---

## 1. What we have to work with

Inventory verified against VPS DB this session:

| Table | Coverage | Use |
|---|---|---|
| `weather_forecast_snapshots` | 6 cities × ~17 sources × 13K-18K rows since 2026-04-24 (~10 days, every-cycle live captures) | Per-source predicted (μ, σ) with `recorded_at`, `hours_out` |
| `weather_gaussian_snapshots_backfill` | Backfill table with `observed_high_f` ground truth, populated by `tools/backfill_weather_effective_n.py` | Per-source historical fits with truth |
| `weather_metar_hourly_backfill` | Hourly METAR with `daily_high_f`, by station/LST-hour | Per-station LST-aligned ground truth |
| `alpha_backtest` | Per-decision atomic log w/ ensemble_p_yes, market quotes, settlement_result | Decision-time evaluation |
| `bot/daemon/stations.py` | LST offset per station (`tz_local_std`) | UTC ↔ LST conversion |

What's missing:
- A unified per-(city, source, LST hour) bias/RMSE/σ-cal report.
- Per-(city, source) within-group correlation measurements (HRRR vs NBM, ECMWF vs metno, etc.).
- A per-city diurnal-peak detector (most cities are stable but DEN can peak at 14-16 depending on synoptic regime; LAX marine layer drift).

### Data gaps already visible from `SELECT AVG(forecast_high_f)` on snapshots

These jumped out without any analysis — sources with obvious systematic bias on at least one city:

| City | Source | Sample mean (°F) | Combined-v2 mean (°F) | Apparent bias |
|---|---|---|---|---|
| AUS | gem | 72.5 | 82.4 | **-10°F** |
| AUS | icon | 73.5 | 82.4 | -9°F |
| AUS | ukmo | 74.6 | 82.4 | -8°F |
| AUS | metno | 77.4 | 82.4 | -5°F |
| AUS | nbm | 87.2 | 82.4 | +5°F |
| AUS | tomorrow | 89.8 | 82.4 | +7°F |
| NY | nws_point | 58.7 | 65.3 | -6°F |
| NY | madis | 57.9 | 65.3 | -7°F |
| NY | metno | 68.4 | 65.3 | +3°F |
| NY | gem | 69.6 | 65.3 | +4°F |
| MIA | tomorrow | 78.0 | 85.5 | -7°F |
| MIA | nbm | 83.1 | 85.5 | -2°F |
| MIA | ukmo | 89.2 | 85.5 | +4°F |
| LAX | madis | 65.7 | 70.5 | -5°F |
| DEN | madis | 50.0 | 62.7 | **-13°F** |

These are *raw means*, not bias-vs-truth. But the spread between sources for the same city (AUS gem→tomorrow = 17°F!) demonstrates the ensemble is averaging garbage with signal in unknown ratios. A first-principles audit will catch which.

---

## 2. The plan

Per-city, per-source, six-phase analysis. Build once, run on all 6 currently-traded cities. Produce per-city scorecards with concrete tuning recommendations.

### Phase 1 — Build the analytical primitives (one-time infra)

**1a. `tools/lst_align.py`** — utility module: given (UTC timestamp, station/city), return LST hour and diurnal phase (`pre_peak`, `peak_window`, `post_peak`) from station's `tz_local_std`. Used by every downstream tool. ~80 LOC.

**1b. `tools/per_city_source_scorecard.py --city CITY`** — runs the 6-phase audit (below) for one city, emits a markdown report. ~600 LOC.

**1c. Backfill missing observations.** Pull NOAA CLI archive or IEM ASOS for daily high temps Jan-May 2026 for all 6 cities. Populate `weather_gaussian_snapshots_backfill` for sources we don't yet have backfilled (currently only weather + hrrr + nbm + metar are backfillable via the existing tool). For ECMWF / GEM / ICON / metno / NWS_point we may have to use `weather_forecast_snapshots` joined to settlements — only ~10 days of history but better than nothing. ~200 LOC extension to existing backfill tool.

**Estimate:** 1 session.

### Phase 2 — Per-city per-source first-principles audit (the deep dive)

For each of the 6 cities, for each source, compute:

#### 2a. Bias by LST-hour-of-forecast
For each LST hour bucket (06, 09, 12, 15, 18, 21), compute mean(forecast - observed). A source biased high pre-peak but accurate post-peak (or vice versa) tells us *when* to weight it.

#### 2b. RMSE by LST-hour-of-forecast vs claimed σ
Same buckets. Compute realized RMSE vs the source's claimed σ. The ratio (realized RMSE / claimed σ) is the σ inflation factor needed for that (city, source, LST hour).

If ratio ≈ 1.0, source is well-calibrated. If ratio = 3.0, σ should be tripled.

#### 2c. Bias / RMSE by hours_out (lead time)
Independent of LST hour, how does each source decay with lead time? E.g., HRRR is good at 0-18h, dies at 24h+. NWS_point is updated 4×/day so its skill resets every 6h.

#### 2d. Correlated-source residual analysis
For each source group (HRRR/NBM, ECMWF/GFS family, metno/GEM/ICON, weather/Open-Meteo composites): compute pairwise correlation of forecast residuals (forecast - observed). If two sources have correlated residuals > 0.7, they're not independent and the ensemble is double-counting.

For each city, output: effective number of independent sources (`n_eff`) by phase.

#### 2e. METAR running-high signal value by LST hour
Special case for METAR: when does running-high become a sharper signal than the model forecasts? Answer should be "after LST 14:00 but only if running-high - METAR-current < 1°F" (post-peak detection). Quantify the threshold per city.

#### 2f. Per-source dominance regimes
Combine 2a-2e into a per-(city, LST phase) ranking: which source is most accurate, which is most precise (tightest realized σ), which should drive the combined estimate.

**Output per city:** [reports/SCORECARD_<CITY>_2026-05-05.md] — single doc with 6 sections matching 2a-2f, plus a recommendations section with concrete code changes.

**Estimate:** 1-2 sessions per city × 6 cities. Realistically, the first city takes ~1.5 sessions (build the report template), subsequent cities are ~30 minutes each since the tool runs them.

### Phase 3 — Ensemble redesign per city

Based on Phase 2 findings, propose per-city changes to:

**3a. Source exclusions per LST phase.** Replace the static `EXCLUDED_SOURCES_BY_CITY` with `{city: {phase: frozenset_of_excluded_sources}}`. E.g., post-peak NY might exclude all NWP and ride METAR alone.

**3b. Source weights per LST phase.** Replace static weights in `bot/config.py` with `{(city, phase, source): weight}`. Pre-peak: weight HRRR/ECMWF heavily. Post-peak: weight METAR.

**3c. Per-source σ inflation per (city, LST hour).** From 2b — multiply each source's claimed σ by the empirical ratio.

**3d. Within-group `n_eff` adjustment per (city, phase).** From 2d — the combined σ should reflect actual independence, not assumed.

**3e. METAR fast-path for post-peak.** If LST > 14:00 AND `running_high - current_temp ≥ X°F` (X per-city from 2e), pin μ=running_high, σ=0.5°F. Bypasses the v2 combine entirely. This is what the existing past-peak clamp tried to do globally — make it per-city.

**3f. Don't fire cross-bracket pre-peak.** From the loss data: every loss came from decisions made before LST 14:00 the previous afternoon (pre-peak for next-day settlement). Add LST gate: only fire cross-bracket between LST 14:00-22:00 of the settlement day.

Each item has a per-city table with the recommended values from Phase 2. Implementation is a single PR per city.

**Estimate:** 0.5 session per city.

### Phase 4 — Validate the redesign offline

Re-run `tools/backtest_cross_bracket_historical.py` (extended from CITY_EXPANSION plan) with the per-city-redesigned ensemble against historical settlements. Compare:
- Old ensemble Brier vs new ensemble Brier per (city, LST phase)
- Old strategy P&L vs new strategy P&L per city
- Specifically: would the 5 losing positions still have fired? At what size?

Acceptance: new ensemble must beat old on Brier across ≥ 4 of 6 cities AND have non-negative simulated P&L on at least 4 of 5 historical losses.

**Estimate:** 0.5 session.

### Phase 5 — Ship one city as canary

Promote the **highest-improvement** city's redesigned ensemble to live. Soak ≥7 days. Compare live shadow markout vs realized P&L — gap should narrow significantly. Then promote next city.

**Estimate:** 7-day soak per city, in parallel with continued analysis.

---

## 3. What "first principles" actually means here

I want to be specific about the standard for each source's analysis, not hand-wave "look at it":

### NWP models (HRRR, ECMWF, GFS-derivatives, ICON, GEM, metno, UKMO, NBM)

Each NWP run is initialized from observations at fixed cycle hours. Forecasts have a known skill curve: error grows roughly as RMSE = a + b × forecast_hour. We can fit this from `weather_gaussian_snapshots_backfill` (joined to observed high). First-principles question per (city, source):
- What's the empirical skill curve at this city?
- Does the city have a feature the model handles poorly? (e.g., LAX marine layer; DEN orographic effects)
- At what lead time does this model become noise?

### Observation-based (METAR, MADIS, NWS_5min)

These are not forecasts — they're real-time temperatures. Their value comes from being a leading indicator of the day's high IF the day is past peak. First-principles questions:
- At what LST hour does running-high become a reliable estimate of the daily high?
- What's the post-peak σ (residual variance after running-high stops climbing)?
- For mesonet (MADIS): is the station representative of the official settlement station, or is there a microclimate offset?

### Synthesized / proprietary (NWS_point, AFD, Tomorrow.io, NWS_5min_diurnal)

NWS_point is a human-curated forecast (forecasters edit the NDFD grid). AFD is text-based forecaster discussion. Tomorrow.io is proprietary blend. First-principles questions:
- Is the human-edited NWS_point sharper than the raw NWP it's based on?
- Does AFD bias correctly identify when models are wrong? (low-frequency signal but high-value when it fires)
- Tomorrow.io: black-box. Treat as one more source; compute bias/RMSE.

### The combine itself

Once per-source σ is corrected, the combined-Gaussian assumption (`combine_gaussian` in `bot/signals/weather_forecast.py`) requires sources to be independent. They aren't. The within-group correlation analysis (2d) tells us how much to discount the combined σ.

---

## 4. Decisions (Josh, 2026-05-05)

1. **Sequencing**: NY first, then LAX, then the other 4 cities sequentially. Different cities may genuinely have different answers — the per-city report shouldn't try to homogenize.
2. **Boil the ocean on Phase 3a**: full per-(city, LST phase, source) exclusion + weight + σ-inflation config, not a simpler regime-multiplier shortcut. The complexity is justified because the data shows per-city per-source biases differ by 5-13°F.
3. **Phase 4 runs incrementally after each city** — backtest the city's redesign as soon as Phase 3 lands for it. Don't wait for all 6.
4. **Cross-bracket LST gate confirmed.** Replace the TTE 3-7h gate with an LST window of the settlement day. Exact window per-city derived from Phase 2f findings. Default if Phase 2f is inconclusive: LST 18:00-22:00 of settle day. (Tighter than the [14:00, 22:00] originally proposed because Phase 2f will tell us where the diurnal-peak σ is genuinely tight per city.)
5. **Ship-to-live bar:**
   - **Required:** EV-positive forecasts (per-bracket Brier strictly better than current ensemble on ≥ 14 days of held-out historical data) AND EV-positive trade decisions (mean per-decision EV > 0 with appropriate fee accounting).
   - **Ideal:** historical-backtest P&L profitability with sample size ≥ 30 settlements per city. Required only if live data is unavailable or insufficient.
   - **Acceptance fallback:** if backtest sample too small for P&L significance, ship to live and require ≥ 7 days of live shadow + ≥ 5 settled days where shadow markout converges to realized P&L within ±20%.
6. **Candidate-city scorecards deferred** until existing-6 ensemble work is complete and validated. Don't spend cycles on Phoenix/Seattle until we've shown the deep-dive methodology actually yields working ensembles on cities we already trade.

---

## 5. Concrete sequencing (now committed)

```
Phase 1 — Build analytical primitives                       [1 session]
  1a. tools/lst_align.py
  1b. tools/per_city_source_scorecard.py (skeleton)
  1c. Backfill missing observations + extend backfill tool

Phase 2 — NY scorecard                                      [1.5 sessions]
  2a-f on NY; emit reports/SCORECARD_NY_2026-05-05.md
  Refine the tool based on what NY surfaces

Phase 3 — NY redesign                                       [0.5 session]
  Per-(LST phase, source) exclusions/weights for NY
  Cross-bracket LST gate per Phase 2f findings
  PR

Phase 4 — NY validation                                     [0.5 session]
  Re-run backtest on NY history with redesigned ensemble
  EV-positive forecasts + EV-positive trades check
  If pass → Phase 5; if fail → diagnose + iterate Phase 3

Phase 5 — Ship NY                                           [7+ days passive]
  Promote NY to live with redesigned ensemble
  Soak ≥7 days; track shadow-markout vs realized

Phase 2-5 for LAX                                           [parallel to NY soak]
  LAX is climate-different (marine layer); sanity-check the methodology generalizes

Phase 2-5 for CHI/AUS/MIA/DEN                               [sequential]
  Run scorecards back-to-back; each ~0.5 session if tool holds

Then (and only then) → candidate-city scorecards → Track B (city expansion)
```

Total: ~6-9 sessions of active work over ~3-6 weeks of elapsed time.

---

## 5. Why I think this is the right move

- Tinkering with σ floor or per-source priors without first knowing what's broken per-city would be guess-and-check. We tried that; it lost $8.43.
- The data is already there — 10 days of live captures across 17 sources × 6 cities. We don't need to wait for new data; we need to extract what's in front of us.
- The same tool (per-city scorecard) is exactly what city expansion needs (Layer 1 of the framework). One investment, two payoffs.
- First-principles per-city is what would make us actually understand the system instead of treating the v2 ensemble as a black box. After this, we can reason about every change rather than bake-off-and-pray.
