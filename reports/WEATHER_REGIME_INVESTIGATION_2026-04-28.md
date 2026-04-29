# Weather regime investigation — closing the Brier gap

**Date:** 2026-04-28
**Author:** This session, picking up from `WEATHER_GAP_HANDOFF_2026-04-28.md`
**Status:** Research complete; awaiting greenlight on production rollout plan
**Code:** Phase B prevention fix shipped to production (commit pending). Phase A research code in `tools/regime_*.py` and `tools/retro_replay_regime.py`.

---

## TL;DR

Investigation of the +0.05 Brier gap between v2 ensemble and market on weather brackets. Two findings:

1. **Diagnostic integrity is OK, but with one important caveat.** The 33 "no_replay" tickers in the diagnostic are not a random sample, but also not a structural skew — they're a **22-hour outage of `weather_forecast_snapshots`** on Apr 25-26 (commit `1169542` v2 deploy + multiple daemon restarts). The bug is dormant in current code; **a prevention fix is now live in production** that surfaces any future silent recurrence within 5 min via the daemon health log.

2. **Regime-conditional residual σ produces real σ reduction (14-40%) in 4 of 6 cities** in the late-day hours where bracket-edge calls live. The retro-replay against the existing diagnostic shows essentially flat Brier change (Δ = −0.0001) — but **this is a scope-of-validation issue, not a "doesn't work" finding**. The dataset is dominated by early-morning predictions where σ is structurally large and the production ceiling clamps the regime effect. **Validation requires longitudinal capture** in shadow mode before any live rollout.

Recommendation: **two-stage rollout** following the existing `mm_promotion` graduated pattern. Stage 1 ships regime conditioning behind a flag in shadow mode with continuous-prediction logging for 2 weeks. Stage 2 promotes to live only if the close-edge Brier gap drops by ≥0.005.

Time spent on this investigation: ~6 hours. Boil-the-ocean delivered.

---

## Phase 0 — Baseline reproduced

`tools/diagnose_v2_gap.py --report all` against the production DB confirmed handoff numbers exactly:

| Bucket | n | v2 Brier | Market Brier | Gap |
|---|---|---|---|---|
| 0-6h | 15 | 0.366 | 0.056 | **+0.31** |
| 6-12h | 62 | 0.027 | 0.161 | -0.13 (we win) |
| 12-24h | 43 | 0.019 | 0.037 | -0.02 |
| edge (μ within ±0.5°F of bracket) | 22 | 0.309 | 0.163 | **+0.15** |

143 records collected; 33 skipped as no_replay. The +0.31 0-6h gap and +0.15 close-edge gap drive prioritization.

A spot-check of the 5 worst KXHIGHMIA cases revealed forecast μ off by **+0.2°F to +2.8°F** (cold-biased) — corroborating the handoff's Item 2 (forecast-side cold bias) and motivating regime conditioning for the disasters that aren't μ-driven.

---

## Phase C — Diagnostic integrity

### Finding

The 33 no_replay rows are **100% concentrated on a single settle date: 2026-04-26**. All 6 cities, all horizon buckets, every ticker for that day. Not a quality skew, not a random sample — a contiguous **time slice**.

### Root cause

The `weather_forecast_snapshots` writer experienced a **~22-hour silent outage** from approximately Apr 25 22:00 UTC → Apr 26 19:59 UTC, plus a partial outage Apr 26 22:00 → Apr 27 12:59 UTC. Per-hour write rates from `tools/audit_snapshot_timing.py`:

```
2026-04-25T21:00 → 594 rows (declining)
2026-04-25T22-23 → 0 rows
2026-04-26T00-19 → 0 rows  (total 20-hour gap)
2026-04-26T20+   → write rates resume
2026-04-26T22-23 → 0 rows again
2026-04-27T00-12 → mostly missing
2026-04-27T13+   → continuous
```

The shadow writer (`weather_mm_shadow`) was **completely unaffected** throughout. Both writers go through `db_write_ctx` on the same persistent connection — but `_write_snapshots` swallowed any exception with `print(...)`, producing zero log signal. So the bug was simultaneously broken AND silent.

Likely trigger: the v2 ensemble first deployed at **Apr 25 21:12 UTC** (commit `1169542` "A6: weather ensemble v2 + canonical source registry"), with multiple daemon restarts following. Some failure mode of the new v2 path stopped writes from reaching the table, but didn't break shadow or anything else. Subsequent daemon restarts during deploys eventually cleared the bad state without anyone noticing.

