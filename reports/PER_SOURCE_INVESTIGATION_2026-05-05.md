# Per-source ensemble investigation — NY + LAX (2026-05-05)

**Status:** Findings + per-source verdict + Phase 3 (Option C) redesign proposal. **For Josh's review** before any code changes.
**Inputs:** [SCORECARD_NY_2026-05-05.md](SCORECARD_NY_2026-05-05.md), [SCORECARD_LAX_2026-05-05.md](SCORECARD_LAX_2026-05-05.md), source-code map (delivered by explore agent in this session).

---

## 0. Headline findings

1. **The current combine is FUNDAMENTALLY DIFFERENT in quality across cities.** NY's `combined_v2` is well-centered (peak bias -0.5 to +0.4°F). LAX's `combined_v2` is **+3.4°F hot at peak** — it's been systematically over-predicting LA highs all along. NY redesign and LAX redesign are different problems with different solutions.

2. **A lot of the "obvious wins" are already in code.** nbm/madis/tomorrow already removed from `GAUSSIAN_COMBINE_SOURCES`; per-city HRRR σ priors exist for denver/miami/austin; per-city exclusions exist (NYC excludes nws_point; LAX excludes metno+gem); effective-N grouping with rho discount already implemented. The combine is more sophisticated than the cross-bracket investigation suggested.

3. **The remaining bug is per-(city, phase, source) tuning.** Sources whose σ is over-stated (NWP at NY) get *under-weighted* by the precision-weighted combine. Sources whose bias is large at certain phases (LAX NWP at peak) need **either** removal or pre-combine bias correction. The combine logic is fine; the inputs are wrong.

4. **LAX is a marine-layer special case.** Almost every NWP source is +3-8°F hot at peak window — they all model heating without correctly capturing marine-layer dissipation timing. Only ECMWF (+0.8°F) and METAR (+1.8°F) are usable at peak. The current LAX exclusion list (`metno`, `gem`) catches only the worst two; HRRR/weather/icon/ukmo/nws_point should also be excluded or heavily corrected at peak phases.

5. **Diurnal peak times differ by city more than I assumed.** NY peaks LST 13. LAX peaks LST 11 (marine layer suppresses afternoon heating; high is hit by late morning). Implication: the LST cross-bracket gate must be per-city, not global.

---

## 1. Per-source investigation (16 sources)

### Format
Each source gets: Identity → NY profile → LAX profile → Verdict.
Verdict codes:
- **DROP** = remove from `GAUSSIAN_COMBINE_SOURCES`
- **EXCLUDE-PHASE(city, phases)** = keep in combine but exclude per-(city, phase)
- **CORRECT(city, phase, ±°F)** = apply pre-combine bias shift
- **σ-INFLATE(city, phase, ×N)** = scale claimed σ by factor N
- **KEEP** = current treatment is fine

---

### 1. `hrrr` — NOAA HRRR via Open-Meteo

- **Source code:** `bot/signals/sources/hrrr.py:144-158` (σ schedule), per-city σ priors at lines 119-141.
- **Data:** Open-Meteo's `gfs_hrrr` model (3km convection-allowing, hourly cycles, 18-48h horizon).
- **Current σ:** Day0 = 1.2°F (denver/miami/austin), 2.0°F elsewhere. Day1 = +0.5°F.

**NY profile:**
- Bias by LST: pre-peak +0.9 to +2.0°F; peak +0.9 to +1.4°F; post-peak +1.1 to +1.3°F.
- σ ratio: 0.4-0.9 (over-calibrated — claimed σ too wide).
- Best-fit reading: HRRR at NY is consistently **slightly hot (+1°F) but more accurate than its claimed σ**. Should be upweighted.

**LAX profile:**
- Bias by LST: **+3.4 to +4.3°F across all LST hours**. Catastrophic at peak: +4.0°F.
- σ ratio: 3.1-3.7 — claimed σ way too tight given realized error.
- HRRR at LAX has **structural marine-layer bias** that no σ scaling fixes. Bias is the issue, not uncertainty.

**Verdict:**
- **NY:** CORRECT(NY, all_phases, -1.0°F) — apply mild downward bias correction
- **LAX:** EXCLUDE-PHASE(LAX, peak_window+post_peak) OR CORRECT(LAX, all, -3.5°F). Recommend exclude.

---

### 2. `weather` — Open-Meteo default blend

