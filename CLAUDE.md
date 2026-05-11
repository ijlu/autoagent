# Kalshi Prediction Market Trading Bot

## What This Is

An autonomous trading bot for Kalshi prediction markets. Runs as a **persistent daemon** on a DigitalOcean VPS: a supervisor process (`bot.daemon.main`) with a 60s cycle task plus event-driven pollers on their own threads. All trading logic lives in `trade.py` (~7K lines) invoked in-process by the daemon's `CycleRunner` — no more forking every 2 minutes.

**Owner:** Josh Lu (joshlu@a16z.com)
**VPS:** 45.55.79.193 (user: `kalshi`, path: `/home/kalshi/autoagent/`)
**Database:** `kalshi_trades.db` (SQLite, WAL mode, on VPS)
**Logs:** `/home/kalshi/autoagent/cron.log` (service uses `StandardOutput=append`, NOT journalctl)
**Service:** `kalshi-daemon.service` (persistent, `Restart=always`). Legacy `kalshi-bot.timer` oneshot units remain in `deploy/` for rollback but are not the current shape.

## Current Phase

**Phase 0 (signal validation) passed; Phase 1 (learning infra + event-driven weather MM) is the current work.**

- Phase 0 gate: per-family Brier on weather beat baseline by 4–8× on 5 families (KXHIGHMIA/CHI/AUS/LAX/NY). See [reports/BACKTEST_APR17.md](reports/BACKTEST_APR17.md).
- Phase 1 core items (atomic decision logging, settlement-driven learning population, Platt calibration in ensemble, METAR→`WeatherQuoter` wiring, directional shadow evaluator, deploy swap) have landed; the remaining work is the shadow-to-live graduated promotion gate and the T3 fills-ledger consumer migration. See §Phase 1 Work below.
- **Nothing trades live right now.** MM is disabled (all 11 weather series blocked in config; no non-weather MM). Directional is DRY_RUN. Safe Compounder has `SC_ENABLED=true` but is still gated by phase + DRY_RUN.
- Weather MM re-enable is **shadow-first**: log what `WeatherQuoter` would have posted, compare shadow-markout to shadow-settlement P&L over N settlements, flip to live only if the favorable markout (+4.7¢ historical) converts to realized P&L under the new event-driven cancel-replace path.

## Architecture

### Daemon (current)

`bot/daemon/main.py` is the supervisor. It:

1. Calls `init_db()` once → long-lived WAL connection shared across threads under `DB_WRITE_LOCK`
2. Starts pollers on their own threads (currently: METAR @ 30s; more coming)
3. Runs `Scheduler` on the main thread with periodic tasks:
    - `cycle` @ 60s → `CycleRunner.run_once()` → `trade.main(conn=shared_conn, close_conn=False)`
    - `kv_cleanup` @ 3600s → expire stale `kv_cache` rows
    - `health_log` @ 300s → log poller + cycle + scheduler stats
4. On SIGTERM/SIGINT → `Scheduler.stop()` runs `on_stop` hooks, pollers drain, DB closes, clean exit 0
5. On unhandled exceptions in pollers → caught; in cycle → caught by `CycleRunner`; in scheduler itself → process dies, systemd restarts

All cross-thread coordination goes through locks in `bot/daemon/locks.py`:
- `DB_WRITE_LOCK` — serializes SQLite writes (WAL tolerates concurrent reads)
- `API_LOCK` — serializes outbound Kalshi API calls (rate limit + RSA signing)
- `PIPELINE_STATS_LOCK` — serializes ensemble pipeline health counters

### Code Structure

