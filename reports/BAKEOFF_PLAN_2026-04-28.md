# Bake-off plan — pre-registered gates for 4 signal/strategy ideas

**Date:** 2026-04-28
**Author:** Planning session, post-CF6-fix
**Status:** Pre-registered. Run analyses against these gates **before** writing
any production code. Gates set BEFORE looking at the analysis output.
**Related docs:**
- [BACKTEST_APR17.md](BACKTEST_APR17.md) — Phase 0 baseline
- [WEATHER_GAP_HANDOFF_2026-04-28.md](WEATHER_GAP_HANDOFF_2026-04-28.md) — current 0.049 gap
- [WEATHER_REGIME_INVESTIGATION_2026-04-28.md](WEATHER_REGIME_INVESTIGATION_2026-04-28.md) — regime σ stage-1 in flight
- [POSTMORTEM_SHADOW_DATA_CORRUPTION_2026-04-21.md](POSTMORTEM_SHADOW_DATA_CORRUPTION_2026-04-21.md) — shadow rows pre-2026-04-21 are contaminated

## Scope

Four ideas from the public-bot survey. **#5 (cross-bracket portfolio)
deferred to Phase 2** — too much surface area in `trade.py`/exposure/exit code
to land in this sprint.

| Step | Idea                              | Risk class | Order |
|------|-----------------------------------|------------|-------|
| #4   | Penny-floor + TTE-floor filters   | low        | first |
| #1   | MM-vs-take strategy bake-off      | low        | second |
| #2   | GFS ensemble CDF source           | medium     | third |
| #3   | Trade-flow as Bayesian source     | med-high   | fourth |

Order rationale: cheapest information first. #4 and #1 are pure-read analyses
against existing tables; if they already give us most of the lift, #2 and #3
become marginal. #2 is replace-not-add (avoids double-counting in
`weather_ensemble_v2._collect_gaussians`). #3 is a Bayesian update for v1
(no sklearn) given the 116-settlement label volume; full ML deferred to
≥500-settlement future.

## Workflow contract

For **every** step:
1. Pass criterion fixed in this doc *before* running the analysis.
2. Analysis runs against a fresh VPS DB pull, output to `/tmp/<step>_<date>.txt` and a one-page report appended here.
3. **Pass** → write production code behind a config flag (default off), land tests, deploy with flag off, then turn on in shadow mode.
4. **Fail** → annotate this doc with the failure mode and stop. No code committed.
5. Promotion shadow → canary → live uses existing `bot/learning/mm_promotion.py` rails. No new promotion machinery.

## Baselines (today, post-CF6-fix)

| Metric                         | Value (n=143) |
|--------------------------------|---------------|
| v2 ensemble Brier (close-edge) | 0.309         |
| Market Brier (close-edge)      | 0.163         |
| **Close-edge gap**             | **+0.146**    |
| v2 ensemble Brier (pooled all-horizon) | 0.14  |
| Market Brier (pooled all-horizon)      | 0.09  |
| **Pooled gap**                 | **+0.049**    |

Concurrent work: regime-conditional σ stage 1 targets +0.005 close-edge
improvement. We must not double-count its effect on our gates.

---

## Step #4 — Penny-floor + TTE-floor filters

### Hypothesis
Sub-10¢ trades and >24h-to-expiry trades on weather have asymmetric
loss-vs-gain profiles relative to mid-range / late-window trades.

### Falsifier
If sub-10¢ Brier ≤ overall Brier AND >24h Brier ≤ overall Brier, no filter
is justified — the existing pricing/sizing already handles them.

### Data slice
- Table: `alpha_backtest` filtered to `ts_settle_unix IS NOT NULL`
- `decision_type IN ('mm_quote', 'directional_shadow', 'directional_live')`
- `family LIKE 'KXHIGH%'` (weather only)
- `ts_decision_unix > <2026-04-21 UTC midnight>` (post-corruption window)
- N must be ≥ 30 in EACH bucket compared (penny / non-penny; >24h / <24h)

