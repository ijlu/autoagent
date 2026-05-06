# Per-source ensemble scorecard — LOS ANGELES (KLAX)

**Series:** `KXHIGHLAX`  
**LST offset:** -8h  
**Generated:** 2026-05-05T18:51:48Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 16,061 snapshots, 2 settled days, 9 sources.
- **Empirical peak hour:** LST 11; running-high locks (≥80% days) by LST 12.
- **Biggest peak-window bias offenders:** nws_5min_diurnal (+2.5°F), ecmwf (-1.8°F), nws_point (+1.6°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 3.44 | 4.03 | 2.25 | 1.53 | 2.10 | -0.51 | -0.84 | -0.83 | 2214 |
| ecmwf | -1.99 | -1.91 | -1.75 | -1.75 | -1.34 | -2.24 | -1.99 | -1.84 | 2213 |
| hrrr | -2.12 | -1.44 | -2.02 | -1.48 | -0.78 | 0.14 | -0.56 | -0.73 | 2018 |
| metar | 3.44 | 4.03 | 2.25 | 1.55 | 2.10 | -0.55 | -0.86 | -0.83 | 2213 |
| metno | — | — | — | — | — | 1.30 | 1.30 | — | 192 |
| nws_5min | — | — | — | 1.95 | 1.95 | -2.16 | -5.49 | -4.15 | 1010 |
| nws_5min_diurnal | — | — | — | 1.86 | 2.24 | 2.68 | 2.81 | 3.04 | 860 |
| nws_point | 2.25 | 2.25 | 2.25 | 2.25 | 2.25 | 0.95 | -2.33 | -3.87 | 2212 |
| weather | -2.21 | -1.52 | -2.11 | -1.57 | -0.87 | 0.05 | -0.65 | -1.31 | 2213 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | — | — | — | — | — | -1.31 | 126 |
| ecmwf | — | — | — | — | — | — | — | -2.61 | 126 |
| hrrr | — | — | — | — | — | — | — | -1.63 | 126 |
| metar | — | — | — | — | — | — | — | -1.30 | 125 |
| nws_5min | — | — | — | — | — | — | — | -4.71 | 126 |
| nws_5min_diurnal | — | — | — | — | — | — | — | 3.30 | 36 |
| nws_point | — | — | — | — | — | — | — | 1.30 | 126 |
| weather | — | — | — | — | — | — | — | -2.13 | 125 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.58 | 0.68 | 0.41 | 0.27 | 0.36 | 0.50 | 0.87 | 0.96 | 2214 |
| ecmwf | 0.69 | 0.66 | 0.60 | 0.60 | 0.47 | 0.88 | 1.17 | 1.03 | 2213 |
| hrrr | 1.09 | 0.77 | 1.04 | 0.78 | 0.54 | 0.07 | 0.57 | 0.60 | 2018 |
| metar | 1.72 | 2.01 | 1.22 | 0.81 | 1.05 | 2.34 | 3.14 | 2.56 | 2213 |
| metno | — | — | — | — | — | 0.28 | 0.65 | — | 192 |
| nws_5min | — | — | — | 0.98 | 1.03 | 2.02 | 4.62 | 6.95 | 1010 |
| nws_5min_diurnal | — | — | — | 0.93 | 1.13 | 1.38 | 1.42 | 1.55 | 860 |
| nws_point | 0.46 | 0.45 | 0.45 | 0.45 | 0.45 | 0.32 | 1.22 | 1.99 | 2212 |
| weather | 1.13 | 0.80 | 1.07 | 0.81 | 0.56 | 0.03 | 0.57 | 0.83 | 2213 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 3.45 | 4.03 | 2.45 | 1.62 | 2.11 | 1.16 | 1.00 | 0.96 | 2214 |
| ecmwf | 1.99 | 1.91 | 1.75 | 1.75 | 1.35 | 2.54 | 2.35 | 2.06 | 2213 |
| hrrr | 2.13 | 1.50 | 2.05 | 1.54 | 1.08 | 0.14 | 1.09 | 1.21 | 2018 |
| metar | 3.45 | 4.03 | 2.45 | 1.61 | 2.11 | 1.17 | 1.02 | 0.97 | 2213 |
| metno | — | — | — | — | — | 1.30 | 1.30 | — | 192 |
| nws_5min | — | — | — | 1.95 | 1.95 | 3.16 | 5.69 | 4.42 | 1010 |
| nws_5min_diurnal | — | — | — | 1.86 | 2.27 | 2.76 | 2.84 | 3.10 | 860 |
| nws_point | 2.25 | 2.25 | 2.25 | 2.25 | 2.25 | 1.61 | 2.43 | 3.98 | 2212 |
| weather | 2.22 | 1.58 | 2.14 | 1.62 | 1.14 | 0.06 | 1.14 | 1.65 | 2213 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.04 | 1.00 | 1.87 | 2.31 | 3.72 | — | — | 2340 |
| ecmwf | 2.09 | 2.49 | 1.73 | 1.75 | 1.98 | 2.74 | — | 2339 |
| hrrr | 1.24 | 0.83 | 0.87 | 1.80 | 1.90 | 1.62 | — | 2144 |
| metar | 1.04 | 1.03 | 1.87 | 2.31 | 3.72 | — | — | 2338 |
| metno | — | 1.30 | 1.30 | — | — | — | — | 192 |
| nws_5min | 4.72 | 4.60 | 2.16 | 1.95 | — | — | — | 1136 |
| nws_5min_diurnal | 3.13 | 2.83 | 2.32 | 1.86 | — | — | — | 896 |
| nws_point | 3.86 | 2.03 | 2.17 | 2.25 | 2.25 | 1.39 | — | 2338 |
| weather | 1.63 | 0.86 | 0.92 | 1.89 | 2.04 | 2.06 | — | 2338 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| hrrr | weather | 8 | 1.000 |
| combined_v2 | metar | 8 | 1.000 |
| combined_v2 | nws_5min | 8 | 0.958 |
| metar | nws_5min | 8 | 0.954 |
| ecmwf | nws_5min | 8 | 0.862 |
| nws_5min_diurnal | nws_point | 8 | -0.832 |
| combined_v2 | ecmwf | 8 | 0.692 |
| ecmwf | metar | 8 | 0.682 |
| metar | nws_point | 8 | 0.668 |
| combined_v2 | nws_point | 8 | 0.661 |
| nws_5min_diurnal | weather | 8 | 0.592 |
| hrrr | nws_5min_diurnal | 8 | 0.591 |
| hrrr | metar | 8 | -0.555 |
| metar | weather | 8 | -0.552 |
| combined_v2 | hrrr | 8 | -0.552 |
| combined_v2 | weather | 8 | -0.549 |
| hrrr | nws_5min | 8 | -0.498 |
| nws_5min | nws_point | 8 | 0.496 |
| nws_5min | weather | 8 | -0.494 |
| hrrr | nws_point | 8 | -0.443 |
| nws_point | weather | 8 | -0.443 |
| metar | nws_5min_diurnal | 8 | -0.398 |
| combined_v2 | nws_5min_diurnal | 8 | -0.391 |
| nws_5min | nws_5min_diurnal | 8 | -0.259 |
| ecmwf | hrrr | 8 | -0.220 |
| ecmwf | weather | 8 | -0.216 |
| ecmwf | nws_5min_diurnal | 8 | 0.148 |
| ecmwf | nws_point | 8 | 0.069 |

_corr ≥ 0.7 = effectively redundant; treat as one source._


### 6. METAR signal value by LST hour

Distribution of (running_max_at_hour − daily_high). Negative = running_max still climbing. ~0 = high reached. `frac_at_high` = fraction of days where running_max ≥ daily_high − 0.5°F at that hour.
| lst_hour | n | median_gap_F | p10 | p90 | frac_at_high | frac_within_1F |
|---|---|---|---|---|---|---|
| 0 | 101 | -10.00 | -18.00 | -5.00 | 0.01 | 0.03 |
| 1 | 100 | -10.00 | -18.00 | -5.00 | 0.01 | 0.03 |
| 2 | 100 | -10.00 | -18.00 | -5.00 | 0.01 | 0.03 |
| 3 | 101 | -10.00 | -18.00 | -5.00 | 0.01 | 0.03 |
| 4 | 101 | -10.00 | -18.00 | -5.00 | 0.01 | 0.03 |
| 5 | 101 | -10.00 | -18.00 | -5.00 | 0.01 | 0.03 |
| 6 | 101 | -9.00 | -18.00 | -5.00 | 0.02 | 0.03 |
| 7 | 101 | -8.00 | -16.00 | -3.00 | 0.06 | 0.07 |
| 8 | 101 | -6.00 | -12.00 | 0.00 | 0.14 | 0.16 |
| 9 | 101 | -3.00 | -9.00 | 3.00 | 0.19 | 0.28 |
| 10 | 101 | -2.00 | -5.00 | 5.00 | 0.29 | 0.49 |
| 11 | 101 | -1.00 | -3.00 | 5.00 | 0.46 | 0.67 |
| 12 | 101 | 0.00 | -3.00 | 5.00 | 0.63 | 0.84 |
| 13 | 101 | 0.00 | -3.00 | 5.00 | 0.68 | 0.87 |
| 14 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 15 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 16 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 17 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 18 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 19 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 20 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 21 | 101 | 0.00 | -3.00 | 5.00 | 0.70 | 0.87 |
| 22 | 100 | 0.00 | -3.00 | 8.00 | 0.70 | 0.87 |
| 23 | 100 | 0.00 | -3.00 | 8.00 | 0.70 | 0.87 |

_Note: `daily_high_f` is the official NWS daily field. Hourly METAR can miss the actual peak by 0.5-2°F due to reporting cadence; `frac_within_1F` is the more reliable 'high-is-set' indicator._


### 7. Empirical diurnal-phase boundaries

- **Peak hour (LST, mode):** 11
- **First post-peak hour (LST, ≥80% days locked):** 12


### 8. Recommended config (auto-derived heuristics)

_These are starting points. Phase 3 should review, not blindly accept._

**Sources biased >1.5°F at peak window (consider exclusion or correction):**
- `nws_5min_diurnal`: bias = +2.46°F
- `ecmwf`: bias = -1.79°F
- `nws_point`: bias = +1.60°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 6.95
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 4.62
- `metar` LST 18-20: realized RMSE / claimed σ = 3.14
- `metar` LST 21-23: realized RMSE / claimed σ = 2.56
- `metar` LST 15-17: realized RMSE / claimed σ = 2.34
- `nws_5min` LST 15-17: realized RMSE / claimed σ = 2.02
- `metar` LST 03-05: realized RMSE / claimed σ = 2.01
- `nws_point` LST 21-23: realized RMSE / claimed σ = 1.99
- `metar` LST 00-02: realized RMSE / claimed σ = 1.72
- `nws_5min_diurnal` LST 21-23: realized RMSE / claimed σ = 1.55

**Highly-correlated source pairs (n_eff < n):**
- `hrrr` ↔ `weather`: corr = 1.000
- `combined_v2` ↔ `metar`: corr = 1.000
- `combined_v2` ↔ `nws_5min`: corr = 0.958
- `metar` ↔ `nws_5min`: corr = 0.954
- `ecmwf` ↔ `nws_5min`: corr = 0.862

**Cross-bracket LST gate suggestion:** fire only when recording_lst_hour ≥ 12 AND day_offset == 0.

## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
