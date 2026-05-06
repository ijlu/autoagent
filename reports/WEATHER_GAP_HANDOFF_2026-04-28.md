# Weather Brier gap — research handoff

**Date:** 2026-04-28
**Status of the work:** Phase 1 (infrastructure + ground-truth fix) shipped. Two
research-grade follow-ups remain open. This doc is everything a new session
needs to pick up where this one ended.

## 0. TL;DR

The original 0.04 Brier gap (v2 ensemble vs market mid on weather brackets)
chased down to a **wrong-source-of-truth bug**: Kalshi settles all 6 weather
series on the NWS Climatological Daily Report (CF6 TMAX), not raw METAR `tmpf`
max. Our backfill table — and every calibration fit downstream — was trained on
`tmpf` max, which systematically undershoots TMAX by 1–3°F because ASOS captures
peaks *between* hourly observations.

That structural bug is fixed (see commit `8eac0cf`). After deploying:

| | v2 Brier | Market | Gap |
|---|---|---|---|
| Pre-fix (corrected diagnostic, n=143) | 0.15 | 0.09 | −0.064 |
| Post-fix (n=143) | 0.14 | 0.09 | −0.049 |

**Closing ~25% of the remaining gap requires research** into:

1. **Regime-conditional residual peak σ** — the per-(station, lst_hour) σ we
   fit from the hourly backfill is unconditional. Real residual σ varies by
   weather regime (sea breeze, cloud cover, synoptic pattern). Conditioning
   would shrink uncertainty on clear-trajectory days and widen it on volatile
   ones.

2. **Forecast-side cold bias on close-edge cases** — even with correct ground
   truth, the model forecasts (HRRR, NBM, etc.) still run ~0.5°F cold on
   average for some (city, regime) cells. MOS bias should absorb this as more
   CF6-corrected days accumulate, but the bracket-edge behavior is sensitive
   to this last fraction of a degree.

Where the gap concentrates (per-bucket from `tools/diagnose_v2_gap.py`):

```
hours-to-settle:   0-6h gap +0.31  ← the disasters live here
                  6-12h gap −0.13  ← v2 actually wins
                 12-24h gap −0.02
bracket distance:    out (-2 to -0.5°F) gap +0.12
                    edge (-0.5 to 0.5°F) gap +0.15  ← close calls
                  deep_in/out gaps small
```

So the research target is: **how do we predict the residual peak in the final
0-6h, particularly when our μ is within ±0.5°F of a bracket boundary?**

---

## 1. What's deployed and where it lives

### Daemon + scheduler (live on VPS at 45.55.79.193)

`bot/daemon/main.py` registers 15 scheduled tasks. The two new ones from this
session:

* **`hourly_backfill`** (interval 86400s, fires 06:00 UTC daily) — pulls 7-day
  rolling window of IEM ASOS hourly METAR per station, then pulls CF6 monthly
  product for current + previous calendar month and overwrites `daily_high_f`
  with the official TMAX. Idempotent INSERT OR REPLACE keeps overlap free.
  See the `_run_hourly_backfill` body and `HOURLY_BACKFILL_INTERVAL_S` comment
  block for the full rationale.

* **forecast_cache prime** — synchronous `forecast_cache.refresh()` inside the
  `start_pollers()` callback, before any METAR poller fires. Eliminated the
  ~6-per-restart `[wx-handler] no forecast for KMIA; fallback X°F` warnings.

### Ensemble (`bot/signals/weather_ensemble_v2.py`)

Two changes from this session:

* **H3 conditional truncation** at step 5 of `predict_v2`. Only truncate at
  METAR.μ when `combined.μ < METAR.μ - 0.5`. Pre-CF6 this fix was net-harmful
  (−0.0046); post-CF6 it became net-helpful (+0.0146 pooled, validated by
  the sweep at n=143).

* **`_COMBINED_SIGMA_FLOOR_F = 1.0`** (was 0.5). Post-CF6 sweep optimum.
  Pinned by `test_combined_sigma_floor_is_one`.

