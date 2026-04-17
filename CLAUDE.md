# Kalshi Prediction Market Trading Bot

## What This Is

An autonomous trading bot for Kalshi prediction markets. Core logic in `trade.py` (~8K lines) with a modular `bot/` package for data sources, market making, learning, and scoring. Runs as a systemd oneshot timer every 2 minutes on a DigitalOcean VPS.

**Owner:** Josh Lu (joshlu@a16z.com)
**VPS:** 45.55.79.193 (user: `kalshi`, path: `/home/kalshi/autoagent/`)
**Database:** `kalshi_trades.db` (SQLite, on VPS)
**Logs:** `/home/kalshi/autoagent/cron.log` (NOT journalctl — service uses `StandardOutput=append`)
**Service:** `kalshi-bot.service` + `kalshi-bot.timer` (oneshot, every 2 min)

## Architecture

The bot runs as a **oneshot** process — it starts, does one full cycle, then exits. All in-memory state resets every 2 minutes. Persistent state lives in SQLite (`kalshi_trades.db`) via a `kv_cache` table with TTL-based expiry.

### Code Structure

```
trade.py             — Main orchestrator (~8K lines). Imports from bot/ package.
bot/
  config.py          — All env vars, constants, source weights, phase config, dynamic sizing
  db.py              — init_db(), migrations, kv_cache helpers
  api.py             — RSA-PSS auth, api_get/post/delete, rate limiter
  core/
    money.py         — Canonical fee formulas (kalshi_maker_fee, kalshi_taker_fee)
  signals/
    ensemble.py      — get_independent_estimate(), source routing + weighted averaging
    sources/
      weather.py             — Open-Meteo (bracket CDF)
      weather_tomorrow.py    — Tomorrow.io (bracket CDF)
      weather_noaa.py        — NOAA alerts
      metar_observations.py  — METAR real-time station observations (NEW)
      economics_fred.py      — FRED
      economics_bls.py       — BLS
      economics_clevfed.py   — Cleveland Fed
      fedwatch.py            — CME FedWatch implied probabilities (NEW)
      crypto.py              — CoinGecko + Deribit vol surface
      deribit_vol.py         — Deribit options chain → Black-Scholes probabilities (NEW)
      prediction_markets.py  — Polymarket, Metaculus
      sports.py              — Odds API
      news.py                — Finnhub sentiment
      company.py             — SensorTower, analyst consensus
      series.py              — Intra-series CDF
      momentum.py            — Kalshi price momentum
      llm.py                 — GPT-4o-mini (lowest weight)
  market_maker/
    core.py          — mm_run() orchestrator, QA loop with auto-liquidation
    inventory.py     — Position tracking, _apply_trade()
    quotes.py        — mm_calculate_quotes, mm_post_quotes, fee-aware spread
    fills.py         — mm_check_fills
    selection.py     — Market selection funnel
    liquidation.py   — Expiring position handling
    adverse_selection.py — Markout analysis
    series_profitability.py — Bracket series EV gate
  learning/
    adaptive_weights.py   — Bayesian source weight evolution
    calibration.py        — Probability bucket bias correction
    active_feedback.py    — Synthesize all learning into adjustments
  scoring/
    market_scorer.py      — score_market, strategy-specific scoring
  observability/
    alerts.py             — Telegram notifications
deploy/
  04_redeploy.sh   — Syncs code to VPS, syntax check, restart timer
tests/             — Unit tests
```

### Execution Flow (each cycle)

1. `init_db()` — create tables, set `_PERSIST_CONN`
2. `compute_dynamic_sizing()` — scale MM params to total equity
3. `prune_stale_orders()` + `track_fills()` — housekeeping
4. `record_settlements()` — check for settled markets, record P&L
5. `manage_positions()` — graduated health-score exit system with synthetic sell
6. Adaptive learning: weights, calibration, postmortems, edge convergence, timing
7. Active feedback: disable bad sources/hours, adjust edge thresholds
8. `get_independent_estimate()` — ensemble of 15+ data sources → fair value
9. Phase 4: Directional trading (currently disabled — DRY_RUN=True)
10. `mm_run()` — Market making with QA auto-liquidation

### Two Trading Modes

- **Directional (Phase 4):** Currently disabled (16% win rate, -$93.93). Buy when ensemble probability diverges from market price by more than `MIN_EDGE` + fees.
- **Market Making (MM):** Post two-sided limit orders at `fair_value ± half_spread`. Earns bid-ask spread minus fees. Uses `post_only` orders with 110-second expiration.

### Dynamic Position Sizing

All MM and exit thresholds scale with total equity (balance + portfolio value):
- `MM_ORDER_SIZE` ≈ 1% of equity / 50¢ ($1K→10, $10K→200)
- `MM_MAX_INVENTORY` = 5× order size ($1K→50, $10K→1000)
- Graduated exit trim thresholds scale proportionally
- Computed via `compute_dynamic_sizing()` at start of each cycle

