# Per-source ensemble scorecard — CHICAGO (KMDW)

**Series:** `KXHIGHCHI`  
**LST offset:** -6h  
**Generated:** 2026-05-05T18:38:34Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 106,550 snapshots, 38 settled days, 16 sources.
- **Empirical peak hour:** LST 14; running-high locks (≥80% days) by LST -1.
- **Biggest peak-window bias offenders:** nws_5min (-2.9°F), nws_point (-2.9°F), metno (-2.2°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | -4.01 | -4.76 | -4.76 | -2.80 | -1.38 | 0.06 | -0.17 | -1.48 | 10521 |
| ecmwf | -4.49 | -4.42 | -4.23 | -3.07 | -2.79 | -1.64 | -2.26 | -2.85 | 4491 |
| gem | -3.22 | — | -2.43 | -0.05 | -0.05 | 0.54 | -0.93 | -3.07 | 3604 |
| hrrr | -2.43 | -2.37 | -3.74 | -2.02 | -1.50 | -0.49 | -0.51 | -0.50 | 8440 |
| icon | -1.95 | — | — | -0.93 | -1.07 | -1.32 | -1.35 | -2.09 | 2160 |
| madis | 3.37 | -0.82 | -1.87 | 0.30 | 0.85 | 1.00 | -2.10 | -12.44 | 4123 |
| metar | -5.01 | -6.21 | -5.64 | -3.24 | -1.37 | -0.15 | -0.08 | -0.29 | 10507 |
| metno | -4.24 | -4.97 | -4.48 | -2.88 | -2.48 | -1.98 | -1.89 | -2.82 | 4492 |
| nbm | -3.28 | -3.45 | -4.55 | -4.12 | -2.24 | 0.20 | 0.92 | -7.80 | 4138 |
| nws_5min | -7.17 | — | — | -2.48 | -3.33 | -2.44 | -3.82 | -12.09 | 1205 |
| nws_5min_analog | — | — | — | — | — | — | 3.08 | — | 1 |
| nws_5min_diurnal | — | — | — | -2.64 | -1.29 | 1.03 | 2.94 | -0.43 | 1012 |
| nws_point | -2.95 | -2.74 | -2.27 | -3.02 | -2.71 | -3.06 | -5.79 | -10.62 | 6716 |
| tomorrow | 18.26 | 11.24 | -6.37 | -5.58 | -1.96 | 2.95 | 3.26 | 3.26 | 1692 |
| ukmo | -2.37 | — | 0.10 | -0.55 | 0.16 | 0.08 | -1.15 | -1.04 | 3480 |
| weather | -2.19 | -2.17 | -3.56 | -1.82 | -1.31 | -0.29 | -0.27 | -3.81 | 9534 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | 1.64 | -2.96 | -3.29 | -1.49 | -2.82 | 0.70 | 3522 |
| ecmwf | — | — | -6.34 | -5.45 | -4.83 | -4.97 | -2.87 | -2.44 | 979 |
| gem | — | — | — | -1.50 | -4.78 | -3.50 | -3.40 | -2.14 | 734 |
| hrrr | — | — | -5.74 | -7.67 | -6.10 | -4.09 | -3.70 | -2.36 | 2523 |
| icon | — | — | — | -0.29 | -1.46 | -2.66 | -4.24 | -2.99 | 1078 |
| metar | — | — | 1.34 | -4.64 | -5.42 | -2.52 | -2.97 | 0.66 | 3518 |
| metno | — | — | -6.00 | -3.09 | -2.58 | -3.04 | -4.74 | -4.30 | 1378 |
| nbm | — | — | -5.71 | -4.80 | -3.97 | -3.23 | -2.80 | -6.46 | 1031 |
| nws_5min | — | — | — | -8.53 | -10.38 | -10.32 | -10.93 | — | 534 |
| nws_5min_analog | — | — | — | — | — | -7.93 | -7.92 | — | 8 |
| nws_5min_diurnal | — | — | — | — | — | — | — | -10.59 | 13 |
| nws_point | — | — | -3.65 | -5.13 | -3.39 | -3.57 | -6.32 | -4.11 | 3432 |
| ukmo | — | — | — | -3.81 | -3.00 | -3.17 | -4.66 | -3.40 | 1767 |
| weather | — | — | -6.82 | -7.44 | -6.15 | -3.73 | -3.30 | -3.59 | 2595 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.81 | 1.72 | 1.81 | 1.31 | 1.12 | 0.94 | 1.72 | 2.78 | 10521 |
| ecmwf | 1.65 | 1.53 | 1.47 | 1.15 | 1.09 | 0.65 | 1.26 | 3.75 | 4491 |
| gem | 0.88 | — | 0.57 | 0.43 | 0.46 | 0.50 | 1.81 | 3.76 | 3604 |
| hrrr | 1.22 | 2.26 | 2.57 | 0.82 | 0.61 | 0.49 | 1.82 | 1.59 | 8440 |
| icon | 0.74 | — | — | 0.30 | 0.36 | 0.39 | 0.46 | 1.12 | 2160 |
| madis | 1.67 | 0.56 | 1.06 | 1.05 | 1.30 | 1.68 | 2.16 | 4.40 | 4123 |
| metar | 2.60 | 2.89 | 2.27 | 1.37 | 0.86 | 0.87 | 1.15 | 1.43 | 10507 |
| metno | 1.08 | 1.08 | 0.97 | 0.90 | 0.82 | 0.79 | 1.43 | 3.98 | 4492 |
| nbm | 2.59 | 2.84 | 3.19 | 2.90 | 2.02 | 1.42 | 1.59 | 6.47 | 4138 |
| nws_5min | 3.58 | — | — | 1.40 | 1.96 | 1.85 | 3.47 | 28.25 | 1205 |
| nws_5min_analog | — | — | — | — | — | — | 1.54 | — | 1 |
| nws_5min_diurnal | — | — | — | 1.32 | 1.06 | 0.95 | 1.48 | 2.75 | 1012 |
| nws_point | 1.28 | 2.05 | 1.41 | 1.14 | 1.24 | 1.51 | 3.21 | 5.92 | 6716 |
| tomorrow | 9.13 | 7.98 | 3.22 | 2.80 | 2.36 | 1.69 | 1.63 | 1.63 | 1692 |
| ukmo | 0.75 | — | 0.02 | 0.77 | 0.71 | 0.65 | 1.25 | 2.22 | 3480 |
| weather | 1.01 | 1.66 | 1.92 | 0.73 | 0.55 | 0.43 | 1.18 | 4.38 | 9534 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 4.95 | 5.99 | 5.80 | 3.78 | 3.38 | 2.61 | 2.63 | 3.24 | 10521 |
| ecmwf | 4.64 | 4.42 | 4.24 | 3.27 | 3.07 | 1.82 | 2.52 | 7.50 | 4491 |
| gem | 3.23 | — | 2.63 | 1.72 | 1.91 | 1.95 | 3.62 | 7.53 | 3604 |
| hrrr | 3.29 | 3.36 | 4.17 | 3.27 | 2.74 | 2.17 | 2.53 | 2.50 | 8440 |
| icon | 1.95 | — | — | 1.06 | 1.18 | 1.35 | 1.36 | 3.30 | 2160 |
| madis | 3.92 | 1.28 | 2.60 | 3.08 | 4.10 | 3.26 | 4.26 | 13.65 | 4123 |
| metar | 6.68 | 8.39 | 7.40 | 4.29 | 2.40 | 2.22 | 2.29 | 1.97 | 10507 |
| metno | 4.53 | 4.97 | 4.50 | 3.75 | 3.41 | 3.13 | 2.85 | 7.96 | 4492 |
| nbm | 3.72 | 4.17 | 4.77 | 4.20 | 3.54 | 2.45 | 3.06 | 12.13 | 4138 |
| nws_5min | 7.17 | — | — | 2.79 | 3.60 | 2.92 | 4.54 | 12.12 | 1205 |
| nws_5min_analog | — | — | — | — | — | — | 3.08 | — | 1 |
| nws_5min_diurnal | — | — | — | 2.65 | 2.12 | 1.90 | 2.95 | 5.50 | 1012 |
| nws_point | 3.83 | 4.10 | 3.09 | 3.94 | 4.71 | 5.13 | 6.41 | 11.84 | 6716 |
| tomorrow | 18.26 | 15.95 | 6.45 | 5.60 | 4.73 | 3.37 | 3.26 | 3.26 | 1692 |
| ukmo | 2.70 | — | 0.10 | 3.51 | 3.11 | 2.86 | 3.29 | 5.84 | 3480 |
| weather | 3.15 | 3.33 | 4.06 | 3.16 | 2.62 | 2.04 | 2.37 | 8.75 | 9534 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 4.16 | 4.17 | 5.22 | 5.15 | 5.31 | 2.30 | 8.94 | 15783 |
| ecmwf | 7.11 | 2.30 | 2.79 | 3.70 | 4.49 | 4.35 | 5.61 | 5527 |
| gem | 7.18 | 3.24 | 1.84 | 1.83 | 3.24 | 3.54 | 1.50 | 4339 |
| hrrr | 2.50 | 2.46 | 4.66 | 3.66 | 3.22 | 4.65 | 8.23 | 12702 |
| icon | 3.14 | 1.36 | 1.23 | 1.01 | 1.83 | 3.42 | 0.57 | 3238 |
| madis | 13.24 | 3.71 | 3.85 | 2.68 | 3.15 | — | — | 4200 |
| metar | 3.61 | 4.22 | 5.65 | 6.64 | 7.34 | — | — | 14103 |
| metno | 7.55 | 2.94 | 3.36 | 4.10 | 4.66 | 3.85 | 4.08 | 5871 |
| nbm | 11.42 | 2.96 | 5.48 | 4.54 | 4.81 | 4.62 | 5.41 | 6908 |
| nws_5min | 11.20 | 6.07 | 6.83 | 6.44 | 7.17 | — | — | 1739 |
| nws_5min_analog | — | 7.54 | — | — | — | — | — | 9 |
| nws_5min_diurnal | 5.53 | 2.71 | 2.05 | 2.88 | — | — | — | 1025 |
| nws_point | 11.96 | 5.89 | 4.78 | 3.49 | 4.17 | 5.61 | 5.52 | 10225 |
| tomorrow | 3.26 | 3.26 | 4.68 | 6.20 | 17.77 | — | — | 1764 |
| ukmo | 5.63 | 3.14 | 3.02 | 3.49 | 2.63 | 4.07 | 4.06 | 5248 |
| weather | 8.30 | 2.32 | 4.62 | 3.55 | 3.82 | 4.95 | 7.78 | 13869 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| nbm | weather | 22 | 1.000 |
| hrrr | nbm | 22 | 1.000 |
| hrrr | weather | 50 | 0.996 |
| hrrr | tomorrow | 10 | 0.981 |
| nbm | tomorrow | 10 | 0.981 |
| tomorrow | weather | 10 | 0.981 |
| combined_v2 | tomorrow | 10 | 0.969 |
| metar | tomorrow | 10 | 0.961 |
| gem | weather | 22 | 0.952 |
| gem | hrrr | 22 | 0.951 |
| nws_5min | nws_5min_diurnal | 5 | 0.940 |
| metno | nws_5min_diurnal | 10 | 0.915 |
| ecmwf | nws_5min_diurnal | 10 | 0.898 |
| icon | ukmo | 12 | -0.893 |
| combined_v2 | nbm | 22 | 0.872 |
| madis | nws_point | 22 | 0.870 |
| combined_v2 | nws_5min_diurnal | 10 | 0.829 |
| combined_v2 | icon | 12 | -0.827 |
| icon | metar | 12 | -0.826 |
| combined_v2 | weather | 50 | 0.820 |
| metar | nws_5min_diurnal | 10 | 0.810 |
| combined_v2 | gem | 22 | 0.802 |
| combined_v2 | hrrr | 50 | 0.798 |
| nws_point | tomorrow | 10 | 0.773 |
| nws_5min | ukmo | 15 | 0.753 |
| hrrr | nws_5min_diurnal | 10 | -0.745 |
| nws_5min | nws_point | 13 | 0.740 |
| gem | metar | 22 | 0.739 |
| ecmwf | icon | 6 | -0.732 |
| metno | nws_point | 14 | 0.723 |
| combined_v2 | ukmo | 22 | 0.715 |
| combined_v2 | nws_point | 47 | 0.700 |
| metno | ukmo | 16 | 0.699 |
| metar | ukmo | 22 | 0.687 |
| metar | nws_5min | 15 | 0.677 |
| combined_v2 | metno | 22 | 0.665 |
| combined_v2 | nws_5min | 15 | 0.655 |
| metar | metno | 22 | 0.642 |
| hrrr | nws_point | 42 | 0.615 |
| nws_point | weather | 42 | 0.614 |
| nbm | nws_point | 22 | 0.600 |
| combined_v2 | madis | 22 | 0.558 |
| combined_v2 | metar | 55 | 0.555 |
| metno | weather | 22 | 0.508 |
| hrrr | metar | 50 | 0.500 |
| ecmwf | metno | 22 | 0.497 |
| metar | weather | 50 | 0.493 |
| metar | nbm | 22 | 0.489 |
| metno | nws_5min | 15 | 0.477 |
| gem | nws_point | 14 | 0.468 |
| hrrr | metno | 22 | 0.442 |
| gem | metno | 22 | 0.426 |
| gem | ukmo | 16 | 0.423 |
| icon | weather | 12 | 0.406 |
| madis | nbm | 22 | 0.394 |
| madis | weather | 22 | 0.393 |
| hrrr | nws_5min | 15 | 0.378 |
| hrrr | madis | 22 | 0.377 |
| icon | nws_point | 10 | -0.360 |
| madis | metar | 22 | 0.349 |
| nws_5min | weather | 15 | 0.343 |
| ecmwf | nws_5min | 15 | -0.338 |
| nws_point | ukmo | 20 | 0.334 |
| metar | nws_point | 47 | 0.331 |
| gem | nws_5min | 15 | 0.241 |
| hrrr | ukmo | 22 | 0.231 |
| ecmwf | ukmo | 16 | -0.228 |
| madis | tomorrow | 10 | 0.218 |
| ecmwf | gem | 22 | 0.192 |
| ukmo | weather | 22 | 0.192 |
| ecmwf | metar | 22 | 0.189 |
| nws_5min_diurnal | weather | 10 | -0.157 |
| gem | nws_5min_diurnal | 10 | 0.152 |
| icon | nws_5min | 6 | -0.118 |
| ecmwf | nws_point | 14 | -0.107 |
| ecmwf | weather | 22 | 0.083 |
| combined_v2 | ecmwf | 22 | 0.052 |
| hrrr | icon | 12 | 0.039 |
| ecmwf | hrrr | 22 | 0.011 |
| gem | icon | 6 | 0.000 |
| icon | metno | 6 | 0.000 |
| nws_5min_diurnal | nws_point | 6 | 0.000 |
| nws_5min_diurnal | ukmo | 6 | 0.000 |

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
- `nws_5min`: bias = -2.88°F
- `nws_point`: bias = -2.88°F
- `metno`: bias = -2.23°F
- `ecmwf`: bias = -2.22°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 28.25
- `tomorrow` LST 00-02: realized RMSE / claimed σ = 9.13
- `tomorrow` LST 03-05: realized RMSE / claimed σ = 7.98
- `nbm` LST 21-23: realized RMSE / claimed σ = 6.47
- `nws_point` LST 21-23: realized RMSE / claimed σ = 5.92
- `madis` LST 21-23: realized RMSE / claimed σ = 4.40
- `weather` LST 21-23: realized RMSE / claimed σ = 4.38
- `metno` LST 21-23: realized RMSE / claimed σ = 3.98
- `gem` LST 21-23: realized RMSE / claimed σ = 3.76
- `ecmwf` LST 21-23: realized RMSE / claimed σ = 3.75
- `nws_5min` LST 00-02: realized RMSE / claimed σ = 3.58
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 3.47
- `tomorrow` LST 06-08: realized RMSE / claimed σ = 3.22
- `nws_point` LST 18-20: realized RMSE / claimed σ = 3.21
- `nbm` LST 06-08: realized RMSE / claimed σ = 3.19
- `nbm` LST 09-11: realized RMSE / claimed σ = 2.90
- `metar` LST 03-05: realized RMSE / claimed σ = 2.89
- `nbm` LST 03-05: realized RMSE / claimed σ = 2.84
- `tomorrow` LST 09-11: realized RMSE / claimed σ = 2.80
- `combined_v2` LST 21-23: realized RMSE / claimed σ = 2.78

**Highly-correlated source pairs (n_eff < n):**
- `nbm` ↔ `weather`: corr = 1.000
- `hrrr` ↔ `nbm`: corr = 1.000
- `hrrr` ↔ `weather`: corr = 0.996
- `hrrr` ↔ `tomorrow`: corr = 0.981
- `nbm` ↔ `tomorrow`: corr = 0.981
- `tomorrow` ↔ `weather`: corr = 0.981
- `combined_v2` ↔ `tomorrow`: corr = 0.969
- `metar` ↔ `tomorrow`: corr = 0.961
- `gem` ↔ `weather`: corr = 0.952
- `gem` ↔ `hrrr`: corr = 0.951
- `nws_5min` ↔ `nws_5min_diurnal`: corr = 0.940
- `metno` ↔ `nws_5min_diurnal`: corr = 0.915
- `ecmwf` ↔ `nws_5min_diurnal`: corr = 0.898
- `combined_v2` ↔ `nbm`: corr = 0.872
- `madis` ↔ `nws_point`: corr = 0.870
- `combined_v2` ↔ `nws_5min_diurnal`: corr = 0.829
- `combined_v2` ↔ `weather`: corr = 0.820
- `metar` ↔ `nws_5min_diurnal`: corr = 0.810
- `combined_v2` ↔ `gem`: corr = 0.802
- `combined_v2` ↔ `hrrr`: corr = 0.798
- `nws_point` ↔ `tomorrow`: corr = 0.773
- `nws_5min` ↔ `ukmo`: corr = 0.753
- `nws_5min` ↔ `nws_point`: corr = 0.740
- `gem` ↔ `metar`: corr = 0.739
- `metno` ↔ `nws_point`: corr = 0.723
- `combined_v2` ↔ `ukmo`: corr = 0.715


## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