- **Source code:** `bot/signals/sources/weather.py`. Open-Meteo's default model selection (blends GFS at US lat/lons).
- **Current σ:** Schedule similar to HRRR, ~2°F base.

**NY profile:**
- Bias: peak +1.1 to +1.6°F (mildly hot).
- σ ratio: 0.4-0.6 (over-calibrated).
- **Correlated with HRRR at 0.994 (NY) / 1.000 (LAX).** They are essentially the same source.

**LAX profile:**
- Bias: peak +4.0 to +4.2°F (matches HRRR exactly, as expected from corr).

**Verdict:**
- **DROP** entirely from `GAUSSIAN_COMBINE_SOURCES`. Functionally identical to HRRR. Removing eliminates double-counting at zero information loss.

---

### 3. `nbm` — Open-Meteo `gfs_seamless`

- **Status:** Already removed from `GAUSSIAN_COMBINE_SOURCES` 2026-04-29.
- Correlation with `weather` was 1.000. Same source, two API endpoints.

**Verdict:** Already DROPPED. Confirm the removal hasn't drifted back.

---

### 4. `metar` — Real-time KNYC/KLAX/etc. METAR observations

- **Source code:** `bot/signals/sources/metar_observations.py`. Per-(station, LST hour) diurnal fit stored in kv_cache.
- **Special case:** Past-peak clamp (`_PAST_PEAK_DELTA_F=1.0`) to avoid extrapolating warming past peak.

**NY profile:**
- Bias: peak +0.2 to +0.5°F (best-calibrated source for NY).
- σ ratio: 0.4-1.5. Pre-peak σ over-stated; post-peak nearly perfect (0.4-0.7).
- METAR is the workhorse at NY — keep at full weight.

**LAX profile:**
- Bias: peak +1.8 to +1.0°F (slightly hot but acceptable).
- σ ratio: 0.7-1.5. Tighter at post-peak (0.7).
- Best non-NWP source for LAX.

**Verdict:**
- **NY:** KEEP at full weight in all phases.
- **LAX:** KEEP at full weight in all phases. Mild +1.8°F peak bias acceptable since alternative sources are worse.

---

### 5. `nws_point` — NWS Point Forecast

- **Source code:** `bot/signals/sources/nws_point.py`. NWS api.weather.gov, `/points/{lat,lon}/forecastHourly`.
- **Current state:** Already EXCLUDED for NYC, Chicago, Miami via `EXCLUDED_SOURCES_BY_CITY`.

**NY profile:**
- Bias: peak -2.6 to -3.4°F (cold), worsening to -10°F at LST 21-23.
- σ ratio: 0.97-1.4. Roughly calibrated but bias is the killer.
- Already excluded for NY — confirms the exclusion was correct.

**LAX profile:**
- Bias: peak +5.3°F (hot — matches LAX NWP pattern).
- σ ratio: 1.3-1.9. Under-calibrated.
- **NOT currently excluded for LAX**, but should be.

**Verdict:**
- **NY:** KEEP excluded (no change).
- **LAX:** EXCLUDE-PHASE(LAX, all_phases). Add `nws_point` to LAX's exclusion set.

---

### 6. `ecmwf` — European Centre for Medium-Range Weather Forecasts

- **Source code:** `bot/signals/sources/ecmwf.py:92`. Open-Meteo `ecmwf_ifs025` model (IFS HRES 0.25°).
- **Current σ:** 3.0 + day_idx × 0.5°F (widest among models because pooled MAE was highest in eval).
- **Status:** PROBATIONARY (added 2026-04-30); σ × 1.3 inflation while probationary.

**NY profile:**
- Bias: peak -0.6 to -0.9°F (near-neutral).
- σ ratio: 0.5-0.6 (claimed σ too wide; realized RMSE much smaller than 3°F prior).
- Currently UNDER-weighted because its claimed σ is huge.

**LAX profile:**
- Bias: peak **+0.8°F (best NWP for LAX, by far)**.
- σ ratio: 1.4 — slightly under-calibrated but acceptable.
- The only NWP source that gets LAX right.

**Verdict:**
- **NY:** σ-INFLATE(NY, all, ×0.6) — tighten claimed σ to match realized. Or just drop the ×1.3 probationary inflation now.
- **LAX:** KEEP, σ-INFLATE(LAX, all, ×1.4) — slightly inflate to match realized RMSE. Most important non-METAR source for LAX.