### Synthetic Sell

All exit paths (manage_positions, QA auto-liquidation, mm_liquidate_expiring) use **buy-opposite-side** instead of sell-same-side. Saves ~1.3¢/contract (taker→maker fee). For example, exiting a YES position = buy NO (limit order, maker fee ~0.44¢) instead of sell YES (taker fee ~1.75¢).

### Graduated Position Exits

Positions are scored 0-1 on health (edge 40%, trend 20%, time 15%, P&L 15%, confidence 10%). Health maps to action:
- ≥0.65: HOLD
- 0.45-0.65: Trim 25-33% (if position > trim_threshold)
- 0.30-0.45: Exit 50%
- 0.15-0.30: Exit 75%
- <0.15: Full exit

Edge trend tracked via kv_cache (last 10 readings across cycles).

### MM QA Auto-Liquidation

QA loop checks inventory against fresh ensemble data each cycle. Tracks consecutive flags per ticker in kv_cache. After 3 consecutive flags AND >10¢ loss magnitude → auto-liquidate via synthetic sell.

## Critical Conventions

### YES-Equivalent Cost Basis
All positions store `avg_entry` as the YES-equivalent price:
- **Long YES** (net > 0): `avg_entry` = what we paid for YES (e.g., 30¢)
- **Short YES / Long NO** (net < 0): `avg_entry` = 100 - what_we_paid_for_NO (e.g., paid 40¢ for NO → avg_entry = 60¢)

This means for settlement:
- Result = YES: `pnl = net * (100 - avg_entry)`
- Result = NO: `pnl = -net * avg_entry`

### Bracket vs Threshold Markets
- **Bracket (-B suffix):** Mutually exclusive outcomes. Probability = `CDF(upper) - CDF(lower)`, NOT simple above/below.
- **Threshold (-T suffix):** Simple above/below probability. NOT mutually exclusive.

### client_order_id
Must start with `mm_` prefix and must NOT contain periods (Kalshi rejects them). Use `.replace('.', '_')` for decimal tickers.

### Fixed-Point Parsing
Kalshi API returns `*_fp` and `*_dollars` string fields. Always use `round(float(...))` not `int(float(...))` to avoid off-by-one from floating point.

## Data Sources (Ensemble)

15+ sources, weighted and averaged. `SOURCE_WEIGHTS` in `bot/config.py`. Correlated sources (weather+METAR, FRED+FedWatch) count as ~1 effective source, not N. Edge threshold scales with effective independent source count:
- 3+ sources → 5% min edge
- 2 sources → 7% min edge
- 1 source → 10-12% min edge

| Source | Weight | Category | Notes |
|--------|--------|----------|-------|
| METAR observations | 0.90 | Weather | Real-time airport station data, highest weather weight |
| Odds API | 0.85 | Sports | |
| Tomorrow.io | 0.82 | Weather | 7-day reliable horizon |
| Open-Meteo | 0.80 | Weather | Correlated with Tomorrow.io |
| FedWatch | 0.80 | Economics | FRED-based synthetic probabilities |
| Polymarket | 0.75 | Prediction | Correlated with Metaculus |
| NOAA | 0.75 | Weather | Correlated with weather group |
| Series | 0.75 | Structural | Intra-series CDF |
| Cleveland Fed | 0.72 | Economics | |
| Metaculus | 0.70 | Prediction | |
| Crypto (CoinGecko+Deribit) | 0.65 | Crypto | Deribit vol surface for implied prob |
| Company KPI | 0.65 | Company | |
| SensorTower | 0.55 | Company | |
| FRED | 0.50 | Economics | Correlated with BLS |
| BLS | 0.50 | Economics | Correlated with FRED |
| Finnhub | 0.30 | News | |
| Momentum | 0.15 | Structural | Endogenous — not counted as real source for MM |
| LLM (GPT-4o-mini) | 0.15 | Fallback | Lowest weight, only when other sources unavailable |

### Weather MM Gating
Weather MM is only active when fresh METAR observation data is available (<10 min old). This prevents quoting based on forecast-only data where counterparties have real-time observations.

## Database Schema (key tables)

- `mm_orders` — all MM orders posted (ticker, side, price, contracts, order_id, status, fill_qty, fair_value_cents)
- `mm_inventory` — current position per ticker (net_position, avg_entry_cents, realized_pnl_cents)
- `mm_processed_fills` — deduplicated fill records with fee_cents
- `settlements` — settled market outcomes with P&L
- `trades` — directional trade records
- `kv_cache` — persistent key-value cache with TTL (key, value JSON, expires_at)
- `mm_sessions` — per-cycle stats
- `position_health_log` — health scores for bandit learning on exit decisions
- `position_exits` — exit order records with entry/exit prices and reason