```
trade.py                 — Cycle body (~7K lines). Called once per cycle by CycleRunner.
bot/
  config.py              — Env vars, constants, source weights, phase config, dynamic sizing, block lists
  db.py                  — init_db(), WAL setup, migrations, kv_cache helpers
  api.py                 — RSA-PSS auth, api_get/post/delete, rate limiter, API_LOCK
  core/
    money.py             — Canonical fee formulas (kalshi_maker_fee, kalshi_taker_fee)
    categorization.py    — Ticker → family/category mapping
    exposure.py          — Per-family / per-expiry exposure caps and sizing
    exit_model.py        — Directional exit evaluator (edge-decay + time backstops)
    sizing.py            — Thompson-sampled MM sizing (replaces shadow/canary/full step gate)
  daemon/                — THE MM ARCHITECTURE (replaces bot/market_maker/, deleted)
    __main__.py          — `python -m bot.daemon` entrypoint wrapper
    main.py              — Supervisor (scheduler + pollers + shared DB conn)
    scheduler.py         — Periodic-task scheduler with on_start/on_stop hooks
    poller_base.py       — Poller ABC (own thread, health stats, graceful stop)
    metar_poller.py      — METAR observations poller, emits TemperatureChange events
    stations.py          — Station catalogue + SERIES_TO_STATION map
    smart_gates.py       — Pre-quote gates (trajectory, volatility, spread sanity)
    weather_quoter.py    — Event-driven weather MM (on METAR change → cancel-replace)
    weather_handler.py   — METAR change → WeatherQuoter bridge (shadow/live routing)
    requote_triggers.py  — Material-change detector for cancel-replace decisions
    dispatcher.py        — Order write dispatcher (serializes mm_orders writes)
    fills_writer.py      — Canonical writer for the fills_ledger table (T3)
    forecast_cache.py    — Shared forecast cache (METAR + quoter + requote triggers)
    orchestrator.py      — WeatherDaemon integration layer
    cycle_runner.py      — Wraps trade.main() for in-process invocation
    locks.py             — DB_WRITE_LOCK, API_LOCK, PIPELINE_STATS_LOCK
  signals/
    ensemble.py          — get_independent_estimate(), source routing, weighted averaging,
                            correlated-group handling, pipeline health
    weather_ensemble.py  — Multi-source weather stitcher (METAR + NBM + HRRR + NWS + MADIS + AFD)
    family_routers.py    — Per-family source routing policy
    regime.py            — Market-regime classification (volatility, time-to-expiry bucketing)
    sources/
      weather.py             — Open-Meteo + Tomorrow.io + NOAA alerts (consolidated)
      metar_observations.py  — METAR real-time airport observations
      nws_point.py           — NWS point forecast
      ndfd_nbm.py            — National Blend of Models
      hrrr.py                — HRRR (high-res rapid refresh)
      madis.py                — MADIS mesonet
      afd.py                  — Area Forecast Discussion
      economics.py           — FRED + BLS + Cleveland Fed (consolidated)
      fedwatch.py            — CME FedWatch implied rate probabilities
      zq_futures.py          — Yahoo ZQ fed-funds futures → meeting probabilities
      _fomc_calendar.py      — Shared FOMC meeting calendar (fedwatch + zq_futures)
      adp_nfp.py             — ADP private payrolls
      gdpnow.py              — Atlanta Fed GDPNow
      commodity_futures.py   — CME / ICE futures
      crypto.py              — CoinGecko spot
      deribit_vol.py         — Deribit options → Black-Scholes probabilities
      prediction_markets.py  — Polymarket, Metaculus
      sports.py              — Odds API
      news.py                — Finnhub sentiment
      company.py             — SensorTower, analyst consensus
      series.py              — Intra-series CDF
      momentum.py            — Kalshi price momentum (endogenous, excluded from MM edge count)
      llm.py                 — GPT-4o-mini (fallback only)
  learning/
    adaptive_weights.py    — Bayesian source weight evolution
    calibration.py         — Platt/isotonic probability calibration (fit per cycle)
    active_feedback.py     — Synthesize learning into edge threshold + source gating
    bandit.py              — Exit decision bandit over graduated health bands
    category_scoring.py    — Per-family score aggregation
    timing_patterns.py     — Hour-of-day / time-to-expiry edge patterns
    postmortems.py         — Loss classification tagger
    edge_convergence.py    — Did our estimate beat market mid over time? (Phase 0 gate leg 2)
    alpha_log.py           — Atomic decision-time logger → alpha_backtest
    populate_from_alpha.py — Cascade settled alpha_backtest rows into learning tables
    directional_shadow.py  — Pre-trade gate for DRY_RUN directional evaluator
    shadow_testing.py      — Shadow-mode evaluator plumbing (weather MM, directional)
    shadow_promotion.py    — Shadow-vs-live P&L delta tracking
    mm_promotion.py        — Graduated shadow→canary→full family promotion gate
    bakeoff.py             — Competing-strategy bake-off evaluator
    self_modifier.py       — Persists tuned hyperparameters to learned_config
    fills_validator.py     — T3.1 dual-run validator (fills_ledger vs mm_processed_fills)
  scoring/
    market_scorer.py       — score_market, strategy-specific scoring
    filters.py             — Pre-scan filters (volume, spread, age, blocklist)
    four_factor.py         — Four-factor composite score (edge, confidence, liquidity, risk)
  arbitrage/
    bracket_arb.py         — Intra-series bracket arb
    correlation_arb.py     — Cross-market correlation arb
  observability/
    alerts.py              — Telegram notifications
    cost_tracker.py        — Per-source API cost / latency accounting
deploy/
  04_redeploy.sh              — Sync .py + bot/ + tests/ + .env to VPS, full import-check battery
  05_deploy_weather_daemon.sh — Daemon-specific deploy + systemd unit install
  kalshi-daemon.service       — systemd unit for persistent daemon (Restart=always)
  kalshi-weather-daemon.service — legacy weather-only daemon unit
tests/
  daemon/                — cycle_runner, scheduler, poller_base, smart_gates_trajectory,
                            dispatcher, requote_triggers, writer_ownership, station_registry,
                            forecast_cache, weather_handler, integration_{cadence,row_shape}
  signals/               — source + ensemble + routing coverage
                            (adp_nfp, commodity_futures, ensemble_family_routing,
                             family_routers, gdpnow, new_weather_sources, weather_ensemble)
  bot/                   — api_concurrency, db_wal
  test_{weather_daemon,weather_quoter,smart_gates,money,fee_calc,fedwatch_parser,
        positions,settlement_certainty,db_schema,exposure,sizing,exit_model,
        alpha_log,calibration,directional_shadow,populate_from_alpha,
        position_health_backfill,mm_promotion,shadow_promotion,bakeoff,
        backtest_comprehensive,weather_direction,config_no_drift,db_discipline,
        client_order_id_coverage,no_secrets_in_repo,writer_ownership}.py
backtest_comprehensive.py — 12-section per-family Brier / calibration / alpha analysis
reports/                 — Committed backtest and phase-gate reports
```