### Backfill code (`tools/backfill_weather_effective_n.py`)

Three new functions:

* `_parse_cf6_daily_max(body)` — pure text parser, robust against the
  `===`/blank-line/SUMMARY interleaving in real CF6 product bodies.
* `fetch_cf6_tmax(station_icao, year, month)` — IEM AFOS retrieve endpoint,
  one HTTP call per (station, month).
* `update_daily_high_from_cf6(conn, station, year, month)` — UPDATEs
  `weather_metar_hourly_backfill.daily_high_f` for that (station, month).

`_CF6_PIL_BY_STATION` maps every primary station to its CF6 PIL. A drift-guard
test ensures every `STATION_BY_SERIES` entry starting with `KXHIGH` has a CF6
PIL.

### Replay tool fix (`tools/backtest_v2_replay.py`)

The pre-existing replay tool had a bug: its monkey-patch of
`v2._collect_gaussians` bypassed `_apply_learned_sigma`,
`_apply_staleness_inflation`, `_apply_mos_bias`, and the `_SOURCE_SIGMA_CEILING_F`
cap. So every prior sweep result was measuring **raw-σ ensemble**, not what
production actually does.

The fix applies those corrections explicitly inside `_replay_predict_v2`
before calling predict_v2. Conclusions about which knobs help/hurt remain
relative-correct (toggled vs baseline both bypassed); absolute numbers
shifted under the fix.

**If you write new investigation tools, use `_replay_predict_v2` from this
file — it now reflects production calibration.**

---

## 2. Diagnostic tools available (commit `d1f7fd1`)

Six tools in `tools/`, all runnable as `python -m tools.<name>` on the VPS:

| Tool | What it does | When to reach for it |
|---|---|---|
| `sweep_v2_hypotheses.py` | Single-pass sweep of σ floor / AFD / truncation / ρ knobs across n≈143 settled tickers. Pooled + per-family Brier table. | Hypothesis testing — toggle one v2 internal at a time and measure pooled effect. |
| `diagnose_v2_gap.py` | Three reports on one replay: by horizon-bucket, by μ-bracket-edge-distance, Miami drill (worst-20 per case). Captures combined.μ + σ via redirected snapshot writes. | Localizing where in the population the gap lives. |
| `investigate_miami_late_day.py` | Per-ticker drill on 4 specific catastrophic Miami cases — METAR snapshot freshness, shadow-row activity by horizon, hourly METAR trajectory. | Understanding individual case failures. |
| `probe_kmia_data.py` | Direct IEM ASOS query for KMIA + KFLL/KMFL/KOPF, cross-check against our backfill + Kalshi settlement. | Settling "is the data source we use right?" type questions. |
| `audit_kalshi_settlement_sources.py` | Public Kalshi markets API → fetches `rules_primary` text per series, prints alongside our STATION_BY_SERIES assumption. | Verifying station/source assumptions match Kalshi's contract. |
| `validate_cf6_hypothesis.py` | Fetches CF6MIA from IEM AFOS, parses TMAX, compares to settled bracket per case. | Validating the CF6 ground-truth hypothesis. |

Plus the existing diagnostics:

| Tool | What it does |
|---|---|
| `audit_source_residuals.py` | Per-(source, city, bucket) residuals vs current kv fit. |
| `audit_source_accuracy_by_horizon.py` | Per-source accuracy stratified by hours_out. |
| `audit_afd_stratified.py` | AFD signal quality slices (city, confidence, agreement). |
| `diagnose_methodology.py` | Per-source vs combined Brier + projection sanity. |
| `diagnose_nowcast.py` | Multi-horizon nowcast Brier (h ∈ {12, 8, 4}). |

---

## 3. The two open research items, in detail

### Item 1: Regime-conditional residual peak σ

**The current model:** `fit_and_persist_metar_residual_sigma` in
`bot/learning/weather_mos_materializer.py` fits one σ per (station, LST hour)
cell from `weather_metar_hourly_backfill`. The σ measures
`std(eventual_daily_high − running_max_at_hour_h)` across the backfill.

