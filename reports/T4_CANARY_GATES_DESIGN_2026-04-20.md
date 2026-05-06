# T4 — Canary live re-enable gates (design)

**2026-04-20.** Concrete thresholds for the SHADOW → LIVE transition on
weather MM and directional shadow, plus the kill-switch that auto-demotes
anything that goes wrong in live.

> **⚠️ 2026-04-21 update — §2 (state model) and §3.1 (SHADOW → LIVE gate)
> are superseded by the B+D rewrite.** A shadow-data corruption bug
> shipped 2026-04-17 invalidated the "Thompson handles it" reasoning in
> §3.1: Thompson shrinks size against noisy *real* data, but against
> fabricated near-zero-P&L data it samples a small positive multiplier
> and the `max(1, …)` floor rounds to 1 contract — an unintended live
> promotion on fake data. See
> [POSTMORTEM_SHADOW_DATA_CORRUPTION_2026-04-21.md](POSTMORTEM_SHADOW_DATA_CORRUPTION_2026-04-21.md).
>
> The state model is now **SHADOW → LIVE_CANARY → LIVE_FULL**, not
> SHADOW → LIVE. `LIVE_CANARY` is no longer legacy; it is an active
> state with a fixed 1-contract-per-quote size. Promotion to CANARY
> requires N-floor **and** positive realized shadow P&L.  Graduation
> from CANARY to FULL requires paired live/shadow P&L evidence. §2 and
> §3.1 below are kept for historical context; see §9 at the end of this
> doc for the current state model and numbers.
>
> Kill-switch (§4), rollback (§6), and the concrete next-audit triggers
> (§8) are unchanged. The operator procedure (§5) is updated inline.

The mechanism is already built and tested:

- `bot/core/sizing.py::thompson_mm_size_multiplier` — per-series Thompson draw (LIVE_FULL only)
- `bot/learning/mm_promotion.py::evaluate_mm_promotion` — N + P&L gate → LIVE_CANARY
- `bot/learning/mm_promotion.py::evaluate_mm_graduation` — CANARY → FULL paired-ratio gate
- `bot/learning/mm_promotion.py::evaluate_mm_kill_switch` — four trip conditions
- `bot/learning/mm_promotion.py::run_mm_promotion_sweep` — daily orchestrator
- `bot/learning/directional_shadow.py` — parallel state machine for directional

What this doc adds: **the numbers we'll actually use** when we flip each
series, plus the operator procedure around the flip and the specific
observations the sweep needs to satisfy before the state transition fires.

---

## 1. Why canary gates at all

The Apr-17 Phase-0 backtest proved weather signal alpha (Brier 0.09-0.21
vs 0.24 baseline on five families). It did not prove that the post-T1.1/T1.2
event-driven architecture will *realize* that edge under the new
cancel-replace quoter. The +4.7¢ historical markout has to survive:

1. The event-driven requote latency (now ~30 s METAR + 15 min forecast
   coalescing, versus 60 s cycle before).
2. The T1.2 synthetic triggers (time-decay + forecast-change) firing
   cancel-replaces even without a real METAR change.
3. Real queue-position dynamics that the shadow fill model
   (`match_shadow_fills`) approximates with "market crossed our price"
   — which is strictly more pessimistic than queue-position reality,
   but still unobserved at live.

Canary answers: **does shadow-realized P&L survive conversion to
live-realized P&L, at small size, without operator intervention?**

## 2. State model

Three states, per series (one kv_cache row per series in `mm_live:<SERIES>`):

| State | Multiplier ceiling | Who sets it |
|-------|-------------------:|-------------|
| SHADOW | 0 | default; all series start here |
| LIVE   | Thompson draw ∈ [0, cap_multiplier] | auto, N-floor met |
| LIVE_CANARY | (legacy; back-compat only) | operator manual override |

`LIVE_CANARY` remains as a kv_cache enum value so old rows parse; the
active code path is SHADOW → LIVE. The Thompson draw *is* the canary:
at n=5 the SE is ~0.45 × σ, so the multiplier comes out closer to zero
than to mean on the first draws. Exploration is baked into the sampler.

## 3. Trigger conditions

### 3.1 SHADOW → LIVE (per series, weather MM)

Single condition, checked by `evaluate_mm_promotion`:

```
n_fills >= MM_SIZING_MIN_N    (default: 5)
```

where `n_fills` is the count of settled `weather_mm_shadow` rows with
`shadow_bid_filled=1 OR shadow_ask_filled=1`.

That's it. No P&L floor at promotion — the Thompson posterior handles
size. A series with 5 losing fills produces a mean_cents ≈ −X, and
`multiplier = max(0, mu_sample / target_edge_cents)` → 0. So a bad
series is auto-sized to zero contracts without ever blocking the state
flip.

