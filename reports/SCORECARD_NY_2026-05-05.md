# Per-source ensemble scorecard — NYC (KNYC)

**Series:** `KXHIGHNY`  
**LST offset:** -5h  
**Generated:** 2026-05-05T18:02:06Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 104,738 snapshots, 38 settled days, 16 sources.
- **Empirical peak hour:** LST 13; running-high locks (≥80% days) by LST -1.
- **Biggest peak-window bias offenders:** nws_5min_analog (+3.4°F), tomorrow (-3.4°F), nws_point (-3.0°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | -0.62 | -1.75 | -1.49 | 1.66 | 0.35 | -0.51 | -0.23 | -1.38 | 11047 |
| ecmwf | -0.75 | 0.78 | 0.97 | 0.15 | -0.61 | -0.90 | -0.27 | 3.32 | 4522 |
| gem | 0.17 | -1.62 | -2.01 | -1.14 | -0.88 | -0.66 | -0.67 | 3.47 | 4471 |
| hrrr | -0.06 | -0.56 | 0.90 | 1.99 | 1.38 | 0.94 | 1.27 | 1.06 | 9521 |
| icon | 0.23 | — | — | 0.82 | 0.83 | 0.97 | 0.94 | 1.40 | 2373 |
| madis | -2.11 | -3.16 | 2.06 | 4.34 | 1.47 | -4.35 | -9.62 | -14.95 | 4220 |
| metar | -1.18 | -3.20 | -3.00 | 0.70 | 0.22 | 0.45 | 0.79 | 0.40 | 11030 |
| metno | -1.19 | 0.11 | -0.48 | -0.58 | -0.40 | -0.39 | 1.02 | 4.53 | 4380 |
| nbm | -0.65 | -1.10 | 0.45 | 1.73 | 1.20 | 0.31 | 0.63 | -6.20 | 4239 |
| nws_5min | — | — | — | -1.61 | -1.12 | 1.68 | 4.92 | -2.05 | 1091 |
| nws_5min_analog | — | — | — | — | — | 3.41 | 3.41 | 3.41 | 35 |
| nws_5min_diurnal | — | — | — | 2.04 | 0.77 | — | 9.38 | — | 143 |
| nws_point | -1.92 | -1.06 | -1.48 | -1.42 | -2.58 | -3.38 | -5.61 | -9.99 | 7281 |
| tomorrow | 12.51 | 8.68 | -0.42 | -0.07 | -2.31 | -4.44 | -5.49 | -5.49 | 1744 |
| ukmo | 0.99 | — | — | -0.19 | -0.09 | -0.37 | 0.56 | 1.04 | 2373 |
| weather | 0.15 | -0.43 | 1.01 | 2.15 | 1.58 | 1.12 | 1.49 | -0.14 | 10812 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | — | 2.26 | 1.77 | 1.52 | 3.42 | 2.62 | 2846 |
| ecmwf | — | — | — | -0.54 | -0.31 | -1.08 | -1.51 | -2.62 | 681 |
| gem | — | — | — | -0.93 | -1.11 | -0.76 | 1.29 | -0.67 | 1170 |
| hrrr | — | — | — | -4.53 | -3.40 | 0.57 | 0.91 | 0.52 | 2402 |
| icon | — | — | — | 0.29 | -0.17 | 0.18 | 0.09 | -0.62 | 806 |
| metar | — | — | — | 1.55 | 0.45 | 0.10 | 3.46 | 2.39 | 2835 |
| metno | — | — | — | -0.24 | -1.18 | -1.10 | -1.94 | -2.53 | 674 |
| nbm | — | — | — | -8.74 | -7.94 | 1.13 | 1.67 | -3.04 | 1063 |
| nws_5min | — | — | — | -2.90 | -0.53 | -4.10 | -3.80 | — | 434 |
| nws_5min_analog | — | — | — | — | — | 5.41 | 5.41 | 5.41 | 9 |
| nws_point | — | — | — | -1.28 | 0.38 | 0.27 | 0.43 | -2.37 | 1950 |
| ukmo | — | — | — | 0.74 | 0.70 | 1.65 | 1.63 | 0.45 | 713 |
| weather | — | — | — | -4.39 | -3.15 | 0.80 | 1.17 | -1.14 | 2397 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.92 | 0.81 | 0.79 | 1.28 | 0.95 | 1.47 | 2.56 | 3.84 | 11047 |
| ecmwf | 0.67 | 0.34 | 0.34 | 0.46 | 0.53 | 0.61 | 0.87 | 3.59 | 4522 |
| gem | 0.41 | 0.37 | 0.45 | 0.40 | 0.35 | 0.32 | 0.52 | 3.59 | 4471 |
| hrrr | 0.54 | 0.48 | 0.37 | 0.85 | 0.52 | 0.56 | 1.38 | 1.23 | 9521 |
| icon | 0.20 | — | — | 0.24 | 0.24 | 0.30 | 0.35 | 0.95 | 2373 |
| madis | 2.65 | 3.00 | 2.10 | 1.93 | 1.36 | 1.87 | 4.46 | 6.86 | 4220 |
| metar | 1.20 | 1.54 | 1.53 | 0.87 | 0.58 | 0.40 | 0.93 | 0.67 | 11030 |
| metno | 0.43 | 0.02 | 0.14 | 0.18 | 0.16 | 0.16 | 0.70 | 4.07 | 4380 |
| nbm | 0.74 | 0.90 | 0.75 | 1.90 | 1.33 | 1.11 | 0.95 | 5.74 | 4239 |
| nws_5min | — | — | — | 0.81 | 1.06 | 2.10 | 3.93 | 8.19 | 1091 |
| nws_5min_analog | — | — | — | — | — | 1.71 | 1.71 | 1.70 | 35 |
| nws_5min_diurnal | — | — | — | 1.02 | 0.39 | — | 4.69 | — | 143 |
| nws_point | 1.06 | 1.40 | 1.29 | 1.04 | 0.97 | 1.37 | 2.60 | 3.47 | 7281 |
| tomorrow | 6.25 | 5.33 | 0.46 | 0.17 | 1.67 | 2.46 | 2.75 | 2.75 | 1744 |
| ukmo | 0.22 | — | — | 0.34 | 0.32 | 0.27 | 0.49 | 0.66 | 2373 |
| weather | 0.55 | 0.43 | 0.38 | 0.88 | 0.54 | 0.58 | 1.05 | 4.11 | 10812 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.72 | 1.95 | 2.28 | 2.88 | 2.36 | 2.92 | 3.48 | 4.38 | 11047 |
| ecmwf | 1.82 | 0.99 | 0.98 | 1.30 | 1.49 | 1.71 | 1.74 | 7.18 | 4522 |
| gem | 1.53 | 1.65 | 2.01 | 1.62 | 1.33 | 1.24 | 1.05 | 7.19 | 4471 |
| hrrr | 1.51 | 1.28 | 1.16 | 2.56 | 1.88 | 1.70 | 1.91 | 1.87 | 9521 |
| icon | 0.85 | — | — | 0.84 | 0.84 | 0.98 | 0.96 | 2.60 | 2373 |
| madis | 6.18 | 7.09 | 5.12 | 4.82 | 4.32 | 5.89 | 11.16 | 15.39 | 4220 |
| metar | 2.63 | 3.75 | 4.21 | 2.20 | 1.24 | 0.91 | 1.69 | 0.86 | 11030 |
| metno | 1.50 | 0.11 | 0.66 | 0.73 | 0.60 | 0.61 | 1.40 | 8.14 | 4380 |
| nbm | 0.96 | 1.19 | 1.09 | 2.67 | 2.23 | 1.77 | 1.84 | 10.82 | 4239 |
| nws_5min | — | — | — | 1.61 | 1.97 | 3.41 | 4.99 | 5.45 | 1091 |
| nws_5min_analog | — | — | — | — | — | 3.41 | 3.41 | 3.41 | 35 |
| nws_5min_diurnal | — | — | — | 2.04 | 0.77 | — | 9.38 | — | 143 |
| nws_point | 3.06 | 2.80 | 2.58 | 3.28 | 3.55 | 4.35 | 7.92 | 12.20 | 7281 |
| tomorrow | 12.51 | 10.65 | 0.92 | 0.34 | 3.35 | 4.91 | 5.49 | 5.49 | 1744 |
| ukmo | 1.03 | — | — | 1.23 | 1.20 | 0.94 | 1.68 | 2.39 | 2373 |
| weather | 1.58 | 1.21 | 1.28 | 2.73 | 2.06 | 1.87 | 2.10 | 8.21 | 10812 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 4.13 | 3.61 | 4.29 | 3.00 | 1.78 | 4.64 | 5.65 | 15663 |
| ecmwf | 6.77 | 1.81 | 1.52 | 1.18 | 1.78 | 1.60 | 0.54 | 5257 |
| gem | 6.77 | 1.12 | 1.33 | 1.82 | 1.60 | 1.47 | 1.22 | 5641 |
| hrrr | 1.86 | 1.89 | 3.86 | 2.10 | 1.47 | 3.46 | 5.88 | 13692 |
| icon | 2.47 | 1.00 | 0.86 | 0.73 | 1.24 | 1.06 | 0.83 | 3179 |
| madis | 15.10 | 9.45 | 4.71 | 5.15 | 6.58 | — | — | 4292 |
| metar | 1.74 | 3.26 | 4.09 | 3.63 | 3.00 | — | — | 13991 |
| metno | 7.67 | 1.16 | 0.62 | 0.71 | 1.49 | 1.83 | 0.74 | 5054 |
| nbm | 10.10 | 1.84 | 4.58 | 2.03 | 2.53 | 4.98 | 8.74 | 7072 |
| nws_5min | 5.39 | 5.48 | 3.55 | 2.49 | — | — | — | 1525 |
| nws_5min_analog | 4.05 | 3.75 | — | — | — | — | — | 44 |
| nws_5min_diurnal | — | 9.38 | 1.45 | 2.04 | — | — | — | 143 |
| nws_point | 11.92 | 6.71 | 3.75 | 2.91 | 2.98 | 2.92 | 4.81 | 9303 |
| tomorrow | 5.49 | 5.49 | 3.53 | 0.75 | 12.15 | — | — | 1811 |
| ukmo | 2.33 | 1.43 | 1.20 | 0.95 | 1.07 | 1.41 | 0.59 | 3086 |
| weather | 7.77 | 2.07 | 3.90 | 2.25 | 2.03 | 3.71 | 5.83 | 14985 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| nbm | weather | 22 | 1.000 |
| hrrr | nbm | 22 | 0.999 |
| hrrr | weather | 50 | 0.994 |
| ecmwf | ukmo | 6 | 0.988 |
| combined_v2 | nbm | 22 | 0.976 |
| combined_v2 | weather | 50 | 0.896 |
| combined_v2 | hrrr | 50 | 0.881 |
| combined_v2 | madis | 22 | 0.866 |
| ecmwf | metno | 22 | -0.850 |
| madis | nbm | 22 | 0.834 |
| madis | weather | 22 | 0.834 |
| hrrr | madis | 22 | 0.831 |
| metar | nbm | 22 | 0.817 |
| nws_point | tomorrow | 10 | 0.810 |
| ecmwf | icon | 6 | -0.787 |
| hrrr | ukmo | 12 | 0.765 |
| ukmo | weather | 12 | 0.738 |
| madis | metar | 22 | 0.727 |
| icon | ukmo | 12 | -0.721 |
| nws_point | ukmo | 9 | -0.718 |
| hrrr | tomorrow | 10 | 0.705 |
| nbm | tomorrow | 10 | 0.705 |
| tomorrow | weather | 10 | 0.705 |
| nbm | nws_point | 22 | 0.676 |
| madis | nws_point | 22 | 0.676 |
| metno | ukmo | 6 | 0.632 |
| nws_point | weather | 34 | 0.629 |
| combined_v2 | ukmo | 12 | -0.626 |
| metar | ukmo | 12 | -0.612 |
| icon | nws_point | 9 | 0.609 |
| hrrr | icon | 12 | -0.606 |
| hrrr | nws_point | 34 | 0.603 |
| hrrr | metar | 50 | 0.594 |
| icon | weather | 12 | -0.590 |
| metar | weather | 50 | 0.585 |
| combined_v2 | nws_point | 38 | 0.585 |
| metar | metno | 22 | -0.565 |
| icon | metar | 12 | 0.553 |
| icon | nws_5min | 5 | -0.542 |
| combined_v2 | metar | 54 | 0.514 |
| combined_v2 | icon | 12 | 0.511 |
| gem | nws_5min | 15 | 0.492 |
| gem | nws_point | 6 | 0.447 |
| ecmwf | nws_point | 6 | 0.447 |
| hrrr | nws_5min | 15 | -0.433 |
| metar | nws_point | 38 | 0.418 |
| icon | metno | 6 | -0.407 |
| metno | nws_point | 6 | -0.406 |
| nws_5min | weather | 15 | -0.404 |
| combined_v2 | nws_5min | 15 | 0.396 |
| hrrr | metno | 22 | -0.375 |
| ecmwf | gem | 22 | 0.367 |
| metno | nws_5min | 15 | 0.357 |
| metno | weather | 22 | -0.334 |
| ecmwf | metar | 22 | 0.305 |
| gem | hrrr | 22 | -0.272 |
| combined_v2 | metno | 22 | -0.261 |
| metar | nws_5min | 15 | -0.253 |
| combined_v2 | tomorrow | 10 | 0.237 |
| nws_5min | nws_point | 6 | -0.216 |
| combined_v2 | ecmwf | 22 | 0.198 |
| metar | tomorrow | 10 | 0.192 |
| combined_v2 | gem | 22 | 0.180 |
| nws_5min | ukmo | 5 | 0.169 |
| madis | tomorrow | 10 | 0.166 |
| gem | weather | 22 | -0.150 |
| gem | metar | 22 | 0.104 |
| gem | metno | 22 | -0.073 |
| ecmwf | nws_5min | 15 | -0.056 |
| ecmwf | weather | 22 | -0.030 |
| ecmwf | hrrr | 22 | -0.007 |
| gem | icon | 6 | 0.000 |
| gem | ukmo | 6 | 0.000 |

_corr ≥ 0.7 = effectively redundant; treat as one source._


### 6. METAR signal value by LST hour

Distribution of (running_max_at_hour − daily_high). Negative = running_max still climbing. ~0 = high reached. `frac_at_high` = fraction of days where running_max ≥ daily_high − 0.5°F at that hour.
| lst_hour | n | median_gap_F | p10 | p90 | frac_at_high | frac_within_1F |
|---|---|---|---|---|---|---|
| 0 | 101 | -10.00 | -29.00 | -1.00 | 0.06 | 0.11 |
| 1 | 101 | -10.00 | -29.00 | -1.00 | 0.07 | 0.12 |
| 2 | 101 | -10.00 | -28.00 | -1.00 | 0.07 | 0.12 |
| 3 | 101 | -10.00 | -28.00 | -1.00 | 0.07 | 0.12 |
| 4 | 101 | -10.00 | -27.00 | -1.00 | 0.08 | 0.12 |
| 5 | 101 | -9.00 | -27.00 | -1.00 | 0.08 | 0.12 |
| 6 | 101 | -9.00 | -26.00 | -1.00 | 0.09 | 0.12 |
| 7 | 101 | -9.00 | -26.00 | -1.00 | 0.09 | 0.12 |
| 8 | 101 | -8.00 | -26.00 | -1.00 | 0.09 | 0.14 |
| 9 | 101 | -7.00 | -26.00 | 0.00 | 0.14 | 0.19 |
| 10 | 101 | -5.00 | -26.00 | 0.00 | 0.17 | 0.23 |
| 11 | 101 | -4.00 | -26.00 | 0.00 | 0.20 | 0.29 |
| 12 | 101 | -2.00 | -23.00 | 0.00 | 0.28 | 0.45 |
| 13 | 101 | -1.00 | -22.00 | 2.00 | 0.38 | 0.59 |
| 14 | 101 | -1.00 | -22.00 | 3.00 | 0.47 | 0.70 |
| 15 | 101 | 0.00 | -22.00 | 3.00 | 0.62 | 0.77 |
| 16 | 101 | 0.00 | -22.00 | 3.00 | 0.64 | 0.77 |
| 17 | 101 | 0.00 | -22.00 | 3.00 | 0.65 | 0.77 |
| 18 | 101 | 0.00 | -22.00 | 3.00 | 0.65 | 0.77 |
| 19 | 101 | 0.00 | -22.00 | 3.00 | 0.66 | 0.77 |
| 20 | 101 | 0.00 | -22.00 | 3.00 | 0.66 | 0.77 |
| 21 | 101 | 0.00 | -22.00 | 3.00 | 0.66 | 0.78 |
| 22 | 101 | 0.00 | -22.00 | 3.00 | 0.68 | 0.78 |
| 23 | 101 | 0.00 | -22.00 | 3.00 | 0.68 | 0.78 |

_Note: `daily_high_f` is the official NWS daily field. Hourly METAR can miss the actual peak by 0.5-2°F due to reporting cadence; `frac_within_1F` is the more reliable 'high-is-set' indicator._


### 7. Empirical diurnal-phase boundaries

- **Peak hour (LST, mode):** 13
- **First post-peak hour (LST, ≥80% days locked):** -1


### 8. Recommended config (auto-derived heuristics)

_These are starting points. Phase 3 should review, not blindly accept._

**Sources biased >1.5°F at peak window (consider exclusion or correction):**
- `nws_5min_analog`: bias = +3.41°F
- `tomorrow`: bias = -3.38°F
- `nws_point`: bias = -2.98°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 8.19
- `madis` LST 21-23: realized RMSE / claimed σ = 6.86
- `tomorrow` LST 00-02: realized RMSE / claimed σ = 6.25
- `nbm` LST 21-23: realized RMSE / claimed σ = 5.74
- `tomorrow` LST 03-05: realized RMSE / claimed σ = 5.33
- `nws_5min_diurnal` LST 18-20: realized RMSE / claimed σ = 4.69
- `madis` LST 18-20: realized RMSE / claimed σ = 4.46
- `weather` LST 21-23: realized RMSE / claimed σ = 4.11
- `metno` LST 21-23: realized RMSE / claimed σ = 4.07
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 3.93
- `combined_v2` LST 21-23: realized RMSE / claimed σ = 3.84
- `gem` LST 21-23: realized RMSE / claimed σ = 3.59
- `ecmwf` LST 21-23: realized RMSE / claimed σ = 3.59
- `nws_point` LST 21-23: realized RMSE / claimed σ = 3.47
- `madis` LST 03-05: realized RMSE / claimed σ = 3.00
- `tomorrow` LST 18-20: realized RMSE / claimed σ = 2.75
- `tomorrow` LST 21-23: realized RMSE / claimed σ = 2.75
- `madis` LST 00-02: realized RMSE / claimed σ = 2.65
- `nws_point` LST 18-20: realized RMSE / claimed σ = 2.60
- `combined_v2` LST 18-20: realized RMSE / claimed σ = 2.56

**Highly-correlated source pairs (n_eff < n):**
- `nbm` ↔ `weather`: corr = 1.000
- `hrrr` ↔ `nbm`: corr = 0.999
- `hrrr` ↔ `weather`: corr = 0.994
- `ecmwf` ↔ `ukmo`: corr = 0.988
- `combined_v2` ↔ `nbm`: corr = 0.976
- `combined_v2` ↔ `weather`: corr = 0.896
- `combined_v2` ↔ `hrrr`: corr = 0.881
- `combined_v2` ↔ `madis`: corr = 0.866
- `madis` ↔ `nbm`: corr = 0.834
- `madis` ↔ `weather`: corr = 0.834
- `hrrr` ↔ `madis`: corr = 0.831
- `metar` ↔ `nbm`: corr = 0.817
- `nws_point` ↔ `tomorrow`: corr = 0.810
- `hrrr` ↔ `ukmo`: corr = 0.765
- `ukmo` ↔ `weather`: corr = 0.738
- `madis` ↔ `metar`: corr = 0.727
- `hrrr` ↔ `tomorrow`: corr = 0.705
- `nbm` ↔ `tomorrow`: corr = 0.705
- `tomorrow` ↔ `weather`: corr = 0.705


## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
