# Per-source ensemble scorecard — MIAMI (KMIA)

**Series:** `KXHIGHMIA`  
**LST offset:** -5h  
**Generated:** 2026-05-05T18:53:08Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 18,805 snapshots, 2 settled days, 11 sources.
- **Empirical peak hour:** LST 12; running-high locks (≥80% days) by LST -1.
- **Biggest peak-window bias offenders:** combined_v2 (+3.0°F), metar (+2.5°F), metno (+2.1°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 4.94 | 4.74 | 3.21 | 2.86 | 2.31 | 3.62 | 0.61 | -0.14 | 2337 |
| ecmwf | — | -0.94 | -0.55 | 0.86 | 0.86 | -0.74 | -0.45 | -0.58 | 1905 |
| gem | -0.16 | -1.11 | -0.95 | -0.42 | 0.23 | 0.25 | 2.27 | 1.63 | 1842 |
| hrrr | 6.85 | 3.83 | 4.99 | 2.41 | 0.82 | 0.87 | 0.39 | 0.58 | 2006 |
| icon | 3.81 | 2.46 | 0.71 | 0.29 | -1.59 | -1.59 | -1.44 | 0.46 | 2336 |
| metar | 4.30 | 3.56 | 1.68 | 1.43 | 1.48 | 3.62 | 0.55 | -0.28 | 2337 |
| metno | 2.64 | 2.28 | 2.73 | 2.98 | 2.73 | 1.47 | 1.81 | 2.10 | 2336 |
| nws_5min | — | — | — | — | — | — | -4.44 | — | 96 |
| nws_5min_diurnal | — | — | — | 1.92 | 0.86 | 3.12 | 2.69 | 1.68 | 1129 |
| nws_point | — | — | — | — | — | — | 0.11 | 0.11 | 258 |
| weather | 6.90 | 3.82 | 4.97 | 2.39 | 0.80 | 0.85 | 0.43 | 2.65 | 2223 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.97 | 0.92 | 0.63 | 0.57 | 0.46 | 0.80 | 1.44 | 0.46 | 2337 |
| ecmwf | — | 0.32 | 0.32 | 0.30 | 0.30 | 0.32 | 0.37 | 0.61 | 1905 |
| gem | 0.03 | 0.25 | 0.21 | 0.16 | 0.07 | 0.06 | 1.56 | 1.69 | 1842 |
| hrrr | 2.04 | 1.34 | 1.44 | 0.78 | 0.24 | 0.24 | 0.48 | 0.48 | 2006 |
| icon | 0.89 | 0.65 | 0.17 | 0.22 | 0.37 | 0.37 | 0.56 | 0.81 | 2336 |
| metar | 2.16 | 1.80 | 0.91 | 0.81 | 0.78 | 1.87 | 3.70 | 1.61 | 2337 |
| metno | 0.59 | 0.50 | 0.60 | 0.65 | 0.60 | 0.32 | 0.92 | 1.18 | 2336 |
| nws_5min | — | — | — | — | — | — | 3.61 | — | 96 |
| nws_5min_diurnal | — | — | — | 1.01 | 0.80 | 1.59 | 1.47 | 0.85 | 1129 |
| nws_point | — | — | — | — | — | — | 0.06 | 0.06 | 258 |
| weather | 2.05 | 1.33 | 1.42 | 0.77 | 0.23 | 0.23 | 0.31 | 1.87 | 2223 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 5.03 | 4.78 | 3.26 | 2.92 | 2.38 | 3.74 | 2.11 | 0.48 | 2337 |
| ecmwf | — | 0.94 | 0.92 | 0.86 | 0.86 | 0.93 | 0.73 | 1.23 | 1905 |
| gem | 0.16 | 1.11 | 0.95 | 0.73 | 0.33 | 0.25 | 3.13 | 3.38 | 1842 |
| hrrr | 7.05 | 4.64 | 5.00 | 2.72 | 0.84 | 0.87 | 0.64 | 0.75 | 2006 |
| icon | 3.83 | 2.78 | 0.71 | 0.93 | 1.59 | 1.59 | 1.45 | 2.09 | 2336 |
| metar | 4.32 | 3.61 | 1.82 | 1.62 | 1.56 | 3.74 | 2.11 | 0.60 | 2337 |
| metno | 2.70 | 2.28 | 2.75 | 2.98 | 2.79 | 1.48 | 1.84 | 2.36 | 2336 |
| nws_5min | — | — | — | — | — | — | 4.52 | — | 96 |
| nws_5min_diurnal | — | — | — | 2.03 | 1.59 | 3.19 | 2.95 | 1.71 | 1129 |
| nws_point | — | — | — | — | — | — | 0.11 | 0.11 | 258 |
| weather | 7.11 | 4.62 | 4.98 | 2.70 | 0.81 | 0.85 | 0.62 | 3.74 | 2223 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.48 | 3.03 | 2.53 | 3.22 | 4.95 | — | — | 2337 |
| ecmwf | 1.19 | 0.79 | 0.88 | 0.90 | 0.94 | — | — | 1905 |
| gem | 3.40 | 2.56 | 0.30 | 0.89 | 1.00 | — | — | 1842 |
| hrrr | 0.72 | 0.72 | 1.03 | 4.30 | 6.01 | — | — | 2006 |
| icon | 2.03 | 1.50 | 1.58 | 0.71 | 3.44 | — | — | 2336 |
| metar | 0.60 | 3.03 | 1.90 | 1.84 | 4.04 | — | — | 2337 |
| metno | 2.32 | 1.73 | 2.59 | 2.82 | 2.51 | — | — | 2336 |
| nws_5min | — | 4.52 | — | — | — | — | — | 96 |
| nws_5min_diurnal | 1.67 | 3.33 | 1.91 | 2.37 | — | — | — | 1129 |
| nws_point | 0.11 | 0.11 | — | — | — | — | — | 258 |
| weather | 3.51 | 0.70 | 1.01 | 4.28 | 6.04 | — | — | 2223 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| hrrr | weather | 6 | 1.000 |
| ecmwf | nws_5min_diurnal | 6 | -0.954 |
| metar | nws_5min_diurnal | 6 | 0.947 |
| combined_v2 | metar | 6 | 0.930 |
| ecmwf | metar | 6 | -0.873 |
| combined_v2 | nws_5min_diurnal | 6 | 0.822 |
| combined_v2 | ecmwf | 6 | -0.782 |
| metno | nws_5min_diurnal | 6 | -0.740 |
| ecmwf | metno | 6 | 0.672 |
| metar | metno | 6 | -0.660 |
| gem | nws_5min_diurnal | 6 | 0.651 |
| gem | metno | 6 | -0.632 |
| gem | metar | 6 | 0.589 |
| gem | hrrr | 6 | -0.524 |
| gem | weather | 6 | -0.524 |
| ecmwf | gem | 6 | -0.421 |
| combined_v2 | gem | 6 | 0.375 |
| combined_v2 | metno | 6 | -0.343 |
| hrrr | metno | 6 | -0.328 |
| metno | weather | 6 | -0.328 |
| ecmwf | hrrr | 6 | -0.218 |
| ecmwf | weather | 6 | -0.218 |
| combined_v2 | hrrr | 6 | -0.081 |
| combined_v2 | weather | 6 | -0.081 |
| hrrr | nws_5min_diurnal | 6 | 0.015 |
| nws_5min_diurnal | weather | 6 | 0.015 |
| hrrr | metar | 6 | 0.006 |
| metar | weather | 6 | 0.006 |
| combined_v2 | icon | 6 | 0.000 |
| ecmwf | icon | 6 | 0.000 |
| gem | icon | 6 | 0.000 |
| hrrr | icon | 6 | 0.000 |
| icon | metar | 6 | 0.000 |
| icon | metno | 6 | 0.000 |
| icon | nws_5min_diurnal | 6 | 0.000 |
| icon | weather | 6 | 0.000 |

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
- `combined_v2`: bias = +2.96°F
- `metar`: bias = +2.55°F
- `metno`: bias = +2.10°F
- `nws_5min_diurnal`: bias = +1.99°F
- `icon`: bias = -1.59°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `metar` LST 18-20: realized RMSE / claimed σ = 3.70
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 3.61
- `metar` LST 00-02: realized RMSE / claimed σ = 2.16
- `weather` LST 00-02: realized RMSE / claimed σ = 2.05
- `hrrr` LST 00-02: realized RMSE / claimed σ = 2.04
- `metar` LST 15-17: realized RMSE / claimed σ = 1.87
- `weather` LST 21-23: realized RMSE / claimed σ = 1.87
- `metar` LST 03-05: realized RMSE / claimed σ = 1.80
- `gem` LST 21-23: realized RMSE / claimed σ = 1.69
- `metar` LST 21-23: realized RMSE / claimed σ = 1.61
- `nws_5min_diurnal` LST 15-17: realized RMSE / claimed σ = 1.59
- `gem` LST 18-20: realized RMSE / claimed σ = 1.56

**Highly-correlated source pairs (n_eff < n):**
- `hrrr` ↔ `weather`: corr = 1.000
- `metar` ↔ `nws_5min_diurnal`: corr = 0.947
- `combined_v2` ↔ `metar`: corr = 0.930
- `combined_v2` ↔ `nws_5min_diurnal`: corr = 0.822


## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