### Buckets
| Bucket            | Definition                                |
|-------------------|-------------------------------------------|
| penny             | `price_cents < 10` OR `price_cents > 90`  |
| non-penny         | `10 ≤ price_cents ≤ 90`                   |
| late (≤24h TTE)   | `ts_settle_unix - ts_decision_unix ≤ 86400` |
| early (>24h TTE)  | `ts_settle_unix - ts_decision_unix > 86400` |

(price_cents > 90 is the symmetric "expensive YES" case — same penny dynamic.)

### Metrics computed per bucket
- N
- Brier (mean (ensemble_p_yes − won_yes)²)
- Realized PnL per row (cents)
- Win rate

### Pass criterion (filter is justified)
**ANY of**:
1. Penny-bucket Brier ≥ non-penny Brier + 0.05 with N ≥ 30 each
2. Penny-bucket realized PnL/row ≤ non-penny PnL/row − 1¢ with N ≥ 30 each
3. Early (>24h) Brier ≥ late (≤24h) Brier + 0.05 with N ≥ 30 each
4. Early realized PnL/row ≤ late PnL/row − 1¢ with N ≥ 30 each

### If pass
Add `WEATHER_PENNY_FLOOR_CENTS=10` and/or `WEATHER_MAX_TTE_HOURS=24` env
vars (default off). Filter applied in `bot/scoring/filters.py` only for
families starting with `KXHIGH`. Shadow-log via `alpha_backtest` skip_reason.

### If fail
Document and move on. No filter added.

### Tool
New `tools/bakeoff_step4_penny_tte.py` — pure-read SQL, prints two tables.

---

## Step #1 — MM-vs-take strategy bake-off

### Hypothesis
On weather families, directional taking realizes more of the predicted edge
than MM quoting does. The Phase 0 +6.12¢ markout never converted to MM PnL;
public-bot consensus is that taking is the correct shape for this signal.

### Falsifier
If `mm_quote` realization-ratio ≥ `directional_shadow` realization-ratio
AND paired-ticker MM PnL ≥ paired-ticker directional PnL, MM is the right
shape and we should keep building toward live MM rather than directional-first.

### Tool (existing, no new code)
`bot/learning/bakeoff.py::render_bakeoff_report` — already implements
per-strategy × family rollup AND paired-ticker head-to-head.

```python
from bot.learning.bakeoff import render_bakeoff_report
print(render_bakeoff_report(conn, clean_market_slice=True, min_n=10))
```

### Data slice
- Table: `alpha_backtest`
- `clean_market_slice=True` (only `market_prob_source IN ('mid','last')`)
- `decision_type IN ('mm_quote', 'directional_shadow')`
- `min_n=10` per (strategy, family) cell
- `ts_decision_unix > <2026-04-21 UTC midnight>` (post-corruption)

### Pass criterion (directional-first is correct)
**BOTH**:
1. Pooled across weather families: `directional_shadow.realized_pnl_cents_sum > mm_quote.realized_pnl_cents_sum` by ≥ 50¢
2. Per-family: directional wins on ≥ 3 of the families with N ≥ 10 in both legs

Paired-ticker tie-break: if (1) and (2) marginal, paired-ticker MM-vs-directional winner count + total PnL settles it.

### If pass
Direct evidence to keep `WEATHER_MM_LIVE=false` and route weather signal
through directional. **Does not auto-justify going live directional** —
that requires its own gate (current Phase 1 plan: ≥0.005 market-mid beat
post-shadow).

