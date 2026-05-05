# Per-source ensemble scorecard — DENVER (KDEN)

**Series:** `KXHIGHDEN`  
**LST offset:** -7h  
**Generated:** 2026-05-05T18:44:24Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 110,808 snapshots, 38 settled days, 15 sources.
- **Empirical peak hour:** LST 13; running-high locks (≥80% days) by LST 15.
- **Biggest peak-window bias offenders:** nws_5min_diurnal (-4.7°F), nbm (+2.9°F), gem (-2.9°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 1.56 | -0.52 | -1.51 | -0.76 | 0.62 | 0.53 | -0.20 | -2.16 | 10856 |
| ecmwf | -4.04 | -3.21 | -4.16 | -3.67 | -3.34 | -1.09 | -2.00 | -2.95 | 4294 |
| gem | -3.84 | -4.11 | -3.22 | -4.72 | -3.29 | -2.46 | -1.89 | 0.20 | 3703 |
| hrrr | 3.51 | 3.20 | 2.77 | 1.84 | 1.92 | 1.24 | 1.23 | 1.10 | 9488 |
| icon | 3.16 | — | -2.43 | -2.65 | -2.54 | -2.05 | -2.18 | -2.86 | 2283 |
| madis | -10.22 | -12.35 | -9.17 | -2.40 | 2.48 | 1.54 | -6.73 | -25.52 | 4241 |
| metar | 1.34 | -1.61 | -2.87 | -2.83 | -0.39 | 0.32 | 0.15 | 0.05 | 10849 |
| metno | -3.98 | -4.25 | -3.42 | -3.90 | -2.18 | -2.38 | -2.67 | -3.78 | 3901 |
| nbm | 6.40 | 7.84 | 4.74 | 2.58 | 3.01 | 2.76 | 3.08 | 0.84 | 4250 |
| nws_5min | — | — | -14.60 | 0.28 | -1.07 | -1.14 | -3.44 | -19.88 | 1260 |
| nws_5min_diurnal | — | — | — | -3.90 | -4.54 | -4.85 | 0.16 | 0.67 | 840 |
| nws_point | 3.39 | 4.00 | 3.50 | 3.26 | 2.93 | 1.57 | -0.84 | -12.04 | 8134 |
| tomorrow | 20.12 | 17.30 | 5.45 | 1.89 | 1.68 | 1.80 | 2.12 | 2.12 | 1794 |
| ukmo | — | — | 2.39 | 1.63 | 1.00 | 0.26 | -0.54 | -0.26 | 2229 |
| weather | 3.34 | 2.97 | 2.53 | 1.54 | 1.61 | 0.98 | 1.00 | -0.56 | 10449 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | 4.20 | 1.57 | 0.35 | 0.46 | -3.65 | -2.21 | 3995 |
| ecmwf | — | — | -5.22 | -3.50 | -3.73 | -3.62 | -2.72 | -2.63 | 1621 |
| gem | — | — | -3.81 | -2.12 | -1.99 | -1.69 | -2.11 | -2.43 | 1151 |
| hrrr | — | — | 5.07 | 4.38 | 1.90 | 1.38 | 3.00 | 3.50 | 2954 |
| icon | — | — | -1.66 | -1.44 | -1.36 | -0.74 | -1.52 | 1.12 | 1174 |
| metar | — | — | 2.52 | -1.72 | -2.04 | -1.74 | -6.23 | -3.32 | 3992 |
| metno | — | — | -0.96 | -3.62 | -1.14 | -1.25 | -1.44 | -1.73 | 1240 |
| nbm | — | — | 3.71 | 3.58 | 2.71 | 2.51 | 4.32 | 4.16 | 1208 |
| nws_5min | — | — | — | -0.92 | 1.62 | -0.63 | — | -12.97 | 348 |
| nws_5min_diurnal | — | — | — | -4.47 | -4.58 | -5.18 | — | -0.14 | 313 |
| nws_point | — | — | 2.29 | 4.34 | 3.59 | 2.91 | 2.37 | 2.54 | 3245 |
| ukmo | — | — | — | — | 0.66 | 0.69 | 0.46 | 0.39 | 646 |
| weather | — | — | 4.85 | 4.14 | 1.48 | 1.04 | 2.75 | 3.07 | 3069 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 2.91 | 2.65 | 1.66 | 1.22 | 1.00 | 1.06 | 0.91 | 4.96 | 10856 |
| ecmwf | 1.44 | 1.11 | 1.46 | 1.40 | 1.32 | 0.43 | 1.17 | 4.57 | 4294 |
| gem | 0.85 | 0.92 | 0.84 | 1.39 | 1.02 | 1.19 | 1.76 | 2.87 | 3703 |
| hrrr | 2.09 | 1.95 | 1.42 | 0.88 | 0.87 | 0.68 | 1.82 | 1.73 | 9488 |
| icon | 1.22 | — | 0.60 | 0.73 | 0.74 | 0.64 | 0.71 | 1.13 | 2283 |
| madis | 4.21 | 4.52 | 3.90 | 2.05 | 1.65 | 1.55 | 3.35 | 14.46 | 4241 |
| metar | 2.02 | 1.60 | 1.26 | 1.27 | 0.62 | 0.31 | 0.33 | 0.98 | 10849 |
| metno | 0.87 | 0.92 | 0.89 | 1.20 | 0.66 | 0.89 | 1.65 | 5.23 | 3901 |
| nbm | 3.53 | 4.28 | 2.81 | 2.18 | 2.01 | 1.49 | 1.62 | 2.92 | 4250 |
| nws_5min | — | — | 7.30 | 0.80 | 1.55 | 1.19 | 2.96 | 37.57 | 1260 |
| nws_5min_diurnal | — | — | — | 2.00 | 2.28 | 2.45 | 1.12 | 0.75 | 840 |
| nws_point | 1.83 | 2.12 | 1.35 | 0.97 | 0.92 | 1.00 | 1.20 | 4.77 | 8134 |
| tomorrow | 10.06 | 9.10 | 2.77 | 1.15 | 0.96 | 0.92 | 1.06 | 1.06 | 1794 |
| ukmo | — | — | 0.55 | 0.51 | 0.41 | 0.34 | 0.75 | 0.97 | 2229 |
| weather | 2.03 | 1.86 | 1.37 | 0.82 | 0.79 | 0.65 | 1.23 | 3.31 | 10449 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 4.46 | 4.21 | 2.59 | 2.22 | 1.58 | 1.30 | 1.02 | 5.61 | 10856 |
| ecmwf | 4.15 | 3.21 | 4.18 | 3.90 | 3.66 | 1.20 | 2.33 | 9.15 | 4294 |
| gem | 3.84 | 4.12 | 3.52 | 5.02 | 3.63 | 3.58 | 3.52 | 5.75 | 3703 |
| hrrr | 5.85 | 6.05 | 4.30 | 3.10 | 2.95 | 2.34 | 2.42 | 2.49 | 9488 |
| icon | 3.16 | — | 2.46 | 2.65 | 2.56 | 2.08 | 2.21 | 3.65 | 2283 |
| madis | 10.35 | 12.39 | 9.57 | 5.38 | 4.42 | 3.12 | 10.55 | 27.94 | 4241 |
| metar | 5.50 | 5.35 | 3.71 | 3.44 | 1.55 | 0.61 | 0.55 | 1.21 | 10849 |
| metno | 3.99 | 4.26 | 3.82 | 4.14 | 2.31 | 2.69 | 3.30 | 10.47 | 3901 |
| nbm | 6.89 | 8.11 | 5.28 | 3.93 | 3.81 | 2.90 | 3.10 | 5.46 | 4250 |
| nws_5min | — | — | 14.60 | 1.59 | 2.93 | 1.91 | 3.67 | 21.46 | 1260 |
| nws_5min_diurnal | — | — | — | 3.99 | 4.56 | 4.91 | 2.23 | 1.51 | 840 |
| nws_point | 3.73 | 4.24 | 3.88 | 3.87 | 3.62 | 3.69 | 3.71 | 14.49 | 8134 |
| tomorrow | 20.12 | 18.20 | 5.55 | 2.31 | 1.93 | 1.85 | 2.12 | 2.12 | 1794 |
| ukmo | — | — | 2.39 | 1.88 | 1.47 | 1.14 | 2.13 | 2.72 | 2229 |
| weather | 5.91 | 6.13 | 4.29 | 3.01 | 2.84 | 2.35 | 2.46 | 7.74 | 10449 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 5.53 | 2.32 | 4.50 | 3.69 | 4.41 | 1.13 | 3.31 | 16585 |
| ecmwf | 8.61 | 1.98 | 3.32 | 3.99 | 3.65 | 3.40 | 3.93 | 5972 |
| gem | 5.48 | 3.58 | 3.85 | 4.51 | 3.96 | 2.16 | 2.75 | 4854 |
| hrrr | 2.47 | 2.38 | 4.66 | 3.82 | 5.87 | 3.43 | 5.43 | 14176 |
| icon | 3.43 | 2.22 | 2.40 | 2.63 | 8.73 | 2.42 | 1.46 | 3457 |
| madis | 27.74 | 6.70 | 3.97 | 8.10 | 10.98 | — | — | 4313 |
| metar | 4.24 | 3.83 | 4.69 | 5.10 | 5.48 | — | — | 14913 |
| metno | 9.87 | 3.00 | 2.74 | 4.01 | 4.06 | 1.77 | 4.04 | 5141 |
| nbm | 5.31 | 3.06 | 5.44 | 4.85 | 7.07 | 3.68 | 3.61 | 7192 |
| nws_5min | 17.17 | 2.84 | 2.38 | 3.98 | — | — | — | 1608 |
| nws_5min_diurnal | 1.50 | 3.52 | 4.60 | 4.38 | — | — | — | 1153 |
| nws_point | 14.06 | 3.48 | 3.66 | 3.83 | 3.78 | 3.47 | 3.97 | 11451 |
| tomorrow | 2.12 | 2.12 | 1.92 | 4.53 | 20.08 | — | — | 1866 |
| ukmo | 2.63 | 1.78 | 1.47 | 1.94 | — | 0.58 | — | 2875 |
| weather | 7.31 | 2.41 | 4.63 | 3.79 | 5.92 | 3.12 | 5.31 | 15252 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| nbm | weather | 21 | 1.000 |
| hrrr | nbm | 21 | 1.000 |
| hrrr | weather | 52 | 0.994 |
| nws_5min | ukmo | 6 | -0.940 |
| madis | nws_point | 21 | 0.895 |
| gem | ukmo | 7 | -0.885 |
| icon | nws_5min | 6 | 0.882 |
| gem | metno | 19 | 0.868 |
| hrrr | tomorrow | 9 | 0.860 |
| nbm | tomorrow | 9 | 0.860 |
| tomorrow | weather | 9 | 0.860 |
| gem | weather | 20 | 0.837 |
| metno | ukmo | 7 | -0.833 |
| hrrr | madis | 21 | 0.825 |
| metno | weather | 19 | 0.815 |
| hrrr | metno | 19 | 0.812 |
| madis | nbm | 21 | 0.811 |
| madis | weather | 21 | 0.811 |
| gem | hrrr | 20 | 0.810 |
| ecmwf | hrrr | 19 | -0.697 |
| combined_v2 | nws_5min | 22 | 0.692 |
| metar | nbm | 21 | 0.691 |
| ecmwf | nws_point | 13 | -0.688 |
| ecmwf | weather | 19 | -0.685 |
| hrrr | nws_point | 46 | 0.627 |
| nws_point | weather | 46 | 0.620 |
| nbm | nws_point | 21 | 0.591 |
| nws_point | ukmo | 13 | -0.572 |
| metar | tomorrow | 9 | 0.565 |
| madis | metar | 21 | 0.563 |
| combined_v2 | metar | 57 | 0.563 |
| ecmwf | metno | 13 | -0.522 |
| combined_v2 | icon | 13 | 0.519 |
| ecmwf | metar | 19 | 0.473 |
| madis | tomorrow | 9 | 0.461 |
| metar | ukmo | 13 | -0.437 |
| metno | nws_point | 19 | 0.425 |
| combined_v2 | weather | 52 | 0.419 |
| icon | metar | 13 | 0.416 |
| metar | weather | 52 | 0.409 |
| combined_v2 | hrrr | 52 | 0.408 |
| hrrr | metar | 52 | 0.401 |
| nws_5min_diurnal | weather | 7 | 0.400 |
| icon | metno | 7 | -0.394 |
| hrrr | nws_5min_diurnal | 7 | 0.393 |
| combined_v2 | nbm | 21 | 0.376 |
| nws_point | tomorrow | 9 | -0.372 |
| metar | nws_5min | 22 | 0.355 |
| combined_v2 | madis | 21 | 0.318 |
| combined_v2 | ecmwf | 19 | 0.311 |
| gem | nws_5min | 17 | 0.302 |
| metar | nws_5min_diurnal | 7 | -0.296 |
| ecmwf | gem | 14 | -0.296 |
| hrrr | nws_5min | 22 | 0.290 |
| nws_5min | weather | 22 | 0.287 |
| icon | nws_point | 13 | 0.286 |
| icon | ukmo | 13 | -0.270 |
| gem | icon | 7 | -0.252 |
| nws_5min | nws_point | 16 | 0.223 |
| combined_v2 | gem | 20 | 0.220 |
| icon | weather | 13 | -0.178 |
| combined_v2 | nws_5min_diurnal | 7 | 0.169 |
| hrrr | icon | 13 | -0.152 |
| hrrr | ukmo | 13 | 0.146 |
| metar | metno | 19 | -0.122 |
| metno | nws_5min | 16 | 0.110 |
| metar | nws_point | 51 | 0.109 |
| combined_v2 | ukmo | 13 | 0.102 |
| nws_5min | nws_5min_diurnal | 7 | 0.102 |
| combined_v2 | nws_point | 51 | 0.095 |
| ecmwf | nws_5min | 16 | 0.092 |
| gem | nws_point | 19 | 0.090 |
| gem | metar | 20 | -0.052 |
| ecmwf | nws_5min_diurnal | 7 | 0.046 |
| combined_v2 | metno | 19 | -0.034 |
| ukmo | weather | 13 | -0.016 |
| combined_v2 | tomorrow | 9 | -0.009 |

_corr ≥ 0.7 = effectively redundant; treat as one source._


### 6. METAR signal value by LST hour

Distribution of (running_max_at_hour − daily_high). Negative = running_max still climbing. ~0 = high reached. `frac_at_high` = fraction of days where running_max ≥ daily_high − 0.5°F at that hour.
| lst_hour | n | median_gap_F | p10 | p90 | frac_at_high | frac_within_1F |
|---|---|---|---|---|---|---|
| 0 | 100 | -23.00 | -38.00 | -7.00 | 0.04 | 0.04 |
| 1 | 101 | -22.00 | -38.00 | -10.00 | 0.05 | 0.05 |
| 2 | 100 | -21.00 | -35.00 | -7.00 | 0.05 | 0.05 |
| 3 | 100 | -21.00 | -34.00 | -7.00 | 0.05 | 0.05 |
| 4 | 101 | -21.00 | -33.00 | -8.00 | 0.05 | 0.05 |
| 5 | 101 | -21.00 | -31.00 | -8.00 | 0.05 | 0.05 |
| 6 | 101 | -20.00 | -30.00 | -7.00 | 0.05 | 0.05 |
| 7 | 101 | -17.00 | -29.00 | -5.00 | 0.06 | 0.07 |
| 8 | 101 | -13.00 | -25.00 | -3.00 | 0.09 | 0.09 |
| 9 | 101 | -9.00 | -21.00 | 0.00 | 0.11 | 0.11 |
| 10 | 101 | -6.00 | -15.00 | 0.00 | 0.14 | 0.17 |
| 11 | 101 | -4.00 | -11.00 | 0.00 | 0.17 | 0.22 |
| 12 | 101 | -2.00 | -9.00 | 0.00 | 0.25 | 0.39 |
| 13 | 101 | -1.00 | -7.00 | 1.00 | 0.48 | 0.64 |
| 14 | 101 | 0.00 | -7.00 | 2.00 | 0.62 | 0.75 |
| 15 | 101 | 0.00 | -7.00 | 2.00 | 0.69 | 0.82 |
| 16 | 101 | 0.00 | -7.00 | 2.00 | 0.71 | 0.83 |
| 17 | 101 | 0.00 | -7.00 | 2.00 | 0.71 | 0.83 |
| 18 | 101 | 0.00 | -7.00 | 2.00 | 0.71 | 0.83 |
| 19 | 101 | 0.00 | -7.00 | 2.00 | 0.71 | 0.83 |
| 20 | 101 | 0.00 | -7.00 | 2.00 | 0.71 | 0.83 |
| 21 | 101 | 0.00 | -7.00 | 2.00 | 0.71 | 0.83 |
| 22 | 101 | 0.00 | -7.00 | 2.00 | 0.71 | 0.83 |
| 23 | 100 | 0.00 | -7.00 | 6.00 | 0.71 | 0.83 |

_Note: `daily_high_f` is the official NWS daily field. Hourly METAR can miss the actual peak by 0.5-2°F due to reporting cadence; `frac_within_1F` is the more reliable 'high-is-set' indicator._


### 7. Empirical diurnal-phase boundaries

- **Peak hour (LST, mode):** 13
- **First post-peak hour (LST, ≥80% days locked):** 15


### 8. Recommended config (auto-derived heuristics)

_These are starting points. Phase 3 should review, not blindly accept._

**Sources biased >1.5°F at peak window (consider exclusion or correction):**
- `nws_5min_diurnal`: bias = -4.69°F
- `nbm`: bias = +2.88°F
- `gem`: bias = -2.87°F
- `icon`: bias = -2.30°F
- `metno`: bias = -2.28°F
- `nws_point`: bias = +2.25°F
- `ecmwf`: bias = -2.22°F
- `madis`: bias = +2.01°F
- `tomorrow`: bias = +1.74°F
- `hrrr`: bias = +1.58°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 37.57
- `madis` LST 21-23: realized RMSE / claimed σ = 14.46
- `tomorrow` LST 00-02: realized RMSE / claimed σ = 10.06
- `tomorrow` LST 03-05: realized RMSE / claimed σ = 9.10
- `nws_5min` LST 06-08: realized RMSE / claimed σ = 7.30
- `metno` LST 21-23: realized RMSE / claimed σ = 5.23
- `combined_v2` LST 21-23: realized RMSE / claimed σ = 4.96
- `nws_point` LST 21-23: realized RMSE / claimed σ = 4.77
- `ecmwf` LST 21-23: realized RMSE / claimed σ = 4.57
- `madis` LST 03-05: realized RMSE / claimed σ = 4.52
- `nbm` LST 03-05: realized RMSE / claimed σ = 4.28
- `madis` LST 00-02: realized RMSE / claimed σ = 4.21
- `madis` LST 06-08: realized RMSE / claimed σ = 3.90
- `nbm` LST 00-02: realized RMSE / claimed σ = 3.53
- `madis` LST 18-20: realized RMSE / claimed σ = 3.35
- `weather` LST 21-23: realized RMSE / claimed σ = 3.31
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 2.96
- `nbm` LST 21-23: realized RMSE / claimed σ = 2.92
- `combined_v2` LST 00-02: realized RMSE / claimed σ = 2.91
- `gem` LST 21-23: realized RMSE / claimed σ = 2.87

**Highly-correlated source pairs (n_eff < n):**
- `nbm` ↔ `weather`: corr = 1.000
- `hrrr` ↔ `nbm`: corr = 1.000
- `hrrr` ↔ `weather`: corr = 0.994
- `madis` ↔ `nws_point`: corr = 0.895
- `icon` ↔ `nws_5min`: corr = 0.882
- `gem` ↔ `metno`: corr = 0.868
- `hrrr` ↔ `tomorrow`: corr = 0.860
- `nbm` ↔ `tomorrow`: corr = 0.860
- `tomorrow` ↔ `weather`: corr = 0.860
- `gem` ↔ `weather`: corr = 0.837
- `hrrr` ↔ `madis`: corr = 0.825
- `metno` ↔ `weather`: corr = 0.815
- `hrrr` ↔ `metno`: corr = 0.812
- `madis` ↔ `nbm`: corr = 0.811
- `madis` ↔ `weather`: corr = 0.811
- `gem` ↔ `hrrr`: corr = 0.810

**Cross-bracket LST gate suggestion:** fire only when recording_lst_hour ≥ 15 AND day_offset == 0.

## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
