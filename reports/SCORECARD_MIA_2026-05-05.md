# Per-source ensemble scorecard — MIAMI (KMIA)

**Series:** `KXHIGHMIA`  
**LST offset:** -5h  
**Generated:** 2026-05-05T18:44:17Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 109,769 snapshots, 38 settled days, 16 sources.
- **Empirical peak hour:** LST 12; running-high locks (≥80% days) by LST -1.
- **Biggest peak-window bias offenders:** tomorrow (-4.7°F), ukmo (+3.6°F), nws_5min_diurnal (+1.6°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.21 | 2.21 | 1.59 | 1.19 | 1.33 | 1.59 | 1.11 | 0.91 | 10810 |
| ecmwf | -0.45 | -0.94 | -0.55 | -0.04 | 0.01 | -0.48 | 0.20 | 0.26 | 4134 |
| gem | -4.49 | -1.11 | -0.95 | -1.10 | 0.22 | -0.15 | -0.12 | -0.43 | 4069 |
| hrrr | 0.23 | 1.25 | 1.66 | -0.26 | -0.74 | -0.33 | -0.12 | -0.11 | 9338 |
| icon | 2.13 | 2.46 | 0.71 | 0.77 | 0.90 | 0.90 | 1.05 | 1.87 | 5223 |
| madis | -1.16 | -3.61 | -1.16 | 4.13 | 3.17 | -1.31 | -4.93 | -7.49 | 4051 |
| metar | -0.63 | 0.11 | -0.93 | -0.98 | 0.36 | 0.87 | 0.49 | 0.26 | 10801 |
| metno | -1.19 | 2.28 | 2.73 | 1.30 | 0.65 | -0.14 | -0.17 | 0.23 | 4565 |
| nbm | -1.49 | -1.13 | -1.64 | -1.88 | -1.71 | -0.30 | -0.21 | -0.68 | 4070 |
| nws_5min | — | — | — | -2.53 | -0.72 | -1.66 | -4.01 | -13.40 | 1029 |
| nws_5min_analog | — | — | — | — | — | — | — | -11.69 | 4 |
| nws_5min_diurnal | — | — | — | 1.02 | 0.74 | 2.39 | 2.80 | 1.68 | 1528 |
| nws_point | -1.96 | -1.24 | -1.79 | -0.99 | -0.94 | -1.70 | -2.94 | -4.42 | 7922 |
| tomorrow | -6.10 | -6.27 | -6.50 | -5.66 | -4.98 | -4.41 | -4.10 | -4.10 | 1625 |
| ukmo | 2.10 | — | — | 3.19 | 3.13 | 4.01 | 3.27 | 3.12 | 2458 |
| weather | 0.28 | 1.28 | 1.67 | -0.22 | -0.69 | -0.29 | -0.07 | 0.50 | 10532 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | — | -0.10 | 0.99 | 1.27 | -0.89 | 1.05 | 2774 |
| ecmwf | — | — | — | -2.16 | -1.59 | -1.56 | -1.40 | -0.29 | 1086 |
| gem | — | — | — | -4.22 | -3.96 | -3.43 | -5.60 | -3.82 | 1227 |
| hrrr | — | — | — | -0.96 | -1.21 | -1.83 | -3.63 | -1.81 | 2638 |
| icon | — | — | — | 3.27 | 4.22 | 2.53 | -0.61 | 0.10 | 909 |
| metar | — | — | — | -2.71 | -0.63 | 0.13 | -3.07 | -0.50 | 2772 |
| metno | — | — | — | -2.98 | -2.20 | -2.88 | -5.70 | -3.47 | 1302 |
| nbm | — | — | — | -0.46 | -0.34 | 0.50 | 1.46 | -0.58 | 824 |
| nws_5min | — | — | — | -6.38 | -3.81 | -3.37 | 7.60 | — | 790 |
| nws_5min_diurnal | — | — | — | 0.69 | 1.61 | 2.53 | — | — | 366 |
| nws_point | — | — | — | -0.34 | -0.54 | -0.25 | -3.83 | -2.95 | 2774 |
| ukmo | — | — | — | 2.12 | 2.12 | 2.42 | 2.42 | -1.95 | 308 |
| weather | — | — | — | -0.90 | -1.11 | -1.74 | -3.53 | -2.18 | 2595 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.30 | 1.17 | 0.82 | 0.90 | 0.91 | 1.32 | 1.82 | 1.74 | 10810 |
| ecmwf | 0.64 | 0.32 | 0.32 | 0.55 | 0.49 | 0.53 | 0.54 | 1.08 | 4134 |
| gem | 1.28 | 0.25 | 0.21 | 0.63 | 0.93 | 0.86 | 1.66 | 1.70 | 4069 |
| hrrr | 1.79 | 1.34 | 1.52 | 0.93 | 0.84 | 0.78 | 0.86 | 0.76 | 9338 |
| icon | 0.67 | 0.65 | 0.17 | 0.46 | 0.69 | 0.66 | 0.98 | 1.21 | 5223 |
| madis | 0.70 | 1.35 | 1.06 | 1.85 | 1.39 | 1.85 | 3.58 | 4.76 | 4051 |
| metar | 1.15 | 0.98 | 0.81 | 0.71 | 0.48 | 0.64 | 0.73 | 0.69 | 10801 |
| metno | 0.96 | 0.50 | 0.60 | 0.67 | 0.73 | 0.57 | 1.10 | 1.29 | 4565 |
| nbm | 1.11 | 0.83 | 1.12 | 1.53 | 1.50 | 0.81 | 0.71 | 0.90 | 4070 |
| nws_5min | — | — | — | 1.35 | 0.70 | 1.29 | 3.15 | 43.81 | 1029 |
| nws_5min_analog | — | — | — | — | — | — | — | 5.84 | 4 |
| nws_5min_diurnal | — | — | — | 0.79 | 0.64 | 1.30 | 1.51 | 0.85 | 1528 |
| nws_point | 1.17 | 1.82 | 1.86 | 1.19 | 0.88 | 0.95 | 1.49 | 1.30 | 7922 |
| tomorrow | 3.05 | 3.14 | 3.26 | 2.83 | 2.52 | 2.23 | 2.05 | 2.05 | 1625 |
| ukmo | 0.62 | — | — | 0.84 | 0.88 | 1.14 | 1.09 | 1.01 | 2458 |
| weather | 1.73 | 1.28 | 1.41 | 0.89 | 0.78 | 0.72 | 0.57 | 1.12 | 10532 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 2.67 | 3.20 | 2.41 | 1.96 | 2.00 | 2.46 | 2.13 | 1.91 | 10810 |
| ecmwf | 1.71 | 0.94 | 0.92 | 1.55 | 1.36 | 1.47 | 1.08 | 2.16 | 4134 |
| gem | 4.73 | 1.11 | 0.95 | 2.49 | 3.48 | 3.29 | 3.33 | 3.39 | 4069 |
| hrrr | 3.49 | 2.96 | 3.45 | 1.91 | 1.79 | 1.62 | 1.13 | 1.11 | 9338 |
| icon | 2.87 | 2.78 | 0.71 | 1.91 | 2.68 | 2.57 | 2.43 | 3.01 | 5223 |
| madis | 1.66 | 3.87 | 3.17 | 4.43 | 4.04 | 3.51 | 5.66 | 7.74 | 4051 |
| metar | 2.79 | 2.78 | 2.56 | 1.95 | 1.11 | 1.57 | 1.24 | 0.87 | 10801 |
| metno | 3.77 | 2.28 | 2.75 | 2.72 | 2.75 | 2.20 | 2.20 | 2.58 | 4565 |
| nbm | 1.50 | 1.16 | 1.70 | 2.03 | 2.30 | 1.38 | 1.36 | 1.69 | 4070 |
| nws_5min | — | — | — | 2.70 | 1.31 | 2.06 | 4.12 | 13.40 | 1029 |
| nws_5min_analog | — | — | — | — | — | — | — | 11.69 | 4 |
| nws_5min_diurnal | — | — | — | 1.58 | 1.28 | 2.61 | 3.03 | 1.71 | 1528 |
| nws_point | 3.91 | 3.64 | 3.71 | 3.71 | 3.55 | 3.47 | 4.47 | 5.75 | 7922 |
| tomorrow | 6.10 | 6.28 | 6.51 | 5.66 | 5.03 | 4.45 | 4.10 | 4.10 | 1625 |
| ukmo | 2.26 | — | — | 3.20 | 3.14 | 4.04 | 3.46 | 3.45 | 2458 |
| weather | 3.50 | 2.96 | 3.44 | 1.90 | 1.78 | 1.63 | 1.14 | 2.24 | 10532 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 2.34 | 2.62 | 3.00 | 2.15 | 2.86 | — | — | 15312 |
| ecmwf | 2.04 | 1.14 | 1.47 | 1.44 | 1.55 | 1.48 | 2.57 | 5271 |
| gem | 3.40 | 3.21 | 3.45 | 1.83 | 4.17 | 4.85 | 5.07 | 5296 |
| hrrr | 1.10 | 1.19 | 2.46 | 2.71 | 3.28 | 3.36 | 1.98 | 13703 |
| icon | 2.95 | 2.47 | 2.65 | 1.24 | 2.89 | 2.73 | 3.04 | 6132 |
| madis | 7.55 | 4.94 | 3.77 | 3.96 | 2.62 | — | — | 4123 |
| metar | 1.70 | 2.38 | 2.81 | 2.60 | 2.79 | — | — | 13645 |
| metno | 2.53 | 2.16 | 2.69 | 2.64 | 3.39 | 4.63 | 3.89 | 5867 |
| nbm | 1.66 | 1.36 | 2.76 | 1.96 | 1.38 | 1.44 | 0.46 | 6622 |
| nws_5min | 13.40 | 5.23 | 4.25 | 5.16 | — | — | — | 1819 |
| nws_5min_analog | 11.69 | — | — | — | — | — | — | 4 |
| nws_5min_diurnal | 1.67 | 3.05 | 1.71 | 1.84 | — | — | — | 1894 |
| nws_point | 5.70 | 4.08 | 3.49 | 3.68 | 3.92 | 4.73 | 3.33 | 10768 |
| tomorrow | 4.10 | 4.10 | 5.01 | 6.19 | 6.15 | — | — | 1692 |
| ukmo | 3.34 | 3.87 | 3.34 | 3.26 | 2.49 | 3.56 | 2.12 | 2766 |
| weather | 2.14 | 1.21 | 2.46 | 2.71 | 3.42 | 3.38 | 1.92 | 14855 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| nbm | weather | 22 | 1.000 |
| hrrr | nbm | 22 | 1.000 |
| hrrr | weather | 50 | 1.000 |
| gem | nws_point | 16 | 0.976 |
| metno | nws_point | 16 | 0.967 |
| combined_v2 | tomorrow | 10 | -0.928 |
| gem | metno | 22 | 0.883 |
| hrrr | metno | 22 | 0.833 |
| metno | weather | 22 | 0.828 |
| metar | nws_5min_diurnal | 12 | 0.827 |
| combined_v2 | nbm | 22 | 0.812 |
| icon | metno | 22 | -0.780 |
| metno | nws_5min_diurnal | 12 | -0.776 |
| nws_5min_diurnal | nws_point | 6 | -0.763 |
| hrrr | tomorrow | 10 | -0.736 |
| nbm | tomorrow | 10 | -0.736 |
| tomorrow | weather | 10 | -0.736 |
| combined_v2 | nws_5min_diurnal | 12 | 0.722 |
| gem | icon | 22 | -0.719 |
| combined_v2 | weather | 50 | 0.713 |
| ecmwf | weather | 22 | 0.708 |
| combined_v2 | hrrr | 50 | 0.708 |
| gem | ukmo | 6 | 0.704 |
| ecmwf | hrrr | 22 | 0.701 |
| madis | nws_point | 22 | 0.688 |
| gem | weather | 22 | 0.686 |
| gem | hrrr | 22 | 0.681 |
| combined_v2 | metar | 54 | 0.681 |
| metar | nbm | 22 | 0.670 |
| metno | ukmo | 6 | 0.652 |
| nbm | nws_point | 22 | 0.644 |
| nws_5min | ukmo | 6 | -0.627 |
| nws_point | weather | 44 | 0.596 |
| combined_v2 | ecmwf | 22 | 0.587 |
| hrrr | nws_point | 44 | 0.584 |
| hrrr | metar | 50 | 0.551 |
| metar | weather | 50 | 0.550 |
| combined_v2 | nws_point | 48 | 0.533 |
| gem | nws_5min_diurnal | 12 | -0.470 |
| combined_v2 | madis | 22 | 0.457 |
| ecmwf | metar | 22 | 0.448 |
| metar | nws_point | 48 | 0.425 |
| madis | tomorrow | 10 | -0.422 |
| icon | ukmo | 12 | -0.406 |
| metar | metno | 22 | 0.373 |
| combined_v2 | metno | 22 | 0.366 |
| ecmwf | metno | 22 | 0.336 |
| nws_point | tomorrow | 10 | -0.333 |
| metar | ukmo | 12 | 0.328 |
| metar | tomorrow | 10 | 0.324 |
| combined_v2 | icon | 28 | 0.310 |
| icon | nws_point | 22 | -0.309 |
| combined_v2 | nws_5min | 16 | 0.276 |
| icon | nws_5min_diurnal | 12 | 0.275 |
| ecmwf | icon | 22 | 0.275 |
| ecmwf | nws_5min_diurnal | 12 | -0.269 |
| gem | nws_5min | 16 | -0.250 |
| metar | nws_5min | 16 | 0.249 |
| madis | weather | 22 | 0.242 |
| madis | nbm | 22 | 0.241 |
| metno | nws_5min | 16 | -0.240 |
| nws_point | ukmo | 12 | -0.238 |
| hrrr | madis | 22 | 0.232 |
| icon | metar | 28 | -0.220 |
| ukmo | weather | 12 | 0.173 |
| ecmwf | gem | 22 | 0.173 |
| nws_5min | nws_5min_diurnal | 6 | -0.172 |
| hrrr | ukmo | 12 | 0.171 |
| hrrr | nws_5min | 16 | -0.165 |
| nws_5min | weather | 16 | -0.165 |
| gem | metar | 22 | 0.163 |
| icon | nws_5min | 16 | 0.148 |
| nws_5min | nws_point | 16 | -0.127 |
| hrrr | icon | 28 | -0.121 |
| ecmwf | nws_5min | 16 | -0.108 |
| icon | weather | 28 | -0.103 |
| nws_5min_diurnal | weather | 12 | -0.097 |
| combined_v2 | gem | 22 | 0.090 |
| madis | metar | 22 | 0.077 |
| combined_v2 | ukmo | 12 | -0.070 |
| ecmwf | nws_point | 16 | 0.034 |
| hrrr | nws_5min_diurnal | 12 | -0.027 |
| ecmwf | ukmo | 6 | 0.000 |

_corr ≥ 0.7 = effectively redundant; treat as one source._


### 6. METAR signal value by LST hour

Distribution of (running_max_at_hour − daily_high). Negative = running_max still climbing. ~0 = high reached. `frac_at_high` = fraction of days where running_max ≥ daily_high − 0.5°F at that hour.
| lst_hour | n | median_gap_F | p10 | p90 | frac_at_high | frac_within_1F |
|---|---|---|---|---|---|---|
| 0 | 101 | -10.00 | -17.00 | -6.00 | 0.00 | 0.00 |
| 1 | 100 | -10.00 | -17.00 | -6.00 | 0.00 | 0.00 |
| 2 | 100 | -10.00 | -17.00 | -6.00 | 0.00 | 0.00 |
| 3 | 101 | -10.00 | -17.00 | -6.00 | 0.00 | 0.00 |
| 4 | 100 | -10.00 | -17.00 | -5.00 | 0.00 | 0.00 |
| 5 | 100 | -10.00 | -17.00 | -5.00 | 0.00 | 0.00 |
| 6 | 101 | -10.00 | -17.00 | -6.00 | 0.00 | 0.00 |
| 7 | 101 | -8.00 | -17.00 | -4.00 | 0.00 | 0.00 |
| 8 | 101 | -7.00 | -14.00 | -2.00 | 0.02 | 0.03 |
| 9 | 101 | -4.00 | -11.00 | -1.00 | 0.06 | 0.12 |
| 10 | 101 | -3.00 | -8.00 | 0.00 | 0.12 | 0.21 |
| 11 | 101 | -2.00 | -6.00 | 0.00 | 0.24 | 0.41 |
| 12 | 101 | -1.00 | -4.00 | 0.00 | 0.40 | 0.57 |
| 13 | 101 | 0.00 | -3.00 | 1.00 | 0.53 | 0.76 |
| 14 | 101 | 0.00 | -2.00 | 1.00 | 0.60 | 0.78 |
| 15 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 16 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 17 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 18 | 100 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 19 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 20 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 21 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 22 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |
| 23 | 101 | 0.00 | -2.00 | 1.00 | 0.66 | 0.79 |

_Note: `daily_high_f` is the official NWS daily field. Hourly METAR can miss the actual peak by 0.5-2°F due to reporting cadence; `frac_within_1F` is the more reliable 'high-is-set' indicator._


### 7. Empirical diurnal-phase boundaries

- **Peak hour (LST, mode):** 12
- **First post-peak hour (LST, ≥80% days locked):** -1


### 8. Recommended config (auto-derived heuristics)

_These are starting points. Phase 3 should review, not blindly accept._

**Sources biased >1.5°F at peak window (consider exclusion or correction):**
- `tomorrow`: bias = -4.70°F
- `ukmo`: bias = +3.57°F
- `nws_5min_diurnal`: bias = +1.57°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 43.81
- `nws_5min_analog` LST 21-23: realized RMSE / claimed σ = 5.84
- `madis` LST 21-23: realized RMSE / claimed σ = 4.76
- `madis` LST 18-20: realized RMSE / claimed σ = 3.58
- `tomorrow` LST 06-08: realized RMSE / claimed σ = 3.26
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 3.15
- `tomorrow` LST 03-05: realized RMSE / claimed σ = 3.14
- `tomorrow` LST 00-02: realized RMSE / claimed σ = 3.05
- `tomorrow` LST 09-11: realized RMSE / claimed σ = 2.83
- `tomorrow` LST 12-14: realized RMSE / claimed σ = 2.52
- `tomorrow` LST 15-17: realized RMSE / claimed σ = 2.23
- `tomorrow` LST 18-20: realized RMSE / claimed σ = 2.05
- `tomorrow` LST 21-23: realized RMSE / claimed σ = 2.05
- `nws_point` LST 06-08: realized RMSE / claimed σ = 1.86
- `madis` LST 09-11: realized RMSE / claimed σ = 1.85
- `madis` LST 15-17: realized RMSE / claimed σ = 1.85
- `combined_v2` LST 18-20: realized RMSE / claimed σ = 1.82
- `nws_point` LST 03-05: realized RMSE / claimed σ = 1.82
- `hrrr` LST 00-02: realized RMSE / claimed σ = 1.79
- `combined_v2` LST 21-23: realized RMSE / claimed σ = 1.74

**Highly-correlated source pairs (n_eff < n):**
- `nbm` ↔ `weather`: corr = 1.000
- `hrrr` ↔ `nbm`: corr = 1.000
- `hrrr` ↔ `weather`: corr = 1.000
- `gem` ↔ `nws_point`: corr = 0.976
- `metno` ↔ `nws_point`: corr = 0.967
- `gem` ↔ `metno`: corr = 0.883
- `hrrr` ↔ `metno`: corr = 0.833
- `metno` ↔ `weather`: corr = 0.828
- `metar` ↔ `nws_5min_diurnal`: corr = 0.827
- `combined_v2` ↔ `nbm`: corr = 0.812
- `combined_v2` ↔ `nws_5min_diurnal`: corr = 0.722
- `combined_v2` ↔ `weather`: corr = 0.713
- `ecmwf` ↔ `weather`: corr = 0.708
- `combined_v2` ↔ `hrrr`: corr = 0.708
- `gem` ↔ `ukmo`: corr = 0.704
- `ecmwf` ↔ `hrrr`: corr = 0.701


## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
