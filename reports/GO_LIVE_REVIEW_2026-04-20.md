# Go-live review — when can we trade real money again?

**2026-04-20 · Josh Lu**

---

## TL;DR

Three independent gates block real-money trading today. All three must be
green before we flip `WEATHER_MM_LIVE=true`. One is close; two are
genuinely blocking.

| Gate | State | Owner | Unblocks when |
|------|-------|-------|---------------|
| **G1. Shadow data proves conversion** | ⏳ waiting on data | daemon | Five weather families each show ≥ 5 filled shadow rows settled, realization ratio ≥ 0.5, no single-row loss > 3% of equity-equivalent on shadow |
| **G2. Canonical fills ledger** (T3.1–3.4) | 🚧 not built | engineering | Immutable ledger shipped + dual-run vs legacy ≥ 7 days with zero divergence |
| **G3. Kill-switch armed on real money** | ✅ code done | — | Visible in `promotion_events` firing correctly during first-series canary (N=20 live settled) |

Everything else (signal quality, architecture, observability, operator
procedure) is done or documented. The two remaining blockers are pure
integration questions — shadow data needs calendar time to accrue, and
the fills ledger needs engineering time.

---

## Where we are today

### ✅ What's in place

- **Signal alpha.** Phase-0 backtest on April 17: five weather families
  beat the no-edge baseline by 4–8× on per-family Brier (0.09-0.21 vs
  0.24). See [BACKTEST_APR17.md](BACKTEST_APR17.md).

- **Event-driven MM architecture.** `bot/daemon/` supervisor + poller +
  scheduler + dispatcher + weather_quoter + smart_gates. T1.1 flipped
  NY primary from KJFK→KNYC. T1.2 added time-decay and forecast-change
  requote drivers. See [AUDIT_T1_2_2026-04-20.md](AUDIT_T1_2_2026-04-20.md).

- **Per-series live-state gate.** `bot/learning/mm_promotion.py`:
  kv_cache-backed SHADOW / LIVE state with N-floor auto-promotion,
  four-trigger kill switch (single-loss, rolling drawdown, fill-rate,
  shadow-vs-live calibration), and Thompson-sampled sizing
  (`bot/core/sizing.py`).

- **Canary thresholds.** Concrete first-flip procedure, per-series
  recommended order, rollback procedure: see
  [T4_CANARY_GATES_DESIGN_2026-04-20.md](T4_CANARY_GATES_DESIGN_2026-04-20.md).

- **Decision logging.** Every quote / directional evaluation / shadow
  decision writes one row to `alpha_backtest` with ensemble estimate,
  raw market snapshot, and on-settlement outcome backfill
  (`bot/learning/alpha_log.py`).

- **Bakeoff report.** Per-strategy × family realization ratio + paired
  ticker head-to-head comparison (`bot/learning/bakeoff.py`, shipped
  with T2 today).

- **Writer-ownership registry.** Single-owner-per-table enforced by
  CI test (`tests/test_writer_ownership.py`). Prevents drift.

- **Secret hygiene.** `.kalshi_private_key.pem` removed from the repo
  tree; CI guard (`tests/test_no_secrets_in_repo.py`) prevents it
  coming back.

- **Test coverage.** 894 tests passing as of this report.

### ⏳ What needs calendar time

- **Shadow data accumulation.** T1.2 just shipped. The daemon needs
  at least a week of real-world driving before we have enough paired
  shadow rows + settlements to evaluate G1. See §2 below.

### 🚧 What still needs engineering

- **Canonical fills ledger (T3.1-T3.4).** Today the P&L the kill-switch
  reads (`weather_mm_shadow.shadow_pnl_cents`) is computed post-hoc
  from Kalshi's fill feed *and* cross-annotated in the shadow table.
  Two readers of the same data means two chances to drift. At shadow
  scale this is tolerable; at live scale, a divergence between what
  we think we filled at and what Kalshi says we filled at is the kind
  of bug that loses money silently. T3 builds the one immutable
  ledger all other tables derive from. **Must land before live.**

- **Observability writer consolidation follow-ups.** T1.3 locked the
  registry but did not restructure `trade.py`'s many small writers.
  Those stay as-is until T3.4 ("freeze trade.py") — not a live blocker.

### ❌ Blockers that do **not** apply