---

### 7. `gem` — Canadian Meteorological Centre

- **Source code:** `bot/signals/sources/gem.py:96`. Open-Meteo `gem_global` model.
- **Current σ:** 2.0 + day_idx × 0.5°F.
- **Status:** PROBATIONARY. **Already EXCLUDED for LAX** per `EXCLUDED_SOURCES_BY_CITY`.

**NY profile:**
- Bias: peak -0.7 to -0.9°F. σ ratio 0.3-0.4 (way over-stated).
- Decent at NY but redundant with other low-bias sources.

**LAX profile:**
- Bias: **+7.2°F at peak** (catastrophic). Already excluded — confirms exclusion correct.

**Verdict:**
- **NY:** σ-INFLATE(NY, all, ×0.4) to fix the σ overstatement. Otherwise KEEP.
- **LAX:** Already EXCLUDED — keep.

---

### 8. `metno` — MET Norway

- **Source code:** `bot/signals/sources/metno.py:82`. Open-Meteo `metno_seamless` model.
- **Current σ:** 2.2 + day_idx × 0.5°F.
- **Status:** PROBATIONARY. **Already EXCLUDED for LAX**.

**NY profile:**
- Bias: peak -0.4°F (best-centered NWP at NY).
- σ ratio: 0.16 (claimed σ ~6× too wide). Strongly under-weighted.

**LAX profile:**
- Bias: +4.9°F at peak (hot). Already excluded.

**Verdict:**
- **NY:** σ-INFLATE(NY, peak+post_peak, ×0.2) — claimed σ is enormously overstated. Should be heavily upweighted at NY.
- **LAX:** Already EXCLUDED.

---

### 9. `icon` — German DWD ICON

- **Source code:** `bot/signals/sources/icon.py:96`. Open-Meteo `icon_seamless` model.
- **Current σ:** 2.5 + day_idx × 0.5°F. PROBATIONARY.

**NY profile:**
- Bias: peak +0.8 to +1.0°F (mildly hot).
- σ ratio: 0.2-0.3 (over-stated by 4-5×).

**LAX profile:**
- Bias: +5.1°F at peak (hot).
- σ ratio: 1.4-1.7.

**Verdict:**
- **NY:** σ-INFLATE(NY, all, ×0.3). KEEP.
- **LAX:** EXCLUDE-PHASE(LAX, peak+post_peak). Or CORRECT(LAX, all, -5°F) — but exclusion is cleaner.

---

### 10. `ukmo` — UK Met Office Unified Model

- **Source code:** `bot/signals/sources/ukmo.py:80`. Open-Meteo `ukmo_seamless` model.
- **Current σ:** 2.85 + day_idx × 0.5°F. PROBATIONARY.

**NY profile:**
- Bias: peak -0.4°F (centered).
- σ ratio: 0.27-0.34 (over-stated).

**LAX profile:**
- Bias: **+7.0°F at peak** (worst at LAX).
- σ ratio: 1.9-2.3 (under-calibrated to compound the bias).

**Verdict:**
- **NY:** σ-INFLATE(NY, all, ×0.3). KEEP.
- **LAX:** EXCLUDE-PHASE(LAX, all_phases). Add to LAX exclusion list immediately.

---

### 11. `madis` — Mesonet via NWS MADIS

- **Status:** Already removed from `GAUSSIAN_COMBINE_SOURCES` 2026-04-29.
- **NY:** -9.6 to -15°F bias at LST 18-23 (catastrophic). Removal was correct.
- **LAX:** -7 to -11°F bias at LST 18-23. Removal was correct.

**Verdict:** Already DROPPED. Confirm removal hasn't drifted back.

---

### 12. `nws_5min` — 5-minute ASOS observations

- **Source code:** `bot/signals/sources/nws_5min.py`. NWS `/stations/{station}/observations`. Per-city station mapping in `PRIMARY_5MIN_STATION_BY_CITY`.
- **Status:** "DARK MODE" per source map — present in source code but not yet wired to live combine. Already EXCLUDED for NYC, CHI, MIA.

**NY profile (snapshots include it because tool reads everything):**
- Bias swings -1.6 → +4.9 across LST hours. σ ratio 1.0-8.2 (very under-calibrated late-day).