### Cycle Flow (every 60s, inside the daemon)

`CycleRunner.run_once()` → `trade.main(conn=shared, close_conn=False)`:

1. `compute_current_phase()` + `apply_phase_limits()` — track-record-driven sizing envelope (PHASE_CONFIG 1–5)
2. `compute_dynamic_sizing()` — scale MM/exit thresholds to total equity
3. `prune_stale_orders()` + `track_fills()` + `record_settlements()` — housekeeping + P&L
4. `_alpha_populate_all()` — cascade newly-settled `alpha_backtest` rows into `calibration` / `timing_patterns` / `edge_convergence` / `postmortems`
5. `manage_positions()` — graduated health-score exit with synthetic sell (sole exit path)
6. `compute_avoid_filters()` — loss-pattern avoidance filters
7. Adaptive learning: `compute_adaptive_weights`, `_cal_fit_and_persist` (Platt, persisted to `kv_cache`), `compute_category_edge_thresholds`
8. Advanced learning: `run_loss_postmortems`, `check_edge_convergence`, `record_timing_data`, `analyze_shadow_performance`
9. `compute_active_feedback()` — disable bad sources/hours, adjust `MIN_EDGE` via multiplier
10. Portfolio balance + check_limits (halt if over caps)
11. Scan markets (paginated `/markets?status=open`), filter parlays
12. `score_market()` per candidate → ensemble estimate + strategy selection
13. Correlation limits (`MAX_PER_CATEGORY`), per-family + per-expiry exposure caps (`bot/core/exposure.py`)
14. Per-family graduated sizing multiplier (shadow=0 / canary=0.5 / full=1.0 from `mm_promotion`)
15. `_eval_directional_shadow()` — block-list + kelly-zero + below-edge gate → logs to `alpha_backtest`
16. Order book depth check + Kelly-by-market sizing → place order (DRY_RUN respects phase)

No MM quoting path in `trade.main` — legacy MM code was deleted; weather MM runs entirely
event-driven through `WeatherQuoter` on the METAR poller thread. `mm_inventory` may still
hold pre-deletion positions that settle naturally; their carry value is subtracted from the
directional exposure budget.

### Event-driven path (between cycles)

Separate from the 60s cycle:

- `METARPoller` fetches station observations every 30s
- On a material temperature change it emits `TemperatureChange` → currently `on_result=None`
- **Phase 1 wires this to `WeatherQuoter.handle_temp_change()`** — which cancels stale quotes and reposts at the revised fair value, closing the latency gap with counterparties that have real-time METAR

This is the architectural reason Phase 1 lets us re-enable weather MM: counterparty speed matches, not just signal quality.

## Trading Modes

