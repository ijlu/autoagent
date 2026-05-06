# Dynamic per-source calibration — plan

**Author:** Claude + Josh
**Date:** 2026-04-24
**Status:** Approved 2026-04-24 — building

## Resolved decisions (2026-04-24)

1. **Backfill depth:** 30 days. Beyond that EWMA(14d) discounts samples to noise.
2. **Granularity:** Per-station bias, pooled-across-stations RMSE. Captures station microclimate without splitting RMSE samples thin.
3. **EWMA half-life:** 14 days. Balances seasonal responsiveness against day-to-day noise.
4. **Safety-floor σ for un-backfillable sources** (nws_point, tomorrow, weather): `σ_eff = max(raw_σ × 1.25, 2.5°F)` until per-source `n_obs ≥ 14`.
5. **AFD multiplier bounds:** `α ∈ [0.25, 1.5]`. Wider band lets evidence move α faster.
6. **Ground truth:** Kalshi settlement primary, METAR running-high fallback.
7. **Flip thresholds:** condition-based, not calendar-based. Three conjunctive gates: (a) v2 n ≥ 500 pooled AND n ≥ 50 per major family; (b) v2 markout > v1 markout by ≥ 2σ on bootstrap; (c) no family with n ≥ 30 shows v2 significantly worse (>2σ) than v1.
8. **Scope:** stay in source_calibration only. Don't touch `weather_source_weights`. Revisit after 2-4 weeks of calibration data.
**Context:** Markout analysis of post-flip WEATHER_ENSEMBLE_V2 data ([reports/WEATHER_MARKOUT_APR24.md](WEATHER_MARKOUT_APR24.md)) showed v2 is directionally encouraging on 4 of 6 weather families at Δ=900s, but catastrophically wrong on two specific markets (NY T67, MIA B84.5) where a cold-biased `nws_point` (−1.9°F vs consensus) compounded with an AFD shift of −2.1°F to put the combined mean ~3°F below truth. Rather than patch with static constants, Josh asked that we make per-source bias and skill a learned trailing measurement.

## Goal

Replace every hand-tuned source constant in `WEATHER_ENSEMBLE_V2` with a trailing-data-driven estimate of that source's bias and skill. Each source self-corrects as data accumulates; new sources added later inherit the same treatment for free.

## Success criteria

1. For each of the 7 Gaussian sources (hrrr, nbm, nws_point, tomorrow, weather, metar, madis) plus AFD bias, we persist a rolling EWMA of `error = actual_high - forecast_high` (bias) and RMSE per source, optionally per station.
2. `predict_v2` reads calibration at predict-time, applies `corrected_mean = raw - ewma_bias` and `effective_sigma = sqrt(raw_sigma² + ewma_rmse²)` before combine.
3. AFD shift is scaled by a learned multiplier that reflects whether its past parse-to-actual-bias agreement was strong.
4. Cold-start: backfill 30+ days of history for the 4 sources we can archive (HRRR, NBM, AFD, METAR), so those sources are calibrated on day 1. The other 3 (nws_point, tomorrow, weather/Open-Meteo) get a safety-floor σ expansion until their own live history stacks up.
5. Verify on known bad cases: the NY T67 row that produced v2 FV = 3¢ would have produced FV ≥ 25¢ under calibrated v2.
6. All logic behind `WEATHER_SOURCE_CALIBRATION_V1` flag; default off; flip after 48h shadow soak.

## Assumptions

| Assumption | Status | Verification |
|---|---|---|
| NOAA NOMADS stores full HRRR/NBM GRIB2 runs back ≥30 days | **Unverified** | Curl a sample archive URL before writing backfill code |
| iem.weather.gov archives AFD text by office/date back ≥30 days | **Unverified** | Check their archive page for KBGM, KLWX, KLOT, KMIA offices |
| Actual daily highs derivable from METAR running-high-at-midnight-local | **Partially verified** | We already do this online; need to confirm historical METAR archive (Iowa State Mesonet or equivalent) is queryable by station+date |
| `_collect_gaussians` happens before any corrected-mean step could apply, so we can transform each Gaussian before `combine_gaussian` | **Verified** — code read | `bot/signals/weather_ensemble_v2.py:535` — `_weighted_inputs_with_group_discount(gaussians)` then `combine_gaussian` |
| nws_point cold bias is persistent, not a one-day weather artifact | **Unverified** | Need 5+ days of snapshot data to confirm; have 2 days so far |
| Daily-high ground truth is unambiguous (local midnight-to-midnight) | **Mostly verified** | Kalshi settlement is on NWS-official daily high; METAR reconstruction should match within 0.1°F |
| predict_v2 is invoked often enough that calibration reads can't hit the DB cold every call | **Verified** | We have `forecast_cache`; easy to add a calibration cache with a short TTL |

## Source-by-source backfillability

