# The Kalshi Trading Bot, in Plain English

This is a long-form, no-jargon walkthrough of the entire trading bot — what it
does, why each piece exists, and the reasoning behind every major design
decision. It's written for someone who has never traded, never coded, and has
never heard of a prediction market. By the end you should be able to picture
the whole machine in your head.

---

## 1. The thirty-second version

The bot is a robot trader. It runs 24/7 on a small cloud server, watches a
website called Kalshi where people bet on real-world events ("Will it be over
80°F in Miami today?", "Will the Fed cut rates in May?"), and tries to make
money by placing smarter bets than the average person on the other side of
those bets.

It does this by combining ~20 different real-world data feeds — weather
stations, central bank data, futures markets, sports odds, crypto exchanges —
into a single educated guess about how likely each outcome is. When its guess
disagrees with the market's price by enough to overcome trading fees, it bets.

It is not currently betting real money in volume. It's in a careful
ramp-up phase where most of its activity is "shadow mode" — it logs what it
*would have done* and grades itself against actual outcomes. Once a strategy
proves it would have made money on paper for long enough, it gets graduated
into live trading with very small position sizes, and only scaled up after
the live results match the paper results.

---

## 2. What is Kalshi?

Kalshi is a CFTC-regulated prediction market. Each market is a yes/no
question — "Will Bitcoin close above $70K on Friday?" — that resolves to
either YES or NO at a known time. While the question is open, you can buy
shares of either side. A YES share pays $1 if the answer is yes and $0 if
no. A NO share is the mirror image.

The price of a YES share, between 1¢ and 99¢, is essentially the market's
estimate of the probability that the answer will be YES. If a share is
trading at 60¢, the market is collectively saying "60% chance this happens."
If you think the real probability is 75%, buying that share at 60¢ is a
profitable trade in expectation.

That gap — between *the market's price* and *what we think the true
probability is* — is called **edge**, and edge is the only thing this bot
chases.

Kalshi takes a small fee on every trade. The fee structure matters a lot,
because edge has to be big enough to clear fees and still leave profit. The
bot has a dedicated module (`bot/core/money.py`) that knows exactly what
Kalshi charges so it never accidentally bets into a losing fee structure.

---

## 3. Two ways to make money

The bot supports two completely different trading styles. They have different
risk profiles, different infrastructure, and different gating rules.

### 3a. Directional trading

This is the simple style. The bot looks at a market, calls its 20-source
ensemble, gets back a probability — say "we think this is 75% likely" — looks
at the market price — say 60¢ — sees a 15% gap, and buys the YES side. Then
it holds, and exits when either:

- the gap closes (the market price catches up to our estimate, locking in the
  profit), or
- our own estimate moves against us (new data came in and we no longer think
  this is a good bet), or
- time is running out and the position is going nowhere.

Directional is **high conviction, low frequency**. Each bet is bigger and
held longer.

### 3b. Market making (MM)

This is the more sophisticated style. Instead of betting a direction, the bot
sits on *both sides* of a market and earns the spread. Imagine a market where
YES is bid at 58¢ and offered at 62¢. The bot posts a buy order at 59¢ and a
sell order at 61¢. If both orders fill, it has bought low and sold high
without ever taking a directional view — it just collected 2¢ from being the
intermediary.

Market making is **low edge, high frequency**. Each trade earns pennies, but
you do thousands of them. It only works if:

- your spread is wide enough to cover Kalshi's fees,
- your estimate of fair value is right (so the side that fills isn't always
  the side that turns out wrong),
- you can react fast when the world changes, so you're not still offering
  yesterday's price after new info arrived.

That last point is the architectural pivot that defines the current phase of
this project. More on that below.

---

## 4. How the bot thinks: the signal ensemble

When the bot looks at a market, it doesn't have one opinion — it has twenty.
Each "source" is a separate module that turns some real-world data into a
probability estimate.

For weather markets, we ask:

- **METAR** (real-time airport sensor data — the stuff pilots use)
- **HRRR** (NOAA's high-resolution short-range model)
- **NBM** (NOAA's National Blend of Models)
- **Tomorrow.io** (commercial weather API, reliable for ~7 days)
- **Open-Meteo** (open-source meteorological aggregator)
- **NWS Point Forecast** (the official US government forecast)
- **MADIS** (citizen-science mesonet stations — noisier but dense)
- **AFD** (text-based forecaster discussion, parsed for hints)
- **NOAA alerts** (severe weather warnings)

For Fed/economics markets, we ask:

- **ZQ futures** (Yahoo data on 30-day fed funds futures — this is what
  professional traders use to bet on rate decisions)
- **CME FedWatch** (CME's published implied rate probabilities)
- **GDPNow** (the Atlanta Fed's real-time GDP tracker)
- **ADP/NFP** (private payroll data, leads the official BLS number)
- **FRED, BLS, BEA, Census, EIA** (raw government data feeds)

For crypto, sports, company earnings, etc., there are dedicated sources
(CoinGecko spot, Deribit options-implied probabilities, Odds API, SensorTower
app downloads, Finnhub news sentiment, and so on).

Each source has a **weight** — how much we trust it. METAR (real-time sensor
data) gets weight 0.90. The fallback LLM (asking GPT-4o-mini) gets 0.15. The
weights live in `bot/config.py` and the code that combines all sources lives
in `bot/signals/ensemble.py`.

### The "correlated sources" problem

Twenty sources sounds like a lot, but if eight of them are weather forecasts
that are all derived from the same NOAA model, they're not really independent
opinions — they're the same opinion repeated eight times.

The ensemble is aware of this. Sources are grouped into correlation groups
("weather forecast models", "FRED + BLS"), and the *effective* number of
independent voices is computed before deciding how confident to be. The
required edge to trade scales with this:

- 3+ truly independent sources agreeing → 5% edge required
- 2 → 7% edge required
- 1 → 10–12% edge required

The intuition: if everyone's just one guy in a trenchcoat, demand a much
bigger gap before you bet on him.

### Calibration: making the probabilities mean what they say

Raw model outputs are usually overconfident. A model that says "80% likely"
is probably right 70% of the time. Kalshi pays out on actual outcomes, not on
self-reported confidence, so a miscalibrated 80% costs real money.

After every trade settles, the bot logs (its prediction, the actual outcome)
into a `calibration` table. Once a cycle, it fits a **Platt correction** — a
simple statistical curve that bends raw predictions toward reality — and
saves it. Future predictions get passed through this curve before being used
for trading. The bot literally learns to be less overconfident over time.

---

## 5. The brain: how the bot is wired

This is where the most important architectural decision in the whole project
lives, so it's worth slowing down.

### The old way: a cron job

For the first months of this project, the bot was a script that ran every 2
minutes via a Linux cron timer. Every 2 minutes it would: spin up a fresh
process, connect to the database, fetch market data, run all 20 signals,
make decisions, place orders, and exit.

This was fine for directional trading, where 2 minutes is acceptable
latency. It was a disaster for market making. By the time the bot woke up
and noticed that the temperature in Chicago had jumped 3 degrees, faster
counterparties had already swept its now-stale weather quotes. We were
posting yesterday's price and getting picked off.

This is called **adverse selection**, and the bot's own data showed it
clearly: weather market making had a +6¢ favorable markout at the time of
fill (meaning the price moved in our favor right after we filled — proof
the *signal* was right) but still lost money on settlement. Translation:
*we were getting filled on the orders that were stale, not the ones that
were good.*

### The new way: a persistent daemon

The bot is now a single long-running process — `bot.daemon.main` — that
never exits. Inside that process:

- A **scheduler** on the main thread runs periodic tasks: the trading
  cycle every 60 seconds, a database cache cleanup every hour, a health
  log every 5 minutes.
- **Pollers** run on their own threads, doing one specific job. The METAR
  poller hits airport weather stations every 30 seconds.
- A **shared database connection** is used by everyone, with a write lock
  to keep things safe.
- An **API lock** serializes outbound calls to Kalshi, both to respect their
  rate limit and because the cryptographic signing they require can't be
  done in parallel cleanly.
- When a poller detects a material temperature change, it doesn't wait for
  the next cycle. It fires an event, which is caught by a **WeatherQuoter**
  that immediately cancels stale orders and posts new ones at the revised
  fair value.

This is the unlock. The cycle still runs every 60 seconds for things that
don't need to be instant (deciding whether to enter new directional trades,
sizing, learning updates), but the *quotes themselves* are now event-driven
and react in seconds to new data. Counterparty speed matches; adverse
selection should disappear.

### The 60-second cycle, in order

When `CycleRunner.run_once()` fires every 60s, here's what happens, top to
bottom:

1. **Phase + sizing** — figure out how much we're allowed to risk based on
   our track record (more on phases below) and our current equity.
2. **Housekeeping** — cancel any stale orders, record any new fills, mark
   any settled markets and book the P&L.
3. **Cascade** — take any markets that just settled and feed the outcomes
   back into the learning tables (calibration, timing patterns, edge
   convergence, loss postmortems).
4. **Manage existing positions** — for every position we hold, compute a
   "health score" (more below) and decide whether to hold, trim, or exit.
5. **Update learning** — refit the calibration curve, recompute per-family
   edge thresholds, retag losses with reason codes, update edge-decay
   tracking.
6. **Adjust gates** — turn off underperforming sources, raise/lower the
   minimum-edge threshold, ban hours of the day that have been bad.
7. **Risk budget check** — are we within global / per-family / per-expiry
   exposure caps? If not, halt new entries.
8. **Scan markets** — pull the list of open markets from Kalshi, filter out
   noise (low volume, weird tickers, parlays), and score each candidate.
9. **Decide and act** — for each market that scores above threshold, run a
   final order-book depth check, size the position via Kelly criterion, and
   place the trade.

This loop is in `trade.py` (~7,000 lines). Around it sit the daemon
infrastructure, the pollers, the event-driven quoter, and the learning
modules.

---

## 6. Risk management

Trading systems lose money in two ways: they pick wrong (signal failure)
and they bet too big when they're right (sizing failure). The signal side
is the ensemble. The sizing side has its own architecture.

### Position sizing

Three concepts compose:

**Phase config (track record gate).** The bot lives in one of five phases
based on how it's actually performing in production. Phase 1 is "tiny
positions, prove the signal works at all." Phase 5 is "you have a real
track record, scale up." You can't skip phases; you graduate by hitting
metrics. You can also be demoted if performance regresses. Phases bound
the maximum position size as a fraction of equity.

**Dynamic sizing.** Within a phase, every order size scales with current
total equity. Market-making order size is roughly 1% of equity divided by
50¢. So $1K of equity → 10-contract orders; $10K → 200-contract orders.
This means sizing automatically deflates after a drawdown and inflates
after a winning streak, without any manual knobs to forget to turn.

**Kelly criterion.** For directional trades, the bet size is determined by
the Kelly formula, which says: bet a fraction of bankroll proportional to
your edge divided by the variance of the bet. Bigger edge → bigger bet,
but capped so a single wrong bet can't wipe you out.

### Exposure caps

Three caps stack:

- **Global**: total exposure ≤ 50% of equity. We never go all-in.
- **Per-family**: exposure to any one market family (KXFED, KXBTC,
  KXHIGHMIA, etc.) ≤ 25% of equity. We never bet the farm on Bitcoin.
- **Per-expiry**: exposure to any one settlement date ≤ 7.5% of equity.
  This protects against catastrophes like "FOMC announcement was a
  surprise and every Fed market settles against us at the same instant."

These are checked before every entry. If we'd breach a cap, the entry is
skipped.

### The graduated exit policy

Exits used to be ad-hoc — different code paths for different reasons,
some of which had subtle bugs. The current architecture has *one* exit
path: `manage_positions`. For every position, every cycle, it computes a
**health score** between 0 and 1 from five components:

- 40% remaining edge (does our current estimate still beat the market
  enough to justify staying in?)
- 20% trend (is our estimate moving in our favor or against us?)
- 15% time (is settlement close enough to bother holding for?)
- 15% P&L (are we already up enough that locking in is the right call?)
- 10% confidence (how many independent sources agree?)

Then:

- score ≥ 0.65 → hold
- 0.45–0.65 → trim 25–33%
- 0.30–0.45 → exit half
- 0.15–0.30 → exit 75%
- < 0.15 → flat the position

There are also two backstops fully inside this same function: an
**edge-decayed** exit (we no longer have meaningfully more edge than when
we entered) and a **time backstop** (we're inside the last 15 minutes
and edge is below a threshold). All exits log a reason code and feed
back into the bandit-learning module that tunes the policy over time.

### The synthetic sell trick

When you're long YES and want to exit, the obvious move is to sell YES.
But on Kalshi, selling YES is a **taker** action and pays the higher fee
(~1.75¢). Buying NO at the same price is mathematically equivalent (a
YES + a NO sums to $1 by definition) but it's a **maker** action with the
lower fee (~0.44¢). That's a 1.3¢-per-contract savings, which is enormous
in a business where edge is measured in single-digit cents.

Every exit in the system is implemented as this synthetic-sell. There is
no path that pays the taker fee.

---

## 7. The learning loop

A trading bot that doesn't learn is a one-shot guess. This bot has six
distinct learning systems, each writing to its own database table, each
read by the cycle to adjust behavior.

1. **Calibration** (`learning/calibration.py`) — the Platt curve mentioned
   earlier. Bends raw probabilities toward reality.
2. **Adaptive weights** (`learning/adaptive_weights.py`) — Bayesian
   updating of source trustworthiness. Sources that have been right get
   more weight; sources that have been wrong get less.
3. **Active feedback** (`learning/active_feedback.py`) — synthesizes the
   above into operational knobs: "disable source X for the next 24 hours,"
   "raise minimum edge to 7% in the morning hours."
4. **Bandit** (`learning/bandit.py`) — multi-armed-bandit algorithm that
   tunes the exit-policy thresholds based on which thresholds have led to
   the best outcomes.
5. **Timing patterns** (`learning/timing_patterns.py`) — detects whether
   certain hours of day or times-to-expiry are systematically worse.
6. **Loss postmortems** (`learning/postmortems.py`) — every losing trade
   gets tagged with a category ("calibration", "stale-quote", "regime
   change", etc.) so we can spot patterns in failure modes.

The hyperparameters that come out of this learning don't sit in memory —
they're written to a `learned_config` table and persist across restarts.
The bot is genuinely accumulating knowledge over time.

### The atomic decision log

A bug we had to track down: the old learning loop only saw outcomes for
trades that *actually happened*. That gave a biased dataset — we couldn't
tell if our rejected candidates would have been good. So there's now an
`alpha_backtest` table that logs *every* decision at the moment it's made
— what we estimated, what the market price was, what side, whether we
traded or shadowed — keyed to the eventual outcome. When a market settles,
we back-fill the result, and the learning loop pulls from this complete
dataset. Selection bias gone.

---

## 8. The shadow→canary→full promotion gate

This is how new strategies get into production. We learned the hard way
not to flip switches.

**Shadow mode.** The strategy runs but doesn't trade. Every order it
*would have* placed is logged to `weather_mm_shadow` (or equivalent), with
all the context: fair value, bid, ask, gate decision, even the METAR data
at the time. When the market settles, we back-fill what the trade *would
have* paid out. The strategy accumulates a paper P&L.

**Canary mode.** Once shadow has accumulated a meaningful sample with
positive expected P&L (currently the threshold is per-family, computed in
`bot/learning/mm_promotion.py`), the strategy is promoted to canary. It
trades real money but at half normal size, in a single family at a time.
The actual fills are tracked and compared to what the shadow predicted.

**Full mode.** If canary's realized P&L tracks shadow's predicted P&L
within tolerance over a sufficient sample, the family graduates to full
size. If realized lags predicted (meaning shadow is fooling us), the
family is demoted back to shadow.

The state machine and metrics are all stored in a `promotion_events`
table — every transition is auditable.

This is the safety harness for the entire operation. Nothing, ever, goes
from "I tested this in a backtest" to "live full size" in one step.

---

## 9. The fills ledger: a story about data hygiene

A subtle problem in any trading system: where does the source-of-truth for
"a fill happened" live? Historically, multiple parts of this codebase
wrote fill records — the directional path, the (deleted) market-making
path, manual ingest scripts. That meant: subtle differences in what a
"fill" record looks like, double-counts, missing fills, and the inability
to reconstruct exact P&L from the database.

The current architecture introduces a single canonical table called
`fills_ledger`. Exactly one writer (`bot/daemon/fills_writer.py`) is
allowed to touch it, and Kalshi's own `trade_id` is the primary key, so
duplicates are mathematically impossible. Every reader (the regime
detector, the backtester, the shadow-vs-live P&L joiner) has been
migrated to read from this single ledger.

A dual-run validator runs once a day comparing the ledger against the
legacy fills table to catch divergence during the migration. This is
boring infrastructure work but it's the difference between "we have a
trading system" and "we have a system that thinks it's trading and
might be."

---

## 10. The current state: where we actually are

As of late April 2026:

- The bot is **running 24/7** as a persistent daemon on a small
  DigitalOcean server.
- **No live market making.** All 11 weather families are blocked. The
  WeatherQuoter is logging shadow trades but `WEATHER_MM_LIVE` is false.
- **No live directional.** Directional trading is in DRY_RUN mode,
  logging every decision to `alpha_backtest` for evaluation.
- **Three families banned outright** for directional: KXBTC, KXETH,
  KXHIGHDEN. The first two had Brier scores so bad (0.76–0.94 vs ~0.24
  baseline — a Brier score is a measure of how wrong probabilistic
  predictions are; lower is better, 0.25 is "useless coin flip" for a
  binary, anything above 0.5 is *anti-correlated* with truth) that
  they were destroying P&L. KXHIGHDEN looks like a station-specific
  weather quirk we don't yet understand.
- **Five weather families passed the signal-quality gate** (KXHIGHMIA,
  KXHIGHCHI, KXHIGHAUS, KXHIGHLAX, KXHIGHNY) with Brier scores 4–8×
  better than baseline. These are the candidates queued for the
  shadow→canary→full promotion path.

The headline number from the recent backtest: at the time of fill, our
weather MM had a +6.12¢ favorable markout — meaning the market price
moved in our favor right after we filled, 99.9% of the time. The signal
was working. We just couldn't *capture* it because the cron architecture
was too slow. That's the entire reason for the daemon refactor, the
event-driven quoter, and the shadow-first re-enable plan.

---

## 11. The major architectural decisions, and why

A summary of the why-did-we-do-it-this-way calls, in roughly the order
they were made.

**Daemon over cron.** Cron's 2-minute granularity made us systematically
slow. Daemon lets quotes react in seconds.

**Event-driven quoter, not faster polling.** We could have polled every
5 seconds in the cron model. But polling and *reacting* are different
problems — even if you poll fast, processing is slow if you have to
reload state every cycle. A long-running process with cached state and
threaded pollers is structurally different.

**One database connection, shared, with a write lock.** SQLite + WAL
mode tolerates concurrent reads, but writes have to be serialized. A
shared connection with a lock is simpler and faster than a connection
pool, given our scale. The lock is held for milliseconds; contention is
not a real problem.

**Synthetic sells.** Saving 1.3¢ per exit is the difference between
profitable and not. There is no scenario where we'd rather pay the
taker fee.

**Shadow-first promotion, not direct flip.** We've been burned before by
"the backtest looks great, ship it." The promotion gate enforces that
*live* P&L matches *shadow* P&L before we scale. It is slow and
intentionally conservative.

**Single-writer fills ledger.** Multiple writers caused subtle data
corruption. Canonical source-of-truth tables with a single writer is a
common discipline in production systems and it solved the problem
permanently.

**Atomic decision logging.** Without it we could only learn from trades
we made, not from trades we considered. That's a biased training set.
With it, our learning loop sees the full universe of decisions.

**Blocking entire market families.** When a family's calibration is
catastrophically bad (Brier 0.76 on Bitcoin), no parameter tuning fixes
it — the underlying signal is broken. The right move is to disable the
family entirely until we rebuild that signal source from scratch.
Pretending we have a signal we don't is worse than admitting we don't.

**Phase-gated sizing.** The biggest risk in any trading system is
oversizing during the period when you *think* you have an edge but
haven't yet proven it. Phases force a track record before scaling.

**Per-family / per-expiry exposure caps.** Diversification doesn't help
if everything you own settles on the same day on the same news event.
Exposure caps along multiple axes are the only protection.

**Plain-English logs and a registry of "dangerous patterns."** Every
known historical bug has a regression test pinned to it (currently 15
of them, listed in CLAUDE.md). When you change related code, you have
to keep those tests passing. This is how the system stays correct as
it evolves.

---

## 12. What it can't do (yet)

Honesty section.

- **Crypto.** The Bitcoin and Ethereum signal is not just bad — it's
  actively wrong. Phase 3 will rebuild it from scratch, probably starting
  from Deribit's options-implied volatility surface and a proper
  bracket-pricing model.
- **Sports.** We have an Odds API source plumbed in but we don't trade
  sports markets meaningfully yet. The infrastructure is ready; the
  per-family validation hasn't been done.
- **Earnings / company-KPI markets.** Same story. SensorTower and
  Finnhub are wired in but not validated end-to-end.
- **Cross-market structures.** Bracket arbitrage and correlation
  arbitrage modules exist (`bot/arbitrage/`) but are dormant. They need
  per-strategy validation before they get turned on.
- **Real-time alerting.** Telegram alerts exist for catastrophic
  conditions (halts, big losses) but we don't have a proper dashboard.
- **Structured logging.** Logs are append-only text files, not JSON.
  Future work.

---

## 13. The shape of a typical day

Just to make this concrete. On a normal Tuesday in this current phase:

- 00:00 UTC: daemon is already running. METAR poller is hitting 11
  airport stations every 30 seconds. The 60s cycle is firing.
- Throughout the day: every cycle, we score 50–200 open markets.
  Almost none meet the edge threshold. The handful that do get logged
  to `alpha_backtest` as DRY_RUN directional decisions.
- METAR detects a 2°F jump in Chicago. The WeatherQuoter computes new
  fair values for all open KXHIGHCHI markets and logs what it would
  have posted to `weather_mm_shadow`. No live order is placed (all
  weather families are blocked).
- 14:00 UTC FOMC release. Fed-watching markets resolve. The
  `record_settlements` step picks up the outcomes, books P&L on any
  positions held, and cascades the result into the calibration table.
  Within the next cycle, the Platt curve is refit.
- 23:59 UTC: the daily fills validator wakes up, compares the canonical
  `fills_ledger` against the legacy fills view, alerts if anything
  diverges, and goes back to sleep.

No trader watching, no buttons pushed, no overnight crises. The whole
point is that this thing operates correctly without a human in the
loop. The human's job is to study the logs, fix the bugs, and decide
when each family graduates.

---

## 14. The one-paragraph summary

The bot is a long-running process that combines twenty real-world data
feeds into a single probability for each Kalshi market, compares that
probability to the market price, bets when the gap is bigger than fees,
and learns from every outcome. It uses a 60-second cycle for slow
decisions and event-driven pollers for fast ones, sizes positions
through a phase-gated track-record system, exits via a unified
health-score policy, never lets new strategies go live without first
proving themselves in a shadow-then-canary-then-full progression, and
treats data hygiene (one writer per table, atomic decision logs,
single-source-of-truth fills) as a first-class concern. It is not yet
making real money in volume; it is in the careful phase where signal
quality is being validated family by family before sizing up.
