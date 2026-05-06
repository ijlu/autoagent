# Per-source ensemble scorecard — AUSTIN (KAUS)

**Series:** `KXHIGHAUS`  
**LST offset:** -6h  
**Generated:** 2026-05-05T18:53:07Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 16,691 snapshots, 2 settled days, 10 sources.
- **Empirical peak hour:** LST 15; running-high locks (≥80% days) by LST 15.
- **Biggest peak-window bias offenders:** nws_5min (+3.2°F), ecmwf (-3.2°F), metno (-2.8°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | -1.38 | -2.95 | -1.97 | -0.65 | 1.27 | 0.75 | -0.41 | -0.64 | 2293 |
| ecmwf | -1.60 | -1.91 | -2.43 | -2.91 | -3.00 | -3.31 | -3.28 | -1.01 | 2292 |
| gem | — | — | — | — | — | — | 2.01 | 1.71 | 484 |
| hrrr | 0.78 | 0.90 | 0.65 | 0.40 | -0.18 | -0.14 | 0.45 | -0.30 | 2094 |
| metar | -3.19 | -5.08 | -3.73 | -2.37 | -1.46 | -0.40 | -0.44 | -0.76 | 2291 |
| metno | -1.29 | -1.30 | -2.45 | -2.45 | -2.71 | -2.82 | -2.80 | -0.99 | 2292 |
| nws_5min | — | — | — | -2.43 | 2.72 | 3.65 | -0.93 | — | 735 |
| nws_5min_diurnal | — | — | — | -1.47 | -0.23 | -0.01 | 1.30 | 0.71 | 1249 |
| nws_point | — | — | — | — | — | — | 1.09 | -1.65 | 522 |
| weather | 1.03 | 1.12 | 0.87 | 0.62 | 0.04 | 0.08 | 0.73 | 0.56 | 2292 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | — | — | — | — | — | -2.28 | 24 |
| ecmwf | — | — | — | — | — | — | — | 3.86 | 24 |
| hrrr | — | — | — | — | — | — | — | 0.97 | 24 |
| metar | — | — | — | — | — | — | — | -2.85 | 24 |
| metno | — | — | — | — | — | — | — | 3.30 | 24 |
| nws_5min_diurnal | — | — | — | — | — | — | — | -1.08 | 24 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.20 | 0.42 | 0.34 | 0.13 | 0.21 | 0.14 | 0.66 | 0.70 | 2293 |
| ecmwf | 0.56 | 0.66 | 0.86 | 1.01 | 1.04 | 1.15 | 1.68 | 1.45 | 2292 |
| gem | — | — | — | — | — | — | 1.24 | 0.89 | 484 |
| hrrr | 0.10 | 0.12 | 0.09 | 0.07 | 0.04 | 0.02 | 0.52 | 0.29 | 2094 |
| metar | 1.62 | 2.57 | 2.08 | 1.28 | 0.87 | 0.27 | 2.24 | 2.16 | 2291 |
| metno | 0.28 | 0.29 | 0.53 | 0.53 | 0.59 | 0.61 | 1.41 | 1.49 | 2292 |
| nws_5min | — | — | — | 1.38 | 1.59 | 2.36 | 1.82 | — | 735 |
| nws_5min_diurnal | — | — | — | 0.91 | 0.36 | 0.34 | 1.18 | 0.78 | 1249 |
| nws_point | — | — | — | — | — | — | 1.43 | 1.07 | 522 |
| weather | 0.14 | 0.15 | 0.12 | 0.10 | 0.04 | 0.02 | 0.47 | 0.62 | 2292 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.45 | 3.01 | 2.44 | 0.91 | 1.48 | 0.84 | 1.08 | 0.70 | 2293 |
| ecmwf | 1.61 | 1.91 | 2.48 | 2.91 | 3.00 | 3.31 | 3.37 | 2.91 | 2292 |
| gem | — | — | — | — | — | — | 2.47 | 1.77 | 484 |
| hrrr | 0.78 | 0.90 | 0.67 | 0.56 | 0.33 | 0.19 | 0.70 | 0.46 | 2094 |
| metar | 3.24 | 5.13 | 4.16 | 2.56 | 1.73 | 0.53 | 1.11 | 0.79 | 2291 |
| metno | 1.30 | 1.32 | 2.45 | 2.45 | 2.71 | 2.83 | 2.82 | 2.97 | 2292 |
| nws_5min | — | — | — | 2.77 | 3.00 | 3.76 | 2.45 | — | 735 |
| nws_5min_diurnal | — | — | — | 1.82 | 0.72 | 0.68 | 2.35 | 1.57 | 1249 |
| nws_point | — | — | — | — | — | — | 2.87 | 2.14 | 522 |
| weather | 1.04 | 1.12 | 0.89 | 0.73 | 0.28 | 0.15 | 0.94 | 1.24 | 2292 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.83 | 1.09 | 1.26 | 2.12 | 2.16 | — | — | 2318 |
| ecmwf | 2.92 | 3.42 | 3.07 | 2.63 | 1.83 | 3.86 | — | 2316 |
| gem | 1.71 | 2.64 | — | — | — | — | — | 484 |
| hrrr | 0.51 | 0.62 | 0.30 | 0.67 | 0.84 | 1.04 | — | 2118 |
| metar | 0.94 | 1.05 | 1.79 | 3.74 | 4.04 | — | — | 2316 |
| metno | 2.96 | 2.81 | 2.72 | 2.37 | 1.37 | 3.30 | — | 2316 |
| nws_5min | 4.63 | 2.69 | 3.16 | 3.57 | — | — | — | 735 |
| nws_5min_diurnal | 1.62 | 2.07 | 0.72 | 2.43 | — | — | — | 1273 |
| nws_point | 2.63 | 2.51 | — | — | — | — | — | 522 |
| weather | 1.22 | 0.83 | 0.26 | 0.87 | 1.07 | — | — | 2293 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| hrrr | weather | 6 | 1.000 |
| hrrr | metno | 6 | 0.838 |
| metno | weather | 6 | 0.838 |
| metno | nws_5min | 6 | -0.803 |
| combined_v2 | ecmwf | 6 | 0.794 |
| metar | metno | 6 | -0.778 |
| ecmwf | metar | 6 | -0.770 |
| ecmwf | nws_5min | 6 | -0.696 |
| nws_5min | weather | 6 | -0.676 |
| hrrr | nws_5min | 6 | -0.676 |
| metar | nws_5min | 6 | 0.565 |
| combined_v2 | nws_5min | 6 | -0.554 |
| ecmwf | metno | 6 | 0.518 |
| metar | nws_5min_diurnal | 6 | 0.479 |
| ecmwf | nws_5min_diurnal | 6 | -0.434 |
| hrrr | metar | 6 | -0.379 |
| metar | weather | 6 | -0.379 |
| nws_5min_diurnal | weather | 6 | 0.377 |
| hrrr | nws_5min_diurnal | 6 | 0.377 |
| combined_v2 | nws_5min_diurnal | 6 | -0.370 |
| combined_v2 | metar | 6 | -0.320 |
| combined_v2 | weather | 6 | -0.132 |
| combined_v2 | hrrr | 6 | -0.132 |
| combined_v2 | metno | 6 | 0.091 |
| metno | nws_5min_diurnal | 6 | 0.081 |
| nws_5min | nws_5min_diurnal | 6 | -0.073 |
| ecmwf | hrrr | 6 | 0.070 |
| ecmwf | weather | 6 | 0.070 |

_corr ≥ 0.7 = effectively redundant; treat as one source._


### 6. METAR signal value by LST hour

Distribution of (running_max_at_hour − daily_high). Negative = running_max still climbing. ~0 = high reached. `frac_at_high` = fraction of days where running_max ≥ daily_high − 0.5°F at that hour.
| lst_hour | n | median_gap_F | p10 | p90 | frac_at_high | frac_within_1F |
|---|---|---|---|---|---|---|
| 0 | 100 | -18.00 | -32.00 | -2.00 | 0.06 | 0.08 |
| 1 | 100 | -17.00 | -32.00 | -2.00 | 0.07 | 0.09 |
| 2 | 101 | -17.00 | -32.00 | -2.00 | 0.07 | 0.09 |
| 3 | 100 | -17.00 | -32.00 | -1.00 | 0.07 | 0.10 |
| 4 | 100 | -17.00 | -32.00 | -1.00 | 0.07 | 0.10 |
| 5 | 101 | -17.00 | -32.00 | -2.00 | 0.07 | 0.10 |
| 6 | 101 | -17.00 | -32.00 | -2.00 | 0.07 | 0.10 |
| 7 | 101 | -16.00 | -31.00 | -1.00 | 0.07 | 0.11 |
| 8 | 101 | -14.00 | -26.00 | -1.00 | 0.08 | 0.12 |
| 9 | 101 | -10.00 | -20.00 | -1.00 | 0.09 | 0.16 |
| 10 | 101 | -8.00 | -15.00 | 0.00 | 0.12 | 0.17 |
| 11 | 101 | -5.00 | -11.00 | 0.00 | 0.16 | 0.21 |
| 12 | 101 | -3.00 | -7.00 | 2.00 | 0.21 | 0.30 |
| 13 | 101 | -2.00 | -4.00 | 3.00 | 0.31 | 0.43 |
| 14 | 100 | -1.00 | -3.00 | 4.00 | 0.41 | 0.69 |
| 15 | 101 | 0.00 | -3.00 | 4.00 | 0.70 | 0.80 |
| 16 | 101 | 0.00 | -3.00 | 4.00 | 0.72 | 0.82 |
| 17 | 101 | 0.00 | -3.00 | 4.00 | 0.73 | 0.82 |
| 18 | 101 | 0.00 | -3.00 | 4.00 | 0.73 | 0.82 |
| 19 | 101 | 0.00 | -3.00 | 4.00 | 0.73 | 0.82 |
| 20 | 101 | 0.00 | -3.00 | 4.00 | 0.73 | 0.82 |
| 21 | 100 | 0.00 | -3.00 | 7.00 | 0.74 | 0.83 |
| 22 | 101 | 0.00 | -3.00 | 4.00 | 0.73 | 0.82 |
| 23 | 101 | 0.00 | -3.00 | 4.00 | 0.73 | 0.82 |

_Note: `daily_high_f` is the official NWS daily field. Hourly METAR can miss the actual peak by 0.5-2°F due to reporting cadence; `frac_within_1F` is the more reliable 'high-is-set' indicator._


### 7. Empirical diurnal-phase boundaries

- **Peak hour (LST, mode):** 15
- **First post-peak hour (LST, ≥80% days locked):** 15


### 8. Recommended config (auto-derived heuristics)

_These are starting points. Phase 3 should review, not blindly accept._

**Sources biased >1.5°F at peak window (consider exclusion or correction):**
- `nws_5min`: bias = +3.18°F
- `ecmwf`: bias = -3.16°F
- `metno`: bias = -2.77°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `metar` LST 03-05: realized RMSE / claimed σ = 2.57
- `nws_5min` LST 15-17: realized RMSE / claimed σ = 2.36
- `metar` LST 18-20: realized RMSE / claimed σ = 2.24
- `metar` LST 21-23: realized RMSE / claimed σ = 2.16
- `metar` LST 06-08: realized RMSE / claimed σ = 2.08
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 1.82
- `ecmwf` LST 18-20: realized RMSE / claimed σ = 1.68
- `metar` LST 00-02: realized RMSE / claimed σ = 1.62
- `nws_5min` LST 12-14: realized RMSE / claimed σ = 1.59

**Highly-correlated source pairs (n_eff < n):**
- `hrrr` ↔ `weather`: corr = 1.000
- `hrrr` ↔ `metno`: corr = 0.838
- `metno` ↔ `weather`: corr = 0.838
- `combined_v2` ↔ `ecmwf`: corr = 0.794

**Cross-bracket LST gate suggestion:** fire only when recording_lst_hour ≥ 15 AND day_offset == 0.

## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