- **Market Making:** Post two-sided limit orders at `fair_value ± half_spread`. Earns spread minus fees. `post_only` orders, 110s expiration. Currently disabled — weather blocked per Phase 0 analysis, no non-weather targets.
- **Directional:** Buy when ensemble diverges from market price by ≥ `MIN_EDGE` + fees. Currently DRY_RUN. Phase 1 adds atomic decision logging to `alpha_backtest` for the market-mid gate evaluation that Phase 0 couldn't run.
- **Blocks for directional:** KXBTC, KXETH (ensemble Brier 0.76–0.94, catastrophically anti-calibrated), KXHIGHDEN (Brier 0.316 vs 0.244 baseline — worse than other weather families, likely KDEN METAR station quirks).

## Dynamic Position Sizing

All MM and exit thresholds scale with total equity (balance + portfolio value):
- `MM_ORDER_SIZE` ≈ 1% of equity / 50¢ ($1K → 10, $10K → 200)
- `MM_MAX_INVENTORY` = 5× order size
- Graduated exit trim thresholds scale proportionally
- Computed via `compute_dynamic_sizing()` at start of each cycle

## Synthetic Sell

The single exit path (`manage_positions`) uses **buy-opposite-side** instead of sell-same-side. Saves ~1.3¢/contract (taker → maker fee). Exiting a YES position = buy NO (limit, maker fee ~0.44¢) instead of sell YES (taker fee ~1.75¢).

Legacy exit paths `QA auto-liquidation` and `mm_liquidate_expiring` were removed during the daemon refactor — they no longer exist. All exits now flow through `manage_positions`.

## Graduated Position Exits

`manage_positions` is the sole exit policy. Health score 0–1 (edge 40%, trend 20%, time 15%, P&L 15%, confidence 10%):
- ≥0.65: HOLD
- 0.45–0.65: Trim 25–33%
- 0.30–0.45: Exit 50%
- 0.15–0.30: Exit 75%
- <0.15: Full exit

Edge-decay and time-based backstops fire within the same function (`edge_flipped`, `edge_decayed`, `time_exit`). Every exit sets an `exit_reason` string which is logged to `position_exits` for bandit training, and posts orders tagged with `client_order_id` prefix `mm_exit_` for the T3 fills-ledger source tagger.

Edge trend tracked in `kv_cache` (last 10 readings across cycles). Exit decisions logged to `position_health_log` for bandit training.

## Critical Conventions

### YES-Equivalent Cost Basis
`avg_entry` is always the YES-equivalent price:
- **Long YES** (net > 0): `avg_entry` = what we paid for YES
- **Short YES / Long NO** (net < 0): `avg_entry` = 100 − what_we_paid_for_NO

Settlement P&L:
- Result = YES: `pnl = net * (100 - avg_entry)`
- Result = NO: `pnl = -net * avg_entry`

### Bracket vs Threshold
- **Bracket (-B suffix):** Mutually exclusive outcomes. Probability = `CDF(upper) − CDF(lower)`, NOT simple above/below.
- **Threshold (-T suffix):** Simple above/below probability. NOT mutually exclusive.

### `mm_orders.fair_value_cents` stores P(YES), *not* per-order-side
The now-deleted MM writer (`mm_post_quotes`) stored the **same** `fair_value_cents` value on both the YES and NO sides of a pair (empirically verified on the Apr 17 live DB: `KXFED-27APR-T2.00` YES fv=69 price=61, NO fv=69 price=25). The sum of prices (61+25=86) proves prices were correct; storage was simply P(YES) for both rows.

CLAUDE.md previously described a "per-order-side" convention that the code never implemented — that was aspirational. The v1 Apr 17 backtest (Brier 0.50) was broken because it assumed per-side storage. The v2 fix normalizes by reading `CASE WHEN side='yes' THEN fv ELSE 100-fv END` — this turns the buggy-but-consistent P(YES) storage into correct per-side values on read.

Rules for any future code:
- **Readers** aggregating across mixed YES/NO rows MUST apply `CASE WHEN side='yes' THEN fv ELSE 100-fv END`.
- **New writers** should continue storing P(YES) regardless of side, to preserve compatibility with existing data. Or introduce a new column with a clearly-documented convention — don't silently change the semantics of `fair_value_cents`.

### `opportunity_log.ensemble_prob` *is* P(our-side)
Different convention from `mm_orders.fair_value_cents` — this one is genuinely per-order-side. `score_market` computes `indep_prob = ensemble_prob if side=='yes' else (1 - ensemble_prob)` before logging. So reads can use `ensemble_prob` directly, or flip back to P(YES) via `CASE WHEN side='yes' THEN ensemble_prob ELSE 1 - ensemble_prob END`.

