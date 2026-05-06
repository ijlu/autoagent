# Phase 1 — ensemble market evaluator (spec)

**Audience:** a fresh agent session picking up this work cold. This doc is
self-contained; no need to read the parent thread.

**Author:** Claude (drafted 2026-05-06 in the snapshotter-expansion session).
**Status:** draft, awaiting Josh sign-off + ≥14 days of snapshotter data.

---

## 1. Goal

Build a **measurement framework** that quantifies, per Kalshi weather market,
how well our existing ensemble predicts vs. the market mid — and decomposes
the result into per-source contributions.

Without this, every subsequent improvement (new sources, bias corrections,
hierarchical model) is a guess. With it, every improvement gets a
before/after on the same fixed evaluation set.

**Non-goals**

- This phase does not modify the ensemble. It only measures it.
- This phase does not pick which markets to trade live. It produces the
  evidence that future promotion decisions will reference.
- This phase does not propose source weight changes. It surfaces where
  weights look wrong; the *fixing* happens in Phase 2.

## 2. Why now (and why not sooner)

The bot has run a weather ensemble for months but never had a per-market
out-of-sample scorecard. The Phase 0 backtest (April 17) was per-family
Brier on settled positions — that gave us "do we have signal" for the
6 traded cities, but not:

- Per-source attribution (which source drove the win/loss?)
- Calibration curves (does our 70% prediction hit 70% of the time?)
- Edge-vs-market-mid (do we beat the implicit consensus, or just the
  uniform prior?)
- Coverage on the 14 candidate cities (we have zero history on them)

The 2026-05-06 deploy started the `kalshi_market_snapshots` poller for
all 40 weather series (20 HIGH × 20 LOW cities). Once that table has
≥14 days of bid/ask data — joined to settled outcomes — we can
back-test the ensemble against the full 40-market universe.

Earlier than that and the per-market n is too small for non-noise
inference.

## 3. Inputs

### 3.1 Required tables (all live in `kalshi_trades.db`)

| Table | What it gives us | Owner |
|---|---|---|
| `kalshi_market_snapshots` | bid/ask + depth + status per ticker, 60s cadence | `bot/daemon/market_snapshotter_poller.py` |
| `weather_forecast_snapshots` | per-source forecast components per ticker per snapshot | `bot/signals/weather_ensemble_v2.py` |
| `alpha_backtest` | atomic decision-time log (cross-bracket + directional) | `bot/learning/alpha_log.py` |
| `settlements` | per-ticker outcome + P&L | `trade.py::record_settlements` |
| `mm_orders` | quote history (legacy MM, drains naturally) | n/a (pre-deletion writer) |
| `weather_mm_shadow` | shadow MM quote history | `bot/daemon/weather_quoter.py` |

### 3.2 Ensemble entry points

Both signatures: `(ticker: str, market_data: dict, yes_ask: Optional[float]) -> (prob: Optional[float], source_tag: Optional[str])`.

- **v1** — `bot/signals/weather_ensemble.py::predict` — METAR-only logistic CDF.
- **v2** — `bot/signals/weather_ensemble_v2.py::predict_v2` — Gaussian
  ensemble, multi-source. **Does not currently handle KXLOWT* tickers**
  (`is_weather_ticker` check at line ~1517 only accepts KXHIGH /
  KXHMONTHRANGE / KXHURR prefixes). Phase 1 should treat that as a
  *finding* — measure HIGH coverage now, surface the LOW gap as a
  Phase 2 todo. Don't fix it inside the eval tool.

Toggle via `WEATHER_ENSEMBLE_V2=true` env var.

### 3.3 Snapshot replay

The eval tool needs to reconstruct, for each settled bracket, what the
ensemble *would have predicted* at multiple timestamps before settlement.

Two replay modes:

1. **From `weather_forecast_snapshots`** — preferred, has per-source
   components captured at decision time. Fast, deterministic.
2. **From scratch** — call `predict_v2(ticker, market_data)` with stored
   bid/ask + cached forecast data. Slow, requires reconstructing
   `market_data` from `kalshi_market_snapshots`. Use only when
   `weather_forecast_snapshots` is missing.

Default to (1). Mode (2) is fallback with a `WARNING: replay-from-scratch`
flag in the report.

## 4. Outputs