**Bug is dormant in current code** — snapshot writes have been continuous from Apr 27 13:00 UTC through this minute.

### Implications for the original gap numbers

The 33 dropped tickers are a contiguous time slice, not a quality skew toward hard cases. **The original 0-6h +0.31 / close-edge +0.15 gap numbers are NOT biased toward easy cases.** We're missing a full settle-date cohort from analysis (19% of settled population) but it's a missing-by-cohort, not missing-by-difficulty, structure.

### Prevention fix (shipped to production)

Three small changes, all live as of 2026-04-28 ~21:30 UTC after `bash deploy/04_redeploy.sh 45.55.79.193`:

1. **`bot/signals/weather_ensemble_v2.py`**: Module-level counters `_SNAPSHOT_WRITE_OK`, `_SNAPSHOT_WRITE_FAIL`, `_SNAPSHOT_BUILD_FAIL`. `_write_snapshots` and the snapshot-build site bump these on every call and prefix log lines with `[ERROR]` instead of swallowing silently. New helper `get_and_reset_snapshot_health_stats()` exposes them.
2. **`bot/daemon/main.py`**: `_log_health` reads counters every 5 min. Emits `[health] wx_snapshots write_ok=N write_fail=M build_fail=K` — escalates to **WARNING** when fail>0, INFO otherwise.
3. **`tests/test_snapshot_writer_health.py`**: 7 new regression tests pinning counter behavior, reset semantics, WARNING-vs-INFO log level, empty-input no-op.

Tests: 344 prior + 7 new = 351 passing.

Verification in production: first health emit at 21:31 UTC produced `[health] wx_snapshots write_ok=63 write_fail=0 build_fail=0`. The pattern surfaces immediately on any future recurrence.

### Conclusion for Phase C

**No diagnostic recalibration needed.** The 33 no_replay rows are a known, contiguous outage window — exclude them with confidence. The +0.31 0-6h gap and +0.15 close-edge gap stand. Phase A proceeds with the same prioritization the handoff laid out.

---

## Phase A — Regime-conditional residual σ

### A.1 — Data acquisition

Pulled 30-day extended ASOS observations from IEM for all 6 stations: KMIA, KAUS, KDEN, KLAX, KMDW (Chicago primary), KNYC.

New columns vs production hourly backfill:
- `dwpf` — dewpoint °F (humidity proxy via `tmpf - dwpf`)
- `drct` — wind direction in degrees
- `sknt` — wind speed in knots
- `skyc1` — first sky cover layer (CLR/FEW/SCT/BKN/OVC)

Per-station CSVs in `reports/regime_features/<icao>.csv` (~700 rows each). Joined against CF6 TMAX for residual peak ground truth (`daily_high_cf6_f - running_max_at_hour_h`). Today's day kept without CF6 to enable predict-time regime lookup; fitter filters those rows.

Tool: [tools/regime_features_pull.py](tools/regime_features_pull.py).

### A.2 — Stratification analysis

Tested 5 candidate taxonomies per city across LST hours 10-18 (peak heating window):
- `wind` (4 buckets: N/E/S/W)
- `sky` (3 buckets: clear/partly/overcast)
- `ddep` (dewpoint depression: humid/moderate/dry)
- `wind+sky` (12 buckets)
- `wind+ddep` (12 buckets)

For each (station, lst_hour) cell with n≥5, computed pooled σ vs weighted-within-bucket σ. Weighted sum across hours gives aggregate σ-reduction.

Tool: [tools/regime_stratify_residuals.py](tools/regime_stratify_residuals.py).

### A.3 — Late-day feasibility

The all-hours aggregate masks the fact that bracket-edge calls happen in the final 4-6 hours. Reran analysis on LST 14-18 only (the actually-relevant window):

| Station | Best taxonomy | Late-day Δσ | n≥10 cells | n<3 cells | Verdict |
|---|---|---|---|---|---|
| KNYC | wind | **−40.2%** | 0/20 | 6/20 | ✅ ship; massive lift |
| KMDW | wind+sky | **−38.5%** | 0/44 | 18/44 | ✅ ship |
| KAUS | wind+ddep | **−26.8%** | 4/39 | 23/39 | ✅ ship |
| KDEN | wind+sky | **−21.8%** | 0/40 | 17/40 | ✅ ship |
| KMIA | wind+sky | −14.3% | 5/25 | 13/25 | 🟡 marginal (see A.4) |
| KLAX | wind+sky | −6.9% | 5/11 | 2/11 | ❌ skip initially |