### client_order_id
Must start with `mm_` and must NOT contain periods (Kalshi rejects). Use `.replace('.', '_')` for decimal tickers.

### Fixed-Point Parsing
Kalshi returns `*_fp` and `*_dollars` as strings. Always `round(float(...))`, never `int(float(...))` — off-by-one from floating point otherwise.

## Data Sources (Ensemble)

20+ sources, weighted and averaged in `bot/signals/ensemble.py`. `SOURCE_WEIGHTS` in `bot/config.py`. Correlated sources count as ~1 effective source, not N. Edge threshold scales with effective independent count: 3+ → 5%, 2 → 7%, 1 → 10–12%.

| Source | Weight | Category | Notes |
|--------|--------|----------|-------|
| Weather ensemble (router) | 0.95 | Weather | Combined router output; dominates when present |
| METAR observations | 0.90 | Weather | Real-time airport station data, highest single-source weather weight |
| Odds API | 0.85 | Sports | |
| HRRR | 0.85 | Weather | High-res short-range (tightest sigma) |
| ZQ futures | 0.85 | Economics | Yahoo 30-day fed funds futures → meeting probabilities |
| Tomorrow.io | 0.82 | Weather | 7-day reliable horizon |
| Open-Meteo | 0.80 | Weather | Correlated with Tomorrow.io |
| FedWatch | 0.80 | Economics | FRED-based synthetic probabilities |
| NBM | 0.80 | Weather | NOAA National Blend of Models |
| ADP/NFP | 0.80 | Economics | ADP 2-day lead on BLS NFP |
| NWS Point | 0.78 | Weather | Official US forecast |
| GDPNow | 0.78 | Economics | Atlanta Fed nowcast |
| Polymarket | 0.75 | Prediction | Correlated with Metaculus |
| NOAA alerts | 0.75 | Weather | Correlated with weather group |
| Series | 0.75 | Structural | Intra-series CDF |
| Cleveland Fed | 0.72 | Economics | |
| Metaculus | 0.70 | Prediction | |
| Crypto (CoinGecko+Deribit) | 0.65 | Crypto | **Blocked for directional — Brier 0.76–0.94** |
| Company KPI | 0.65 | Company | |
| MADIS | 0.60 | Weather | Mesonet/citizen stations, noisier |
| SensorTower | 0.55 | Company | |
| Commodity Futures | 0.55 | Commodity | CME/ICE → CPI transmission |
| FRED | 0.50 | Economics | Correlated with BLS |
| BLS | 0.50 | Economics | Correlated with FRED |
| AFD | 0.50 | Weather | Forecaster discussion text |
| Finnhub | 0.30 | News | |
| Momentum | 0.15 | Structural | Endogenous — not counted as real source for MM |
| LLM (GPT-4o-mini) | 0.15 | Fallback | Lowest weight |

Weather sources are routed through `bot/signals/weather_ensemble.py` which handles the correlated-group stitching before the ensemble sees them.

### Weather MM Gating (historical note)
Pre-daemon, weather MM required fresh (<10 min) METAR to quote. Phase 1's event-driven `WeatherQuoter` replaces this static gate: the quoter only exists while the poller is healthy, and it requotes on every material temperature change rather than gating on cache age.

## Database Schema (key tables)

Core trading:
- `trades` — directional trade records
- `settlements` — settled market outcomes with P&L
- `sessions` — per-cycle supervisor stats (balance, scanned, halts)
- `position_exits` — exit orders with entry/exit prices and reason
- `position_health_log` — health scores for bandit learning (back-filled with settlement result on resolve)
- `kv_cache` — persistent key-value with TTL (Platt curve, edge-trend series, pipeline stats)
- `learned_config` — self-tuned hyperparameters persisted by `bot/learning/self_modifier.py`

Market making (mostly legacy — MM writer deleted; `weather_mm_shadow` is the active MM table):
- `mm_orders` — pre-deletion MM orders (ticker, side, price, fair_value_cents; see Conventions — stores P(YES))
- `mm_inventory` — residual position per ticker (drains naturally)
- `mm_processed_fills` — deduplicated fills with fee_cents
- `mm_sessions` — per-cycle stats

