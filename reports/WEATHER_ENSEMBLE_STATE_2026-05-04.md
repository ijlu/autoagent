# Weather Ensemble State — 2026-05-04

**Status:** Production. Daemon active on 45.55.79.193, running `weather_ensemble_v2.predict_v2`. All weather MM still gated by `WEATHER_MM_LIVE=false`; cross-bracket KXHIGHNY canary was live overnight 2026-05-03→04 and is currently de-armed (kv `cross_bracket_live:KXHIGHNY` expired).

This doc is the single source of truth for "what does the weather ensemble look like right now." If you spin up a new session, start here.

## Quick context

The system makes daily-high-temperature predictions for 6 Kalshi market families (KXHIGH{NY, MIA, CHI, AUS, LAX, DEN}), each settling on a specific airport METAR (KNYC, KMIA, KMDW, KAUS, KLAX, KDEN). Predictions are projected onto threshold (`-T`) and bracket (`-B`) markets. The combine path is `bot.signals.weather_ensemble_v2._collect_gaussians` → `combine_gaussian` (precision-weighted) → MOS-bias-corrected μ + σ → bracket-projection.

## Active sources (GAUSSIAN_COMBINE_SOURCES)

Defined in `bot/signals/weather_sources.py`. Members:

| Source | Type | Notes |
|---|---|---|
| `hrrr` | NOAA NOMADS forecast | Per-city σ priors (see below). Best single forecast in pre-fix RMSE table. |
| `weather` | Open-Meteo (paid commercial endpoint) | Lat/lon driven |
| `nws_point` | NWS api.weather.gov hourly | Per-city excluded for NYC/CHI/MIA (cold-bias) |
| `metar` | NWS api.weather.gov / METAR observations | Real-time, σ=2.0 prior |
| `icon` | ICON via Open-Meteo | Probationary state machine |
| `ukmo` | UKMO via Open-Meteo | Probationary |
| `gem` | Canadian GEM via Open-Meteo | **Excluded for LAX** (+9°F bias from marine layer) |
| `metno` | MET Norway via Open-Meteo | **Excluded for LAX** (+10°F bias) |
| `ecmwf` | ECMWF IFS 0.25° via Open-Meteo | Active |
| `nws_5min` | NWS 5-min ASOS observations | **Excluded for NYC** (KLGA proxy was 5°F warm); also excluded for CHI (-7°F) and MIA (-4°F) |
| `nws_5min_diurnal` | NWS 5-min × METAR diurnal regression | **Excluded for NYC** (same KLGA issue) |
| `afd` | NWS Area Forecast Discussion | Bias-shift, not Gaussian |

## Demoted / archived

- **`tomorrow.io`** — dropped 2026-05-04. Last firing 2026-04-28; -5.49°F bias at KNYC. Source code retained in `bot/signals/sources/weather.py:get_tomorrow_forecast` for archeology. Re-enable requires fresh validation.
- **`madis`** — already deprecated 2026-04-29 per CLAUDE.md. Regression confirms broken (-9 to -25°F bias across cities). Not in `GAUSSIAN_COMBINE_SOURCES`; data still flows into `weather_gaussian_snapshots_backfill` for legacy MOS-bias keys but never enters live combine.
- **`nbm`** — deprecated 2026-04-29 (was Open-Meteo GFS proxy, not real NBM).
- **`nws_5min_analog`** — demoted to SHADOW 2026-05-02. Feature-vintage bug + insufficient regime coverage. Module retained.

## Per-city architecture (the postmortem fix)

**Why per-city:** the 2026-05-03 KXHIGHNY canary lost $1.45 because forecast lat/lons grid-resolved to *different cities* than the settlement station. NYC at (40.71, -74.01) → "Hoboken NJ"; LAX at (34.05, -118.24) → "Vernon CA inland" (no marine layer); Denver → "Glendale CO". Plus several sources had persistent station-specific biases (e.g., metno +9°F at KLAX) that the global MOS-bias clamp couldn't correct.

### 1. Settlement-aligned lat/lons (`bot/signals/sources/weather.py::WEATHER_CITIES`)

