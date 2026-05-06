# Per-source ensemble scorecard — CHICAGO (KMDW)

**Series:** `KXHIGHCHI`  
**LST offset:** -6h  
**Generated:** 2026-05-05T18:53:07Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 15,283 snapshots, 2 settled days, 11 sources.
- **Empirical peak hour:** LST 14; running-high locks (≥80% days) by LST -1.
- **Biggest peak-window bias offenders:** nws_5min_diurnal (-4.5°F), metno (-4.1°F), ecmwf (-3.2°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | -7.92 | -8.66 | -7.77 | -4.13 | 0.20 | 0.83 | 1.90 | 0.95 | 2375 |
| ecmwf | -4.84 | -4.42 | -4.32 | -4.22 | -3.88 | -2.62 | -2.01 | -2.70 | 2372 |
| gem | — | — | 1.31 | 1.31 | 1.31 | 2.16 | 2.07 | -2.27 | 1484 |
| hrrr | — | — | 1.15 | 1.47 | 0.71 | 0.60 | 0.27 | 0.40 | 1376 |
| metar | -12.08 | -13.08 | -12.00 | -7.28 | -1.02 | 0.83 | 1.90 | 0.87 | 2372 |
| metno | -3.81 | -4.97 | -4.58 | -4.58 | -4.24 | -3.98 | -0.47 | -1.55 | 2372 |
| nws_5min | — | — | — | — | — | — | -2.66 | — | 174 |
| nws_5min_diurnal | — | — | — | — | -4.55 | -4.40 | 2.94 | -0.43 | 540 |
| nws_point | — | — | — | — | — | — | -2.04 | — | 49 |
| ukmo | — | — | — | — | — | — | -0.46 | 2.92 | 514 |
| weather | — | — | 1.13 | 1.45 | 0.69 | 0.58 | 0.46 | -0.71 | 1562 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | — | — | — | — | — | -12.07 | 37 |
| metar | — | — | — | — | — | — | — | -12.10 | 37 |
| nws_5min_diurnal | — | — | — | — | — | — | — | -10.59 | 13 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.16 | 1.27 | 1.14 | 0.61 | 0.15 | 0.17 | 1.05 | 0.97 | 2375 |
| ecmwf | 1.69 | 1.53 | 1.49 | 1.46 | 1.36 | 0.91 | 1.05 | 4.41 | 2372 |
| gem | — | — | 0.29 | 0.29 | 0.29 | 0.50 | 1.06 | 4.65 | 1484 |
| hrrr | — | — | 0.16 | 0.20 | 0.10 | 0.09 | 0.37 | 0.28 | 1376 |
| metar | 6.05 | 6.54 | 6.01 | 3.73 | 0.88 | 0.58 | 3.06 | 2.42 | 2372 |
| metno | 0.91 | 1.08 | 0.99 | 0.99 | 0.92 | 0.87 | 1.05 | 4.76 | 2372 |
| nws_5min | — | — | — | — | — | — | 2.44 | — | 174 |
| nws_5min_diurnal | — | — | — | — | 2.29 | 2.20 | 1.48 | 2.75 | 540 |
| nws_point | — | — | — | — | — | — | 1.03 | — | 49 |
| ukmo | — | — | — | — | — | — | 0.64 | 2.69 | 514 |
| weather | — | — | 0.15 | 0.20 | 0.10 | 0.08 | 0.29 | 4.40 | 1562 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 7.94 | 8.66 | 7.82 | 4.38 | 1.07 | 0.83 | 2.50 | 0.97 | 2375 |
| ecmwf | 4.89 | 4.42 | 4.32 | 4.22 | 3.94 | 2.62 | 2.10 | 8.81 | 2372 |
| gem | — | — | 1.31 | 1.31 | 1.31 | 2.23 | 2.11 | 9.29 | 1484 |
| hrrr | — | — | 1.15 | 1.48 | 0.75 | 0.65 | 0.60 | 0.56 | 1376 |
| metar | 12.09 | 13.08 | 12.01 | 7.46 | 1.77 | 0.83 | 2.50 | 0.87 | 2372 |
| metno | 4.22 | 4.97 | 4.58 | 4.58 | 4.25 | 3.99 | 2.11 | 9.53 | 2372 |
| nws_5min | — | — | — | — | — | — | 3.13 | — | 174 |
| nws_5min_diurnal | — | — | — | — | 4.58 | 4.40 | 2.95 | 5.50 | 540 |
| nws_point | — | — | — | — | — | — | 2.06 | — | 49 |
| ukmo | — | — | — | — | — | — | 1.65 | 7.00 | 514 |
| weather | — | — | 1.13 | 1.47 | 0.73 | 0.63 | 0.58 | 8.80 | 1562 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 2.97 | 2.46 | 1.48 | 6.77 | 8.29 | — | — | 2413 |
| ecmwf | 8.34 | 2.20 | 3.74 | 4.29 | 4.67 | — | — | 2372 |
| gem | 8.74 | 2.20 | 1.45 | 1.31 | — | — | — | 1485 |
| hrrr | 0.56 | 0.65 | 0.79 | 1.56 | — | — | — | 1376 |
| metar | 2.94 | 2.46 | 2.61 | 10.58 | 12.57 | — | — | 2410 |
| metno | 9.01 | 2.59 | 4.26 | 4.61 | 4.63 | — | — | 2373 |
| nws_5min | 4.40 | 2.87 | — | — | — | — | — | 174 |
| nws_5min_diurnal | 5.53 | 3.03 | 4.53 | — | — | — | — | 553 |
| nws_point | — | 2.06 | — | — | — | — | — | 49 |
| ukmo | 6.62 | 1.60 | — | — | — | — | — | 515 |
| weather | 8.31 | 0.63 | 0.77 | 1.55 | — | — | — | 1563 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| hrrr | weather | 6 | 1.000 |
| metar | metno | 6 | 0.871 |
| ecmwf | metar | 6 | 0.817 |
| combined_v2 | metar | 6 | 0.800 |
| combined_v2 | metno | 6 | 0.736 |
| ecmwf | gem | 6 | 0.670 |
| ecmwf | metno | 6 | 0.660 |
| gem | metno | 6 | 0.590 |
| hrrr | metar | 6 | -0.571 |
| metar | weather | 6 | -0.571 |
| gem | metar | 6 | 0.550 |
| ecmwf | hrrr | 6 | -0.512 |
| ecmwf | weather | 6 | -0.512 |
| combined_v2 | gem | 6 | 0.388 |
| combined_v2 | ecmwf | 6 | 0.374 |
| combined_v2 | hrrr | 6 | -0.263 |
| combined_v2 | weather | 6 | -0.263 |
| hrrr | metno | 6 | -0.230 |
| metno | weather | 6 | -0.230 |
| gem | hrrr | 6 | 0.223 |
| gem | weather | 6 | 0.223 |

_corr ≥ 0.7 = effectively redundant; treat as one source._


### 6. METAR signal value by LST hour

Distribution of (running_max_at_hour − daily_high). Negative = running_max still climbing. ~0 = high reached. `frac_at_high` = fraction of days where running_max ≥ daily_high − 0.5°F at that hour.
| lst_hour | n | median_gap_F | p10 | p90 | frac_at_high | frac_within_1F |
|---|---|---|---|---|---|---|
| 0 | 51 | -15.00 | -39.00 | -2.00 | 0.06 | 0.10 |
| 1 | 51 | -14.00 | -39.00 | -2.00 | 0.06 | 0.10 |
| 2 | 52 | -14.00 | -39.00 | -2.00 | 0.08 | 0.10 |
| 3 | 51 | -14.00 | -39.00 | -2.00 | 0.08 | 0.10 |
| 4 | 51 | -13.00 | -39.00 | -2.00 | 0.08 | 0.10 |
| 5 | 52 | -13.00 | -38.00 | -2.00 | 0.08 | 0.10 |
| 6 | 52 | -12.00 | -38.00 | -1.00 | 0.10 | 0.12 |
| 7 | 52 | -11.00 | -36.00 | -1.00 | 0.10 | 0.15 |
| 8 | 52 | -9.00 | -35.00 | 0.00 | 0.13 | 0.19 |
| 9 | 52 | -7.00 | -32.00 | 0.00 | 0.19 | 0.25 |
| 10 | 52 | -5.00 | -29.00 | 2.00 | 0.23 | 0.29 |
| 11 | 52 | -4.00 | -26.00 | 2.00 | 0.25 | 0.29 |
| 12 | 52 | -2.00 | -24.00 | 2.00 | 0.29 | 0.37 |
| 13 | 52 | -2.00 | -23.00 | 2.00 | 0.29 | 0.40 |
| 14 | 52 | -1.00 | -21.00 | 2.00 | 0.44 | 0.60 |
| 15 | 52 | -1.00 | -20.00 | 2.00 | 0.48 | 0.69 |
| 16 | 52 | 0.00 | -19.00 | 2.00 | 0.58 | 0.71 |
| 17 | 52 | 0.00 | -19.00 | 2.00 | 0.58 | 0.71 |
| 18 | 52 | 0.00 | -26.00 | 2.00 | 0.56 | 0.69 |
| 19 | 52 | 0.00 | -26.00 | 2.00 | 0.56 | 0.69 |
| 20 | 52 | 0.00 | -26.00 | 2.00 | 0.56 | 0.69 |
| 21 | 52 | 0.00 | -26.00 | 2.00 | 0.56 | 0.69 |
| 22 | 52 | 0.00 | -26.00 | 2.00 | 0.56 | 0.71 |
| 23 | 52 | 0.00 | -26.00 | 2.00 | 0.60 | 0.71 |

_Note: `daily_high_f` is the official NWS daily field. Hourly METAR can miss the actual peak by 0.5-2°F due to reporting cadence; `frac_within_1F` is the more reliable 'high-is-set' indicator._


### 7. Empirical diurnal-phase boundaries

- **Peak hour (LST, mode):** 14
- **First post-peak hour (LST, ≥80% days locked):** -1


### 8. Recommended config (auto-derived heuristics)

_These are starting points. Phase 3 should review, not blindly accept._

**Sources biased >1.5°F at peak window (consider exclusion or correction):**
- `nws_5min_diurnal`: bias = -4.48°F
- `metno`: bias = -4.11°F
- `ecmwf`: bias = -3.25°F
- `gem`: bias = +1.74°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `metar` LST 03-05: realized RMSE / claimed σ = 6.54
- `metar` LST 00-02: realized RMSE / claimed σ = 6.05
- `metar` LST 06-08: realized RMSE / claimed σ = 6.01
- `metno` LST 21-23: realized RMSE / claimed σ = 4.76
- `gem` LST 21-23: realized RMSE / claimed σ = 4.65
- `ecmwf` LST 21-23: realized RMSE / claimed σ = 4.41
- `weather` LST 21-23: realized RMSE / claimed σ = 4.40
- `metar` LST 09-11: realized RMSE / claimed σ = 3.73
- `metar` LST 18-20: realized RMSE / claimed σ = 3.06
- `nws_5min_diurnal` LST 21-23: realized RMSE / claimed σ = 2.75
- `ukmo` LST 21-23: realized RMSE / claimed σ = 2.69
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 2.44
- `metar` LST 21-23: realized RMSE / claimed σ = 2.42
- `nws_5min_diurnal` LST 12-14: realized RMSE / claimed σ = 2.29
- `nws_5min_diurnal` LST 15-17: realized RMSE / claimed σ = 2.20
- `ecmwf` LST 00-02: realized RMSE / claimed σ = 1.69
- `ecmwf` LST 03-05: realized RMSE / claimed σ = 1.53

**Highly-correlated source pairs (n_eff < n):**
- `hrrr` ↔ `weather`: corr = 1.000
- `metar` ↔ `metno`: corr = 0.871
- `ecmwf` ↔ `metar`: corr = 0.817
- `combined_v2` ↔ `metar`: corr = 0.800
- `combined_v2` ↔ `metno`: corr = 0.736


## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