Decision logs:
- `opportunity_log` — every candidate seen per cycle, traded and rejected (ensemble_prob is P(our-side))
- `decision_log` — full audit trail (source estimates, four-factor scores, regime, feedback)
- `strategy_journal` — decision log with accept/discard reasons
- `alpha_backtest` — atomic decision-time log for Phase 0 gate leg 2 (MM quote, directional shadow/live, weather shadow) with settlement back-fill
- `weather_mm_shadow` — every quote `WeatherQuoter` would have posted (FV, bid/ask, gate decision, METAR context) — joined to `settlements` for shadow-to-live P&L

Learning loop:
- `calibration` — Platt/isotonic correction fit from settled outcomes (populated each cycle)
- `timing_patterns` — hour-of-day / day-of-week edge patterns (populated each cycle)
- `edge_convergence` — did our estimate beat market mid? (Phase 0 gate leg 2)
- `loss_postmortems` — tagged loss attributions
- `hyperparam_shadow` — actual-vs-alternative hyperparameter P&L
- `pipeline_health` — per-cycle source health counters
- `weather_source_weights` — per-series Bayesian weights for weather sub-ensemble
- `weather_forecast_snapshots` — per-source component estimates for post-hoc calibration

Promotion / tuning:
- `promotion_events` — shadow→canary→full state transitions with metrics at decision time
- `threshold_proposals` — weekly tuner-proposed threshold changes (audit log)

T3 canonical fills:
- `fills_ledger` — append-only, Kalshi-`trade_id`-keyed fill ledger (see [reports/T3_FILLS_LEDGER_SCOPING.md](reports/T3_FILLS_LEDGER_SCOPING.md)). In progress.

## Deploy

```bash
cd ~/autoagent && bash deploy/04_redeploy.sh 45.55.79.193
```

Rsyncs `.py`, `bot/`, `tests/`, and `.env` to VPS, runs the full daemon-module import-check battery, resets pipeline health, restarts the `kalshi-daemon.service`.

## Environment Variables (.env)

```
KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
FRED_API_KEY, BLS_API_KEY, BEA_API_KEY, CENSUS_API_KEY, EIA_API_KEY, TOMORROW_API_KEY
MM_MIN_VOLUME=25
SC_ENABLED=true                   # Safe Compounder (Phase 4sc). Default ON; only live if phase + DRY_RUN allow.
WEATHER_MM_LIVE=false             # Phase 1 shadow-to-live gate (flip only after shadow data proves out)
FORCE_PHASE=                      # override PHASE_CONFIG track-record gate (empty = auto)
DIRECTIONAL_BLOCKLIST=KXBTC,KXETH,KXHIGHDEN   # families blocked for directional entry
MAX_PORTFOLIO_EXPOSURE_RATIO=0.50 # global exposure throttle (directional + SC + legacy MM)
MAX_FAMILY_EXPOSURE_RATIO=0.25    # per-family cap as fraction of equity
MAX_EXPIRY_EXPOSURE_RATIO=0.075   # per-settlement-date cap (e.g. one FOMC day)
EXIT_EDGE_DECAY_RATIO=0.33        # exit when remaining_edge < entry_edge × this
EXIT_TIME_BACKSTOP_HOURS=0.25     # near-expiry window for edge-below-threshold exit
EXIT_TIME_BACKSTOP_EDGE_ABS=0.02  # |remaining_edge| threshold inside that window
EXIT_STALE_HOLD_HOURS=24.0        # stale-hold backstop (flat-or-deteriorating)
MM_SIZING_TARGET_EDGE_CENTS=2.0   # target realized-per-fill cents for full-size quoting
```

RSA key for Kalshi API auth: `.kalshi_private_key.pem` (gitignored; MUST NOT be committed).

## Known Bug Patterns (Regression Watchlist)

Fixed; re-check on every touch of the relevant code. Each pattern is pinned by a test — if you change behavior here, update the test in the same PR.