| Source | Archive? | Backfill path | Day-1 state |
|---|---|---|---|
| HRRR | Yes — NOMADS GRIB2 | `nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/hrrr.<date>/conus/hrrr.t<cycle>z.wrfsfcf<hour>.grib2`, subset by station lat/lon | 30-day EWMA fit |
| NBM | Yes — NOMADS GRIB2 | Similar URL pattern under `/blend/prod/blend.<date>/...` | 30-day EWMA fit |
| AFD | Yes — iem.weather.gov | `https://mesonet.agron.iastate.edu/wx/afos/p.php?pil=AFDXXX&e=<YYYY-MM-DDThh:mmZ>` | 30-day EWMA fit (parse each doc with our existing parser, log bias prediction, compare to actual) |
| METAR | Yes — iem.weather.gov | ASOS 1-minute archive per station, daily files | Trivial (METAR running-high is observation, not forecast) |
| MADIS | Partial — NOAA archive but sparse | Same treatment as METAR where available | Minimal calibration; wide default σ |
| nws_point | **NO public archive** | Live logging only | Safety-floor σ expansion until ~14 days live data |
| tomorrow | **NO public archive (free tier)** | Live logging only | Safety-floor σ expansion until ~14 days live data |
| weather (Open-Meteo) | **NO** (historical endpoint ignores `models=`) | Live logging only | Safety-floor σ expansion until ~14 days live data |

## Schema

Two tables — bias is per-(source, station) since station microclimate matters; RMSE is pooled-per-source since splitting it thin starves sample size.

```sql
CREATE TABLE source_calibration_bias (
    source            TEXT NOT NULL,
    station           TEXT NOT NULL,
    ewma_bias_f       REAL NOT NULL DEFAULT 0,
    n_obs             INTEGER NOT NULL DEFAULT 0,
    last_updated      TEXT,
    PRIMARY KEY (source, station)
);

CREATE TABLE source_calibration_skill (
    source            TEXT NOT NULL PRIMARY KEY,
    ewma_rmse_f       REAL NOT NULL DEFAULT 0,
    n_obs             INTEGER NOT NULL DEFAULT 0,
    last_updated      TEXT
);
```

EWMA update: for half-life H days, decay factor α = 1 − 2^(−1/H). New sample `e_i`:
- `ewma_bias_f ← α × e_i + (1−α) × ewma_bias_f`
- `ewma_rmse_f ← sqrt(α × e_i² + (1−α) × ewma_rmse_f²)`

H = 14 days initial choice. Rationale: fast enough to catch seasonal drift (winter → spring), slow enough to be noise-resistant with ~1 sample/day/source/station.

## Architecture

Read path (predict-time):
```
predict_v2(ticker, market_data)
 → _collect_gaussians()                       # [unchanged]
 → _apply_source_calibration(gaussians)       # NEW: shift mean, inflate sigma per source
 → _weighted_inputs_with_group_discount(...)  # [unchanged]
 → combine_gaussian(...)                      # [unchanged]
 → AFD shift × learned_α (NEW multiplier)     # [modified]
 → probability_for_market(...)                # [unchanged]
```

Write path (calibration update, runs as scheduler task):
```
update_source_calibration(conn)
 → for each (station, date) where we have snapshots + actual:
     for each source:
         error = actual_high - snapshot.forecast_high_f
         ewma_update(source, station=NULL, error)
 → UPSERT into source_calibration
```

Cadence: once per cycle (60s) read is cached for the cycle. Write runs every 6h (a day's forecasts and their actual high are worth 1 calibration update, so there's nothing to gain running faster than the daily cadence, but 6h lets us catch late-day settlements without a full-day stall).

## Verify pairs (build plan)

