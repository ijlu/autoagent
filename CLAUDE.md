# Kalshi Prediction Market Trading Bot

## What This Is

An autonomous trading bot for Kalshi prediction markets. Runs as a **persistent daemon** on a DigitalOcean VPS: a supervisor process (`bot.daemon.main`) with a 60s cycle task plus event-driven pollers on their own threads. All trading logic lives in `trade.py` (~4.8K lines) invoked in-process by the daemon's `CycleRunner` — no more forking every 2 minutes.

**Owner:** Josh Lu (joshlu@a16z.com)
**VPS:** 45.55.79.193 (user: `kalshi`, path: `/home/kalshi/autoagent/`)
**Database:** `kalshi_trades.db` (SQLite, WAL mode, on VPS)
**Logs:** `/home/kalshi/autoagent/cron.log` (service uses `StandardOutput=append`, NOT journalctl)
**Service:** `kalshi-daemon.service` (persistent, `Restart=always`). Legacy `kalshi-bot.timer` oneshot units remain in `deploy/` for rollback but are not the current shape.

## Current Phase

**Phase 0 (signal validation) passed; Phase 1 (learning infra + event-driven weather MM) is the current work.**

- Phase 0 gate: per-family Brier on weather beat baseline by 4–8× on 5 families (KXHIGHMIA/CHI/AUS/LAX/NY). See [reports/BACKTEST_APR17.md](reports/BACKTEST_APR17.md).
- Phase 1 items: `alpha_backtest` table + atomic decision logging, settlement-driven learning population, Platt calibration correction in the ensemble, METAR → `WeatherQuoter` event wiring in shadow mode, directional shadow evaluator.
- **Nothing trades live right now.** MM is disabled (all 11 weather series blocked in config; no non-weather MM). Directional is DRY_RUN. Safe Compounder is behind `SC_ENABLED=false`.
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
trade.py                 — Cycle body (~4.8K lines). Called once per cycle by CycleRunner.
bot/
  config.py              — Env vars, constants, source weights, phase config, dynamic sizing, block lists
  db.py                  — init_db(), WAL setup, migrations, kv_cache helpers
  api.py                 — RSA-PSS auth, api_get/post/delete, rate limiter, API_LOCK
  core/
    money.py             — Canonical fee formulas (kalshi_maker_fee, kalshi_taker_fee)
    categorization.py    — Ticker → family/category mapping
  daemon/                — THE MM ARCHITECTURE (replaces bot/market_maker/, deleted)
    main.py              — Supervisor entrypoint (`python -m bot.daemon`)
    scheduler.py         — Periodic-task scheduler with on_start/on_stop hooks
    poller_base.py       — Poller ABC (own thread, health stats, graceful stop)
    metar_poller.py      — METAR observations poller, emits TemperatureChange events
    stations.py          — Station catalogue + SERIES_TO_STATION map
    smart_gates.py       — Pre-quote gates (trajectory, volatility, spread sanity)
    weather_quoter.py    — Event-driven weather MM (on METAR change → cancel-replace)
    orchestrator.py      — WeatherDaemon integration layer
    cycle_runner.py      — Wraps trade.main() for in-process invocation
    locks.py             — DB_WRITE_LOCK, API_LOCK, PIPELINE_STATS_LOCK
  signals/
    ensemble.py          — get_independent_estimate(), source routing, weighted averaging,
                            correlated-group handling, pipeline health
    weather_ensemble.py  — Multi-source weather stitcher (METAR + NBM + HRRR + NWS + MADIS)
    family_routers.py    — Per-family source routing policy
    sources/
      weather.py             — Open-Meteo (bracket CDF)
      weather_tomorrow.py    — Tomorrow.io (bracket CDF)
      weather_noaa.py        — NOAA alerts
      metar_observations.py  — METAR real-time airport observations
      nws_point.py           — NWS point forecast
      ndfd_nbm.py            — National Blend of Models
      hrrr.py                — HRRR (high-res rapid refresh)
      madis.py                — MADIS mesonet
      afd.py                  — Area Forecast Discussion
      economics_fred.py      — FRED
      economics_bls.py       — BLS
      economics_clevfed.py   — Cleveland Fed
      fedwatch.py            — CME FedWatch implied rate probabilities
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
    calibration.py         — Probability bucket bias correction (Platt, in Phase 1)
    active_feedback.py     — Synthesize learning into edge threshold + source gating
    bandit.py              — Exit decision bandit over graduated health bands
    category_scoring.py    — Per-family score aggregation
    timing_patterns.py     — Hour-of-day / time-to-expiry edge patterns
    postmortems.py         — Loss classification tagger
  scoring/
    market_scorer.py       — score_market, strategy-specific scoring
    filters.py             — Pre-scan filters (volume, spread, age, blocklist)
  arbitrage/
    bracket_arb.py         — Intra-series bracket arb
    correlation_arb.py     — Cross-market correlation arb
  observability/
    alerts.py              — Telegram notifications