1. `client_order_id` containing periods → 400 Bad Request — `tests/test_client_order_id_coverage.py`, `tests/test_weather_quoter.py::test_client_order_id_no_periods`
2. `_apply_trade()` short-close P&L using `(100 − avg_entry) − price` instead of `avg_entry − price` — `tests/test_money.py`
3. `record_settlements()` not subtracting `fee_cost`, or `won = revenue > 0` instead of `profit > 0` — `tests/test_money.py`, `tests/test_settlement_certainty.py`
4. Any exit path zeroing inventory without confirmed settlement (original offender `mm_liquidate_expiring()` was removed; watch for recurrence in `manage_positions` or future exit policies) — `tests/test_inventory_zero_settlement_only.py`
5. Fixed-point parsing with `int(float(...))` instead of `round(float(...))` — `tests/test_fixed_point_parsing.py`
6. `cancel_failed` orders excluded from exposure headroom — `tests/test_exposure.py`
7. Resting order exposure query silently swallowing exceptions (fail closed instead) — `tests/test_exposure.py`
8. Tomorrow.io forecasts beyond 7-day reliable horizon — `tests/signals/test_tomorrow_horizon.py`
9. Correlated sources (weather+weather, FRED+BLS) counted as fully independent (also covers a single source like `fred` that sits in 4 correlated groups — must count once, not 4×) — `tests/signals/test_correlated_double_count.py`
10. MM spread not checked against expected maker fees — `tests/test_mm_spread_fee_floor.py`
11. Any exit/settlement path not subtracting fees (original offender `mm_liquidate_expiring()` was removed; watch in `manage_positions` and `record_settlements`) — `tests/test_money.py`, `tests/test_settlement_certainty.py`
12. Cache isolation: `trade.py._CACHE` vs `bot.api._CACHE` (daemon now shares a DB-backed cache where possible; both module caches must be bounded `TTLCache`) — `tests/test_cache_bounded.py`, `tests/bot/test_api_concurrency.py`
13. `mm_orders.fair_value_cents` aggregated assuming per-side storage when it's actually P(YES) on both rows (caused the Apr 17 v1 backtest inversion). Aggregations must declare their handling: single-side `WHERE`, `CASE WHEN side` normalisation, or an inline `# fv-mixed-side-ok` ack marker. `opportunity_log.ensemble_prob` has the opposite convention (per-side) — check Conventions section before aggregating either. — `tests/test_fair_value_cents_readers.py`
14. Daemon pollers writing to SQLite without holding `DB_WRITE_LOCK` (causes `database is locked` under contention) — `tests/test_db_discipline.py`
15. Any code path assuming the DB connection is process-local — daemon shares one connection across threads — `tests/test_db_discipline.py`, `tests/bot/test_db_wal.py`

## Audit History

- **2026-04-08:** External audit, 17 P0/P1/P2 issues, all fixed.
- **2026-04-10:** Internal audit, 2 critical + 5 high + 6 medium. Fixed `_apply_trade()` short-close inversion, `cancel_failed` exposure blindspot, MM liquidation fee subtraction, MM spread fee floor.
- **2026-04-13:** Replay backtest, 813 fills + 76 settlements. 90.8% loss rate driven by weather (−$204 of −$220). Deployed METAR gating + graduated exits.
- **2026-04-14:** Full code-export audit (`AUDIT_EXPORT_2026-04-14.md`, gitignored).
- **2026-04-16:** Weather MM fully blocked (commit `0879bde`); `backtest_comprehensive.py` shipped; `opportunity_log` ensemble data capture fixed.
- **2026-04-17:** Phase 0 go/no-go backtest ([reports/BACKTEST_APR17.md](reports/BACKTEST_APR17.md)). Discovered + fixed the per-side `fair_value_cents` averaging bug (v1 Brier 0.50 → v2 0.258). Five weather families pass gate by 4–8×.
- **Recurring audit:** Mon/Thu 9am via Cowork scheduled task (`kalshi-bot-audit`).

## Current Performance (as of 2026-04-16, from Apr 17 backtest DB)

- **Total equity:** ~$982
- **MM fills:** 922 (across the full observed window)
- **Settlements:** 116 (13 wins, 103 losses = 11.2% WR; CI [6.7%, 18.2%], z = −8.36 vs 50%)
- **Net settlement P&L:** ~−$400
- **Adverse selection at entry:** +6.12¢ average markout, 99.9% favorable — **no entry problem**
- **Signal alpha (per-family Brier):** 5 weather families 0.09–0.21 vs baseline ~0.24 (passes by 4–8×)
- **Catastrophic calibration failures:** KXBTC (Brier 0.937), KXETH (0.762), KXHIGHDEN (0.316)
- **Active inventory:** 55 positions, ~$173 exposure, 95% KXFED (settles naturally)
- **Directional:** DRY_RUN throughout
- **Root cause of losses:** MM structure held through directional moves; favorable markout never converted to P&L. Not a signal problem.

## Phase 1 Work