**Why not require a positive P&L floor?** Because at n=5 the sampling
noise around a true mean of +2¢/fill is ±2.6¢ (one SE). Half the
draws of a truly-edged series come back negative at n=5 — a P&L floor
at promotion would reject good series half the time. The Thompson cap
(current 1.0 = full `MM_ORDER_SIZE`) at a target edge of 2¢/fill is the
right knob: more data shrinks σ, raises the floor on worst-case draws,
and size grows organically.

**Recommended first flip.** The five weather families that passed Phase 0:

| series | family | Phase-0 Brier | reason |
|--------|--------|:-------------:|--------|
| KXHIGHNY  | NY high        | 0.211 | ✓ KNYC primary after T1.1 |
| KXHIGHCHI | CHI high       | 0.098 | ✓ tightest Brier beat |
| KXHIGHLAX | LAX high       | 0.151 | ✓ KLAX steady |
| KXHIGHAUS | AUS high       | 0.114 | ✓ passes |
| KXHIGHMIA | MIA high       | 0.186 | ✓ passes |

`KXHIGHDEN` does **not** get promoted in this wave — Phase 0 Brier 0.316
against 0.244 baseline. Keep SHADOW until the weather-ensemble-stitching
improvements in Phase 2 land or a KDEN-specific driver is built.

### 3.2 SHADOW → LIVE (per family, directional)

Mirrors MM but with a Brier floor in addition to N. Directional trades
are discrete bets, so bad calibration manifests as immediate loss;
Thompson sizing can't rescue a family whose ensemble is fundamentally
miscalibrated. `bot/learning/directional_shadow.py` owns the logic.

```
n_shadow_settled >= DIRECTIONAL_MIN_N  (default: 20)
AND  brier_ensemble < brier_market_beats_threshold  (default: 0.005 beat)
AND  family NOT IN DIRECTIONAL_BLOCKED  (KXBTC, KXETH, KXHIGHDEN)
```

### 3.3 Thompson draw (applied every quote sweep)

`sizing.thompson_mm_size_multiplier` called per LIVE series with the
per-fill P&L list:

- `target_edge_cents=2.0` — 2¢/fill maps to full cap
- `cap_multiplier=1.0` — never exceed configured `MM_ORDER_SIZE`
- `min_n=5` — under this, multiplier = 0 regardless of state

Result lands in `mm_mult:<SERIES>` kv with a 5-minute TTL; on cache miss,
resample. Same value used for `MM_ORDER_SIZE` and `MM_MAX_INVENTORY`
scaling in the quoter.

## 4. Kill-switch trip conditions

`evaluate_mm_kill_switch` checks LIVE rows every sweep. Any trip → flip
back to SHADOW and log to `promotion_events`. Four triggers:

| # | Trigger | Default threshold | Rationale |
|---|---------|-------------------|-----------|
| 1 | Single-row realized loss | 3% of equity | Hard stop — even one bad fill bigger than this should stop the series. Fires at n=1. |
| 2 | Rolling last-40 P&L floor | max($20, 2% equity) | Accumulating drawdown. $20 floor prevents over-strict tripping at low equity; 2% equity floor scales up. |
| 3 | Fill-rate floor | 10% on last 40 settled | "No-one-crosses-us" regime — we're posting but the book moved past us faster than we can react. Wide quotes or bad inventory mgmt. |
| 4 | shadow-vs-live calibration ratio | live/shadow ≥ 0.4 over ≥ 20 paired rows | The shadow fill model is a proxy. If live-realized P&L is < 40% of what shadow predicted, the proxy is drifting — stop using it to gate promotion. |

Triggers 2-4 require `MIN_LIVE_SETTLED_FOR_DEMOTION = 20` before they
can fire. Trigger 1 fires at any N (the 3% equity hard stop is absolute).

**Concrete numbers at current ~$980 equity:**

- Trigger 1: single-row loss ≤ −$29.40
- Trigger 2: 40-row sum ≤ −$20 (the $20 floor dominates)
- Trigger 3: <4 fills per 40 settled
- Trigger 4: live_sum < 0.4 × shadow_sum on ≥ 20 paired rows

At $10K funding (target), the 2%-equity bands dominate: $300 single-row
hard stop, $200 40-row drawdown floor.

## 5. Operator procedure (first flip)

1. **Pre-flight.** Verify daemon has been running ≥ 7 days on T1.1/T1.2
   with shadow rows accumulating. Check:
   ```bash
   ssh kalshi@45.55.79.193 "sqlite3 ~/autoagent/kalshi_trades.db \
     'SELECT series, COUNT(*) AS n, \
             SUM(CASE WHEN shadow_bid_filled=1 OR shadow_ask_filled=1 THEN 1 ELSE 0 END) AS fills \
      FROM weather_mm_shadow \
      WHERE ts_settle_unix IS NOT NULL GROUP BY series'"
   ```
   Each of the five promote-eligible series needs `fills >= 5`.