1. **NOMADS + iem.weather.gov spot-checks** → verify: curl a 2026-04-01 HRRR file, a 2026-04-01 NBM file, and an AFD for KLWX; confirm all 3 download successfully.
2. **HRRR backfill script** `tools/backfill_hrrr_history.py` → verify: run for 3 stations × 3 days = 9 (station, date) samples, inspect forecast_high_f values look plausible (50-95°F, reasonable σ).
3. **NBM backfill script** `tools/backfill_nbm_history.py` → verify: same bar.
4. **AFD backfill script** `tools/backfill_afd_history.py` → verify: for 3 offices × 3 days, our `_parse_afd_bias()` returns a value (not None) for ≥80% of fetched docs.
5. **Actuals pipeline** `tools/backfill_actual_highs.py` → verify: daily high for NYC Apr 1 matches Kalshi settlement data we already have within 0.5°F.
6. **Calibration fit script** `tools/fit_source_calibration.py` → verify: running on 30 days of data populates `source_calibration` with non-zero `ewma_bias_f` for HRRR/NBM/AFD, n_obs ≈ 30 each.
7. **Calibration schema migration** → verify: `source_calibration` exists after init, `PRAGMA table_info` matches.
8. **`_apply_source_calibration()` function** in `weather_ensemble_v2.py` behind `WEATHER_SOURCE_CALIBRATION_V1` flag → verify: unit test hits predict_v2 twice with flag on/off, observes bias-shifted mean + inflated sigma only when on.
9. **AFD multiplier** applied in same predict_v2 call → verify: unit test with stubbed AFD bias, asserts shift == raw × α × confidence.
10. **Replay NY T67 + MIA B84.5 rows** under calibrated v2 → verify: corrected combined_v2 mean ≥ 66.5°F (was 64.3), resulting FV ≥ 25¢ (was 3¢). Scripts this as a regression test.
11. **Daemon scheduler hook** — add `update_source_calibration` @ 6h to `bot/daemon/main.py` → verify: run daemon locally for 1 cycle; confirm table gets updated.
12. **Deploy** `deploy/04_redeploy.sh` (automatic via existing script) with flag OFF → verify: import check passes, daemon starts.
13. **48h flag-off soak** → verify: no `source_calibration` regressions; snapshot data continues accumulating.
14. **Flip flag ON** manually in .env on VPS → verify: next cycle's v2 FV on weather markets reflects calibration; compare a few live rows to expected.
15. **Re-run markout analysis at 48h post-flip** → verify gate: v2 ≥ v1 markout at 2σ on Δ=900s pooled; ≥4/6 families positive.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| NOMADS archive doesn't go back 30 days for HRRR/NBM | Day-1 cal covers <30 days of history | Use what's available; accept 14-day cal as floor if needed |
| AFD parser behavior differs on archived vs live text (format drift) | Cal fit biased | Sanity-check parser outputs on archived docs first; skip days where parse fails |
| Station-pooled calibration hides station-specific bias (e.g., KMIA warmer than KJFK) | Miami might be systematically under-corrected | Start pooled; split per-station if pooled RMSE stays > 2°F after 30 days |
| Forecast-vs-actual at different horizons (HRRR 1h vs HRRR 12h) has different error profiles | Pooled-across-horizon understates near-term precision | Start pooled; add (horizon-bucket) dimension later |
| EWMA H=14d is wrong for rapid seasonal shifts | March-to-April transition under- or over-corrects for 1-2 weeks | Monitor `ewma_rmse_f` over time; revisit H choice based on data |
| Calibration reads become a hot path that slows predict_v2 | Latency creep on event-driven requote | Cache calibration in-process for 1-cycle duration; DB read is ~1ms |
| nws_point cold bias isn't persistent (maybe just April) | We widen σ permanently on a seasonal effect | σ widening is modest (~1.5x); the EWMA will drift back in if bias disappears |
| Live calibration table writes race with cycle reads | Corrupt read | Use `DB_WRITE_LOCK` on update; predict reads are read-only so fine |

## Open questions for Josh

1. **Backfill depth**: 30, 60, or 90 days? 30 gives us ~30 samples/source (tight); 60+ adds robustness but more backfill compute.
2. **Per-station vs pooled**: start pooled? My inclination: yes, station-split only if pooled RMSE doesn't stabilize.
3. **EWMA half-life**: 14d (my default)? Faster (7d, more noise) or slower (28d, less seasonal responsiveness)?
4. **Safety-floor σ** for uncalibrated sources (nws_point, tomorrow, weather): start with `σ_eff = max(raw_σ × 1.25, 2.5°F)` until their own n_obs ≥ 14?
5. **AFD multiplier** bounds: `α ∈ [0.25, 1.5]`? Or tighter (`[0.5, 1.25]`)? Lets AFD be amplified or dampened based on track record.
6. **Ground truth source**: Kalshi settlement high (most authoritative, but only available post-settlement) or METAR running-high at local midnight (available immediately)? I'd use both — Kalshi for confirmation, METAR for speed.
7. **Flag rollout**: flip after 48h soak (my default) or wait for 5+ days of live calibration writes before flipping?
8. **Scope creep check**: should this plan also touch the ensemble source weights (`weather_source_weights`), or strictly stay in calibration-of-individual-Gaussians layer?

## Estimate

~10-12 hours of focused work:
- 1h: NOMADS / IEM URL pattern verification + auth/headers
- 2h: HRRR backfill
- 2h: NBM backfill
- 2h: AFD backfill + parser replay on historical
- 1h: Actuals pipeline
- 2h: Calibration fit + schema + daemon scheduler hook
- 1h: `_apply_source_calibration` in predict_v2 + AFD multiplier
- 1h: Unit tests (including NY T67 / MIA B84.5 regression cases)

Then: 48h soak (no code work, just monitoring) → flip flag → 48h measure → gate decision.

Total calendar: ~5 days to go-live decision.