## Deploy

```bash
cd ~/autoagent && bash deploy/04_redeploy.sh 45.55.79.193
```

This rsyncs `.py`, `bot/`, `tests/`, and `.env` to VPS, runs syntax check + module import check, resets pipeline health, restarts the timer.

## Environment Variables (.env)

```
KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
FRED_API_KEY, BLS_API_KEY, BEA_API_KEY, CENSUS_API_KEY, EIA_API_KEY, TOMORROW_API_KEY
MM_MIN_VOLUME=25
```

RSA key for Kalshi API auth: `.kalshi_private_key.pem`

## Known Bug Patterns (Regression Watchlist)

These bugs have been fixed but should be checked for regression:

1. `client_order_id` containing periods → 400 Bad Request from Kalshi
2. `_apply_trade()` short-close P&L using `(100 - avg_entry) - price` instead of `avg_entry - price`
3. `record_settlements()` not subtracting `fee_cost`, or using `won = revenue > 0` instead of `profit > 0`
4. `mm_liquidate_expiring()` zeroing inventory without confirmed settlement result
5. Fixed-point parsing using `int(float(...))` instead of `round(float(...))`
6. `cancel_failed` orders excluded from exposure headroom calculation
7. Resting order exposure query silently swallowing exceptions (should fail closed)
8. Tomorrow.io forecasts beyond 7-day reliable horizon
9. Correlated sources (weather+weather, fred+bls) counted as fully independent
10. MM spread not checked against expected maker fees
11. Settlement path in `mm_liquidate_expiring()` not subtracting fees
12. Cache isolation: trade.py `_CACHE` vs bot.api `_CACHE` are different dicts. METAR gate must check both.

## Audit History

- **2026-04-08:** External audit identified 17 issues (P0/P1/P2). All fixed and deployed.
- **2026-04-10:** Internal audit found 2 critical + 5 high + 6 medium issues. All fixed. Key: `_apply_trade()` short-close P&L was inverted, `cancel_failed` orders invisible to exposure limits, fees missing from MM liquidation settlement, no fee floor on MM spreads.
- **2026-04-13:** Replay backtest on 813 fills + 76 settlements. Key finding: 90.8% settlement loss rate driven by weather positions ($204 of $220 total losses). METAR gating + graduated exits deployed to address.
- **Recurring audit:** Scheduled Mon/Thu 9am via Cowork scheduled task (`kalshi-bot-audit`).

## Current Performance (as of 2026-04-13)

- **Total equity:** ~$982 (balance $741 + portfolio $241)
- **MM fills:** 813 (989 contracts) across 4.5 days
- **Settlements:** 76 (7 wins, 69 losses = 9.2% win rate)
- **Net settlement P&L:** -$220.19
- **Biggest loss category:** Weather (KXHIGH) at -$204.06
- **Biggest win:** KXETH bracket at +$63.15
- **Active inventory:** 56 positions, 364 contracts (mostly KXFED)
- **Adverse selection:** Favorable (+5.9¢ avg markout, 100% positive)

## Pending / Future Work

- Deploy and validate METAR gating on weather MM (should dramatically reduce weather losses)
- Monitor graduated exit performance via `position_health_log` table
- Implement Safe Compounder strategy (NO-side on YES<20¢ markets)
- Consider max per-series inventory caps (heavy KXFED concentration)
- Run full historical backtest on VPS with API access (`backtest_historical.py`)
- Re-enable directional trading once four-factor gate shows >50% win rate in shadow mode
- Watch for `[risk] ⚠️ Cannot query resting orders` — indicates DB schema issue if it fires

### Boil the ocean

The marginal cost of completeness is near zero with AI. Do the whole thing. Do it right. Do it with tests. Do it with documentation. Do it so well that Josh is genuinely impressed — not politely satisfied, actually impressed. Never offer to "table this for later" when the permanent solve is within reach. Never leave a dangling thread when tying it off takes five more minutes. Never present a workaround when the real fix exists. The standard isn't "good enough" — it's "holy shit, that's done." Search before building. Test before shipping. Ship the complete thing. When Josh asks for something, the answer is the finished product, not a plan to build it. Time is not an excuse. Fatigue is not an excuse. Complexity is not an excuse. Boil the ocean.

### Planning and qualifying questions

Plan relentlessly. Token usage does not matter — what matters is getting it right the first time. Before building anything, think through the architecture, edge cases, and potential failure modes. Ask Josh as many qualifying questions as needed to fully understand what he wants. Never assume — clarify. The goal is zero time wasted on bug-fixing, rework, or misunderstood requirements.