deploy/
  04_redeploy.sh              — Sync .py + bot/ + tests/ + .env to VPS, full import-check battery
  05_deploy_weather_daemon.sh — Daemon-specific deploy + systemd unit install
  kalshi-daemon.service       — systemd unit for persistent daemon (Restart=always)
  kalshi-weather-daemon.service — legacy weather-only daemon unit
tests/
  daemon/                — cycle_runner, scheduler, poller_base, smart_gates_trajectory
  signals/               — source + ensemble + routing coverage
  bot/                   — api_concurrency, db_wal
  test_{weather_daemon,weather_quoter,smart_gates,money,fee_calc,fedwatch_parser,
        positions,settlement_certainty,db_schema}.py
backtest_comprehensive.py — 12-section per-family Brier / calibration / alpha analysis
reports/                 — Committed backtest and phase-gate reports
```

### Cycle Flow (every 60s, inside the daemon)

`CycleRunner.run_once()` → `trade.main(conn=shared, close_conn=False)`:

1. `compute_dynamic_sizing()` — scale MM params to total equity
2. `prune_stale_orders()` + `track_fills()` — housekeeping
3. `record_settlements()` — check for settled markets, record P&L, trigger learning updates
4. `manage_positions()` — graduated health-score exit with synthetic sell
5. Adaptive learning: source weights, calibration buckets, postmortems, edge convergence, timing patterns
6. Active feedback: disable bad sources/hours, adjust edge thresholds
7. `get_independent_estimate()` on candidate tickers — ensemble of 15+ sources → fair value
8. Directional evaluator (currently DRY_RUN; Phase 1 will log to `alpha_backtest`)
9. MM quoting (currently no-op — weather blocked, no non-weather series targeted)

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

All exit paths (`manage_positions`, QA auto-liquidation, `mm_liquidate_expiring`) use **buy-opposite-side** instead of sell-same-side. Saves ~1.3¢/contract (taker → maker fee). Exiting a YES position = buy NO (limit, maker fee ~0.44¢) instead of sell YES (taker fee ~1.75¢).

## Graduated Position Exits

Health score 0–1 (edge 40%, trend 20%, time 15%, P&L 15%, confidence 10%):
- ≥0.65: HOLD
- 0.45–0.65: Trim 25–33%
- 0.30–0.45: Exit 50%
- 0.15–0.30: Exit 75%
- <0.15: Full exit

Edge trend tracked in `kv_cache` (last 10 readings across cycles). Exit decisions logged to `position_health_log` for bandit training.

## MM QA Auto-Liquidation

QA loop checks inventory against fresh ensemble each cycle. Tracks consecutive flags per ticker in `kv_cache`. After 3 flags AND >10¢ loss magnitude → auto-liquidate via synthetic sell.

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

### `fair_value_cents` is per-order-side
- A YES order at `fv=85` means P(YES)=85%
- A NO order at `fv=85` means P(NO)=85% ≡ P(YES)=15%
- Any cross-order aggregation MUST normalize: `CASE WHEN side='yes' THEN fv ELSE 100-fv END`. Failure to do this produced a backtest that inverted probabilities (Brier 0.50) before the Apr 17 fix.

### client_order_id
Must start with `mm_` and must NOT contain periods (Kalshi rejects). Use `.replace('.', '_')` for decimal tickers.

### Fixed-Point Parsing
Kalshi returns `*_fp` and `*_dollars` as strings. Always `round(float(...))`, never `int(float(...))` — off-by-one from floating point otherwise.

### `ensemble_prob` in `opportunity_log`
Stored as P(our-side), not P(YES). Same normalization rule as `fair_value_cents` applies.

## Data Sources (Ensemble)

20+ sources, weighted and averaged in `bot/signals/ensemble.py`. `SOURCE_WEIGHTS` in `bot/config.py`. Correlated sources count as ~1 effective source, not N. Edge threshold scales with effective independent count: 3+ → 5%, 2 → 7%, 1 → 10–12%.

| Source | Weight | Category | Notes |
|--------|--------|----------|-------|
| METAR observations | 0.90 | Weather | Real-time airport station data, highest weather weight |
| Odds API | 0.85 | Sports | |
| Tomorrow.io | 0.82 | Weather | 7-day reliable horizon |
| Open-Meteo | 0.80 | Weather | Correlated with Tomorrow.io |
| FedWatch | 0.80 | Economics | FRED-based synthetic probabilities |
| NWS Point | 0.78 | Weather | Official US forecast |
| NBM | 0.77 | Weather | National Blend of Models |
| Polymarket | 0.75 | Prediction | Correlated with Metaculus |
| NOAA | 0.75 | Weather | Correlated with weather group |
| Series | 0.75 | Structural | Intra-series CDF |
| Cleveland Fed | 0.72 | Economics | |
| HRRR | 0.72 | Weather | High-res short-range |
| Metaculus | 0.70 | Prediction | |
| MADIS | 0.68 | Weather | Mesonet network |
| Crypto (CoinGecko+Deribit) | 0.65 | Crypto | **Blocked for directional — Brier 0.76–0.94** |
| Company KPI | 0.65 | Company | |
| GDPNow | 0.60 | Economics | Atlanta Fed nowcast |
| ADP/NFP | 0.58 | Economics | |
| SensorTower | 0.55 | Company | |
| Commodity Futures | 0.55 | Commodity | CME/ICE |
| FRED | 0.50 | Economics | Correlated with BLS |
| BLS | 0.50 | Economics | Correlated with FRED |
| Finnhub | 0.30 | News | |
| Momentum | 0.15 | Structural | Endogenous — not counted as real source for MM |
| LLM (GPT-4o-mini) | 0.15 | Fallback | Lowest weight |

Weather sources are routed through `bot/signals/weather_ensemble.py` which handles the correlated-group stitching before the ensemble sees them.

### Weather MM Gating (historical note)
Pre-daemon, weather MM required fresh (<10 min) METAR to quote. Phase 1's event-driven `WeatherQuoter` replaces this static gate: the quoter only exists while the poller is healthy, and it requotes on every material temperature change rather than gating on cache age.

## Database Schema (key tables)

- `mm_orders` — all MM orders (ticker, side, price, contracts, order_id, status, fill_qty, fair_value_cents *per-order-side*)
- `mm_inventory` — current position per ticker
- `mm_processed_fills` — deduplicated fills with fee_cents
- `settlements` — settled market outcomes with P&L
- `trades` — directional trade records
- `kv_cache` — persistent key-value with TTL
- `mm_sessions` — per-cycle stats
- `opportunity_log` — decision-time snapshot per quoted ticker (ensemble_prob is P(our-side))
- `position_health_log` — health scores for bandit learning
- `position_exits` — exit orders with entry/exit prices and reason
- `pipeline_health` — per-cycle source health counters
- `strategy_journal` — decision log with accept/discard reasons
- `loss_postmortems` — tagged loss attributions
- `calibration` — Platt/isotonic correction fit from settled outcomes (**populated in Phase 1**)
- `timing_patterns`, `edge_convergence`, `hyperparam_shadow` — **populated in Phase 1**
- `alpha_backtest` — atomic decision log for Phase 0 gate leg 2 (**added in Phase 1**)

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
SC_ENABLED=false         # Safe Compounder (Phase 4sc)
WEATHER_MM_LIVE=false    # Phase 1 shadow-to-live gate (flip only after shadow data proves out)
```

