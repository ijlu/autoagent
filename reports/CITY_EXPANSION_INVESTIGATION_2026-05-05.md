# City Expansion Investigation — start-here for 2026-05-05 session

**Status:** TODO. Investigate every weather city Kalshi trades, backtest forecast quality, and build a vetted plan to add one or more cities to cross-bracket.

This doc is the focused entry point. Read [reports/WEATHER_ENSEMBLE_STATE_2026-05-04.md](WEATHER_ENSEMBLE_STATE_2026-05-04.md) first for system context.

## Why we're considering this

Cross-bracket went live 2026-05-04 with the original 6 weather families (NY/MIA/CHI/AUS/LAX/DEN). The strategy is gated to a $5/day portfolio-wide exposure cap — currently spending ~$0.50/night across the 6 because most cycles produce shadow-only decisions (model and market converge with no arb-able mispricing).

Adding more cities **doesn't increase risk** (cap is portfolio-level) but **does increase the surface area of arb opportunities per night**. The constraint is per-city forecast quality — we can only safely add cities where v2 ensemble produces accurate, low-bias predictions.

The user already flagged the **MIA metar source bias** found 2026-05-04 (metar projecting 85.8°F when actual KMIA at 81°F per NWS) — adding cities without per-city validation would compound this risk.

## What "first thing tomorrow" should be (before this investigation)

Run the post-overnight audit on tonight's fills:
```bash
ssh root@45.55.79.193 'cd /home/kalshi/autoagent && \
  python3 tools/cross_bracket_scoreboard.py && \
  python3 tools/cross_bracket_diagnostic.py'
```

Tonight's positions to settle by 06:59 UTC:
- KXHIGHNY-26MAY04-B72.5 NO 1×1¢
- KXHIGHAUS-26MAY04-B82.5 NO 5×~10.4¢ avg

Also review:
- Did exit logic trigger? Look for `[cb_exit] EXIT` lines in daemon.log.
- Did over-posting fix hold? Confirm no new POSTs on already-held tickers.
- The MIA metar bias — see "Known issues" below.

Only after that audit, proceed with this investigation.

## Step 1: enumerate Kalshi's available high-temperature markets

The bot has `bot/daemon/series_discovery.py` (runs daily) which alerts on new tradeable series. Query its log + Kalshi's `/markets?series_ticker=KXHIGH...` to enumerate all currently-traded `KXHIGH*` series.

Likely candidates (verify exists on Kalshi):
- KXHIGHPHX (Phoenix / KPHX) — **leading candidate**
- KXHIGHSEA (Seattle / KSEA)
- KXHIGHHOU (Houston / KIAH or KHOU)
- KXHIGHATL (Atlanta / KATL)
- KXHIGHBOS (Boston / KBOS)
- KXHIGHDFW (Dallas / KDFW)
- KXHIGHSF or KXHIGHSFO (San Francisco / KSFO)
- KXHIGHLAS (Las Vegas / KLAS)
- KXHIGHDTW (Detroit / KDTW)
- KXHIGHPHL (Philadelphia / KPHL)

Kalshi's roster will define the actual candidate list. **Don't assume — check via API.**

## Step 2: backtest forecast quality per candidate

For each candidate city:

### 2a. Backfill METAR observations
Use the existing `tools/backfill_metar_hourly.py` (or equivalent) for the candidate's ICAO station, ≥30 days back. This populates `weather_metar_hourly_backfill` with observed temps.

### 2b. Backfill historical Kalshi market data
Pull historical settled bracket markets for that series. Need: settlement value, bracket thresholds, market mid-prices over time.

### 2c. Replay v2 ensemble offline
Run `bot/signals/weather_ensemble_v2.predict_v2` against historical conditions (using only data available at decision time, not future leak). Compute:

- **Per-bracket Brier** vs. actual settlement outcome — target ≤0.21 (matches current 6 weather families' median)
- **μ bias** = mean(predicted_high - actual_high) — flag if |bias| > 1°F
- **σ calibration** = (predicted_high - actual_high) / predicted_σ should be ~N(0,1) if well-calibrated

### 2d. Per-source breakdown
For each source (HRRR, weather, ECMWF, ICON, GEM, metno, NWS_point, metar, nws_5min, nws_5min_diurnal, AFD), compute:
- Per-source RMSE vs actual
- Per-source bias

Identify candidate-specific source exclusions (e.g., LAX excludes metno+gem; what does Phoenix need?).

### 2e. Cross-bracket simulation
Replay the cross-bracket strategy on the candidate's historical brackets:
- How many cycles produce ≥10% leg edge?
- Simulated fill rate (assume taker fills at top-of-book ask)
- Simulated P&L net of fees
- Compare to current 6 cities' baseline

## Step 3: decision criteria

A city qualifies for live cross-bracket trading if ALL of:

1. **Per-bracket Brier ≤ 0.25** on ≥20 settled days of v2 replay (allow 20% margin over the 0.21 baseline)
2. **|μ bias| ≤ 1°F** averaged across the replay window
3. **σ calibration check passes** — predicted vs actual residuals within 1.5x expected std dev
4. **Cross-bracket sim shows positive simulated P&L** over ≥30 settled cycles
5. **Per-source bias known** for the city — at least HRRR + at least 2 model sources have |bias| ≤ 2°F. Sources outside this get added to per-city exclusion list.

Cities that fail any criterion → don't add live; document why and revisit when underlying issue is fixed.

## Step 4: implementation checklist (per city to add)

When a city passes Step 3:

- [ ] **`bot/daemon/stations.py`**: add `WeatherStation(family, icao, lat, lon, tz_local_std)` entry. Use settlement station coords (not city center).
- [ ] **`bot/signals/sources/weather.py::WEATHER_CITIES`**: add city → (lat, lon) entry matching the ICAO station, NOT the downtown.
- [ ] **`tests/signals/test_weather_cities_alignment.py`**: add a pin so coordinate drift > 0.05° fails CI.
- [ ] **`bot/signals/weather_sources.py::EXCLUDED_SOURCES_BY_CITY`**: add city's source-exclusion frozenset based on Step 2d findings.
- [ ] **`bot/signals/sources/hrrr.py::_HRRR_SIGMA_PRIOR_BY_CITY`**: per-city σ prior (1.2 if HRRR RMSE ≤ 1.5°F at settle horizon, else 2.0).
- [ ] **`kv_cache::weather_sigma_inflation_<FAMILY>`**: per-family σ inflation factor (set via tools/sigma_inflation_per_family.py sweep).
- [ ] **`bot/daemon/cross_bracket_shadow.py`**:
   - Line ~115-126 (`iana_tz` map in `_settlement_unix_from_key`): add IANA timezone (e.g., `"KXHIGHPHX": "America/Phoenix"` — note Phoenix doesn't observe DST!)
   - Line ~256-257 (`_fetch_open_weather_markets`): add the new family to the hardcoded series tuple.
- [ ] **`bot/config.py`**:
   - `_WEATHER_SERIES` tuple: add the new family.
   - `TRADE_SERIES_ALLOWLIST`: already pulls from `_WEATHER_SERIES` so should auto-include.
- [ ] **`bot/daemon/main.py`**: `_run_cross_bracket_rearm` families tuple — add the new family.
- [ ] **`bot/observability/cross_bracket_scoreboard.py::CROSS_BRACKET_EPOCH_ISO`**: no change needed (it's a date, not a city list).
- [ ] **`tools/dst_check.py`**: add to debug script.
- [ ] **MOS bias correction**: trigger `mos_materializer` to fit per-(source, new_city) bias from backfilled METAR.
- [ ] **kv arming**: add `cross_bracket_live:KXHIGH<NEW>` once auto-rearm task is updated, OR manually arm via `tools/arm_cross_bracket.py`.
- [ ] **Tests**: add per-city tests pinning the source exclusions, σ prior, and timezone mapping.

## Special considerations

### Phoenix
- **No DST!** Arizona doesn't observe DST. IANA `"America/Phoenix"` handles this correctly via zoneinfo. **Verify in test** — if it's wrong it'd be a year-round 1h offset bug, worse than the May-only DST bug we just hit.
- Stable diurnal pattern — likely high Brier. Strong candidate.
- Watch for monsoon-season afternoon thunderstorms (July-September) — could disrupt the diurnal model. Avoid going live during monsoon if backtest doesn't cover it.

### Seattle
- Marine layer + cloud cover dominate the high. NWP models may be biased; metar source dominant. Expect LAX-class issues (need per-city source exclusions).
- Lower priority than Phoenix.

### Houston / Miami-class climates
- Convective afternoons. Diurnal model needs late-afternoon thunderstorm handling. Same metar-source bias risk we just saw on MIA.

### Coastal cities (Boston, SF)
- Coastal stations have unique microclimates. Likely need per-city exclusions for non-marine sources.

## Known issues (open from 2026-05-04 session)

These should ideally be fixed BEFORE adding new cities, since they'd compound:

1. **MIA metar source diurnal bias** — past-peak clamp threshold (`_PAST_PEAK_DELTA_F=2.0`) too conservative. MIA's 1°F gap doesn't trigger; metar source projects +5°F more warming when day is actually past peak. Fix path: tune threshold OR add LST-hour-based override (e.g., post-18:00 LST always pin μ=running_high regardless of delta).
2. **fills_writer.client_order_id is NULL** — Kalshi's `/portfolio/fills` doesn't include client_order_id (it's an order property, not a fill property). cross_bracket_exit works around this by joining via `/portfolio/orders` to get order_ids, then filtering fills_ledger by those. Long-term fix: enrich fills_writer to fetch orders and stamp client_order_id at write time.
3. **σ floor too generous post-peak** — `_COMBINED_SIGMA_FLOOR_F=1.0` was tuned to "give meaningful adjacent-bracket probability" but in late-day post-peak regime where metar source σ=0.30 would otherwise dominate via precision-weighting, the floor over-inflates uncertainty. Fix: regime-aware σ floor (smaller post-peak).

These issues affect the existing 6 cities too. Adding cities without fixing them propagates the bug.

## What NOT to do

- Don't add a city based on "intuition" or "looks like it should work."
- Don't skip backtest — every per-city tuning constant needs empirical justification.
- Don't add multiple cities at once — promote one at a time, observe ≥3 settled cycles before adding the next.
- Don't add a city to live trading without ≥7 days of shadow-only data first.
- Don't reuse another city's σ priors as a default — empirical tuning is per-city, full stop.

## Reference: tonight's session deliverables

What was shipped 2026-05-04 (in case you need to roll back or compare):

- DST fix in `cross_bracket_shadow._settlement_unix_from_key` — IANA timezones via zoneinfo
- Cross-bracket exit logic — `bot/daemon/cross_bracket_exit.py` + scheduled task
- Over-posting fix — existing-position guard in `_process_decisions`
- Auto-rearm task — `cross_bracket_rearm` every 12h
- Scoreboard + diagnostic tools — `tools/cross_bracket_scoreboard.py`, `tools/cross_bracket_diagnostic.py`
- Calibration: bounded-Newton Platt fitter, raw-prob logging columns, calibration bake-off in `reports/CALIBRATION_BAKEOFF_2026-05-04.md`
- Weather-only mode — `WEATHER_ONLY_MODE=true` env, narrows TRADE_SERIES_ALLOWLIST to 6 weather families

Recent commits to review:
```
70dc4d2 Cross-bracket: stop over-posting + fix exit position identification
a2f6967 DST bug: cross_bracket settle was 1h late during summer months
9597074 Cross-bracket shadow-vs-realized diagnostic
cd54172 Cross-bracket performance scoreboard: CLI + daily daemon log
003dea1 Auto-rearm cross-bracket per-family kv keys every 12h
a247e0e Calibration bake-off + raw-prob logging + WEATHER_ONLY_MODE
5365321 Bounded-Newton Platt: clamp A,B inside loop + line search
```