2. **Bakeoff review.** Run `render_bakeoff_report(conn)` on the VPS DB
   and paste output into the weekly audit. Look for:
   - Brier beat positive on MM_QUOTE rows for the candidate families
   - Realization ratio ≥ 0.5 on MM_QUOTE (half of implied edge converted
     in shadow model)
   - No single series producing >50% of paired-comparison losses

3. **Env flip.** Set `WEATHER_MM_LIVE=true` in `.env` on the VPS.
   **Do NOT** edit `MM_BLOCKED_SERIES` — leave empty. Per-series gating
   is via kv_cache, not the env var.

4. **Promote first series to CANARY.** Use the operator CLI (or direct kv_set):
   ```python
   from bot.learning.mm_promotion import set_mm_live_state, LiveState
   set_mm_live_state(conn, "KXHIGHCHI", LiveState.LIVE_CANARY, manual=True)
   ```
   Pick KXHIGHCHI first (tightest Brier, 0.098 vs 0.244). Manual=True
   marks the event so the sweep won't auto-demote on first look. The
   CANARY state posts 1 contract per quote regardless of equity — no
   Thompson draw is applied yet.

5. **48-hour CANARY observation window.** Watch `promotion_events` and
   the per-series paired-row rollup:
   - Any kill-switch trip → rollback the series. Root-cause before
     retrying.
   - CANARY stays CANARY until `evaluate_mm_graduation` finds
     `MM_GRADUATION_MIN_PAIRED_N` paired live/shadow rows with
     `live_sum / shadow_sum ≥ MM_GRADUATION_MIN_PNL_RATIO` and a positive
     shadow sum. Graduation flips CANARY → LIVE_FULL and Thompson sizing
     turns on.

6. **Promote remaining four to CANARY.** Same procedure, one every 24h
   until all five are at CANARY. Do not skip CANARY — a series must earn
   LIVE_FULL via paired evidence, not via operator flag.

7. **Steady state.** `run_mm_promotion_sweep` runs daily. It evaluates
   SHADOW → CANARY (N-floor + positive shadow P&L), CANARY → LIVE_FULL
   (paired live/shadow ratio), and kill-switch demotions every sweep.
   LIVE_FULL series adjust sizing themselves via Thompson.

## 6. Rollback / panic stop

Hardest stop: flip `WEATHER_MM_LIVE=false` in `.env` and restart the
service. Every series short-circuits to `shadow_requote_city` regardless
of state. This is the master gate documented in
`tests/daemon/test_requote_triggers.py::test_valid_reasons_frozenset_contract`
and enforced in the WeatherQuoter.

Softer stop: demote a specific series (works at any state — CANARY or
LIVE_FULL both fall straight back to SHADOW).

```python
from bot.learning.mm_promotion import set_mm_live_state, LiveState
set_mm_live_state(conn, "KXHIGHMIA", LiveState.SHADOW, manual=True)
```

`manual=True` suppresses the sweep's auto-promotion for this series
until the operator re-promotes explicitly. This is the right tool for
"I saw something weird, let me look" without needing to kill the whole
daemon.

## 7. What this does NOT cover

- **Directional live re-enable.** Separate state machine
  (`directional_shadow.py`). Requires the paired bakeoff report to show
  `directional_shadow.brier_beat > 0.005` on the clean-slice rows.
  Tracked separately.
- **Safe compounder live.** `SC_ENABLED=false`, separate design.
- **Per-ticker concentration.** A live series could concentrate into
  one ticker — not auto-prevented here. Follow-on: per-ticker exposure
  cap in the WeatherQuoter, documented in Phase-1 §7.
- **Fill ledger consistency.** T3.1-T3.4 build the canonical immutable
  fills ledger that the kill-switch's "realized P&L" ultimately depends
  on. Until that lands, the kill-switch reads
  `weather_mm_shadow.shadow_pnl_cents` (post-hoc annotation) — good
  enough for shadow-mode and early live, not good enough for scale.

## 8. Concrete next-audit triggers

Run `render_bakeoff_report(conn)` + a promotion-readiness SQL snapshot at:

1. **N=5 fills per series** — "can we promote?" audit.
2. **N=20 live settled** — "can the kill-switch fire?" audit (all
   triggers are armed at this point).
3. **N=100 live settled per series** — full posterior re-evaluation.
   At this N, the Thompson SE is ~0.2σ; sizing is close to deterministic.

---

**Bottom line.** The mechanism is built and tested (`tests/test_mm_promotion.py`,
`tests/test_sizing.py`, both green). This doc locks the numbers we'll
actually use on day one. First flip is KXHIGHCHI, manual, to
**LIVE_CANARY** (not LIVE_FULL), after the daemon has accumulated five
filled settled rows per candidate series with positive shadow P&L.