**Why this is incomplete:** the same (station, hour) cell sees very different
residual peaks under different weather regimes. Miami at 4pm:
- Sea-breeze day: temperature dropping, residual peak ≈ 0
- Continental flow day: temperature still climbing, residual peak ≈ 2-3°F

Pooled σ averages over these and over-states uncertainty on sea-breeze days
(causing close-call Brier losses) while under-stating it on continental days.

**Where to start:**

1. **Get regime features into the data.** Cloud cover, wind direction, dewpoint
   trajectory, synoptic-pattern proxy. IEM has these in the same ASOS query —
   our existing `fetch_metar_hourly` only pulls `tmpf`, but IEM supports
   `data: "tmpf,sknt,drct,sky_cover_l1,..."` etc. Schema would need to grow.

2. **Pick a stratification.** Options ranked by complexity:
   - Wind-direction regime per city (onshore vs offshore — captures Miami sea
     breeze, NY/LAX maritime effects)
   - Dewpoint trajectory (rising vs falling)
   - Cloud cover bucketed
   - Full synoptic regime classification (multi-feature; needs labeled data)

3. **Fit and validate.** Add a `(station, lst_hour, regime)` index on the
   residual σ kv, route lookups through it. Re-run `diagnose_v2_gap` to see
   if the close-edge bucket gap closes.

**What's already known:**

- The empirical residual σ at (KMIA, hour 14) is ~1.2°F pooled. By eye on the
  CF6 + hourly data, sea-breeze days have residual ~0.3°F and continental days
  ~2.5°F. Conditioning could halve the σ on most days (sea breeze is dominant
  in Miami in spring/summer) and widen it on the few catastrophic-loss days.
- The CHI/LAX families that *lost* slightly under H3 (+0.001 / +0.006) likely
  have regime-conditioning opportunities too. CHI has lake-effect cooling;
  LAX has marine layer.

**Risks:**

- Overfitting on small-n cells. The (station, hour, regime) split will be
  thin (3-5 samples per cell at 30 days backfill). Need a hierarchical fit
  that backs off to (station, hour) pooled when the cell is thin.
- Regime detection at predict time. We'd need *current* wind direction /
  cloud cover / etc. to look up the right cell. METAR poller already pulls
  this; just needs surfacing into the prediction path.

**Files to touch:**

- `tools/backfill_weather_effective_n.py:fetch_metar_hourly` — extend ASOS
  data column list, add new columns to backfill schema (or sibling table)
- `bot/learning/weather_mos_materializer.py:fit_and_persist_metar_residual_sigma`
  — accept regime as a stratification dimension, write per-regime kv keys
- `bot/signals/sources/metar_observations.py:_get_learned_residual_sigma`
  — accept current regime as an input, look up the right key
- `bot/daemon/metar_poller.py` — emit regime features alongside temp readings

### Item 2: Forecast-side cold bias on close-edge cases

**The observation:** even after CF6 ground truth + refit, the close-edge
bucket (μ within ±0.5°F of bracket boundary) shows a +0.15 gap vs market.
Per-case inspection (Miami 4/22 B81.5 won, μ=78.7, σ=1.4) shows the
forecast itself is 1-2°F too cold relative to the actual high — not the
calibration values, the underlying model μ.

**Where this comes from:** the forecast Gaussians from HRRR/NBM/Open-Meteo
are computed by their respective sources. Each runs through
`_apply_mos_bias` in v2's collect path — which subtracts a learned
`(forecast - observed)` bias per (source, city). After CF6 refit, those
biases reflect 30+ days of CF6-correct data, but the EWMA weight is
half-life=14d so they take ~14 days to fully shift to the new ground-truth
regime.

**Why it might just resolve itself:** as more days pass with CF6-corrected
ground truth flowing into the MOS bias EWMA, the forecast-side cold bias
should be absorbed. Expected timeline: 10-21 days.

**How to verify it's resolving (without a code change):**