**LAX profile:**
- Bias: -7°F at LST 06-08 (very cold), then small. σ ratio 0.8-7.0 (wildly variable).
- Already useful at LAX in some hours — per code map, _MIN_LST_HOUR_TO_FIRE = 11.

**Verdict:**
- **All cities:** KEEP DARK (don't enable). Or EXCLUDE-PHASE(all, post_peak) when enabled.

---

### 13. `nws_5min_diurnal` — 5-min obs through diurnal fit

- **Source code:** `bot/signals/sources/nws_5min_diurnal.py`. Reuses METAR diurnal fit but with 5-min resolution input.
- **Current σ:** From METAR fit RMSE.

**NY profile:**
- Bias: peak +0.8°F (mild hot). σ ratio: 0.4-1.0 at low hours, 4.7 post-peak.
- Sparse data (n=143 total) — small-sample.

**LAX profile:**
- Bias: peak +2.0 to +2.5°F (hot). σ ratio: 1.0-1.6.
- More data than NY.

**Verdict:**
- **NY:** KEEP (currently ON), but σ-INFLATE(NY, post_peak, ×4.7) when more data lands.
- **LAX:** KEEP, but watch for the +2.5°F hot bias.

---

### 14. `nws_5min_analog` — 5-min obs + analog historical match

- **Source code:** `bot/signals/sources/nws_5min_analog.py`.
- **Status:** DEMOTED to SHADOW 2026-05-02 (regime-coverage issue: 35 historical days, today's market 93-95°F outside training range).

**Verdict:** Already SHADOW. Don't re-enable until training data extended.

---

### 15. `tomorrow` — Tomorrow.io

- **Status:** Already removed from `GAUSSIAN_COMBINE_SOURCES` 2026-04-26 (TOS storage clause + reanalysis-only history).
- **NY:** -3 to -5°F bias at peak.
- **LAX:** Mild +1.7°F bias at peak (best of the LAX hot-biased crowd).

**Verdict:** Already DROPPED. Don't revisit unless TOS changes.

---

### 16. `afd_bias` — Area Forecast Discussion-derived bias signal

- **Source code:** `bot/signals/sources/afd.py`. Uses GPT-4o-mini if `OPENAI_API_KEY` set; else keyword heuristics.
- **Special handling:** Not in `GAUSSIAN_COMBINE_SOURCES` — applied as post-combine logit shift (±3°F cap).

**Verdict:** Already correctly handled outside the combine. No change.

---

## 2. Diurnal-peak boundaries (per-city, empirical)

| City | Peak hour (LST) | First post-peak hour (≥80% within-1°F) | Notes |
|---|---|---|---|
| NY | 13 | -1 (never reaches 80%) | Tops at 0.78 — 22% of days have official daily_high > hourly METAR by >1°F |
| LAX | 11 | 12 | Sharp transition; marine-layer suppresses afternoon |

Per-city LST gate for cross-bracket should be:
- **NY:** Fire when LST ≥ 15 (allowing 2h post-peak for METAR to settle)
- **LAX:** Fire when LST ≥ 12

---

## 3. Phase 3 — Option C redesign proposal (concrete configs)

### 3a. Drop sources that are duplicates

**`bot/signals/weather_sources.py::GAUSSIAN_COMBINE_SOURCES`** — remove `weather` (duplicate of `hrrr`):

```python
# Currently includes: hrrr, weather, nws_point, metar, icon, ukmo, gem, metno, ecmwf, nws_5min, nws_5min_diurnal
# After: hrrr, nws_point, metar, icon, ukmo, gem, metno, ecmwf, nws_5min, nws_5min_diurnal
```

Note: `weather` snapshot continues to be recorded for monitoring; just not in the combine.

### 3b. Per-(city, phase) exclusions — new

**Add `EXCLUDED_SOURCES_BY_CITY_PHASE`** to `weather_sources.py`:

```python
EXCLUDED_SOURCES_BY_CITY_PHASE: dict[tuple[str, str], frozenset[str]] = {
    # Existing per-city exclusions (unchanged behavior unless overridden):
    # nyc:        {nws_point}                        — global cold bias at NY
    # chicago:    {nws_point, nws_5min}
    # miami:      {nws_point, nws_5min}
    # los_angeles: {metno, gem}                       — both +5/+7°F hot
    
    # New: LAX peak/post-peak exclusions (marine-layer hot bias on NWP)
    ("los_angeles", "peak_window"): frozenset({"hrrr", "weather", "nws_point", "icon", "ukmo"}),
    ("los_angeles", "post_peak"):   frozenset({"hrrr", "weather", "nws_point", "icon", "ukmo"}),
    # Pre-peak / overnight: keep all NWP — peak bias hasn't manifested yet,
    # diversification value > bias contribution.
}
```

Effect: at LAX peak/post-peak, the combine is `ecmwf + metar + nws_5min_diurnal` (+ gem/metno already excluded). Three independent sources. Two with low bias.

### 3c. Per-(city, phase, source) σ multipliers — new

**Add `SIGMA_MULTIPLIER_BY_CITY_PHASE_SOURCE`** to `weather_ensemble_v2.py`:

```python
# Multiplier applied to source-reported sigma_f BEFORE combine. Derived
# from (realized RMSE / claimed σ) per scorecard data. Ratios <1 mean
# claimed σ is too wide; ratios >1 mean too tight.
SIGMA_MULTIPLIER_BY_CITY_PHASE_SOURCE: dict[tuple[str, str, str], float] = {
    # NY: NWP models all have over-stated σ (over-cautious priors).
    # Tighten to match realized RMSE so they get appropriate weight.
    ("nyc", "peak_window",  "ecmwf"):  0.6,
    ("nyc", "post_peak",    "ecmwf"):  0.6,
    ("nyc", "peak_window",  "gem"):    0.4,
    ("nyc", "post_peak",    "gem"):    0.4,
    ("nyc", "peak_window",  "metno"):  0.2,
    ("nyc", "post_peak",    "metno"):  0.2,
    ("nyc", "peak_window",  "icon"):   0.3,
    ("nyc", "post_peak",    "icon"):   0.3,
    ("nyc", "peak_window",  "ukmo"):   0.3,
    ("nyc", "post_peak",    "ukmo"):   0.3,
    
    # LAX: ECMWF slightly under-calibrated; mild inflation.
    ("los_angeles", "peak_window", "ecmwf"): 1.4,
    ("los_angeles", "post_peak",   "ecmwf"): 1.4,
    
    # Combined-output post-peak σ inflation (handles correlated NWP
    # disagreement that the per-source σ doesn't capture).
    # NOT a per-source key — applied to combined σ at post-peak phase.
    # Lives separately as POST_COMBINE_SIGMA_INFLATION_BY_CITY_PHASE.
}

POST_COMBINE_SIGMA_INFLATION_BY_CITY_PHASE: dict[tuple[str, str], float] = {
    ("nyc", "post_peak"):         3.0,    # combined ratio 2.6-3.8
    ("nyc", "overnight"):         3.0,
    ("los_angeles", "post_peak"): 3.0,    # combined ratio 3.5-3.8
    # Pre-peak / peak_window: no inflation (combined already wide enough)
}
```

### 3d. Per-(city, phase, source) bias corrections — new

```python
BIAS_CORRECTION_BY_CITY_PHASE_SOURCE: dict[tuple[str, str, str], float] = {
    # NY HRRR: mild +1°F hot bias; subtract.
    ("nyc",         "peak_window", "hrrr"):     -1.0,
    ("nyc",         "post_peak",   "hrrr"):     -1.0,
    
    # LAX METAR: +1.8°F bias at peak (not enough to exclude; correct).
    ("los_angeles", "peak_window", "metar"):    -1.5,
    ("los_angeles", "post_peak",   "metar"):    -0.5,
    
    # LAX ECMWF: +0.8°F at peak; mild correction.
    ("los_angeles", "peak_window", "ecmwf"):    -0.8,
    ("los_angeles", "post_peak",   "ecmwf"):    -0.5,
}
```

### 3e. Per-city LST cross-bracket gate — new

**`bot/daemon/cross_bracket_shadow.py`** — add config in `bot/config.py`:

```python
CROSS_BRACKET_LST_GATE_BY_CITY: dict[str, tuple[int, int]] = {
    # (min_lst_hour_inclusive, max_lst_hour_inclusive) on the settlement-day LST
    "nyc":         (15, 23),  # NY peak LST 13; allow 2h for METAR to settle
    "los_angeles": (12, 23),  # LAX peak LST 11; sharper transition
    "chicago":     (15, 23),  # CHI peak ~LST 16; same rule as NY
    "miami":       (14, 23),  # MIA peak ~LST 14
    "austin":      (15, 23),
    "denver":      (15, 23),
}
```

Replaces the global `CROSS_BRACKET_MIN_TTE_HOURS=3, MAX=7` for entry. (TTE gate could remain as a backstop maximum-staleness check.)

### 3f. Phase 3 implementation plan (concrete file edits)

| File | Change | LOC |
|---|---|---|
| `bot/signals/weather_sources.py` | Remove `weather` from GAUSSIAN_COMBINE_SOURCES; add `EXCLUDED_SOURCES_BY_CITY_PHASE` | ~30 |
| `bot/signals/weather_ensemble_v2.py` | Update `is_excluded_for_city` to also check phase; add 3 new dicts (σ-mult, bias-correct, post-combine-σ-inflate); apply in `_collect_gaussians` | ~120 |
| `bot/signals/weather_ensemble_v2.py` | Replace global `_COMBINED_SIGMA_FLOOR_F` with phase-aware function | ~30 |
| `tools/lst_align.py` | Already done | 0 |
| `bot/daemon/cross_bracket_shadow.py` | Replace TTE gate with LST-of-settle-day gate per `CROSS_BRACKET_LST_GATE_BY_CITY` | ~50 |
| `bot/config.py` | Add `CROSS_BRACKET_LST_GATE_BY_CITY` | ~15 |
| `tests/signals/test_per_phase_corrections.py` | New tests for per-phase exclusion + bias + σ | ~200 |
| `tests/daemon/test_cross_bracket_lst_gate.py` | New tests for LST gate | ~100 |
| `tools/per_city_source_scorecard.py` | Add Phase 4 backtest section that compares old-vs-new on the 5 historical losses | ~150 |

Total: ~700 LOC of Phase 3+4 changes.

---

## 4. Phase 4 — backtest acceptance

After Phase 3 ships, re-run the cross-bracket P&L sim (`tools/backtest_cross_bracket_historical.py`) with the new combine. Acceptance per Josh's bar:
- **Required:** EV-positive forecasts (per-bracket Brier strictly better than current ensemble on ≥ 14 days held-out).
- **Required:** EV-positive trade decisions (mean per-decision EV > 0 net of fees).
- **Ideal but not required:** P&L-positive backtest on ≥30 settlements per city.
- **Live validation:** ≥7 days shadow-vs-realized convergence within ±20%.

---

## 5. Open questions before I implement

1. **Drop `weather` from combine — confirm.** It's 0.99-1.00 correlated with `hrrr` at NY and 1.00 at LAX. Removing eliminates a duplicate. Sound?

2. **Per-(city, phase) exclusion structure.** I'm proposing extending `EXCLUDED_SOURCES_BY_CITY` to a `dict[(city, phase)]` keyed structure. Backwards-compatible (existing per-city exclusions stay applied across all phases). Want me to use a different structure?

3. **σ multiplier values.** I derived these from observed RMSE / claimed σ ratios at peak/post-peak. They're empirical but small-sample (10 days of snapshots, ~10-25 same-day samples per source per LST bin). Open to setting more conservative multipliers (e.g., halve the corrections) until live data confirms.

4. **LAX peak-phase exclusions.** I'm proposing dropping HRRR/weather/nws_point/icon/ukmo from LAX peak+post-peak (5 sources!). That leaves the LAX combine with `ecmwf + metar + nws_5min_diurnal` (gem/metno already excluded). Three sources is thin. Acceptable, or do you want me to keep more sources with bias correction instead of exclusion?

5. **LST gate per city.** I picked `(15, 23)` for NY (peak+2). Open to (14, 23) or (16, 23). The narrower the window, the less data we get; the wider, the more pre-peak garbage decisions.

6. **Implementation order:** I propose (a) `weather` drop + per-(city, phase) exclusions first, (b) σ multipliers second, (c) bias corrections third, (d) LST gate fourth. Each is a separate commit with backtest in between. Sound, or batch into one PR?

7. **Should I run scorecards on the other 4 cities before implementing?** Phase 3 redesign currently codifies NY+LAX. CHI/AUS/MIA/DEN might also benefit from per-phase exclusions and we'd save a re-implementation if we know their patterns now. Per the original sequencing decision, NY→LAX→others, so we stop here for now and revisit. Confirm or reconsider?