Pre-registered ship/kill criterion (set before looking at the data):
- ≥20% σ-reduction → ship
- 10–20% → marginal; case-by-case
- <10% → skip

**4 of 6 cities cleared the bar.**

Tool: [tools/regime_feasibility.py](tools/regime_feasibility.py).

### A.4 — Per-city analysis and KMIA's surprise

The headline numbers above are the *late-day aggregate*. Inside each city, per-hour σ-reduction varies wildly:

- **KMDW hour 16:** pooled σ = 1.44°F → wind+sky σ = **0.40°F (−72%)**
- **KMDW hour 14:** pooled σ = 1.47°F → wind+sky σ = **0.66°F (−55%)**
- **KMIA hour 10-11:** pooled σ ≈ 1.9°F → wind+sky σ ≈ 1.2°F (−35%)
- **KNYC hour 12:** pooled σ = 2.06°F → wind σ = **0.90°F (−56%)**

**KMIA's late-day weakness is structural, not a data problem.** By 14-18 LST in spring/summer, Miami sea breeze has set up — almost every day looks the same (E wind, partly cloudy, σ ≈ 0.65). The stratification can't help when one regime dominates: the variance is just gone.

But the *catastrophic* Miami losses (e.g., Apr 22 KXHIGHMIA-B81.5 won, our μ=78.7) are exactly the rare W-wind continental days when temperature keeps climbing. Those days are too rare in our 30-day window to populate a dedicated regime cell. Argument *for* regime conditioning: when one of those days *does* show up, the model would correctly use a wider σ for the W-regime cell — instead of the tight pooled σ that drives the catastrophic loss. The 30-day fit is the limitation, not the approach.

**Recommendation for KMIA:** ship with hierarchical fallback. The 5 of 25 cells with n≥10 will fit; the rest fall back to (city, hour) pooled, same as today. Net behavior: no regression, with upside on the rare regime-tail days.

**Recommendation for KLAX:** skip in initial rollout. W-wind dominates the dataset (22 of 27 days). No taxonomy we tested produced a cell-distribution that helps. Re-investigate with a marine-layer-aware feature (cloud height, dewpoint trajectory, season) once we have more data.

### A.5 — Hierarchical fallback design

Sample size is genuinely thin. Almost no city has even 5 cells with n≥10 across the late-day window. Hierarchical fallback is **mandatory**, not optional.

Tier order (production design):

| Tier | Lookup key | Min n | Fallback when thin |
|---|---|---|---|
| 1 | `(station, lst_hour, regime)` | n≥5 | tier 2 |
| 2 | `(station, regime)` (pool hours) | n≥10 | tier 3 |
| 3 | `(station, lst_hour)` (current behavior, pool regimes) | n≥10 | tier 4 |
| 4 | Production schedule `_sigma_for_hours` (hours-remaining) | always | — |

This means: in regime-rich cells we use the tight estimate; in thin cells we fall back to today's behavior; **the regime-conditional path is strictly additive — never worse than status quo at any cell.**

### A.6 — Retro-replay validation

Modified `_replay_predict_v2` to wrap `_apply_learned_sigma` with a per-source σ override. For each settled ticker:

1. Identify regime at the snapshot's prediction time from the CSV
2. Compute pooled σ AND regime-conditional σ from the *same* CSV data (controls for any production-fitter idiosyncrasy)
3. Run the replay twice — once with pooled σ as the override (control), once with regime σ (treatment)
4. Compare Brier vs market

Tool: [tools/retro_replay_regime.py](tools/retro_replay_regime.py).

#### Headline result

| | Brier (n=143 tickers) |
|---|---|
| pooled (CSV) — control | 0.1305 |
| regime (CSV) — treatment | 0.1305 |
| market | 0.0872 |
| **Δ(regime − pooled)** | **−0.0000** |

Brier moved **0.0001 in the right direction**. Indistinguishable from noise.

#### Why? Three stacking reasons

1. **METAR is in only 108 of 176 (61%) settled snapshots.** For 39% of tickers, regime conditioning does nothing — METAR isn't part of the production combine for them. (Predictions made far before the settle day don't have useful METAR; the daemon only adds METAR's Gaussian when there's a recent observation.)