---

## 9. Current state model (post B+D, 2026-04-21)

Supersedes §2 and §3.1 above. Three active states per series, one
kv_cache row per series in `mm_live:<SERIES>`:

| State | Size per quote | Transition in | Transition out |
|-------|----------------|---------------|----------------|
| SHADOW | 0 (shadow-only logging) | default | `evaluate_mm_promotion` → CANARY |
| LIVE_CANARY | fixed 1 contract (multiplier = `1.0 / MM_ORDER_SIZE`) | SHADOW + both gates pass | `evaluate_mm_graduation` → LIVE_FULL, or kill-switch → SHADOW |
| LIVE_FULL | Thompson draw ∈ [0, cap_multiplier] × MM_ORDER_SIZE | CANARY + paired evidence | kill-switch → SHADOW |

### 9.1 SHADOW → LIVE_CANARY (two-gate)

`evaluate_mm_promotion` requires **both**:

```
n_fills >= MM_SIZING_MIN_N                                    (default 5)
AND  pnl_per_fill_cents >= MM_CANARY_MIN_PNL_PER_FILL_CENTS   (default 1.0)
```

where `n_fills` counts settled `weather_mm_shadow` rows with
`shadow_bid_filled=1 OR shadow_ask_filled=1`, and `pnl_per_fill_cents` is
the mean realized `shadow_pnl_cents` across those rows.

The P&L floor is the direct response to the 2026-04-17 incident: fake
data ( `market_yes_bid=0, market_yes_ask=0` everywhere) produced near-zero
P&L with near-zero variance, which would have auto-promoted without the
floor. The floor trades "some truly-edged series get delayed at n=5 due
to sampling noise" for "no corrupted-data regime can sneak through." A
delayed series stays SHADOW, accumulates more fills, and promotes when
realized pnl/fill crosses 1¢ — **delayed, not missed.**

### 9.2 CANARY sizing

`get_mm_order_size_multiplier` returns `1.0 / max(1, MM_ORDER_SIZE)` for
LIVE_CANARY regardless of equity, shadow P&L, or Thompson posterior.
Applied against `MM_ORDER_SIZE` in the quoter, this rounds to exactly 1
contract per quote. Worst-case single-row blast radius at 1 contract ×
$1 notional = $1; 20 canary fills across N paired rows = ≤ $20
maximum drawdown before graduation evaluates.

### 9.3 CANARY → LIVE_FULL (paired-ratio gate)

`evaluate_mm_graduation` requires **all** of:

```
paired_n  >= MM_GRADUATION_MIN_PAIRED_N                  (default 20)
shadow_sum > 0                                            (positive shadow P&L)
live_sum / shadow_sum >= MM_GRADUATION_MIN_PNL_RATIO      (default 0.5)
```

where a "paired row" is a settled `weather_mm_shadow` row whose
`ts_unix` falls inside the CANARY period and has both a live fill (from
`fills_ledger` once T3.3 lands; `mm_processed_fills` until then) and a
shadow annotation.

Rationale: at CANARY, we're measuring whether `match_shadow_fills`'s
"market crossed our price" proxy actually predicts live realization.
Anything below 0.5 means the proxy is an over-optimistic fantasy, and
Thompson-sized full deployment on that proxy is unsafe.

### 9.4 Tunables (bot/config.py)

```
MM_SIZING_MIN_N                       = 5    # gate §9.1
MM_CANARY_MIN_PNL_PER_FILL_CENTS      = 1.0  # gate §9.1
MM_GRADUATION_MIN_PAIRED_N            = 20   # gate §9.3
MM_GRADUATION_MIN_PNL_RATIO           = 0.5  # gate §9.3
```

All four are env-overridable. Changes to any of them re-run the full
promotion sweep on next tick.

### 9.5 Kill-switch remains §4

No change. `evaluate_mm_kill_switch` still fires on any of the four
triggers and demotes CANARY or LIVE_FULL straight to SHADOW. The T4
design's kill-switch trigger #4 (live/shadow ≥ 0.4) is the *per-tick*
read; graduation's 0.5 floor is the *promotion* read. They use the same
ratio math, but 0.5 at promotion is tighter than 0.4 at demotion —
intentional: easier to keep running than to start running.

### 9.6 What §2 got wrong

§2 described LIVE_CANARY as "legacy back-compat only" and said the
Thompson draw "is the canary." That was correct in expectation but
unsafe under data-integrity failure: a fabricated near-zero P&L series
produces a Thompson sample of ~0, but `max(1, int(round(MM_ORDER_SIZE ×
mult)))` floors to 1 contract — which is enough to post a live order on
a fake signal. The B+D rewrite makes CANARY an explicit mandatory stop
with fixed sizing so that data integrity is verified *before* Thompson
sizing ever fires.