### 4.1 Single artifact: `reports/ENSEMBLE_EVAL_<YYYY-MM-DD>.md`

Five sections:

#### §1. Per-market scorecard

One row per (series, ticker_age_bucket). Sorted by **ensemble vs market-mid
Brier delta**, descending.

```
series      | n  | brier_us | brier_mkt | Δ      | brier_unif | edge_avg | win%_us | win%_mkt
KXHIGHTPHX  | 89 | 0.184    | 0.221     | -0.037 | 0.250      | +3.4¢    | 67%     | 56%
KXHIGHMIA   | 76 | 0.231    | 0.218     | +0.013 | 0.250      | -0.8¢    | 51%     | 53%
...
```

Columns:
- `n` — settled brackets in eval window
- `brier_us` — ensemble Brier (post-calibration, the price we'd actually
  trade against)
- `brier_mkt` — market mid implied probability Brier (the "consensus")
- `Δ` — `brier_us − brier_mkt` (negative = we beat market)
- `brier_unif` — uniform 50/50 baseline (sanity floor)
- `edge_avg` — mean (our_p − market_mid_p) × 100¢, signed by our side
- `win%_us` — empirical win rate when we'd have taken the trade at our
  threshold
- `win%_mkt` — what win rate the market mid implied

#### §2. Per-source attribution per market

For each market in §1, decompose the prediction into per-source contributions.

```
KXHIGHTPHX (n=89, Δ=−0.037)
  source       weight  mean_bias°F  rmse°F  contribution_to_Δ
  HRRR         0.85    +0.12        2.31    -0.012
  NBM          0.80    -0.41        2.89    -0.008
  Tomorrow.io  0.82    +1.84        3.42    +0.005   (degrades us)
  ...
```

A source with `contribution_to_Δ > 0` is *hurting* the ensemble for that
market — Phase 2 candidate for down-weighting or disablement.

#### §3. Calibration curves

10-bucket reliability diagram per market AND aggregated. Each bucket: how
many predictions fell in [0.0, 0.1], [0.1, 0.2], ..., and what fraction
actually settled YES.

ASCII reliability:

```
Aggregate calibration (n=4517)
  pred_bucket    expected   actual   diff    n
  0.0–0.1        0.05       0.04     -0.01    412
  0.1–0.2        0.15       0.18     +0.03    589
  ...
```

A monotonic-but-shifted curve = miscalibrated (Platt-fixable). A
non-monotonic curve = something's wrong; investigate.

#### §4. Time-decay skill curve

Per-source-per-market skill at multiple lead times:

```
KXHIGHTPHX skill by lead time (RMSE °F)
  lead    HRRR   NBM   Tom    METAR  ensemble
  24h     3.2    3.8   4.1    n/a    2.8
   8h     2.1    2.6   2.9    1.8    1.6
   2h     1.4    1.7   2.0    0.9    0.8
   30m    0.7    0.9   1.1    0.4    0.4
```

This tells Phase 2 *when* each source matters. METAR dominates near
settle; NWP dominates 12h+ out.

#### §5. Edge-vs-fee analysis

For each market: what fraction of the ensemble's "edge" survives Kalshi's
fee structure?

```
KXHIGHTPHX
  predicted edge (avg)              +3.4¢
  Kalshi maker fee (avg observed)   -0.44¢
  Kalshi taker fee (avg observed)   -1.75¢
  spread cost (round-trip avg)      -1.20¢
  net edge (maker)                  +1.76¢
  net edge (taker)                  -0.49¢   ← not tradeable as taker
```

Markets with `net edge (taker) ≤ 0` are not directional candidates;
markets with `net edge (maker) ≤ 0` aren't even MM candidates.

This is the section that *answers Josh's framing*: "edge has to exist
AND be big enough to be profitable net of fees."

### 4.2 Machine-readable companion

`reports/ENSEMBLE_EVAL_<YYYY-MM-DD>.json` — same data, structured. So
Phase 2 work can `diff` two JSON files to verify "did this source
addition actually move the per-market Brier on the same eval set."

## 5. Scope discipline

### Things to do

- Read snapshotter + settlement + forecast_snapshots tables
- Run replay mode (1) for every (series, settled-bracket) pair in window
- Compute Brier, RMSE, calibration, edge, fee-net-edge
- Write `reports/ENSEMBLE_EVAL_*.md` and `.json`