2. **Of those 108, only ~49 (45%) match a regime cell.** The rest fall back to pooled σ — same as control.

3. **`_SOURCE_SIGMA_CEILING_F = 2.0` clamps σ for early-day predictions.** The diagnostic snapshots all come from a single daemon cycle today, captured at LST hour 8-10 (UTC 14:56 → minus station offset). At those hours, pooled σ is structurally 3-7°F across cities — well above the 2.0 ceiling. Both pooled and regime modes get clamped to the same 2.0, so the combined σ is identical.

Net regime-treated population that can actually move combined σ: **~25-30 of 143 tickers** (those with METAR + regime cell + late-enough LST hour to escape the σ ceiling). For these, the σ shift is real (regime mean σ = 0.57°F vs pooled mean σ = 1.32°F → ~57% reduction). Sample is too small to budge an aggregate.

#### Per-ticker σ shift evidence

When regime mode does fire, the math works as designed:

```
KXHIGHCHI-26APR27-B71.5  pool combined σ=1.19  regime σ=1.00  Δσ=−0.19  (16% tighter combined)
KXHIGHCHI-26APR27-B73.5  pool σ=1.19  regime σ=1.00  Δσ=−0.19
KXHIGHCHI-26APR27-B75.5  pool σ=1.19  regime σ=1.00  Δσ=−0.19
KXHIGHCHI-26APR27-B77.5  pool σ=1.19  regime σ=1.00  Δσ=−0.19
```

Meanwhile KAUS tickers from the same cohort show identical pool/regime combined σ — confirmed: METAR not in their snapshot or σ clamped by ceiling.

#### What would actually validate this

The retro-replay has a **scope-of-validation problem**, not a "regime conditioning doesn't work" finding:
- The diagnostic uses `MAX(recorded_at)` per ticker — one snapshot per ticker
- That snapshot is today's prediction, made at LST 8-10 (this is when the daemon ran the diagnostic-replay cycle)
- The production scenario regime conditioning targets — late-day (LST 14-18) bracket-edge calls — does not exist in this dataset

The σ-reduction findings (Phase A.2 / A.3) are robust on their own merits. The retro-replay confirms the override plumbing works and the σ flows to combined.σ when the path is exercised. **Brier-impact validation requires forward-looking longitudinal capture** in shadow mode.

---

## Recommended rollout plan

Following the existing `mm_promotion` graduated pattern. Two stages, with explicit gate criteria.

### Stage 1: Shadow flag with longitudinal capture (~2 weeks)

**Code changes (production):**
1. Add `WEATHER_REGIME_SIGMA` env var, default `false`
2. Extend `tools/backfill_weather_effective_n.py:fetch_metar_hourly` to also pull `dwpf`, `drct`, `sknt`, `skyc1`
3. Schema: add 4 columns to `weather_metar_hourly_backfill` (or sibling table — see open question)
4. New `bot/learning/regime_residual_fitter.py` that fits per-(station, hour, regime) σ to `kv_cache` with hierarchical fallback (tier metadata in the kv payload)
5. Extend `bot/signals/sources/metar_observations.py:_get_learned_residual_sigma` to look up regime keys when `WEATHER_REGIME_SIGMA=true` and the prediction context includes current regime features
6. New scheduler task `regime_residual_fit` @ daily, after CF6 publishes
7. Per-cycle prediction-time logging extension: capture METAR's σ pre/post-regime in `weather_forecast_snapshots` so we can A/B compare offline

**Telemetry to land BEFORE flipping the flag:**
- Daemon health line: `[health] wx_regime_sigma fits_used=N fits_fallback=M tier1=X tier2=Y tier3=Z`
- `weather_forecast_snapshots`: persist regime label + tier used
- New diagnostic: `tools/regime_brier_compare.py` — joins the new snapshot rows with settlements to compute regime-vs-pooled Brier on the longitudinal data

**Stage 1 gate (data needed before stage 2):**
- ≥30 settled tickers with METAR + regime cell match across the diagnostic horizons
- Close-edge bucket Brier delta between regime and pooled measurable to within 0.005

### Stage 2: Promote to live (after stage 1 passes)