RSA key for Kalshi API auth: `.kalshi_private_key.pem` (gitignored; MUST NOT be committed).

## Known Bug Patterns (Regression Watchlist)

Fixed; re-check on every touch of the relevant code:

1. `client_order_id` containing periods → 400 Bad Request
2. `_apply_trade()` short-close P&L using `(100 − avg_entry) − price` instead of `avg_entry − price`
3. `record_settlements()` not subtracting `fee_cost`, or `won = revenue > 0` instead of `profit > 0`
4. `mm_liquidate_expiring()` zeroing inventory without confirmed settlement
5. Fixed-point parsing with `int(float(...))` instead of `round(float(...))`
6. `cancel_failed` orders excluded from exposure headroom
7. Resting order exposure query silently swallowing exceptions (fail closed instead)
8. Tomorrow.io forecasts beyond 7-day reliable horizon
9. Correlated sources (weather+weather, FRED+BLS) counted as fully independent
10. MM spread not checked against expected maker fees
11. Settlement path in `mm_liquidate_expiring()` not subtracting fees
12. Cache isolation: `trade.py._CACHE` vs `bot.api._CACHE` (daemon now shares a DB-backed cache where possible)
13. `fair_value_cents` / `ensemble_prob` aggregated without per-side normalization (caused the Apr 17 v1 backtest inversion)
14. Daemon pollers writing to SQLite without holding `DB_WRITE_LOCK` (causes `database is locked` under contention)
15. Any code path assuming the DB connection is process-local — daemon shares one connection across threads

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

## Phase 1 Work (in flight)

1. ✅ `bot/daemon/` architecture committed (checkpoint `04ca78f`)
2. `alpha_backtest` table + atomic decision-time logging
3. Settlement-driven learning population: `calibration`, `timing_patterns`, `edge_convergence`, `position_health_log`, `postmortems`
4. Platt/isotonic correction from `calibration` wired into `get_independent_estimate()`
5. `METARPoller.on_result` → `WeatherQuoter.handle_temp_change()` in shadow mode
6. Directional shadow evaluator (DRY_RUN, logs to `alpha_backtest`, blocks KXBTC/KXETH/KXHIGHDEN)
7. Deploy pipeline swap: `kalshi-bot.timer` oneshot → `kalshi-daemon.service` persistent
8. Shadow-to-live gate: `WEATHER_MM_LIVE=true` only after N shadow settlements prove positive-EV

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