### Things NOT to do

- Don't modify `bot/signals/weather_ensemble*.py` — measurement, not improvement.
- Don't write to any DB table — read-only.
- Don't propose new source weights. Surface the data; humans + Phase 2 decide.
- Don't try to fit a hierarchical Bayesian model. That's Phase 3.
- Don't add new data sources. The eval should run on what we have today.
- Don't try to fix the v2 ensemble's `is_weather_ticker` filter for LOW
  markets. Surface as finding; defer to Phase 2.

## 6. Implementation

### 6.1 File layout

```
tools/
  ensemble_market_evaluator.py   # main entry — produces the report
bot/
  evaluation/                    # NEW package, read-only analysis
    __init__.py
    replay.py                    # snapshot replay logic (mode 1 + 2)
    metrics.py                   # Brier, RMSE, calibration buckets
    fee_model.py                 # per-Kalshi-bracket fee accounting
    attribution.py               # per-source contribution decomposition
    report_writer.py             # markdown + JSON generation
tests/
  evaluation/                    # mirror structure
    test_replay.py
    test_metrics.py
    test_fee_model.py
    test_attribution.py
    test_report_writer.py
```

`bot/evaluation/` is a new top-level package because it's substantial
enough to want internal modularity, and because keeping eval code out of
`bot/learning/` (which is hot path) prevents accidental coupling.

### 6.2 Tool invocation

```bash
PYTHONPATH=. python3 tools/ensemble_market_evaluator.py \
    --window-days 14 \
    --series-set all \
    --replay-mode prefer-snapshots \
    --output reports/ENSEMBLE_EVAL_2026-05-20.md
```

Flags:
- `--window-days N` — how far back to look (default 14, max bounded by
  snapshotter retention)
- `--series-set {all,traded,candidate}` — filter the 40 series
- `--replay-mode {prefer-snapshots,from-scratch,both}` — mode (1) vs (2);
  `both` runs both and asserts they agree (debug mode)
- `--ensemble {v1,v2,both}` — which ensemble to evaluate

### 6.3 Replay correctness check

Add a `--replay-mode both` mode that runs (1) and (2) on the same
markets and asserts the predictions match within ε. Catches drift between
snapshot capture and live ensemble code. Run weekly; fail the report if
they disagree.

## 7. Acceptance criteria

The eval tool is "done" when:

1. ✅ It runs end-to-end without errors on the 6 currently-traded series with
   ≥14 days of pre-existing settlement data (we have this today).
2. ✅ It produces all 5 report sections per §4.1.
3. ✅ Replay mode (1) and (2) agree within `|Δ_brier| < 0.005` on a 30-bracket
   sample (regression test).
4. ✅ Tests cover the metrics module to ≥90% line coverage and assert
   correctness against known-answer fixtures (Brier of {0.0, 0.5, 1.0}
   prediction with various outcomes; calibration of perfectly-calibrated
   forecaster).
5. ✅ Fee model matches `bot/core/money.py::kalshi_maker_fee /
   kalshi_taker_fee` exactly on 100 random (price, contracts) tuples.
6. ✅ Doesn't take >5 min wall clock for `--window-days 30 --series-set all`.

## 8. Open questions (decide before implementing)

These need a human call. Don't pick silently.

### Q1. How to define "the market mid prediction"?

Options:
- a. Average of `(yes_bid + yes_ask) / 2` across all snapshot rows in the
  ticker's lifetime. Weighted by what?
- b. Mid at a fixed lead time (e.g., 8h pre-settle). Requires picking
  a canonical evaluation timestamp.
- c. Mid at the moment we'd have decided to trade (pulled from
  `alpha_backtest.ts_decision`).

Recommendation: (c) when an `alpha_backtest` row exists; (b) at 4h
pre-settle when it doesn't. Document the choice in the report.

### Q2. How to handle settled-but-no-bid-history brackets?

Some settled brackets resolved before the snapshotter started, OR were
out-of-spread the whole time (one side at 1¢, other side bidless). Those
can't contribute to Brier-vs-market because there's no market mid.