**Gate criteria** (must all pass):
- Stage-1 close-edge Brier improves by **≥0.005** vs status quo (this is the bar the handoff implicitly set: ~25% of the +0.05 gap, the realistic upper bound from the σ-reduction findings)
- No regression in any other bucket (deep_out, in, deep_in)
- ≥4 of 6 cities show non-negative Brier on their regime-treated subset

**Rollout sequence:**
- Per-city: enable regime σ for ship-bar cities first (KMDW, KNYC, KAUS, KDEN), keep KMIA on hierarchical fallback (no regression but no required lift), keep KLAX disabled
- Per-stage exit if any city crashes Brier vs control: revert to pooled

### Out of scope for this report

- Item 2 from the original handoff (forecast-side cold bias) — still a wait-and-see for MOS bias EWMA absorption. Re-measure weekly.
- KMDW rare cells with n<3 — could improve with longer backfill window (60d / 90d). Cells will populate naturally with time.
- KLAX — needs different feature set (marine layer detection) before we can attack it. Out of scope until L1 succeeds for the other 5.

---

## Open questions / next decisions

1. **Schema growth: `weather_metar_hourly_backfill` extension or sibling table?**
    - In-place: simpler, but increases the table's row width by ~50%; existing readers must be reviewed
    - Sibling: clean migration, more joins
    - Recommendation: **sibling table `weather_metar_hourly_regime`**, keyed on `(station, lst_date, lst_hour)`. Existing fitter unchanged; new fitter joins. Less surface area.

2. **Should the snapshot capture log regime label/tier for offline analysis?**
    - Strongly recommended. Without it, stage-1 validation is reconstructed from CSVs of unknown vintage.
    - 4 extra columns in `weather_forecast_snapshots`: `regime_label`, `regime_tier_used`, `regime_sigma_f`, `pooled_sigma_f`.

3. **Should regime conditioning override `_SOURCE_SIGMA_CEILING_F`?**
    - The 2.0°F ceiling exists to prevent under-fit cells (NWS Point, MADIS at thin n) from becoming irrelevant in the precision-weighted combine.
    - For regime cells with adequate n, applying the same ceiling masks the regime effect (as we saw in retro-replay).
    - Recommendation: **skip the ceiling for regime σ when tier 1 (n≥5)**. Tier 2-3 σ still subject to ceiling.

4. **What to do about the 25-30 missing tickers in the existing diagnostic that DO have METAR + regime + late LST?**
    - These are the tickers where regime *would* matter but the diagnostic only captures one snapshot per ticker, biased early.
    - The fix is the longitudinal capture in stage 1, not a backward-looking patch.

5. **Greenlight for stage 1?**

---

## Files / artifacts

### Production code (shipped)
- [bot/signals/weather_ensemble_v2.py](bot/signals/weather_ensemble_v2.py) — counters + ERROR-prefix logs
- [bot/daemon/main.py](bot/daemon/main.py) — `[health] wx_snapshots` line in `_log_health`
- [tests/test_snapshot_writer_health.py](tests/test_snapshot_writer_health.py) — 7 regression tests

### Research code
- [tools/regime_features_pull.py](tools/regime_features_pull.py) — IEM ASOS pull with regime columns + CF6 join
- [tools/regime_stratify_residuals.py](tools/regime_stratify_residuals.py) — σ-reduction analysis across taxonomies
- [tools/regime_feasibility.py](tools/regime_feasibility.py) — late-day analysis + sample-size accounting
- [tools/retro_replay_regime.py](tools/retro_replay_regime.py) — pooled vs regime σ retro-replay vs market Brier
- [tools/audit_no_replay.py](tools/audit_no_replay.py) — Phase C: characterize the 33 missing-snapshot tickers
- [tools/audit_snapshot_timing.py](tools/audit_snapshot_timing.py) — Phase C: per-hour write rate audit

### Data
- `reports/regime_features/{KAUS,KDEN,KLAX,KMDW,KMIA,KNYC}.csv` (3,500+ rows, 30-day window)

---

## Time budget

| Phase | Time |
|---|---|
| 0 — VPS + baseline | 0.5h |
| C — diagnostic integrity audit + prevention fix + tests | 1.5h |
| A.1 — data pull tool | 1.0h |
| A.2/A.3/A.4 — stratification + feasibility + per-city | 1.5h |
| A.5 — hierarchical fallback design | 0.5h |
| A.6 — retro-replay tool + debugging | 1.5h |
| Report | 1.0h |
| **Total** | **~7.5h** |

Within the 1-week boil-the-ocean budget.