- **Signal quality** — Phase 0 proved out.
- **Adverse selection at entry** — the live DB showed +6.12¢ average
  markout, 99.9% favorable. We don't have an entry problem; we had an
  exit problem (fixed by graduated exits + synthetic sell) and a
  quote-latency problem (fixed by the event-driven architecture).
- **Calibration of KXBTC / KXETH / KXHIGHDEN** — these families remain
  blocked for directional (`DIRECTIONAL_BLOCKED` in config). They
  don't block the MM re-enable because we're not promoting them.

---

## Gate details

### G1. Shadow data proves conversion

**What we need to see.**

Query against the VPS DB at roughly one-week post-deploy:

```sql
SELECT
  series,
  COUNT(*)                                                 AS n_settled,
  SUM(CASE WHEN shadow_bid_filled=1 OR shadow_ask_filled=1 THEN 1 ELSE 0 END) AS n_fills,
  SUM(shadow_pnl_cents)                                    AS shadow_pnl,
  MIN(shadow_pnl_cents)                                    AS worst_row,
  AVG(shadow_pnl_cents)                                    AS per_row_mean
FROM weather_mm_shadow
WHERE ts_settle_unix IS NOT NULL
GROUP BY series
ORDER BY series;
```

Plus the bakeoff report:

```python
from bot.learning.bakeoff import render_bakeoff_report
print(render_bakeoff_report(conn, clean_market_slice=True, min_n=5))
```

**Acceptance criteria.**

Each of the five Phase-0-approved series (KXHIGHNY, KXHIGHCHI, KXHIGHLAX,
KXHIGHAUS, KXHIGHMIA):

1. `n_fills >= 5`
2. `brier_ensemble < brier_market - 0.005` (Phase 0 gate leg 2 on shadow)
3. `worst_row > -3% * equity_cents` (no single-row catastrophe in shadow)
4. `realization_ratio >= 0.5` on MM_QUOTE rows — at least half of implied
   edge converting in the shadow model
5. Trigger-reason cross-tab from T1.2 audit §4 item 3: zero cases of two
   rows within cooldown_s for the same series

KXHIGHDEN is **not** expected to pass. Its Brier was 0.316 vs 0.244 in
Phase 0 — it stays SHADOW until a KDEN-specific driver is built.

**Time to unblock.** One week post-deploy, assuming ~10 settlements/day
across the five series. The T1.2 audit projects 660-1,080 shadow rows/day
total across the six series, which is ~130/series/day — so N=5 fills per
series arrives in 2-3 days under normal conditions, with a comfortable
margin for analysis.

### G2. Canonical fills ledger (T3.1-T3.4)

**Why it's a live-blocker.**

The kill-switch's Trigger 1 ("single-row realized loss > 3% of equity")
fires at N=1 — the first live fill. For this to be trustworthy, the
"realized loss" number must be the authoritative one. Today:

- Kalshi's `/v2/portfolio/fills` pagination gives us the raw fills.
- `track_fills()` in trade.py parses them and inserts into
  `mm_processed_fills`.
- `weather_mm_shadow.shadow_pnl_cents` is computed later via
  `annotate_shadow_pnl`, joining the shadow row against the settlement
  outcome and the inferred fill price.

Three data flows, three chances for an error to hide one from another.
In shadow mode this manifests as a bug report. In live mode it
manifests as a wrong decision by the kill-switch on real money.

**What T3 builds.**

T3.1: `fills_ledger` — append-only, Kalshi-sourced, immutable. One
row per Kalshi fill event. Primary key = Kalshi fill_id.

T3.2: Dual-run validator — replay the last 30 days of fills in parallel
to the new ledger, compare net-position reconciliations row-by-row with
the existing path. Every divergence → investigate before T3.3.

T3.3: Migrate readers — `track_fills`, `manage_positions`,
`record_settlements`, `mm_promotion.py`, `bakeoff.py` all read from
`fills_ledger` instead of their various intermediaries.

T3.4: Freeze `trade.py` — no new writers into anything other than
`opportunity_log` and `position_health_log`. Legacy inserts become
read-only from the ledger.

**Acceptance criteria.**

1. Ledger built, indexed, written from a single module.
2. ≥ 7 days dual-run with zero divergence on any reconciled position.
3. `weather_mm_shadow.shadow_pnl_cents` re-derived from the ledger,
   matches the post-hoc annotation byte-for-byte on historical rows.