### If fail
Document why MM looks better. Could reflect that taking has un-modeled
slippage on bracket markets (we'd need to model fees-eat-edge). MM cancel-
replace race is then back on the table.

### Important note
Sample size on `directional_shadow` may be the limit. We've been logging
since 2026-04-17 ish — about 11 days of shadow rows. Per-family N likely
in the 5–20 range. If `min_n=10` produces no valid rows, **we're data-
limited, not failed** — log this outcome and re-run in 2 weeks.

---

## Step #2 — GFS ensemble CDF source (replace, not add)

### Hypothesis
Open-Meteo's 31-member GFS ensemble exposes individual member temperatures.
Computing P(temp > strike) directly as `(members above) / 31` produces a
better-calibrated probability for bracket markets than a point-forecast
plus-sigma Gaussian, because it uses the model's natural disagreement
distribution instead of imposing a parametric one.

### Falsifier
If ensemble-CDF-as-source produces Brier ≥ point-estimate-plus-sigma Brier
on per-family weather replays, the parametric Gaussian is fine and we don't
change anything.

### Replace, not add
The existing Open-Meteo source goes through `_collect_gaussians` → MOS bias
→ truncation → combined.μ/σ. Adding ensemble-CDF as a *new source* would
double-count Open-Meteo (same upstream model, two channels). Instead, when
ensemble members are available, the Open-Meteo entry in `_collect_gaussians`
returns a **non-Gaussian P(threshold)** that the combine handles as a
direct probability rather than via the Gaussian-mixture path.

This requires a small extension to `predict_v2` to accept either
`(μ, σ)` Gaussians or direct `P(threshold)` from a source. Initial
implementation: the ensemble-CDF result is folded in via a precision-
weighted average on logit space, with the Gaussian sources contributing
their threshold-implied probability and the ensemble-CDF contributing
its empirical fraction.

### Data slice for retro-replay
- Table: `weather_forecast_snapshots` joined to `settlements`
- `combined_snapshot=1` rows (production v2 outputs only — control)
- Replay tool: `tools/retro_replay_regime.py::_replay_predict_v2`, extended
  to monkey-patch the Open-Meteo entry to return ensemble-CDF when
  `temperature_2m_member01..31` are pulled
- N ≥ 100 settled tickers, post-CF6-fix only (≥ 2026-04-28 19:14 UTC)

### Pass criterion
**ALL**:
1. Pooled close-edge bucket Brier improves by ≥ **0.005** vs current v2 (this is the same bar regime conditioning is held to — it's the 25%-of-the-gap threshold that's actually achievable per the handoff doc)
2. ≥ 4 of 6 weather cities show non-negative Brier delta on their post-CF6 settled-ticker subset
3. No bucket (`deep_in`, `in`, `edge`, `out`, `deep_out` from `diagnose_v2_gap.py`) regresses by > 0.005

### If pass
Behind `WEATHER_OPEN_METEO_ENSEMBLE_CDF=true` env flag. Stage 1: shadow-log
to a new `weather_forecast_snapshots.cdf_path_used` column. Stage 2:
promote per-city after another ≥ 30 settled tickers per city show non-
regression vs the production combine.

### If fail
Document. Don't ship. Possible follow-on: revisit when more ensemble
sources (HRRRE, NBM-blend) come online — single-source CDF may be too noisy
but a multi-source CDF combination might still work.

### Tool
New `tools/bakeoff_step2_gfs_cdf.py` — calls into `_replay_predict_v2`
with an Open-Meteo ensemble-CDF override. Reuses regime investigation's
infrastructure entirely.

### Coordination note
If regime-σ stage 1 is rolling out concurrently, run #2 retro-replay against
the **pre-regime baseline** to avoid confounding. Compare to the v2-without-
regime numbers in the handoff doc.

---

## Step #3 — Trade-flow as Bayesian source (v1; ML deferred)

### Hypothesis
Recent trade direction at a bracket carries information about other traders'
beliefs. A Bayesian update that nudges our prior toward the empirical recent-
trade balance can outperform our current `bot/signals/sources/momentum.py`
heuristic at weight 0.15.

### Falsifier
If the Bayesian-update Brier ≥ current momentum Brier on the same settled
slice, the simpler heuristic is adequate and we don't change anything.

### Why Bayesian, not ML
116 settlements total post-Phase-0, ~13 wins. Per-family this is <30 weather
samples. sklearn voting-classifier-style ML overfits at this scale. A
2-parameter Bayesian update (prior weight `w_p`, evidence weight `w_e`) can
be tuned via grid search without overfitting risk.

### Data slice
- Table: `alpha_backtest` with derived "recent trade flow" feature computed
  from `opportunity_log` joined on `(ticker, ts_decision_unix - 600s, ts_decision_unix)` — the
  recent 10-minute trade balance as proxy for "where are flows pointing"
- Filter: weather families post-2026-04-21
- N ≥ 50 settled rows

### Bayesian update
For each row:
- Prior: `p_prior = ensemble_p_yes` (current ensemble output)
- Evidence: `p_flow = recent_yes_volume / (recent_yes_volume + recent_no_volume)`
- Posterior: `p_post = sigmoid(w_p · logit(p_prior) + w_e · logit(p_flow))`
- Grid-search `w_p ∈ [0.5, 0.7, 0.85, 1.0]`, `w_e ∈ [0.0, 0.05, 0.1, 0.15, 0.2]`
- Use **half** the data to fit best `(w_p, w_e)`, evaluate Brier on the held-out half
- Walk-forward: split by `ts_decision_unix` median, fit on earlier half, test on later half (no look-ahead)

### Pass criterion
**ALL**:
1. Held-out (later half) Brier improvement ≥ **0.005** over current ensemble
2. Best `(w_p, w_e)` is at an interior grid point, not a corner (corner suggests under-determined)
3. Held-out N ≥ 25 (otherwise we're noise-fishing)

### If pass
Add `bot/signals/sources/trade_flow_bayesian.py` returning the posterior
adjustment. Add as a *post-processing* step in `bot/signals/ensemble.py`
(NOT a parallel source — that would re-introduce double counting with
`momentum.py`). `momentum.py` deprecated to weight 0 in same change.
Behind `TRADE_FLOW_BAYESIAN=true` flag, default off.

### If fail
Document. Possible re-attempts: longer window, different prior (market mid
not ensemble), per-family fit, sklearn ML once we have ≥500 settlements.

### Tool
New `tools/bakeoff_step3_trade_flow.py` — pure-read SQL building the
flow-feature table, then numpy/scipy grid search. No production code yet.

### Pre-registration constraint
Grid is fixed in this doc. If results look bad, no expanding the grid post-hoc.

---

## What I'm NOT doing in this sprint

- **#5 cross-bracket portfolio** — deferred to Phase 2. Surface area in
  `trade.py`, `manage_positions`, `record_settlements`, `mm_inventory`,
  exposure caps. Needs a design doc first.
- **Live MM re-enable** — `WEATHER_MM_LIVE=false` stays. The whole point
  of #1 is to gather evidence on whether MM is the right shape at all.
- **Touching regime-conditional σ work** — that's its own track per the
  regime investigation. Coordinate by running #2 against pre-regime baseline.
- **Per-family ML calibration** — same data-volume problem as #3 but worse.
  Wait for ≥ 500 settled rows per family.

## Risk register

| Risk | Mitigation |
|------|------------|
| Stale local DB → analyses run on wrong data | All analyses run against fresh VPS pull each time |
| Shadow corruption pre-Apr-21 polluting `alpha_backtest` | All analyses filter `ts_decision_unix > 2026-04-21 UTC midnight` |
| Concurrent regime-σ rollout shifts close-edge baseline mid-analysis | Run #2 against pre-regime snapshot; rerun if regime ships before #2 lands |
| Over-fitting at small N | Pre-registered gate criteria + walk-forward + minimum-N floors per cell |
| Cherry-picking failed gates ("just one more grid point") | Grid + pass criteria fixed in this doc; no post-hoc expansion |
| `mm_quote` log format predates corruption fix → wrong PnL | Filter post-2026-04-21 only; verify `realized_pnl_cents` is non-zero on a spot check |

## Sequence and resume points

```
[done]  Pre-register gates (this doc)
[next]  Pull fresh VPS DB → /tmp/kalshi_trades_2026-04-28.db
[next]  Step #4: tools/bakeoff_step4_penny_tte.py → append result table here
[then]  Step #1: render_bakeoff_report() → append result here
[then]  Step #2: tools/bakeoff_step2_gfs_cdf.py → append result here
[then]  Step #3: tools/bakeoff_step3_trade_flow.py → append result here
[then]  Per-step: if pass, write production code behind flag and ship.
```

Each step is independently shippable. We don't need to wait for all four
analyses before acting on any one of them.