1. Run `diagnose_v2_gap.py` weekly. Watch the close-edge bucket gap.
2. Spot-check `kv_cache` MOS bias keys for (source, city). The values
   should drift more negative (= subtract more from forecast μ) over time.
   Query: `SELECT key, value FROM kv_cache WHERE key LIKE 'weather_mos_bias_%' ORDER BY key`.
3. Run the sweep monthly. The H3 win should grow as μ accuracy improves.

**Where to push if it doesn't self-resolve:**

1. **Shorter EWMA half-life.** `_MOS_BIAS_HALF_LIFE_DAYS` in
   `weather_mos_materializer.py` — bumping from 14 → 7 days would let the fits
   shift faster but risks overfitting to recent weather.

2. **Per-bracket-distance bias correction.** Independent fix from MOS bias.
   The "close-edge" cases are specifically where μ is within σ of a bracket
   boundary. We could fit a `bracket-edge bias` correction that nudges μ a
   small amount in the direction of the historical bias on close calls.
   Risky — changes prediction conditional on the question being asked.

3. **Source-specific MOS** (instead of source+city). HRRR might be cold at
   Miami but not at NY; current fit already captures that. But there might
   be (source, city, season) splits that matter — check if winter vs summer
   biases differ in the CHI/DEN data.

**Files to monitor:**

- `kv_cache` table — MOS bias values per (source, city) drift over time
- `bot/signals/weather_ensemble_v2.py:_apply_mos_bias` — what the live combine
  applies
- `bot/learning/weather_mos_materializer.py:fit_and_persist_mos_bias` — the
  EWMA fitter

---

## 4. Key data structures

| Table | Owner / Writer | Purpose |
|---|---|---|
| `weather_metar_hourly_backfill` | `tools.backfill_weather_effective_n.replay_hourly_and_write` (new daemon task) + `update_daily_high_from_cf6` | Per-(station, lst_date, lst_hour) tmpf + per-day `daily_high_f` (now CF6 TMAX). Drives skill σ + MOS bias + METAR residual σ fits. |
| `weather_forecast_snapshots` | `predict_v2._write_snapshots` | Per-(source, ticker, recorded_at) forecast μ/σ/hours_out + the combined_v2 row. Read by audit tools. |
| `weather_gaussian_snapshots_backfill` | `tools.backfill_weather_effective_n.replay_and_write` + materializer | Forecast/observed pairs for MOS bias fitting. |
| `weather_mm_shadow` | WeatherQuoter shadow path | Per-cycle quote + market mid + settled outcome. The dataset our diagnostics replay against. |
| `alpha_backtest` | `bot.learning.alpha_log` | Decision-time logs with `ts_settle_unix` (authoritative settle time). |
| `kv_cache` | various fitters | Persisted skill σ / MOS bias / group ρ / METAR residual σ values. |
| `settlements` | settlement back-fill poller | Per-ticker settlement records (no `settle_unix` column — use alpha_backtest for that). |

**Key kv_cache key shapes:**

```
weather_skill_<source>_<city>_<bucket>     — per-city skill σ
weather_skill_<source>_<bucket>             — pooled skill σ fallback
weather_mos_bias_<source>_<city>            — MOS bias EWMA
weather_metar_residual_sigma_<station>_<lst_hour>  — residual σ per (station, hour)
weather_group_corr_<group>                  — learned ρ per correlation group
```

---

## 5. Things already ruled out (don't re-investigate)

| Hypothesis | What was tested | Verdict |
|---|---|---|
| σ floor too tight | Sweep at floors {0.3, 0.5, 0.75, 1.0, 1.25, 1.5} | Floor=1.0 is current optimum (post-CF6). Higher overspread, lower under-cover. Don't sweep this again until a structural change. |
| AFD signal noisy | sweep `--disable-afd` flag | Couldn't measure — AFD wasn't firing in replays (kv-cache empty for replay process). Production has AFD on. Comment in v2 cites prior audit: AFD on improves Brier by 0.013. |
| Group ρ poisoned | sweep with `_get_group_rho → 1.0` | Persisted ρ already = 1.0; force-1 was a no-op. Not a real lever. |
| Truncation always good | pre-CF6 sweep showed −0.0046 trunc_off | Was net-good when μ was cold-biased; net-bad with corrected μ. H3 conditional now ships. |
| Replay accurate | n/a — bug discovered mid-investigation | Was bypassing all calibration corrections. Fixed in `_replay_predict_v2`. |
| CF6 vs tmpf | Direct probe + Kalshi rules audit | CF6 confirmed as Kalshi's settlement source. Shipped. |