**Time to unblock.** 1-2 weeks engineering + 1 week dual-run observation.
This is the critical-path blocker. No real-money trading until this
ships.

### G3. Kill-switch armed on real money

**What needs to happen.**

The kill-switch code is done and tested
(`tests/test_mm_promotion.py`, `tests/test_sizing.py`). But "code
green" and "kill-switch demonstrably trips on a real bad outcome" are
different claims.

**Acceptance criteria.**

1. First series (KXHIGHCHI per T4) promoted with `manual=True`.
2. Within 48 hours of first LIVE row on this series, confirm via
   `promotion_events` table that the kill-switch is being evaluated
   every sweep (not silently failing).
3. At N=20 live settled rows on this series, triggers 2-4 become
   eligible — confirm the thresholds compute correctly by running
   `evaluate_mm_kill_switch(conn, "KXHIGHCHI", equity_dollars=...)`
   manually from a Python shell and checking the returned metrics dict.
4. No auto-demotion in the first 48 hours (normal conditions should
   leave all four triggers clear at this scale).

**Time to unblock.** First-series canary only. Once one series is
observed live with the kill-switch active and clean, G3 is retired
as a gate — it becomes steady-state operational monitoring.

---

## Proposed timeline

Assuming deploy of T1.1/T1.2 to VPS happens this week:

| Week | Milestone |
|------|-----------|
| W0 (now) | T0.5 / T1.3 / T2 / T4 design complete. Deploy T1.2 to VPS. |
| W0 + 2d | First per-series fill count report. Confirm daemon healthy, poller firing, shadow rows accruing with `trigger_reason` distribution matching T1.2 projections. |
| W0 + 1w | Full N=5 fills satisfied for all five candidate series (projected). Run the bakeoff report. Evaluate G1 acceptance criteria. |
| W0 + 1w | T3.1 ledger schema + writer lands in branch (parallel track, no effect on live state). |
| W0 + 2w | T3.2 dual-run validator running on VPS in parallel. |
| W0 + 3w | T3.3 readers migrated. T3.4 trade.py frozen. |
| W0 + 4w | 7-day dual-run window elapsed with zero divergence → G2 green. |
| W0 + 4w | Manual promote KXHIGHCHI to LIVE with `manual=True`. Begin G3 canary observation. |
| W0 + 4w + 48h | G3 retires. Promote KXHIGHNY, then 24h later KXHIGHLAX, KXHIGHAUS, KXHIGHMIA. |
| W0 + 5w | All five families LIVE under Thompson sizing. Steady-state operation begins. |

**Earliest plausible real-money trade: ~4 weeks from today**, gated on
T3 engineering timeline. G1 (shadow data) is on a one-week track and
won't be the binding constraint.

## What could change the timeline

- **G1 fails.** Shadow realization ratio < 0.5 on some series →
  investigate (fill model too optimistic? spread gate too loose?
  cooldown too short?). Hold the line until we understand.
- **T3 dual-run shows divergence.** Root-cause before live, no
  exceptions. This is exactly the class of bug that T3 exists to
  prevent shipping to live.
- **Equity drops below $500.** Rethink the `$20`-floor / `3%`-single-loss
  thresholds; at low equity they become too forgiving.

## What this doc does NOT commit to

- **Non-weather MM.** No target series outside weather today. If we
  want KXFED MM (the recurring Fed-decision series), that's a separate
  Phase-2 design — KXFED has liquidity characteristics very unlike
  weather and should not inherit thresholds unmodified.
- **Directional live re-enable.** Separate gate,
  `bot/learning/directional_shadow.py`. Requires its own
  realization-ratio evidence on paired `directional_shadow` rows —
  specifically brier_beat > 0.005 on the clean-mid slice over N≥30
  settlements.
- **Safe Compounder.** `SC_ENABLED=false`. No change proposed.

---

**In one sentence.** We trade real money again when (1) shadow data
over the next week shows five weather families converting ≥50% of
implied edge to realized shadow-P&L, (2) a canonical fills ledger lands
in 3-4 weeks and dual-runs cleanly for a week, and (3) the first
manual canary flip of KXHIGHCHI clears a 48-hour observation window
with the kill-switch demonstrably alive.
