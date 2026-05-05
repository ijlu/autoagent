# Per-source ensemble scorecard — LOS ANGELES (KLAX)

**Series:** `KXHIGHLAX`  
**LST offset:** -8h  
**Generated:** 2026-05-05T18:10:12Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 97,020 snapshots, 38 settled days, 16 sources.
- **Empirical peak hour:** LST 11; running-high locks (≥80% days) by LST 12.
- **Biggest peak-window bias offenders:** gem (+7.2°F), ukmo (+7.0°F), metno (+4.9°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 4.62 | 3.86 | 3.72 | 3.19 | 3.40 | 2.44 | 1.15 | -0.13 | 10191 |
| ecmwf | -1.99 | -1.91 | 0.48 | 1.35 | 0.79 | 0.97 | 1.04 | 0.69 | 4075 |
| gem | — | — | 3.10 | 2.70 | 5.71 | 8.76 | 8.79 | 11.15 | 904 |
| hrrr | 2.35 | 1.92 | 3.44 | 3.48 | 4.04 | 4.31 | 4.14 | 4.23 | 8963 |
| icon | — | — | 5.61 | 5.61 | 5.12 | 4.49 | 4.51 | 4.81 | 1906 |
| madis | -2.28 | -2.40 | -0.20 | 6.39 | 4.37 | -1.81 | -7.11 | -10.90 | 4019 |
| metar | 3.44 | 2.67 | 1.67 | 1.68 | 1.84 | 0.95 | 0.25 | 0.08 | 10180 |
| metno | — | — | 4.46 | 4.45 | 4.35 | 5.46 | 3.83 | 11.60 | 891 |
| nbm | 2.40 | 2.30 | 3.09 | 3.38 | 4.20 | 4.11 | 3.59 | -1.20 | 4022 |
| nws_5min | — | — | -7.32 | 0.71 | 0.34 | -2.39 | -5.49 | -4.15 | 1251 |
| nws_5min_analog | — | — | — | — | 0.80 | 0.80 | 0.80 | — | 24 |
| nws_5min_diurnal | — | — | — | 1.76 | 2.01 | 2.50 | 2.81 | 3.04 | 1066 |
| nws_point | 5.78 | 3.67 | 3.80 | 4.32 | 5.33 | 2.34 | -3.69 | -9.64 | 8175 |
| tomorrow | 8.76 | 6.04 | 1.86 | 2.65 | 2.53 | 0.83 | 0.76 | 0.76 | 1728 |
| ukmo | — | — | 8.05 | 7.81 | 7.45 | 6.63 | 6.42 | 6.59 | 1897 |
| weather | 2.29 | 1.88 | 3.38 | 3.41 | 3.96 | 4.23 | 4.07 | 1.38 | 9689 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | 4.60 | 4.05 | 4.23 | 5.47 | 3.42 | 1.76 | 3720 |
| ecmwf | — | — | 1.91 | 0.81 | 0.77 | 5.99 | 6.53 | 1.85 | 1483 |
| gem | — | — | 3.25 | 3.89 | 3.51 | 4.70 | — | — | 474 |
| hrrr | — | — | 4.48 | 3.88 | 3.94 | 6.14 | 6.96 | 5.22 | 3603 |
| icon | — | — | 6.21 | 5.85 | 5.87 | 5.81 | 6.19 | 5.35 | 1277 |
| metar | — | — | 2.28 | 2.50 | 2.08 | 2.85 | 0.15 | -0.65 | 3719 |
| metno | — | — | 1.80 | 2.23 | 2.29 | 3.11 | — | — | 504 |
| nbm | — | — | 4.98 | 4.83 | 5.86 | 6.62 | 5.28 | 4.42 | 1245 |
| nws_5min | — | — | -5.40 | -0.50 | -0.33 | -3.37 | — | -4.71 | 586 |
| nws_5min_analog | — | — | — | — | — | 1.80 | — | — | 7 |
| nws_5min_diurnal | — | — | — | 1.44 | 1.73 | — | — | 3.30 | 259 |
| nws_point | — | — | 4.88 | 6.90 | 8.45 | 9.43 | 8.76 | 6.07 | 2850 |
| ukmo | — | — | 8.19 | 7.83 | 7.43 | 7.69 | 9.90 | 7.08 | 997 |
| weather | — | — | 4.42 | 3.79 | 3.85 | 6.03 | 6.88 | 5.23 | 3599 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 2.15 | 1.59 | 2.17 | 1.81 | 1.89 | 2.77 | 3.56 | 3.84 | 10191 |
| ecmwf | 0.69 | 0.66 | 1.11 | 1.33 | 1.39 | 1.44 | 1.79 | 1.65 | 4075 |
| gem | — | — | 0.69 | 0.58 | 1.76 | 3.35 | 4.40 | 5.68 | 904 |
| hrrr | 3.22 | 2.75 | 3.46 | 3.09 | 3.29 | 3.32 | 3.69 | 2.98 | 8963 |
| icon | — | — | 1.54 | 1.67 | 1.55 | 1.38 | 1.38 | 1.35 | 1906 |
| madis | 1.43 | 1.88 | 1.41 | 2.62 | 2.28 | 2.04 | 4.56 | 5.92 | 4019 |
| metar | 1.47 | 1.07 | 0.85 | 0.83 | 0.87 | 0.74 | 0.78 | 0.67 | 10180 |
| metno | — | — | 0.95 | 1.03 | 1.19 | 1.79 | 2.68 | 5.90 | 891 |
| nbm | 2.55 | 2.09 | 2.52 | 2.41 | 2.97 | 2.62 | 2.03 | 3.87 | 4022 |
| nws_5min | — | — | 3.66 | 0.82 | 0.92 | 2.10 | 4.62 | 6.95 | 1251 |
| nws_5min_analog | — | — | — | — | 0.48 | 0.48 | 0.47 | — | 24 |
| nws_5min_diurnal | — | — | — | 0.89 | 1.03 | 1.32 | 1.42 | 1.55 | 1066 |
| nws_point | 2.40 | 1.40 | 1.22 | 1.46 | 1.90 | 1.33 | 2.77 | 5.49 | 8175 |
| tomorrow | 4.38 | 3.58 | 1.03 | 1.41 | 1.31 | 0.43 | 0.38 | 0.38 | 1728 |
| ukmo | — | — | 2.10 | 2.24 | 2.15 | 1.91 | 2.33 | 2.34 | 1897 |
| weather | 2.95 | 2.47 | 3.24 | 2.83 | 2.96 | 3.01 | 2.66 | 2.93 | 9689 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 5.91 | 5.02 | 4.85 | 4.39 | 4.61 | 4.06 | 3.92 | 3.92 | 10191 |
| ecmwf | 1.99 | 1.91 | 3.13 | 3.73 | 3.89 | 3.94 | 3.57 | 3.29 | 4075 |
| gem | — | — | 3.19 | 2.70 | 6.42 | 8.76 | 8.80 | 11.36 | 904 |
| hrrr | 4.79 | 4.25 | 5.25 | 4.91 | 5.21 | 5.31 | 5.39 | 5.37 | 8963 |
| icon | — | — | 5.61 | 5.61 | 5.20 | 4.59 | 4.61 | 4.90 | 1906 |
| madis | 3.50 | 5.13 | 3.57 | 7.10 | 5.96 | 4.87 | 8.02 | 11.21 | 4019 |
| metar | 4.22 | 3.40 | 2.41 | 2.18 | 2.16 | 1.51 | 1.22 | 0.73 | 10180 |
| metno | — | — | 4.50 | 4.88 | 5.14 | 6.72 | 5.36 | 11.81 | 891 |
| nbm | 3.33 | 3.04 | 3.35 | 3.78 | 4.69 | 4.48 | 3.87 | 7.27 | 4022 |
| nws_5min | — | — | 7.32 | 1.65 | 1.74 | 3.31 | 5.69 | 4.42 | 1251 |
| nws_5min_analog | — | — | — | — | 0.80 | 0.80 | 0.80 | — | 24 |
| nws_5min_diurnal | — | — | — | 1.79 | 2.07 | 2.63 | 2.84 | 3.10 | 1066 |
| nws_point | 7.09 | 4.74 | 4.53 | 5.70 | 6.97 | 5.10 | 5.97 | 10.99 | 8175 |
| tomorrow | 8.76 | 7.16 | 2.07 | 2.81 | 2.62 | 0.87 | 0.76 | 0.76 | 1728 |
| ukmo | — | — | 8.06 | 7.83 | 7.51 | 6.72 | 6.46 | 6.62 | 1897 |
| weather | 4.76 | 4.22 | 5.20 | 4.85 | 5.13 | 5.24 | 5.33 | 6.47 | 9689 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 3.76 | 4.19 | 4.34 | 4.68 | 5.61 | — | — | 14750 |
| ecmwf | 3.35 | 3.70 | 3.99 | 3.32 | 1.98 | 5.45 | 5.26 | 5615 |
| gem | 11.36 | 8.80 | 6.79 | 2.93 | — | 3.74 | 3.62 | 1378 |
| hrrr | 5.39 | 5.37 | 4.47 | 4.98 | 4.54 | 6.27 | 5.22 | 13405 |
| icon | 4.83 | 4.60 | 5.06 | 5.61 | — | 5.90 | 6.00 | 3183 |
| madis | 11.22 | 6.59 | 5.74 | 5.54 | 4.18 | — | — | 4096 |
| metar | 1.02 | 1.46 | 2.54 | 2.58 | 3.95 | — | — | 13976 |
| metno | 11.81 | 6.29 | 6.04 | 4.21 | — | 2.45 | 1.92 | 1395 |
| nbm | 7.03 | 4.25 | 3.52 | 3.47 | 3.45 | 5.47 | 4.87 | 6106 |
| nws_5min | 4.72 | 4.59 | 1.96 | 2.75 | — | — | — | 1837 |
| nws_5min_analog | — | 1.08 | 1.14 | — | — | — | — | 31 |
| nws_5min_diurnal | 3.13 | 2.83 | 2.01 | 1.54 | — | — | — | 1325 |
| nws_point | 10.85 | 5.14 | 6.58 | 4.95 | 6.39 | 8.53 | 6.26 | 11102 |
| tomorrow | 0.76 | 0.76 | 2.54 | 2.43 | 8.35 | — | — | 1800 |
| ukmo | 6.58 | 6.48 | 7.40 | 7.91 | — | 8.06 | 7.99 | 2894 |
| weather | 6.36 | 5.31 | 4.41 | 4.93 | 4.57 | 6.21 | 5.15 | 14127 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| nbm | weather | 20 | 1.000 |
| hrrr | nbm | 20 | 1.000 |
| hrrr | weather | 50 | 1.000 |
| gem | icon | 8 | 0.989 |
| ecmwf | metno | 13 | 0.982 |
| hrrr | metno | 13 | 0.975 |
| metno | weather | 13 | 0.975 |
| gem | metno | 10 | 0.971 |
| combined_v2 | gem | 11 | 0.955 |
| ecmwf | ukmo | 8 | 0.952 |
| combined_v2 | metno | 13 | 0.950 |
| metar | tomorrow | 8 | 0.947 |
| madis | tomorrow | 8 | 0.943 |
| ecmwf | gem | 11 | 0.933 |
| ecmwf | hrrr | 24 | 0.909 |
| ecmwf | weather | 24 | 0.909 |
| nbm | nws_point | 20 | 0.902 |
| gem | hrrr | 11 | 0.887 |
| gem | weather | 11 | 0.887 |
| metar | nbm | 20 | 0.883 |
| icon | metno | 7 | 0.882 |
| madis | nws_point | 20 | 0.865 |
| nws_point | tomorrow | 8 | 0.862 |
| combined_v2 | madis | 20 | 0.833 |
| nws_5min_diurnal | nws_point | 12 | -0.833 |
| combined_v2 | nbm | 20 | 0.831 |
| metar | metno | 13 | 0.803 |
| metar | nws_5min | 15 | 0.798 |
| combined_v2 | icon | 14 | 0.796 |
| metno | nws_5min_diurnal | 6 | -0.784 |
| combined_v2 | tomorrow | 8 | 0.759 |
| icon | nws_point | 13 | 0.758 |
| combined_v2 | hrrr | 50 | 0.750 |
| combined_v2 | weather | 50 | 0.746 |
| combined_v2 | metar | 54 | 0.741 |
| combined_v2 | ecmwf | 24 | 0.738 |
| combined_v2 | nws_5min | 15 | 0.737 |
| combined_v2 | nws_point | 49 | 0.714 |
| hrrr | icon | 14 | 0.709 |
| gem | metar | 11 | 0.708 |
| madis | metar | 20 | 0.707 |
| icon | weather | 14 | 0.706 |
| metno | nws_5min | 6 | 0.686 |
| hrrr | metar | 50 | 0.678 |
| metar | weather | 50 | 0.677 |
| hrrr | madis | 20 | 0.658 |
| madis | weather | 20 | 0.653 |
| madis | nbm | 20 | 0.653 |
| hrrr | nws_point | 45 | 0.622 |
| nws_point | weather | 45 | 0.615 |
| icon | metar | 14 | 0.613 |
| ecmwf | nws_5min_diurnal | 12 | 0.533 |
| ecmwf | metar | 24 | 0.515 |
| metno | ukmo | 7 | 0.470 |
| icon | ukmo | 14 | -0.410 |
| metar | nws_point | 49 | 0.407 |
| ecmwf | nws_point | 20 | 0.398 |
| metno | nws_point | 13 | 0.316 |
| combined_v2 | ukmo | 14 | -0.233 |
| nws_5min | nws_5min_diurnal | 12 | 0.224 |
| hrrr | tomorrow | 8 | 0.221 |
| ecmwf | nws_5min | 15 | 0.213 |
| ecmwf | icon | 8 | 0.196 |
| nws_point | ukmo | 13 | -0.177 |
| hrrr | nws_5min_diurnal | 12 | -0.172 |
| nws_5min_diurnal | weather | 12 | -0.171 |
| nbm | tomorrow | 8 | 0.153 |
| tomorrow | weather | 8 | 0.153 |
| hrrr | nws_5min | 15 | -0.117 |
| nws_5min | weather | 15 | -0.116 |
| gem | ukmo | 8 | 0.100 |
| metar | ukmo | 14 | 0.098 |
| metar | nws_5min_diurnal | 12 | -0.091 |
| hrrr | ukmo | 14 | -0.083 |
| combined_v2 | nws_5min_diurnal | 12 | -0.082 |
| ukmo | weather | 14 | -0.076 |
| gem | nws_point | 11 | 0.027 |
| nws_5min | nws_point | 12 | -0.008 |

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
- `gem`: bias = +7.23°F
- `ukmo`: bias = +7.04°F
- `metno`: bias = +4.91°F
- `icon`: bias = +4.81°F
- `hrrr`: bias = +4.17°F
- `nbm`: bias = +4.16°F
- `weather`: bias = +4.10°F
- `nws_point`: bias = +3.84°F
- `combined_v2`: bias = +2.92°F
- `nws_5min_diurnal`: bias = +2.25°F
- `tomorrow`: bias = +1.68°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 6.95
- `madis` LST 21-23: realized RMSE / claimed σ = 5.92
- `metno` LST 21-23: realized RMSE / claimed σ = 5.90
- `gem` LST 21-23: realized RMSE / claimed σ = 5.68
- `nws_point` LST 21-23: realized RMSE / claimed σ = 5.49
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 4.62
- `madis` LST 18-20: realized RMSE / claimed σ = 4.56
- `gem` LST 18-20: realized RMSE / claimed σ = 4.40
- `tomorrow` LST 00-02: realized RMSE / claimed σ = 4.38
- `nbm` LST 21-23: realized RMSE / claimed σ = 3.87
- `combined_v2` LST 21-23: realized RMSE / claimed σ = 3.84
- `hrrr` LST 18-20: realized RMSE / claimed σ = 3.69
- `nws_5min` LST 06-08: realized RMSE / claimed σ = 3.66
- `tomorrow` LST 03-05: realized RMSE / claimed σ = 3.58
- `combined_v2` LST 18-20: realized RMSE / claimed σ = 3.56
- `hrrr` LST 06-08: realized RMSE / claimed σ = 3.46
- `gem` LST 15-17: realized RMSE / claimed σ = 3.35
- `hrrr` LST 15-17: realized RMSE / claimed σ = 3.32
- `hrrr` LST 12-14: realized RMSE / claimed σ = 3.29
- `weather` LST 06-08: realized RMSE / claimed σ = 3.24

**Highly-correlated source pairs (n_eff < n):**
- `nbm` ↔ `weather`: corr = 1.000
- `hrrr` ↔ `nbm`: corr = 1.000
- `hrrr` ↔ `weather`: corr = 1.000
- `gem` ↔ `icon`: corr = 0.989
- `ecmwf` ↔ `metno`: corr = 0.982
- `hrrr` ↔ `metno`: corr = 0.975
- `metno` ↔ `weather`: corr = 0.975
- `gem` ↔ `metno`: corr = 0.971
- `combined_v2` ↔ `gem`: corr = 0.955
- `ecmwf` ↔ `ukmo`: corr = 0.952
- `combined_v2` ↔ `metno`: corr = 0.950
- `metar` ↔ `tomorrow`: corr = 0.947
- `madis` ↔ `tomorrow`: corr = 0.943
- `ecmwf` ↔ `gem`: corr = 0.933
- `ecmwf` ↔ `hrrr`: corr = 0.909
- `ecmwf` ↔ `weather`: corr = 0.909
- `nbm` ↔ `nws_point`: corr = 0.902
- `gem` ↔ `hrrr`: corr = 0.887
- `gem` ↔ `weather`: corr = 0.887
- `metar` ↔ `nbm`: corr = 0.883
- `icon` ↔ `metno`: corr = 0.882
- `madis` ↔ `nws_point`: corr = 0.865
- `nws_point` ↔ `tomorrow`: corr = 0.862
- `combined_v2` ↔ `madis`: corr = 0.833
- `combined_v2` ↔ `nbm`: corr = 0.831
- `metar` ↔ `metno`: corr = 0.803
- `metar` ↔ `nws_5min`: corr = 0.798
- `combined_v2` ↔ `icon`: corr = 0.796
- `combined_v2` ↔ `tomorrow`: corr = 0.759
- `icon` ↔ `nws_point`: corr = 0.758
- `combined_v2` ↔ `hrrr`: corr = 0.750
- `combined_v2` ↔ `weather`: corr = 0.746
- `combined_v2` ↔ `metar`: corr = 0.741
- `combined_v2` ↔ `ecmwf`: corr = 0.738
- `combined_v2` ↔ `nws_5min`: corr = 0.737
- `combined_v2` ↔ `nws_point`: corr = 0.714
- `hrrr` ↔ `icon`: corr = 0.709
- `gem` ↔ `metar`: corr = 0.708
- `madis` ↔ `metar`: corr = 0.707
- `icon` ↔ `weather`: corr = 0.706

**Cross-bracket LST gate suggestion:** fire only when recording_lst_hour ≥ 12 AND day_offset == 0.

## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
