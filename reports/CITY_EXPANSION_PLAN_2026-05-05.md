# City Expansion — Robust Framework + Execution Plan (FOR REVIEW)

**Status:** DRAFT for Josh's review. Do NOT execute until alignment is reached.
**Author:** Claude (planning session 2026-05-05)
**Companion to:** [CITY_EXPANSION_INVESTIGATION_2026-05-05.md](CITY_EXPANSION_INVESTIGATION_2026-05-05.md) — that doc proposed Brier-based criteria; this revises to profit-based.

---

## 0. What this plan changes vs. the investigation doc

| Investigation doc said | This plan says |
|---|---|
| Acceptance gate = Brier ≤ 0.25 + |bias| ≤ 1°F + σ-cal pass + sim P&L positive | Acceptance gate = **profit-based EV per night**, with forecast-quality stats as *inputs to* the EV estimate, not as gates of their own |
| Per-city criteria are independent (binary qualify / don't) | Cities are **ranked by simulated $/night**, then top-N fill the daily exposure cap; cap is set to whatever gives us the most training data |
| Step 1: enumerate via API. Steps 2–4: per-city analysis. Step "Special considerations": per-city notes | Adds **Layer 0** (validate the framework against existing 6 cities first — if the framework can't reproduce live performance ranking on cities we already trade, it's broken and we shouldn't trust it on new ones) |
| `tools/backfill_metar_hourly.py` — assumed to exist | Doesn't exist as named. `tools/backfill_weather_effective_n.py` covers METAR via IEM ASOS archive + Open-Meteo (weather/hrrr/nbm models) and IS extensible to new cities |
| Replay v2 ensemble offline with point-in-time data | **Point-in-time replay does NOT exist for new cities**. We have it only for cities the bot was already quoting (via `replay_postfix_results` joined to live `weather_forecast_snapshots`). For new cities we must either (a) backfill via archive APIs or (b) start recording now and wait. Plan addresses both |
| Fix known issues (MIA past-peak, σ floor, client_order_id NULL) "ideally before adding cities" | Per Josh's instruction: **do in parallel** as Track A |

---

## 1. Audit findings — what infrastructure we actually have

### EXISTS (and we use it)
- `weather_forecast_snapshots` — per-cycle source captures (live). Limited to cities currently quoted.
- `weather_gaussian_snapshots_backfill` — archive-API backfill table with `observed_high_f` ground truth. **Designed for exactly this use case.** Extensible to new cities via `--cities` flag. (Schema: [bot/db.py:296](../bot/db.py#L296))
- `replay_postfix_results` — leak-free σ-fixed v2 (μ, σ) reconstructed from live snapshots. ([tools/replay_postfix_brier.py:96](../tools/replay_postfix_brier.py#L96))
- `tools/backtest_cross_bracket_historical.py` — production-scorer-faithful cross-bracket P&L sim. T = settle - 4h. **Requires `weather_mm_shadow` market quotes**, which only exist for cities the MM has fired on.
- `tools/backfill_weather_effective_n.py` — backfills Open-Meteo (default model = "weather"), gfs_hrrr → "hrrr", gfs_seamless → "nbm", + METAR via IEM ASOS. ([tools/backfill_weather_effective_n.py:93](../tools/backfill_weather_effective_n.py#L93))
- `tools/cross_bracket_scoreboard.py` + `cross_bracket_diagnostic.py` — live performance dashboards.
- `bot/scoring/bracket_portfolio.py::score_market_portfolio` — single source of truth for cross-bracket scoring. Backtest uses production code → no parallel-implementation drift.

### PARTIAL
- `weather_mm_shadow` records bid/ask continuously *only when MM fires*. For new candidate cities (where MM hasn't fired), we have **zero historical market quotes**. Cross-bracket P&L sim is unrunnable without them. → **Need to either backfill from Kalshi historical, or start a new continuous-snapshot poller for the candidate set.**
- `backfill_weather_effective_n.py` covers 3 sources + METAR. v2 uses ~10 sources (HRRR, ECMWF, ICON, GEM, metno, NWS_point, weather, metar, nws_5min, nws_5min_diurnal, AFD, MOS). For new cities, per-source skill curves are partial → must either accept v2 with reduced source set or extend backfill.
- METAR backfill works (IEM ASOS) — but `tools/backfill_metar_hourly.py` referenced in the doc doesn't exist as that filename. Use `backfill_weather_effective_n.py --cities <list>`.

### MISSING
- **Continuous market-data snapshotter.** No table or poller captures bid/ask for arbitrary Kalshi tickers over time, independent of whether MM/cross-bracket fires. This is the load-bearing missing piece.
- Per-city scorecard tool (one-shot report: per-city forecast quality + market capacity + sim P&L + sigma calibration + per-source bias).
- Per-city cross-bracket EV sim that works without prior MM-history dependency.
- Capacity-allocation policy (rank cities, allocate budget; today the `$5/day` cap is global with no per-city allocation).

### Known issues — CURRENT STATE (not the doc's stated state)
- **MIA past-peak clamp:** `_PAST_PEAK_DELTA_F` already lowered from 2.0 → 1.0 yesterday in commit d8cc3f8. ([bot/signals/sources/metar_observations.py:560](../bot/signals/sources/metar_observations.py#L560)) The doc's call for "tune threshold OR add LST-hour override" is **partially done**. The LST-hour override is still missing.
- **σ floor:** `_COMBINED_SIGMA_FLOOR_F = 1.0` ([bot/signals/weather_ensemble_v2.py:750](../bot/signals/weather_ensemble_v2.py#L750)). Touched in d8cc3f8 but is still a global constant, not regime-aware.
- **fills_writer.client_order_id NULL:** Still NULL but handled gracefully via `source_tagger` fallback ([bot/daemon/fills_writer.py:338](../bot/daemon/fills_writer.py#L338)). Cross-bracket exit works around it. Today's commit 21ef414 fixed cross-bracket attribution to filter via `alpha_backtest` instead of all `fills_ledger` — partial mitigation.

### Open input I couldn't get
- Morning audit results: I could find the session in chat history but **could not extract the scoreboard / diagnostic numbers for tonight's KXHIGHNY-B72.5 + KXHIGHAUS-B82.5 settlements**. Need either: paste the output, or authorize SSH to pull from `cron.log`.

---

## 2. The framework (revised — profit-based)

A city qualifies for live cross-bracket trading via 5 layers, gated in order. Each layer must pass for the city to advance.

### Layer 0 — Self-validation (mandatory, do first)
Apply the entire framework to the existing 6 cities (NY, MIA, CHI, AUS, LAX, DEN). The framework's predicted ranking must align with what we observe live (within reasonable noise — e.g., Spearman correlation ≥ 0.7 across the 6).

If the framework predicts MIA is the best city when we *know* MIA has the metar bias issue dragging it down, the framework is broken. Don't promote anything until Layer 0 passes.

**Output:** [reports/FRAMEWORK_VALIDATION_2026-05-XX.md] — table of 6 cities, predicted vs observed P&L per night.

### Layer 1 — Forecast quality (per-city, per-source)
For each candidate city, against ≥30 days of backfilled historical data:
- **Per-source bias** (mean of `forecast - observed`) — flag any source with |bias| > 1.5°F at the 4–8h-pre-settle horizon. Add to per-city exclusion list.
- **Per-source RMSE** — flag any source with RMSE > 2.5°F.
- **Combined v2 σ-calibration:** is the realized fraction of `|μ - observed| < σ` close to 68%? If realized is 50%, σ is too tight; if 85%, too wide. Apply per-city σ inflation factor.

**Layer 1 output:** per-city tuning tuple — (source exclusion list, σ inflation factor, per-source σ priors). Required before Layer 3.

### Layer 2 — Market capacity
For each candidate, against ≥14 days of recent market data:
- Mean daily bracket count traded (Kalshi has ≥6 brackets per city per day, but volume varies massively).
- Mean spread at the 4-8h-pre-settle window.
- Implied daily $ capacity at our maker fee, assuming we capture half the spread on plausible-fill fraction of brackets.

A city with great forecasts but $0.50/night capacity is worth less than one with mediocre forecasts and $20/night capacity. **Layer 2 is the multiplier on Layer 3.**

**Required:** continuous market-data snapshotter (currently MISSING — see §3).

### Layer 3 — Strategy P&L simulator (offline)
Run `backtest_cross_bracket_historical.py` (extended to support cities without prior MM history — see §3) against the candidate's historical brackets. Compute:
- Mean simulated P&L per settled day, gross + net of maker fee
- Win rate, std dev of nightly P&L
- 5th-percentile worst-day P&L (tail risk)
- Per-bracket fill rate (assume taker fills at top-of-book ask)
- Sharpe-like metric: mean / std

**Acceptance:** simulated mean net P&L per night > $0.10 (low bar — we're sizing the cap to whatever gives us learning data, not real returns yet) AND 5th-percentile worst day > -$2 (tail-loss budget).

### Layer 4 — Live shadow (≥7 days)
Promote candidate to **shadow-only** (decisions logged to `weather_mm_shadow`, no posts). Required before any live posting:
- ≥7 settled cycles
- Realized shadow markout (decision-time fair value vs settlement) within 80% of Layer 3 sim's prediction
- No catastrophic source blow-up (e.g., HRRR returns nonsense for 3+ days)

This is where Layer 0 doubles as a sanity check — if shadow markout systematically diverges from sim, the framework's calibration is off.

### Layer 5 — Promotion to live + capacity allocation
After Layer 4 passes:
- **Daily exposure cap is set globally**, not per-city. Cap = $X/day where X is whatever gives us enough fills to learn from (Josh's call: $5, $10, $25 — doesn't matter for risk; matters for data velocity).
- **Allocation policy:** when multiple cities have actionable cross-bracket opportunities on a given night, allocate budget to highest sim-EV city first, descending until cap is exhausted. Re-rank weekly using new shadow data.

This means city #7 doesn't compete with city #6 on risk — it competes on EV ranking. A bad new city won't "steal" exposure; it'll just rank below the others and not get budget.

---

## 3. What we need to build

These are infra deliverables that don't exist today. Sized roughly by lines of new code.

| Item | Why | Approx LOC | Blocks |
|---|---|---|---|
| **Continuous market-data snapshotter** — new poller, new table `kalshi_market_snapshots(ticker, ts, yes_bid, yes_ask, no_bid, no_ask, last_trade_price, volume)` for the candidate-city ticker set, every ~60s. | Layer 2 + Layer 3 cannot run on cities without prior MM history. Best to start NOW so by the time we finish other infra we have ≥14 days of data. | ~250 | Layers 2, 3 for new cities |
| **Per-city forecast scorecard tool** — `tools/forecast_scorecard.py --city CITY` produces one report: per-source bias/RMSE/σ-cal at lead bins 2h/4h/8h/12h/24h, recommended exclusion list and σ inflation. | Layer 1 needs a single artifact per city, not a pile of ad-hoc queries. | ~400 | Layer 1 |
| **Cross-bracket sim that doesn't require `weather_mm_shadow`** — extend `backtest_cross_bracket_historical.py` to optionally pull market quotes from the new snapshot table. | Layer 3 for new cities. | ~150 (extension) | Layer 3 |
| **Capacity-allocation simulator** — given N cities with sim-EV distributions, compute expected fills/night under different cap levels. | Helps Josh pick the cap. | ~200 | Layer 5 |
| **Framework self-validation report** — `tools/validate_framework_on_known_cities.py` runs all 5 layers on the existing 6 cities, compares predicted ranking to observed live P&L. | Layer 0. | ~300 | Everything else |
| **Extension to `backfill_weather_effective_n.py`** — add HRRR-direct, NWS_point, MOS, AFD source backfills. (Optional — can also start with reduced source set.) | Per-source coverage for new cities. | ~500 (could defer) | Layer 1 completeness |

**Total new code:** ~1300–1800 LOC (depending on whether we extend per-source backfill now or later). Roughly 2–4 sessions of focused work, plus passive wait time for snapshotter to accumulate data.

---

## 4. Two parallel tracks (per Josh's instruction)

### Track A — Fix known issues (parallel to Track B)

**A1. MIA past-peak LST-hour override**
- Add `if lst_hour >= 18: μ = running_high regardless of delta` to `metar_observations.py`.
- Backtest the change on the 4 prior MIA losses to confirm it fires correctly without overcorrecting.
- One commit, ~30 LOC + test.

**A2. Regime-aware σ floor**
- Replace `_COMBINED_SIGMA_FLOOR_F = 1.0` with a function `combined_sigma_floor(regime)`:
  - Normal day, far from peak: 1.0
  - Post-peak (LST ≥ 18): 0.5 (let metar's tight σ dominate when it should)
  - Pre-peak high-vol regime: 1.2 (give weather model uncertainty room)
- Backtest on existing 6 cities to confirm Brier improves overall, not just MIA.
- ~50 LOC + test.

**A3. fills_writer client_order_id enrichment**
- In `fills_writer.py::ingest_page`, before writing each fill: fetch the corresponding order via `/portfolio/orders/{order_id}` and stamp `client_order_id`.
- Adds 1 API call per fill — fine at our volume. Cache `order_id → client_order_id` map per cycle.
- ~80 LOC + test. Cleans up downstream attribution everywhere.

**Sequencing for Track A:** A1 first (highest-EV fix, isolated), A2 second (touches the v2 combine — needs more testing), A3 last (mostly cleanup, doesn't affect P&L directly).

### Track B — City expansion (parallel to Track A)

**B0. Get the morning audit + decide whether to proceed.** If tonight's settlements showed unexpected behavior, that re-prioritizes Track A over Track B.

**B1. Enumerate candidate cities via Kalshi API.** Pull all `KXHIGH*` series, dedupe against existing 6. Snapshot the list to [reports/CITY_EXPANSION_CANDIDATES_2026-05-05.md].

**B2. Start the market-data snapshotter on candidate set TODAY.** Even if backfill is fast, having ≥14 days of *our own* recorded data by the time we hit Layer 4 is gold. ([§3 item 1])

**B3. Build framework-validation tool, run on existing 6 cities (Layer 0).**
- This is the gate. If Layer 0 fails, nothing in B4–B8 runs until framework is fixed.

**B4. Backfill per-city METAR + Open-Meteo for candidates** via `backfill_weather_effective_n.py --cities <candidates> --start 2026-01-01 --end today`.

**B5. Run per-city forecast scorecard (Layer 1)** on each candidate.

**B6. Run cross-bracket P&L sim (Layer 3)** on each candidate using snapshotter + backfill data.

**B7. Rank candidates** by sim-EV, select top-N. (Where N = whatever the cap supports — Josh's call.)

**B8. Promote top-N to shadow (Layer 4).** Wait ≥7 settled days per city. Track A fixes should be deployed before this.

**B9. Promote shadow-passing cities to live (Layer 5).** One at a time, ≥3 settled cycles between additions, per the doc's existing guardrail.

---

## 5. Sequencing — what gets done in what order

This is the boil-the-ocean ordering. Each numbered item is a session-or-less of work; parallelism noted.

```
Day 0 (today, after this plan is approved):
  1. Pull morning audit (B0)              [user input or SSH]
  2. Build market-data snapshotter (§3)   [start ASAP — accumulates data passively]
  3. Enumerate candidates (B1)             [trivial Kalshi API call]

Day 1 (Track A starts in parallel):
  4. A1: MIA past-peak LST override
  5. B3: framework validator + Layer 0 on existing 6
  6. B4: kick off backfill for candidates (long-running data fetch)

Day 2:
  7. A2: regime-aware σ floor
  8. B5: per-city scorecard tool + run on candidates

Day 3:
  9. A3: fills_writer enrichment
  10. B6: extend backtest to candidate cities, run sim
  11. B7: rank + shortlist

Day 4–10:
  12. B8: shadow-mode for shortlist (passive — waiting on settlements)
  13. Capacity-allocation sim (§3 item 4)
  14. Pick global cap based on sim

Day 10+:
  15. B9: promote first city to live (one at a time, observe ≥3 cycles)
```

Total: ~4 sessions of active work, ~10 days of elapsed time including shadow waits.

---

## 6. Open questions I need answered before executing

1. **Morning audit results.** Want either the scoreboard output pasted here, or authorization to SSH to VPS and pull. The audit results may re-prioritize Track A over Track B if something is broken.

2. **Cap size for training.** $5/day, $10/day, $25/day, $50/day? You said "doesn't matter for risk" — but it changes how many cities we can sustain in shadow + live simultaneously, and how much fill data we get per night. My recommendation: $25/day, scale up to $50 once Layer 4 is clean for ≥3 cities.

3. **Source coverage for new cities.** Backfilling all v2 sources for new cities is ~500 LOC (HRRR-direct, NWS_point, MOS, AFD). Two options:
   - **(a)** Defer the extension; accept Layer 1 with partial source set (weather + hrrr + nbm + metar) and let live snapshotter accumulate the rest naturally. Faster start, less rigorous Layer 1.
   - **(b)** Build the extension first; longer lead time but Layer 1 is bulletproof.
   - My recommendation: **(a)** — backfill is good enough to sort the obvious winners/losers; rigorous Layer 1 can come after we know which 1–3 cities are worth it.

4. **What counts as Layer 0 pass?** I proposed "Spearman correlation ≥ 0.7 between predicted and observed P&L ranking on existing 6 cities." This is somewhat arbitrary. Open to alternatives — e.g., "predicted top-3 includes ≥2 of the actual top-3 by live P&L."

5. **Track A blocking?** If A1 (MIA past-peak LST override) doesn't ship before Track B Layer 3, the cross-bracket sim for new cities will inherit MIA-class bias for any candidate with similar diurnal pattern (Houston, Atlanta, Phoenix monsoon afternoons). Two options:
   - **(a)** Block B6 until A1 ships. Adds ~1 day.
   - **(b)** Run B6 anyway, flag the per-city result as "subject to A1 fix" and re-run after.
   - My recommendation: **(a)** — we know A1 is needed, doing the sim twice wastes a session.

6. **Safety nets for live promotion (Layer 5).** Beyond the ≥3 settled cycles between adds, do you want any kill-switch criteria? E.g., "if any new city loses >$Y in its first 5 live cycles, auto-revert to shadow." I'd recommend yes; concrete number depends on cap.

---

## 7. What I need from you to start

- **GO / NO-GO on the framework structure** (Layers 0–5).
- Answers to the 6 open questions in §6.
- Either the morning audit output, or SSH authorization to pull it.

Nothing in this plan executes until you say go.