Tradeable-city coordinates now match `bot.daemon.stations.STATIONS`:

| City | Old (downtown) | New (settlement station) |
|---|---|---|
| NYC | 40.71, -74.01 | 40.78, -73.97 (KNYC Central Park) |
| Chicago | 41.88, -87.63 | 41.79, -87.75 (KMDW) |
| Miami | 25.76, -80.19 | 25.79, -80.29 (KMIA) |
| Austin | 30.27, -97.74 | 30.19, -97.67 (KAUS) |
| LAX | 34.05, -118.24 | 33.94, -118.41 (KLAX coast) |
| Denver | 39.74, -104.99 | 39.86, -104.67 (KDEN) |

**Pinning test:** `tests/signals/test_weather_cities_alignment.py` makes any drift > 0.05° (≈3.5 miles) a CI failure.

### 2. Per-city source exclusions (`weather_sources.EXCLUDED_SOURCES_BY_CITY`)

```python
{
    "nyc":          frozenset({"nws_point"}),
    "chicago":      frozenset({"nws_point", "nws_5min"}),
    "miami":        frozenset({"nws_point", "nws_5min"}),
    "los_angeles":  frozenset({"metno", "gem"}),
    # KAUS, KDEN: no exclusions (regression biases within ±3°F)
}
```

Sources are filtered at `_collect_gaussians` after each getter returns. The getter still runs (so snapshots are recorded for offline analysis), but the result is dropped from the live combine.

### 3. NYC special case — `nws_5min` and `nws_5min_diurnal`

Belt-and-suspenders: NYC is also explicitly absent from `bot/signals/sources/nws_5min.py::PRIMARY_5MIN_STATION_BY_CITY`, so the source short-circuits before any HTTP fetch for NYC tickers. Reason: KNYC has no 5-min publication, the original KLGA proxy ran 3-5°F warmer.

### 4. Per-city HRRR σ priors (`bot/signals/sources/hrrr.py::_HRRR_SIGMA_PRIOR_BY_CITY`)

```python
{
    "denver":  1.2,  # HRRR RMSE 2.47 at settlement → well-calibrated
    "miami":   1.2,  # HRRR RMSE 1.19 — best of any city
    "austin":  1.2,  # HRRR RMSE 1.27
    # default 2.0 for nyc, chicago, los_angeles
}
```

Postmortem rationale: bumping HRRR σ globally from 1.2→2.0 down-weighted HRRR uniformly. Cities where HRRR was already well-calibrated (DEN/MIA/AUS) regressed because mass shifted to less-accurate sources. Carve-out keeps these three at the original 1.2°F.

### 5. Per-family σ inflation factors (kv-cache, runtime override)

| Family | factor (base) | Notes |
|---|---|---|
| KXHIGHAUS | 4.0 | per-family Brier sweep optimum |
| KXHIGHCHI | 4.0 | |
| KXHIGHLAX | 4.0 | |
| KXHIGHMIA | 3.0 | |
| KXHIGHNY | 3.0 | |
| KXHIGHDEN | **1.0** | already well-calibrated; no inflation |

Stored in kv as `weather_sigma_inflation_<FAMILY>` (24h TTL). Resolution path in `_get_sigma_inflation`: per-family kv → global kv → env `WEATHER_SIGMA_INFLATION` → default 1.0. Clamped to [1.0, 4.0].

### 6. TTE-aware σ inflation decay (`weather_ensemble_v2._decay_factor_for_tte`)

Pre-peak the full per-family factor applies. Post-peak it decays toward 1.0:

```
TTE >= 8h:    factor = base
TTE in 2-8h:  factor = 1.0 + (base - 1.0) × (TTE - 2.0) / 6.0   (linear)
TTE <= 2h:    factor = 1.0
```

Postmortem rationale: at TTE 5.9h, KXHIGHNY's σ × 3.0 inflated combined σ to 3.44°F — wide enough that every 1°F bracket got only ~7-8% of the mass. With 0% probability across the bracket reality landed in. Wide σ post-peak is wrong because the answer is essentially observed; only pre-peak is forecast uncertainty real.