---

## 6. What's running on the VPS right now

- Daemon `kalshi-daemon.service` — active, 15 scheduled tasks
- Last restart: 2026-04-28 19:14 UTC (deploy of CF6 + H3 + σ floor fix)
- Daily `hourly_backfill` task fires at 06:00 UTC; first scheduled fire after
  deploy at 2026-04-29 06:00 UTC
- Daily `mos_materializer` runs hourly (refits all calibration values)
- Catalog of weather brackets in `TRADE_SERIES_ALLOWLIST` covers 6 cities
- All weather MM is BLOCKED for live trading; v2 runs in shadow mode only
  (writes to `weather_mm_shadow`, no real orders)

**Live data flowing in:** every cycle (60s), predict_v2 runs on each open
weather bracket and writes a snapshot. Calibration values get refit hourly
on the new data.

**To verify the fixes are working in production:** SSH to VPS as root, then

```bash
grep '\[hourly_backfill\]' /home/kalshi/autoagent/daemon.log | tail -3
# Expected: cf6_days_updated=N rows on each daily fire
grep '\[metar_residual_fitter\]' /home/kalshi/autoagent/daemon.log | tail -3
# Expected: cells_fitted ~144, keys_written ~144 each hour
```

---

## 7. Quick start for the next session

If you want to continue this research from a fresh context:

1. **Read this doc + commit messages of `8eac0cf` and `d1f7fd1`.**
2. **Pull the latest data:** the daemon has been writing CF6-corrected data
   since 2026-04-28 19:14 UTC. Re-run the diagnostic with whatever days
   have accumulated:

   ```bash
   ssh root@45.55.79.193 \
     "cd /home/kalshi/autoagent && sudo -u kalshi python3 -m tools.diagnose_v2_gap \
        --report all --db /home/kalshi/autoagent/kalshi_trades.db" | tail -80
   ```

3. **Decide which item to tackle.** Item 2 (forecast-side bias) is mostly a
   waiting game — measurable improvement should appear automatically as MOS
   bias EWMA accumulates CF6-correct samples. Item 1 (regime-conditional σ)
   is the bigger lever and the more interesting research project.

4. **For Item 1, start with data.** Add wind direction + cloud cover columns
   to the IEM ASOS pull, run a 30-day retroactive backfill, then look at
   whether residual peak σ stratifies cleanly by wind direction at Miami.
   That's the cheapest test of the regime hypothesis.

5. **Don't ship blind.** Every hypothesis we tested this session that *felt*
   right empirically failed (or accidentally helped for the wrong reason).
   Run the sweep + diagnostic before deploying any new behavior.

---

## 8. Open questions (helpful, not blocking)

* **Is the CHI/LAX H3 loss real or noise?** n=22-24 per family is thin. Worth
  re-measuring once we have 30+ more days of post-CF6 data.

* **Does AFD actually help in production?** The replay can't measure it; live
  shadow data could. Compare `weather_mm_shadow.fair_value_cents` Brier vs
  market mid pre and post any AFD-related kv-cache change.

* **What about non-weather families?** All of today's work was weather.
  Crypto (KXBTC, KXETH) is blocked from directional trading because of
  catastrophic anti-calibration; macro (KXFED) is the only other live family.
  Same kind of source-of-truth audit could surface analogous bugs there.

* **Is the late-day shadow row sparseness affecting our diagnostic?** ~33 of
  170 settled tickers had `no_replay` (missing snapshot data). Worth
  understanding why before they bias future diagnostic runs.
