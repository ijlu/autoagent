# Per-source ensemble scorecard — NYC (KNYC)

**Series:** `KXHIGHNY`  
**LST offset:** -5h  
**Generated:** 2026-05-05T18:51:47Z  
**DB:** `scratch/weather_analysis.db`

## 1. TL;DR

- **Sample:** 16,052 snapshots, 2 settled days, 10 sources.
- **Empirical peak hour:** LST 13; running-high locks (≥80% days) by LST -1.
- **Biggest peak-window bias offenders:** gem (-1.6°F), weather (+1.4°F), hrrr (+1.3°F).

## 2. Per-source bias (forecast − observed) by LST hour, same-day

### 2a. Same-day (day_offset = 0)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | -2.77 | -2.17 | -2.71 | 1.28 | 1.14 | -0.02 | 1.28 | 0.30 | 2319 |
| ecmwf | 0.62 | 0.78 | 0.97 | 0.73 | 0.73 | 0.06 | -0.25 | 6.56 | 2259 |
| gem | 0.21 | -1.62 | -2.01 | -1.98 | -1.61 | -1.53 | -0.81 | 6.25 | 2208 |
| hrrr | 0.18 | 0.83 | 1.19 | 1.00 | 1.07 | 1.54 | 2.40 | 2.31 | 1994 |
| metar | -5.75 | -5.52 | -6.17 | -1.06 | -0.56 | -0.04 | 1.15 | -0.12 | 2316 |
| metno | 0.16 | 0.11 | -0.48 | -0.79 | -0.72 | -0.59 | 1.19 | 7.90 | 2188 |
| nws_5min | — | — | — | — | — | — | 4.78 | 3.79 | 209 |
| nws_5min_diurnal | — | — | — | — | — | — | 9.38 | — | 30 |
| nws_point | — | — | — | — | — | — | 6.05 | 5.14 | 210 |
| weather | 0.48 | 0.92 | 1.28 | 1.09 | 1.16 | 1.64 | 2.65 | 7.68 | 2319 |


### 2b. Day-before (day_offset = 1)

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|

## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day

_Ratio > 1 means model's σ is too tight (under-calibrated)._

### 3a. Realized RMSE / claimed σ — same-day

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.54 | 0.42 | 0.56 | 0.29 | 0.29 | 0.06 | 2.24 | 0.58 | 2319 |
| ecmwf | 0.43 | 0.34 | 0.34 | 0.25 | 0.25 | 0.10 | 0.46 | 4.65 | 2259 |
| gem | 0.42 | 0.37 | 0.45 | 0.44 | 0.36 | 0.34 | 0.56 | 4.74 | 2208 |
| hrrr | 0.04 | 0.14 | 0.20 | 0.17 | 0.18 | 0.26 | 1.43 | 1.25 | 1994 |
| metar | 2.90 | 2.78 | 3.19 | 0.77 | 0.36 | 0.18 | 5.66 | 0.47 | 2316 |
| metno | 0.09 | 0.02 | 0.14 | 0.17 | 0.16 | 0.13 | 0.69 | 5.29 | 2188 |
| nws_5min | — | — | — | — | — | — | 4.00 | 3.92 | 209 |
| nws_5min_diurnal | — | — | — | — | — | — | 4.69 | — | 30 |
| nws_point | — | — | — | — | — | — | 3.04 | 2.58 | 210 |
| weather | 0.09 | 0.16 | 0.21 | 0.18 | 0.19 | 0.27 | 1.33 | 4.68 | 2319 |


### 3b. Realized RMSE (°F) — same-day, for context

| source | 00-02 | 03-05 | 06-08 | 09-11 | 12-14 | 15-17 | 18-20 | 21-23 | n_total |
|---|---|---|---|---|---|---|---|---|---|
| combined_v2 | 2.88 | 2.26 | 2.99 | 1.55 | 1.53 | 0.22 | 3.22 | 0.60 | 2319 |
| ecmwf | 1.25 | 0.99 | 0.98 | 0.73 | 0.73 | 0.30 | 0.92 | 9.31 | 2259 |
| gem | 1.89 | 1.65 | 2.01 | 1.98 | 1.61 | 1.53 | 1.12 | 9.49 | 2208 |
| hrrr | 0.23 | 0.84 | 1.19 | 1.01 | 1.07 | 1.60 | 2.41 | 2.33 | 1994 |
| metar | 5.81 | 5.57 | 6.38 | 1.53 | 0.71 | 0.24 | 3.21 | 0.17 | 2316 |
| metno | 0.41 | 0.11 | 0.66 | 0.79 | 0.73 | 0.59 | 1.37 | 10.59 | 2188 |
| nws_5min | — | — | — | — | — | — | 4.86 | 3.79 | 209 |
| nws_5min_diurnal | — | — | — | — | — | — | 9.38 | — | 30 |
| nws_point | — | — | — | — | — | — | 6.07 | 5.16 | 210 |
| weather | 0.55 | 0.94 | 1.29 | 1.11 | 1.16 | 1.69 | 2.66 | 9.36 | 2319 |


