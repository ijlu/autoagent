# Per-source ensemble scorecard — AUSTIN (KAUS)

**Series:** `KXHIGHAUS`  
**LST offset:** -6h  
**Generated:** 2026-05-05T18:40:13Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 98,694 snapshots, 38 settled days, 16 sources.
- **Empirical peak hour:** LST 15; running-high locks (≥80% days) by LST 15.
- **Biggest peak-window bias offenders:** nws_5min_analog (+6.5°F), ukmo (+3.9°F), icon (+3.7°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.80 | 1.90 | 1.29 | 1.96 | 1.94 | 1.80 | 0.91 | 0.28 | 10396 |
| ecmwf | -0.30 | -1.91 | -2.36 | -2.31 | -1.88 | -1.63 | -0.27 | 0.90 | 4208 |
| gem | -3.86 | — | -1.10 | 0.41 | 0.08 | 1.08 | 0.99 | 0.21 | 2364 |
| hrrr | 2.07 | 3.51 | 2.28 | 1.24 | 0.11 | 0.54 | 0.50 | 0.41 | 9141 |
| icon | -2.59 | — | — | 4.00 | 4.05 | 3.44 | 1.28 | 0.35 | 2060 |
| madis | -4.31 | -4.94 | -4.63 | -1.21 | 1.22 | 1.22 | -3.71 | -11.03 | 4218 |
| metar | 0.62 | -0.11 | -1.70 | -0.87 | -0.58 | 0.36 | 0.27 | 0.14 | 10384 |
| metno | -1.22 | -1.30 | -2.59 | -2.58 | -2.58 | -2.35 | -1.14 | -0.22 | 4170 |
| nbm | 3.29 | 3.75 | 2.80 | 1.51 | 0.02 | 0.13 | -0.28 | 1.06 | 4235 |
| nws_5min | 0.40 | — | — | -2.00 | 1.19 | 2.75 | -1.16 | -6.52 | 1551 |
| nws_5min_analog | — | — | — | — | — | 6.51 | 6.51 | 6.51 | 29 |
| nws_5min_diurnal | — | — | — | -0.15 | 0.30 | 0.43 | 1.30 | 0.71 | 1605 |
| nws_point | 2.31 | 3.10 | 3.08 | 2.74 | 3.84 | 3.18 | -0.30 | -7.54 | 7414 |
| tomorrow | 0.73 | 1.13 | 1.85 | 1.10 | 0.30 | 0.60 | 0.73 | 0.73 | 1704 |
| ukmo | 0.92 | — | — | 3.24 | 3.36 | 4.48 | 2.88 | 0.79 | 1998 |
| weather | 2.20 | 3.59 | 2.36 | 1.38 | 0.23 | 0.65 | 0.65 | 0.29 | 10224 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | 8.91 | 0.90 | -0.13 | 0.98 | -3.32 | 0.99 | 2506 |
| ecmwf | — | — | -0.12 | -2.49 | -1.96 | -1.96 | 2.93 | 3.41 | 583 |
| gem | — | — | — | -3.87 | -5.56 | -6.02 | -4.83 | -2.07 | 1076 |
| hrrr | — | — | -0.06 | -1.04 | -0.44 | -0.27 | -0.96 | 1.07 | 2328 |
| icon | — | — | — | -2.03 | -2.03 | -1.99 | 1.97 | 2.20 | 432 |
| metar | — | — | 8.77 | -0.60 | -2.08 | -0.35 | -5.33 | 0.18 | 2504 |
| metno | — | — | -2.10 | -2.17 | -2.33 | -2.12 | -1.78 | 1.14 | 1341 |
| nbm | — | — | -1.05 | -0.73 | 1.71 | -1.95 | 3.67 | -0.31 | 619 |
| nws_5min | — | — | — | -3.36 | -1.86 | 0.56 | 3.58 | 1.66 | 474 |
| nws_5min_analog | — | — | — | — | — | 1.51 | 1.51 | — | 9 |
| nws_5min_diurnal | — | — | — | -0.33 | -0.91 | -0.96 | — | -1.08 | 295 |
| nws_point | — | — | 4.82 | 4.72 | 4.79 | 8.22 | 3.96 | 4.90 | 1185 |
| ukmo | — | — | — | — | 0.36 | 0.76 | 3.26 | 3.26 | 149 |
| weather | — | — | -0.02 | -0.73 | -0.16 | 0.02 | -0.60 | 0.28 | 2166 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.38 | 1.30 | 0.92 | 0.99 | 0.78 | 0.92 | 1.28 | 1.36 | 10396 |
| ecmwf | 0.70 | 0.66 | 0.84 | 1.21 | 1.23 | 1.30 | 1.63 | 2.26 | 4208 |
| gem | 1.40 | — | 0.24 | 0.25 | 0.63 | 0.40 | 0.88 | 1.49 | 2364 |
| hrrr | 1.11 | 1.31 | 0.85 | 0.71 | 0.45 | 0.30 | 0.85 | 0.77 | 9141 |
| icon | 1.06 | — | — | 1.31 | 1.34 | 1.07 | 1.02 | 1.81 | 2060 |
| madis | 1.96 | 2.13 | 1.98 | 0.74 | 0.80 | 1.40 | 2.70 | 5.93 | 4218 |
| metar | 1.22 | 1.38 | 0.88 | 1.00 | 0.68 | 0.41 | 0.62 | 0.87 | 10384 |
| metno | 0.37 | 0.29 | 0.57 | 0.70 | 0.70 | 0.72 | 1.07 | 1.96 | 4170 |
| nbm | 2.50 | 2.75 | 2.04 | 1.42 | 0.88 | 0.43 | 0.48 | 1.37 | 4235 |
| nws_5min | 0.20 | — | — | 1.28 | 1.44 | 1.96 | 1.90 | 15.81 | 1551 |
| nws_5min_analog | — | — | — | — | — | 3.26 | 3.26 | 3.26 | 29 |
| nws_5min_diurnal | — | — | — | 0.96 | 0.49 | 0.46 | 1.18 | 0.78 | 1605 |
| nws_point | 0.95 | 1.71 | 1.58 | 1.11 | 1.53 | 1.47 | 1.26 | 3.44 | 7414 |
| tomorrow | 0.37 | 0.65 | 0.94 | 0.56 | 0.33 | 0.33 | 0.37 | 0.37 | 1704 |
| ukmo | 0.38 | — | — | 0.97 | 1.03 | 1.32 | 1.20 | 1.38 | 1998 |
| weather | 1.07 | 1.27 | 0.82 | 0.68 | 0.44 | 0.32 | 0.65 | 1.68 | 10224 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 3.55 | 4.11 | 2.80 | 2.73 | 2.20 | 2.16 | 1.60 | 1.42 | 10396 |
| ecmwf | 1.97 | 1.91 | 2.43 | 3.48 | 3.48 | 3.66 | 3.26 | 4.53 | 4208 |
| gem | 5.46 | — | 1.10 | 0.99 | 2.39 | 1.39 | 1.76 | 2.97 | 2364 |
| hrrr | 3.47 | 4.35 | 2.75 | 2.50 | 1.66 | 1.05 | 1.12 | 1.13 | 9141 |
| icon | 2.81 | — | — | 4.20 | 4.25 | 3.59 | 3.29 | 5.82 | 2060 |
| madis | 4.50 | 5.06 | 4.82 | 1.88 | 2.05 | 2.59 | 5.16 | 11.17 | 4218 |
| metar | 3.21 | 4.07 | 2.91 | 2.89 | 1.64 | 0.98 | 1.06 | 1.03 | 10384 |
| metno | 1.57 | 1.32 | 2.63 | 2.99 | 2.94 | 2.86 | 2.14 | 3.92 | 4170 |
| nbm | 3.60 | 4.03 | 3.02 | 1.97 | 1.37 | 0.72 | 0.92 | 2.57 | 4235 |
| nws_5min | 0.40 | — | — | 2.57 | 2.70 | 3.11 | 2.51 | 6.52 | 1551 |
| nws_5min_analog | — | — | — | — | — | 6.51 | 6.51 | 6.51 | 29 |
| nws_5min_diurnal | — | — | — | 1.92 | 0.98 | 0.91 | 2.35 | 1.57 | 1605 |
| nws_point | 2.67 | 3.42 | 3.45 | 3.51 | 5.33 | 4.87 | 3.76 | 8.73 | 7414 |
| tomorrow | 0.73 | 1.30 | 1.88 | 1.11 | 0.65 | 0.67 | 0.73 | 0.73 | 1704 |
| ukmo | 0.99 | — | — | 3.24 | 3.40 | 4.63 | 3.72 | 4.39 | 1998 |
| weather | 3.47 | 4.39 | 2.78 | 2.51 | 1.68 | 1.18 | 1.31 | 3.36 | 10224 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 3.71 | 3.75 | 4.85 | 4.03 | 3.80 | 3.55 | — | 14643 |
| ecmwf | 4.38 | 3.45 | 3.46 | 3.01 | 2.00 | 2.67 | 2.76 | 4848 |
| gem | 2.80 | 1.77 | 2.07 | 1.00 | 5.35 | 5.67 | 6.50 | 3440 |
| hrrr | 1.13 | 1.12 | 4.25 | 2.64 | 3.86 | 3.27 | 2.77 | 13209 |
| icon | 5.58 | 3.33 | 4.12 | 4.17 | 2.81 | 2.07 | 2.03 | 2492 |
| madis | 10.81 | 3.92 | 2.16 | 3.61 | 4.70 | — | — | 4296 |
| metar | 3.73 | 4.12 | 4.31 | 4.31 | 3.54 | — | — | 12967 |
| metno | 3.75 | 2.39 | 2.94 | 2.73 | 2.04 | 2.44 | 2.17 | 5511 |
| nbm | 2.45 | 0.88 | 4.95 | 2.59 | 4.17 | 4.62 | 2.33 | 6594 |
| nws_5min | 4.56 | 2.45 | 2.83 | 3.81 | 0.40 | — | — | 2025 |
| nws_5min_analog | 6.51 | 5.29 | 6.51 | — | — | — | — | 38 |
| nws_5min_diurnal | 1.62 | 1.87 | 1.06 | 1.93 | — | — | — | 1900 |
| nws_point | 8.40 | 3.65 | 5.26 | 3.36 | 3.25 | 6.70 | 4.54 | 8677 |
| tomorrow | 0.73 | 0.73 | 0.65 | 1.60 | 0.86 | — | — | 1776 |
| ukmo | 4.29 | 3.97 | 3.76 | 3.25 | 0.99 | 2.95 | — | 2147 |
| weather | 3.20 | 1.28 | 4.25 | 2.66 | 4.12 | 3.38 | 2.60 | 14131 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| nbm | weather | 22 | 1.000 |
| hrrr | nbm | 22 | 0.997 |
| hrrr | weather | 51 | 0.986 |
| icon | nws_point | 12 | 0.984 |
| combined_v2 | icon | 12 | 0.964 |
| hrrr | tomorrow | 10 | 0.954 |
| nbm | tomorrow | 10 | 0.954 |
| tomorrow | weather | 10 | 0.954 |
| ecmwf | nws_point | 14 | -0.952 |
| nws_5min | nws_point | 11 | 0.948 |
| ecmwf | metno | 23 | 0.930 |
| ecmwf | nws_5min | 20 | -0.917 |
| icon | metar | 12 | 0.891 |
| ecmwf | nws_5min_diurnal | 12 | -0.888 |
| combined_v2 | tomorrow | 10 | 0.866 |
| metno | nws_5min_diurnal | 12 | -0.851 |
| metno | nws_point | 14 | -0.822 |
| gem | hrrr | 15 | 0.748 |
| hrrr | icon | 12 | 0.742 |
| metno | nws_5min | 20 | -0.738 |
| gem | weather | 15 | 0.737 |
| gem | metar | 15 | -0.720 |
| metno | weather | 23 | 0.671 |
| madis | nws_point | 22 | 0.642 |
| hrrr | metno | 23 | 0.634 |
| combined_v2 | nbm | 22 | 0.608 |
| combined_v2 | ukmo | 12 | 0.602 |
| metar | nws_5min_diurnal | 12 | 0.583 |
| combined_v2 | gem | 15 | -0.529 |
| metar | nws_5min | 20 | -0.527 |
| nws_point | ukmo | 12 | 0.503 |
| madis | tomorrow | 10 | -0.497 |
| ecmwf | weather | 23 | 0.483 |
| nws_5min | nws_5min_diurnal | 12 | -0.481 |
| icon | weather | 12 | 0.477 |
| combined_v2 | metar | 56 | 0.442 |
| ecmwf | hrrr | 23 | 0.432 |
| icon | ukmo | 12 | 0.423 |
| madis | nbm | 22 | -0.421 |
| madis | weather | 22 | -0.421 |
| ecmwf | metar | 23 | 0.376 |
| hrrr | madis | 22 | -0.350 |
| nws_point | tomorrow | 10 | 0.318 |
| combined_v2 | metno | 23 | -0.316 |
| combined_v2 | madis | 22 | 0.306 |
| gem | nws_5min_diurnal | 6 | -0.290 |
| combined_v2 | nws_point | 47 | 0.255 |
| metar | ukmo | 12 | 0.254 |
| nbm | nws_point | 22 | -0.249 |
| metar | nbm | 22 | 0.241 |
| gem | nws_5min | 12 | 0.241 |
| hrrr | ukmo | 12 | 0.203 |
| combined_v2 | nws_5min | 20 | -0.202 |
| nws_5min | weather | 20 | -0.200 |
| combined_v2 | nws_5min_diurnal | 12 | 0.193 |
| gem | metno | 15 | 0.192 |
| metar | nws_point | 47 | -0.189 |
| combined_v2 | weather | 51 | -0.139 |
| hrrr | nws_5min | 20 | -0.136 |
| gem | nws_point | 12 | 0.122 |
| hrrr | nws_5min_diurnal | 12 | -0.103 |
| hrrr | nws_point | 42 | 0.096 |
| metar | weather | 51 | 0.077 |
| combined_v2 | hrrr | 51 | -0.075 |
| combined_v2 | ecmwf | 23 | -0.063 |
| ecmwf | gem | 15 | -0.055 |
| metar | tomorrow | 10 | 0.051 |
| metar | metno | 23 | 0.049 |
| nws_5min_diurnal | weather | 12 | -0.035 |
| nws_point | weather | 42 | -0.032 |
| madis | metar | 22 | 0.030 |
| ukmo | weather | 12 | -0.028 |
| hrrr | metar | 51 | 0.010 |
| ecmwf | icon | 6 | 0.000 |
| ecmwf | ukmo | 6 | 0.000 |
| gem | icon | 6 | 0.000 |
| gem | ukmo | 6 | 0.000 |
| icon | metno | 6 | 0.000 |
| metno | ukmo | 6 | 0.000 |

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
- `nws_5min_analog`: bias = +6.51°F
- `ukmo`: bias = +3.92°F
- `icon`: bias = +3.74°F
- `nws_point`: bias = +3.51°F
- `metno`: bias = -2.46°F
- `nws_5min`: bias = +1.97°F
- `combined_v2`: bias = +1.87°F
- `ecmwf`: bias = -1.75°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 15.81
- `madis` LST 21-23: realized RMSE / claimed σ = 5.93
- `nws_point` LST 21-23: realized RMSE / claimed σ = 3.44
- `nws_5min_analog` LST 21-23: realized RMSE / claimed σ = 3.26
- `nws_5min_analog` LST 18-20: realized RMSE / claimed σ = 3.26
- `nws_5min_analog` LST 15-17: realized RMSE / claimed σ = 3.26
- `nbm` LST 03-05: realized RMSE / claimed σ = 2.75
- `madis` LST 18-20: realized RMSE / claimed σ = 2.70
- `nbm` LST 00-02: realized RMSE / claimed σ = 2.50
- `ecmwf` LST 21-23: realized RMSE / claimed σ = 2.26
- `madis` LST 03-05: realized RMSE / claimed σ = 2.13
- `nbm` LST 06-08: realized RMSE / claimed σ = 2.04
- `madis` LST 06-08: realized RMSE / claimed σ = 1.98
- `nws_5min` LST 15-17: realized RMSE / claimed σ = 1.96
- `metno` LST 21-23: realized RMSE / claimed σ = 1.96
- `madis` LST 00-02: realized RMSE / claimed σ = 1.96
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 1.90
- `icon` LST 21-23: realized RMSE / claimed σ = 1.81
- `nws_point` LST 03-05: realized RMSE / claimed σ = 1.71
- `weather` LST 21-23: realized RMSE / claimed σ = 1.68

**Highly-correlated source pairs (n_eff < n):**
- `nbm` ↔ `weather`: corr = 1.000
- `hrrr` ↔ `nbm`: corr = 0.997
- `hrrr` ↔ `weather`: corr = 0.986
- `icon` ↔ `nws_point`: corr = 0.984
- `combined_v2` ↔ `icon`: corr = 0.964
- `hrrr` ↔ `tomorrow`: corr = 0.954
- `nbm` ↔ `tomorrow`: corr = 0.954
- `tomorrow` ↔ `weather`: corr = 0.954
- `nws_5min` ↔ `nws_point`: corr = 0.948
- `ecmwf` ↔ `metno`: corr = 0.930
- `icon` ↔ `metar`: corr = 0.891
- `combined_v2` ↔ `tomorrow`: corr = 0.866
- `gem` ↔ `hrrr`: corr = 0.748
- `hrrr` ↔ `icon`: corr = 0.742
- `gem` ↔ `weather`: corr = 0.737

**Cross-bracket LST gate suggestion:** fire only when recording_lst_hour ≥ 15 AND day_offset == 0.

## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
