# Per-source ensemble scorecard — DENVER (KDEN)

**Series:** `KXHIGHDEN`  
**LST offset:** -7h  
**Generated:** 2026-05-05T18:53:09Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 16,790 snapshots, 2 settled days, 10 sources.
- **Empirical peak hour:** LST 13; running-high locks (≥80% days) by LST 15.
- **Biggest peak-window bias offenders:** gem (-5.5°F), nws_5min_diurnal (-4.5°F), metno (-3.4°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | -4.93 | -5.26 | -3.99 | -1.70 | 0.01 | -0.27 | -0.28 | -0.28 | 2209 |
| ecmwf | -4.06 | -3.21 | -4.25 | -4.41 | -2.92 | -0.86 | -2.23 | -7.51 | 2209 |
| gem | -3.86 | -4.11 | -4.25 | -5.62 | -5.61 | -5.42 | -5.42 | -5.89 | 1676 |
| hrrr | -3.47 | -2.82 | -2.40 | -1.38 | -1.86 | -2.09 | -1.94 | -2.48 | 2017 |
| metar | -6.74 | -7.55 | -5.87 | -3.29 | -0.24 | -0.27 | -0.28 | -0.28 | 2208 |
| metno | -4.02 | -4.25 | -4.72 | -4.72 | -3.12 | -3.73 | -4.83 | -10.01 | 2005 |
| nws_5min | — | — | — | 1.68 | -0.64 | -4.03 | -4.78 | -23.72 | 343 |
| nws_5min_diurnal | — | — | — | -3.21 | -4.76 | -4.15 | 0.16 | 0.67 | 599 |
| nws_point | — | — | 1.77 | 1.77 | 2.02 | -2.45 | -1.25 | -12.40 | 871 |
| weather | -3.98 | -3.33 | -2.91 | -1.89 | -2.37 | -2.63 | -2.50 | -7.90 | 2209 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | — | — | — | — | — | — | — | -0.48 | 75 |
| ecmwf | — | — | — | — | — | — | — | -6.01 | 36 |
| hrrr | — | — | — | — | — | — | — | -3.78 | 74 |
| metar | — | — | — | — | — | — | — | -0.31 | 74 |
| nws_5min | — | — | — | — | — | — | — | -12.97 | 1 |
| nws_5min_diurnal | — | — | — | — | — | — | — | -0.14 | 74 |
| nws_point | — | — | — | — | — | — | — | 0.43 | 74 |
| weather | — | — | — | — | — | — | — | -4.03 | 36 |

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 2.84 | 2.99 | 2.34 | 1.12 | 0.21 | 0.27 | 0.28 | 0.36 | 2209 |
| ecmwf | 1.44 | 1.11 | 1.48 | 1.52 | 1.20 | 0.35 | 1.15 | 5.77 | 2209 |
| gem | 0.86 | 0.92 | 0.95 | 1.25 | 1.25 | 1.21 | 2.71 | 2.97 | 1676 |
| hrrr | 0.69 | 0.54 | 0.48 | 0.27 | 0.37 | 0.43 | 1.56 | 1.66 | 2017 |
| metar | 3.41 | 3.79 | 3.01 | 1.72 | 0.15 | 0.87 | 0.89 | 0.75 | 2208 |
| metno | 0.87 | 0.92 | 1.02 | 1.02 | 0.68 | 0.83 | 2.43 | 6.71 | 2005 |
| nws_5min | — | — | — | 0.91 | 1.56 | 2.56 | 3.90 | 52.78 | 343 |
| nws_5min_diurnal | — | — | — | 1.62 | 2.39 | 2.09 | 1.12 | 0.75 | 599 |
| nws_point | — | — | 0.36 | 0.36 | 0.42 | 0.66 | 1.76 | 6.53 | 871 |
| weather | 0.78 | 0.63 | 0.57 | 0.36 | 0.46 | 0.50 | 1.33 | 6.09 | 2209 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 5.00 | 5.28 | 4.13 | 1.98 | 0.34 | 0.27 | 0.28 | 0.36 | 2209 |
| ecmwf | 4.16 | 3.21 | 4.27 | 4.41 | 3.48 | 1.01 | 2.31 | 11.54 | 2209 |
| gem | 3.86 | 4.12 | 4.27 | 5.62 | 5.61 | 5.42 | 5.42 | 5.93 | 1676 |
| hrrr | 3.63 | 2.83 | 2.52 | 1.41 | 1.97 | 2.31 | 2.17 | 2.50 | 2017 |
| metar | 6.82 | 7.57 | 6.01 | 3.44 | 0.24 | 0.27 | 0.28 | 0.28 | 2208 |
| metno | 4.02 | 4.26 | 4.72 | 4.72 | 3.12 | 3.79 | 4.86 | 13.41 | 2005 |
| nws_5min | — | — | — | 1.83 | 2.98 | 4.14 | 4.80 | 23.85 | 343 |
| nws_5min_diurnal | — | — | — | 3.25 | 4.78 | 4.18 | 2.23 | 1.51 | 599 |
| nws_point | — | — | 1.77 | 1.77 | 2.13 | 3.37 | 3.52 | 13.06 | 871 |
| weather | 4.12 | 3.34 | 3.01 | 1.91 | 2.46 | 2.80 | 2.67 | 12.18 | 2209 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.60 | 0.28 | 0.35 | 3.47 | 5.13 | — | — | 2284 |
| ecmwf | 10.89 | 1.98 | 3.17 | 4.25 | 3.79 | 6.01 | — | 2245 |
| gem | 5.89 | 5.42 | 5.56 | 4.94 | 3.97 | — | — | 1676 |
| hrrr | 2.49 | 2.09 | 2.15 | 2.15 | 3.37 | 3.46 | — | 2091 |
| metar | 0.28 | 0.28 | 0.81 | 5.19 | 7.17 | — | — | 2282 |
| metno | 12.74 | 4.54 | 3.41 | 4.72 | 4.07 | — | — | 2005 |
| nws_5min | 23.68 | 4.24 | 3.08 | 1.87 | — | — | — | 344 |
| nws_5min_diurnal | 1.50 | 3.02 | 4.34 | 3.11 | — | — | — | 673 |
| nws_point | 11.00 | 3.67 | 1.91 | 1.77 | 0.48 | 0.42 | — | 945 |
| weather | 11.51 | 2.57 | 2.64 | 2.62 | 3.79 | 4.03 | — | 2245 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| hrrr | weather | 7 | 0.999 |
| ecmwf | nws_5min | 5 | -0.991 |
| metno | nws_point | 6 | 0.898 |
| combined_v2 | gem | 6 | -0.880 |
| gem | nws_point | 6 | -0.825 |
| ecmwf | gem | 6 | 0.783 |
| hrrr | metar | 7 | -0.774 |
| gem | hrrr | 6 | -0.773 |
| gem | weather | 6 | -0.773 |
| combined_v2 | nws_5min | 5 | 0.768 |
| metar | weather | 7 | -0.753 |
| combined_v2 | ecmwf | 7 | -0.744 |
| ecmwf | weather | 7 | -0.695 |
| combined_v2 | nws_point | 6 | 0.673 |
| ecmwf | hrrr | 7 | -0.672 |
| ecmwf | nws_point | 6 | -0.607 |
| hrrr | nws_point | 6 | 0.599 |
| nws_point | weather | 6 | 0.599 |
| gem | metno | 6 | -0.545 |
| nws_5min | weather | 5 | 0.506 |
| hrrr | nws_5min | 5 | 0.475 |
| combined_v2 | metar | 7 | 0.403 |
| combined_v2 | metno | 6 | 0.401 |
| ecmwf | metno | 6 | -0.357 |
| hrrr | metno | 6 | 0.352 |
| metno | weather | 6 | 0.352 |
| metar | nws_5min | 5 | 0.202 |
| combined_v2 | weather | 7 | 0.192 |
| combined_v2 | hrrr | 7 | 0.165 |
| ecmwf | metar | 7 | 0.052 |
| gem | metar | 6 | 0.000 |
| metar | metno | 6 | 0.000 |
| metar | nws_point | 6 | 0.000 |

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
- `gem`: bias = -5.52°F
- `nws_5min_diurnal`: bias = -4.45°F
- `metno`: bias = -3.42°F
- `weather`: bias = -2.50°F
- `nws_5min`: bias = -2.33°F
- `hrrr`: bias = -1.97°F
- `ecmwf`: bias = -1.89°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 52.78
- `metno` LST 21-23: realized RMSE / claimed σ = 6.71
- `nws_point` LST 21-23: realized RMSE / claimed σ = 6.53
- `weather` LST 21-23: realized RMSE / claimed σ = 6.09
- `ecmwf` LST 21-23: realized RMSE / claimed σ = 5.77
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 3.90
- `metar` LST 03-05: realized RMSE / claimed σ = 3.79
- `metar` LST 00-02: realized RMSE / claimed σ = 3.41
- `metar` LST 06-08: realized RMSE / claimed σ = 3.01
- `combined_v2` LST 03-05: realized RMSE / claimed σ = 2.99
- `gem` LST 21-23: realized RMSE / claimed σ = 2.97
- `combined_v2` LST 00-02: realized RMSE / claimed σ = 2.84
- `gem` LST 18-20: realized RMSE / claimed σ = 2.71
- `nws_5min` LST 15-17: realized RMSE / claimed σ = 2.56
- `metno` LST 18-20: realized RMSE / claimed σ = 2.43
- `nws_5min_diurnal` LST 12-14: realized RMSE / claimed σ = 2.39
- `combined_v2` LST 06-08: realized RMSE / claimed σ = 2.34
- `nws_5min_diurnal` LST 15-17: realized RMSE / claimed σ = 2.09
- `nws_point` LST 18-20: realized RMSE / claimed σ = 1.76
- `metar` LST 09-11: realized RMSE / claimed σ = 1.72

**Highly-correlated source pairs (n_eff < n):**
- `hrrr` ↔ `weather`: corr = 0.999
- `metno` ↔ `nws_point`: corr = 0.898
- `ecmwf` ↔ `gem`: corr = 0.783
- `combined_v2` ↔ `nws_5min`: corr = 0.768

**Cross-bracket LST gate suggestion:** fire only when recording_lst_hour ≥ 15 AND day_offset == 0.

## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