### 4. Per-source RMSE by lead time (hours_out)

| source | 0-3 | 4-7 | 8-12 | 13-18 | 19-24 | 25-36 | 37+ | n_total |
|---|---|---|---|---|---|---|---|---|
| combined_v2 | 0.57 | 3.18 | 1.52 | 2.40 | 2.55 | — | — | 2319 |
| ecmwf | 8.82 | 0.74 | 0.68 | 0.89 | 1.12 | — | — | 2259 |
| gem | 8.99 | 1.28 | 1.65 | 2.01 | 1.72 | — | — | 2208 |
| hrrr | 2.33 | 2.31 | 1.10 | 1.10 | 0.58 | — | — | 1994 |
| metar | 0.17 | 3.18 | 0.68 | 4.96 | 5.62 | — | — | 2316 |
| metno | 10.03 | 1.18 | 0.71 | 0.69 | 0.25 | — | — | 2188 |
| nws_5min | 4.31 | 4.72 | — | — | — | — | — | 209 |
| nws_5min_diurnal | — | 9.38 | — | — | — | — | — | 30 |
| nws_point | 5.28 | 6.40 | — | — | — | — | — | 210 |
| weather | 8.90 | 2.51 | 1.20 | 1.20 | 0.74 | — | — | 2319 |


### 5. Within-group residual correlation (peak_window, same-day)

| source A | source B | n | corr |
|---|---|---|---|
| hrrr | weather | 6 | 1.000 |
| gem | weather | 6 | 0.960 |
| gem | hrrr | 6 | 0.960 |
| combined_v2 | metno | 6 | -0.937 |
| ecmwf | weather | 6 | -0.865 |
| ecmwf | hrrr | 6 | -0.865 |
| ecmwf | gem | 6 | -0.725 |
| metar | metno | 6 | 0.683 |
| ecmwf | metar | 6 | -0.657 |
| ecmwf | metno | 6 | -0.644 |
| metno | weather | 6 | 0.596 |
| hrrr | metno | 6 | 0.596 |
| combined_v2 | hrrr | 6 | -0.547 |
| combined_v2 | weather | 6 | -0.547 |
| combined_v2 | ecmwf | 6 | 0.544 |
| metar | weather | 6 | 0.494 |
| hrrr | metar | 6 | 0.494 |
| combined_v2 | metar | 6 | -0.389 |
| gem | metno | 6 | 0.387 |
| combined_v2 | gem | 6 | -0.365 |
| gem | metar | 6 | 0.314 |

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
- `gem`: bias = -1.57°F

**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**
- `metar` LST 18-20: realized RMSE / claimed σ = 5.66
- `metno` LST 21-23: realized RMSE / claimed σ = 5.29
- `gem` LST 21-23: realized RMSE / claimed σ = 4.74
- `nws_5min_diurnal` LST 18-20: realized RMSE / claimed σ = 4.69
- `weather` LST 21-23: realized RMSE / claimed σ = 4.68
- `ecmwf` LST 21-23: realized RMSE / claimed σ = 4.65
- `nws_5min` LST 18-20: realized RMSE / claimed σ = 4.00
- `nws_5min` LST 21-23: realized RMSE / claimed σ = 3.92
- `metar` LST 06-08: realized RMSE / claimed σ = 3.19
- `nws_point` LST 18-20: realized RMSE / claimed σ = 3.04
- `metar` LST 00-02: realized RMSE / claimed σ = 2.90
- `metar` LST 03-05: realized RMSE / claimed σ = 2.78
- `nws_point` LST 21-23: realized RMSE / claimed σ = 2.58
- `combined_v2` LST 18-20: realized RMSE / claimed σ = 2.24

**Highly-correlated source pairs (n_eff < n):**
- `hrrr` ↔ `weather`: corr = 1.000
- `gem` ↔ `weather`: corr = 0.960
- `gem` ↔ `hrrr`: corr = 0.960


## 9. Backtest comparison (Phase 4)

_Filled in after Phase 3 redesign + Phase 4 backtest._