Options:
- a. Drop them. Reduces n; transparent.
- b. Use 50/50 prior. Inflates Brier denominator with floor.
- c. Use the most recent bid we have, even if stale. Risk of bias.

Recommendation: (a). Report `n_dropped` in §1.

### Q3. v1 vs v2 reporting

We have two ensembles in the codebase. Run both? Side-by-side?

Recommendation: run **v2 only** (it's the production path under the
WEATHER_ENSEMBLE_V2 flag). Add v1 as a `--ensemble both` opt-in for
historical comparison.

### Q4. KXLOWT* coverage

v2 doesn't currently predict LOW markets. Phase 1 should:
- a. Skip them entirely (n=0 for all 20 LOW markets)
- b. Hack a v2 wrapper that flips the temperature inequality and reuses
  the HIGH math
- c. Run v1 only on LOW markets (v1 doesn't have the prefix gate)

Recommendation: (a). Surface the LOW gap as a clear Phase 2 deliverable.
Hacking around it (b/c) creates ambiguity in the comparison set.

### Q5. Per-source attribution methodology

Two reasonable approaches:
- **Leave-one-out**: re-run the ensemble with each source dropped, see
  how Brier changes. Computationally heavy but theoretically clean.
- **Linear decomposition**: assume ensemble probability is a weighted sum
  (it's roughly that for the v2 Gaussian path); attribute each source's
  contribution by its weighted residual. Cheap; approximate.

Recommendation: linear decomposition for the §2 report (cheap, runs on
every eval). Add a `--attribution leave-one-out` flag for deep
investigation when something looks wrong.

### Q6. Calibration: aggregate or per-market?

Reliability diagrams need n ≥ ~50 per bucket to be meaningful. Per-market
n at 14 days is ~10-20 — not enough for 10 buckets per market.

Recommendation: aggregate-only for calibration in §3. Per-market we just
report `mean_residual` and `mean_abs_residual` as scalars in §1.

## 9. Data prereqs (gating the start of Phase 1)

Don't start implementation until ALL of these hold:

1. **Snapshotter has ≥14 days of data** for at least the 6 traded series.
   - Check: `SELECT MIN(ts) FROM kalshi_market_snapshots WHERE
     series_ticker IN (legacy 6)` returns ≤ now − 14 × 86400.
2. **`weather_forecast_snapshots` has rows for the same window.**
   - Check: matching `MIN(ts_unix)`.
3. **`settlements` has ≥30 settled rows** in the window.
   - Check: `SELECT COUNT(*) FROM settlements WHERE recorded_at >=
     now() − INTERVAL 14 days AND ticker LIKE 'KXHIGH%'`.
4. **No active code change to `bot/signals/weather_ensemble_v2.py`** in
   the eval window — otherwise replay mode (1) and (2) will disagree
   for legitimate reasons. Run `git log --since "14 days ago" --
   bot/signals/weather_ensemble_v2.py` and check.

If any prereq fails, defer Phase 1 implementation. Track the gap as a
data-collection issue, not an implementation issue.

## 10. What success looks like (for the agent picking this up)

When you finish:

- A new operator can run `tools/ensemble_market_evaluator.py` with
  reasonable defaults and get a report in <5 min.
- The report tells them, for each of the 40 weather markets:
  - Do we have signal? (Brier vs uniform)
  - Do we beat the market? (Brier vs mid)
  - Is it tradeable? (edge net of fees)
  - Where is the signal coming from? (per-source attribution)
- Phase 2 starts with the report in hand: it identifies the 3-5 markets
  where the ensemble is *losing to market mid* and the 3-5 sources that
  are *negative-attributing* across multiple markets. Those are the
  Phase 2 fix list.

## 11. Hand-off note from drafter

This is a measurement-only task. Resist the urge to fix anything you find
mid-implementation — surface every "huh, that looks wrong" as a §2/§3/§4
report finding, not as a code change. The whole point of Phase 1 is to
build a stable evaluation set so Phase 2/3 changes can be measured
*against* it. If you change the ensemble while building the eval, you've
contaminated your own baseline.

If you discover that a prereq from §9 isn't satisfied — *stop and surface
it*. Don't lower the bar to make implementation possible. The data
gating is real and the eval is worse-than-useless on insufficient data.

— Claude (drafted in the city-expansion + snapshotter session, 2026-05-06)