1. ✅ `bot/daemon/` architecture committed (checkpoint `04ca78f`)
2. ✅ `alpha_backtest` table + atomic decision-time logging (`bot/learning/alpha_log.py`)
3. ✅ Settlement-driven learning population: `calibration`, `timing_patterns`, `edge_convergence`, `position_health_log`, `postmortems` (`_alpha_populate_all` called per cycle; settlement back-fill in `record_settlements`)
4. ✅ Platt correction fit per cycle from `calibration`, persisted to `kv_cache`, read by ensemble (`_cal_fit_and_persist`)
5. ✅ `METARPoller` → `WeatherChangeHandler` → `WeatherQuoter.handle_temp_change()` (`bot/daemon/weather_handler.py`)
6. ✅ Directional shadow evaluator (DRY_RUN, logs to `alpha_backtest`, blocks KXBTC/KXETH/KXHIGHDEN via `DIRECTIONAL_BLOCKLIST`)
7. ✅ Deploy pipeline swap: `kalshi-bot.timer` oneshot → `kalshi-daemon.service` persistent (`deploy/04_redeploy.sh`)
8. ✅ Shadow-to-live gate: graduated SHADOW→LIVE_CANARY→LIVE_FULL promotion per series. Promotion gate (`evaluate_mm_promotion`), graduation gate (`evaluate_mm_graduation`), kill-switch demotion (`evaluate_mm_kill_switch`), and per-sweep orchestrator (`run_mm_promotion_sweep`) all in `bot/learning/mm_promotion.py`; wired into the daemon as the daily `mm_promotion_sweep` task in `bot/daemon/main.py` (helper `_run_mm_promotion_sweep`). `WEATHER_MM_LIVE` remains the master kill-switch (default `false`); `MM_BLOCKED_SERIES` is the per-series blocklist.
9. ✅ T3 canonical fills ledger — scoping at [reports/T3_FILLS_LEDGER_SCOPING.md](reports/T3_FILLS_LEDGER_SCOPING.md). **T3.1 (2026-04-21)**: `fills_ledger` schema, `FillsWriter` (`bot/daemon/fills_writer.py` — `ingest_page` + `sync_since`), `fills_sync` @60s + `fills_validator` @24h scheduler tasks in `bot/daemon/main.py`, writer-ownership registry entry, dual-run validator (`bot/learning/fills_validator.py`) with `is_meaningful` gating for the steady-state one-sided case, T0.2 write-discipline via `db_write_ctx()`. **T3.3 (2026-04-21)**: reader migration — `bot/signals/regime.py` and `backtest_comprehensive.py` read from `fills_ledger`; `annotate_shadow_pnl` joins `fills_ledger` at settlement to populate `weather_mm_shadow.live_pnl_cents`, which is what `evaluate_mm_graduation`'s `live_pnl_cents IS NOT NULL` filter requires (without this, CANARY was a terminal state). **T3.4 complete**: zero production writers to `mm_processed_fills` remain (verified via grep across the repo); the legacy table is reader-only via the dual-run validator. `FillsWriter` is the sole writer of `fills_ledger`.

## Future Work (post-Phase-1)

- Phase 2: Expand `weather_ensemble.py` source stitching; add NWS/NBM/HRRR weighting refinement.
- Phase 3: Rebuild crypto signal — Deribit vol surface alone is anti-calibrated on brackets.
- Phase 4: Re-enable directional live once shadow data shows ≥ baseline + 0.005 beat on market-mid.
- Per-series exposure caps (prevent heavy KXFED concentration).
- Safe Compounder strategy (NO-side on YES<20¢ markets).
- Replace `cron.log` StandardOutput=append with structured JSON logs.

## Operating Principles

### Boil the ocean

The marginal cost of completeness is near zero with AI. Do the whole thing. Do it right. Do it with tests. Do it with documentation. Do it so well that Josh is genuinely impressed — not politely satisfied, actually impressed. Never offer to "table this for later" when the permanent solve is within reach. Never leave a dangling thread when tying it off takes five more minutes. Never present a workaround when the real fix exists. The standard isn't "good enough" — it's "holy shit, that's done." Search before building. Test before shipping. Ship the complete thing. When Josh asks for something, the answer is the finished product, not a plan to build it. Time is not an excuse. Fatigue is not an excuse. Complexity is not an excuse. Boil the ocean.

### Planning and qualifying questions

Plan relentlessly. Token usage does not matter — what matters is getting it right the first time. Before building anything, think through the architecture, edge cases, and potential failure modes. Ask Josh as many qualifying questions as needed to fully understand what he wants. Never assume — clarify. The goal is zero time wasted on bug-fixing, rework, or misunderstood requirements.