## Pipeline order of operations (v2)

In `_collect_gaussians`:
1. Each source's `get_<name>_gaussian()` is called
2. **Per-city exclusion check** — drop if `is_excluded_for_city(name, city_key)` (NEW 2026-05-04)
3. Defensive name normalization
4. `_apply_learned_sigma_with_flag` — kv-cache override of source's own σ prior
5. `_apply_staleness_inflation` — inflate σ for stale forecasts (NBM @ 6h cycle, etc.)
6. `_apply_mos_bias` — shift μ by per-(source, city, regime) bias from kv
7. σ ceiling for unfit sources only
8. State machine inflation (PROBATIONARY +30%)
9. State machine filter (drops shadow/demoted)

Then in `predict_v2`:
1. Pre-scale weights via group-correlation discount (METAR + nws_5min in same group)
2. `combine_gaussian` → precision-weighted combined Gaussian
3. AFD bias shift
4. **σ inflation with per-family factor + TTE decay** (UPDATED 2026-05-04)
5. Running-high floor (μ ≥ METAR's running max)
6. σ floor (`_COMBINED_SIGMA_FLOOR_F=0.5`)
7. Truncated bracket projection
8. NOAA alerts logit-space blend

## Cross-bracket strategy state

| | |
|---|---|
| Code path | `bot/daemon/cross_bracket_shadow.py` |
| Live env gate | `CROSS_BRACKET_LIVE=true` (currently set in `.env`) |
| Per-family kv gate | `cross_bracket_live:<FAMILY>` (true to enable) |
| TTE window | 3.0–7.0h pre-settle (env `CROSS_BRACKET_MIN_TTE_HOURS` / `_MAX_`) |
| Per-leg edge floor | 0.10 (env `CROSS_BRACKET_LIVE_MIN_EDGE`) |
| Daily exposure cap | 500¢ ($5/day) — `CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS` |
| Slippage protection | Layer 1: limit price ≤ best_ask + `CROSS_BRACKET_SLIP_TOLERANCE_CENTS` (default 2¢). Layer 2: count ≤ top-of-book size. (NEW 2026-05-04 — fixed `post_only=True` bug.) |

KXHIGHNY canary 2026-05-03→04: 18 fills, $1.45 paid, settled in losing bracket B59.5. **Loss as predicted by the strategy's risk model.** Real-money lifecycle (POST → fill → settle → P&L) verified working end-to-end except `fills_ledger` ingest of those 18 fills (Kalshi format change; fix in `fills_writer` deployed today; backfill of historical fills not yet done).

## Recent postmortem findings (2026-05-03 → 04)

1. **Truth corruption** (KAUS 2026-05-01 daily_high=92°F vs hourly_max=64°F across ~30 station-day pairs). CF6 fetcher was correct; the issue was `INSERT OR REPLACE` from running-max during incomplete IEM windows. Fixed via manual rebackfill. Hourly_backfill task continues to overwrite each day; no recurrence so far.
2. **MOS bias regime fitter producing 0 keys** because cells were 1-3 rows each vs `_MIN_FIT_N=15`. Pooled MOS bias still works (n=12-210 per cell).
3. **Diurnal fitter never wired into daemon** (was CLI-tool only). Fixed; now refreshes daily as Stage 3 of `_run_hourly_backfill`.
4. **Calibration globally disabled** (`CALIBRATION_ENABLED=false`) since 2026-04-27. Per-family Platt fits exist in `kv_cache::calibration_curve_v2` but blocked by global gate. Re-enabling needs per-family validation that fits don't degenerate to step functions.
5. **Sigma-inflation infrastructure already existed** but at default factor=1.0 (no-op); empirical sweep showed 17% Brier improvement from σ × 2.0 pooled and 19% per-family.
6. **Cross-bracket POSTs failed for ~30 min** with `post_only cross` because cross-bracket FVs are by design on the wrong side of the spread. Fixed via `post_only=False` + slippage layers.
7. **fills_writer dropped 18 live fills** because Kalshi's response shape switched to dollar-string format (`count_fp`, `yes_price_dollars`). Fixed today; both formats parsed.

## Known issues / backlog

| # | Item | Priority | Why deferred |
|---|---|---|---|
| 1 | Per-city σ priors for non-HRRR sources | medium | Need 24-48h fresh post-lat/lon-fix data |
| 2 | Per-city σ inflation factors (vs per-family) | low | Marginal vs current; per-family captures most of the variance |
| 3 | Combined-level per-city MOS bias residual correction | medium | Need fresh data |
| 4 | Re-enable Platt calibration with per-family fits | high | Per-family code exists; needs fit-quality gate to avoid step-function pathology |
| 5 | Backfill yesterday's 18 cross-bracket fills via `/portfolio/fills?since_unix=...` through fixed writer | low | One-off ingestion; doesn't gate live trading |
| 6 | DEN backtest regression — real or artifact of incomplete re-derivation? | medium | Need fresh data + proper full-pipeline replay |
| 7 | Historical re-fetch with new lat/lons → proper before/after backtest | low | ~2h work; do once we want a definitive "this is how much better" number |
| 8 | LAX combined_v2 still has +2.77°F bias even with metno/gem dropped — need a per-city MOS bias post-combine | medium | Same as #3 |
| 9 | NYC nws_5min KLGA→KNYC bias correction (so we can re-enable nws_5min for NYC) | low | Currently fully excluded |
| 10 | Daemon's slow shutdown caused 9h outage 2026-05-03 (SIGTERM timeout → SIGKILL → systemd start-rate-limiter) | medium | Diagnose what's blocking on shutdown |

## How to verify behavior

### Daemon health

```bash
ssh root@45.55.79.193 'systemctl is-active kalshi-daemon.service'
ssh root@45.55.79.193 'tail -50 /home/kalshi/autoagent/daemon.log | grep -E "raised|Traceback"'
```

### Per-source σ for a specific market

```bash
ssh root@45.55.79.193 'tail -500 /home/kalshi/autoagent/daemon.log | grep -E "weather_ensemble_v2.*KXHIGHNY"'
```

### Source mapping verification

```bash
scp /tmp/verify_all_station_mappings.py root@45.55.79.193:/home/kalshi/autoagent/
ssh root@45.55.79.193 'cd /home/kalshi/autoagent && python3 verify_all_station_mappings.py'
```

(Or check this in repo — should be moved to `tools/` next cycle.)

### Bias-at-settlement audit

For each (source, city) pair, compute mean (forecast_high − truth_daily_high) on the latest hours_out window. Flagged pairs with |bias| > 2°F = likely station mismatch or per-source data issue.

### Backtest

`/tmp/full_backtest_v2.py` (should be moved to `tools/`) re-derives combined_v2 under the current pipeline and compares Brier to the OLD combined_v2 stored in snapshots. Note: it does NOT replay learned-σ overrides, MOS bias correction, or AFD shift, so it underestimates true production Brier.

## Recently committed (last 2 commits)

```
6a39dd7 fills_writer: parse Kalshi's dollar-string format alongside legacy cents-int
8d043a8 Weather ensemble postmortem: per-city architecture + station-mapping fixes
```

## Test count / coverage signals

`pytest tests/ -q --ignore=tests/test_cache_bounded.py` → 2,155 passing, 2 skipped. The 2 skipped are local-architecture-incompatible (cffi binary mismatch on dev machine).

Pinning tests to know about:
- `tests/signals/test_weather_cities_alignment.py` — settlement coordinate drift catcher
- `tests/signals/test_weather_sources_registry.py` — per-city exclusion registry pin
- `tests/signals/test_weather_ensemble_v2_sources.py` — per-city collector tests
- `tests/signals/test_weather_ensemble_v2.py::test_decay_factor_*` — TTE decay math
- `tests/signals/test_source_gaussians.py::test_per_city_sigma_priors_*` — HRRR per-city
- `tests/daemon/test_fills_writer.py::TestDualFormatPayloads` — Kalshi format dual-parsing
- `tests/test_cross_bracket_live_gates.py::test_post_live_order_post_only_is_false` — yesterday's bug pin
