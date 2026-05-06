# Post-mortem — shadow MM data corruption (2026-04-17 → 2026-04-21)

**Author:** Josh (bot ops) · **Status:** resolved · **Severity:** high (auto-promotion fired on fake data)

Two silent bugs in the weather-MM shadow pipeline produced 20,478
`weather_mm_shadow` rows with fabricated fills and near-zero P&L, which
would have auto-promoted two families to LIVE_FULL had the master
`WEATHER_MM_LIVE` gate not been disabled. No live orders were placed.
The corruption persisted for ~4 days and was caught during a routine
post-deploy VPS check while landing T3.1.

## Timeline

- **2026-04-17** — `bot/daemon/weather_quoter.py` ships with
  `_safe_cents(None) → 0` and reads `m.get("yes_bid")` / `m.get("yes_ask")`.
  Kalshi's `/markets?status=open` response uses `yes_bid_dollars` /
  `yes_ask_dollars` (stringified decimal dollars) on this endpoint; the
  unsuffixed keys are absent. Every call returns `None`. `_safe_cents`
  swallows that into `0`. Every shadow row ends up with
  `market_yes_bid=0, market_yes_ask=0`.
- **2026-04-17 → 04-21** — `match_shadow_fills` scans 20k+ rows. Its
  bid-fill condition (`m_ask <= bid_c`) is always satisfied because
  `m_ask=0` is ≤ any posted bid. Every row gets `shadow_bid_filled=1`.
  At settlement, `annotate_shadow_pnl` computes P&L against the fake
  entry price (`bid_c` = our own proposal) and records near-zero values.
- **2026-04-20** — [reports/T4_CANARY_GATES_DESIGN_2026-04-20.md](reports/T4_CANARY_GATES_DESIGN_2026-04-20.md)
  is written on the assumption that shadow data is real. §3.1 explicitly
  rejects a P&L floor at promotion: *"the Thompson posterior handles
  size ... a P&L floor at promotion would reject good series half the
  time."*
- **2026-04-21 morning** — T3.1 fills-ledger work completes and is
  redeployed. Routine post-deploy sanity check pulls shadow stats from
  the VPS and spots `market_yes_bid=0, market_yes_ask=0` across all rows.
- **2026-04-21 mid-day** — diagnosis: two bugs, both in the shadow write
  path. `run_mm_promotion_sweep` is inspected and found to have already
  flipped `KXHIGHCHI` and `KXHIGHNY` to `LIVE_FULL` on the fake fills.
- **2026-04-21 mid-day** — revert: `kv_cache` entries for the two live
  families are deleted manually. No live orders ever placed because
  `WEATHER_MM_LIVE=false` was in effect throughout.
- **2026-04-21 afternoon** — fix designed (plan "B+D": P&L gate +
  CANARY state with graduation). Ships as two commits. 990 tests green.
- **2026-04-21 afternoon** — redeploy. VPS verification shows fresh
  rows have real bid/ask values (e.g. `KXHIGHNY-26APR22-T57: bid=45
  ask=46`). Contaminated rows reset to NULL so the hardened matcher
  will re-evaluate them.

## Root causes

### Bug 1 — wrong API field name

Kalshi's `/markets?status=open&series_ticker=...&limit=200` response:

```json
{ "ticker": "KXHIGHNY-26APR22-T64",
  "yes_bid_dollars": "0.0200",
  "yes_ask_dollars": "0.0300",
  "no_bid_dollars":  "0.9700",
  "no_ask_dollars":  "0.9800",
  "yes_bid_size_fp": "113.00", ... }
```

`bot/daemon/weather_quoter.py::_parse_market` read `m.get("yes_bid")`
and `m.get("yes_ask")`. Both absent on this endpoint. `trade.py` line
487 had the correct fallback pattern (`m.get("yes_bid") or
m.get("yes_bid_dollars")`) but the quoter did not.

### Bug 2 — `_safe_cents(None) → 0`

```python
# pre-fix
def _safe_cents(val) -> int:
    if val is None:
        return 0            # ← silently conflates missing with zero
    ...
```

Kalshi's minimum quoted price is 1¢, so 0 can *only* mean "no
observation." Treating it as a real price poisoned the matcher: a
posted BID at 50¢ "fills" against a non-existent ASK of 0¢ on every
subsequent snapshot.

### The multiplicative failure

Bug 1 made Kalshi's real book invisible. Bug 2 turned that invisibility
into a fabricated crossing price. Either bug alone would have been
noisy-but-recoverable; together they produced a coherent-looking but
entirely fake shadow ledger — the kind of data corruption that doesn't
trip any obvious alarm because every downstream stat looks reasonable.

## What should have caught this earlier

1. **No invariant assertion on market_yes_bid / market_yes_ask.** The
   schema allowed 0 but Kalshi never returns 0. An insert-side check
   would have flagged it immediately. *(Fix: `_safe_cents` now returns
   `None` for missing or zero, and the column stays NULL.)*
2. **No cross-check against a second reader of the same endpoint.**
   `trade.py` had the right field-name fallback for years; the quoter
   shipped without it. A shared parser helper would have prevented
   divergence. *(Follow-on: extract a `parse_kalshi_market_book` helper
   that both call sites use.)*
3. **Promotion gate had no data-integrity guard.** T4 design §3.1
   explicitly trusted the shadow data. The first non-trivial
   data-integrity failure promoted two families. *(Fix: B+D adds a
   positive-P&L gate and a bounded CANARY intermediate state.)*
4. **Dashboards would have noticed.** We don't have any. The corruption
   surfaced from a human glancing at `SELECT DISTINCT market_yes_bid
   FROM weather_mm_shadow` on the VPS. *(Follow-on: a per-cycle health
   log already writes to the daemon log — add an assert that
   `market_yes_bid` distribution has >1 distinct non-null value per
   series over the last hour.)*

## Design implication for T4

T4's "Thompson handles it" reasoning for rejecting a promotion P&L
floor was correct in expectation — at n=5 around a true +2¢/fill mean,
~50% of samples come back negative and a floor would reject genuinely
edged series half the time. The flaw wasn't the math; it was assuming
shadow data is real. When it isn't, Thompson doesn't save you — the
samples are all near zero with near-zero variance, the multiplier comes
out near zero but non-negative, and `max(1, int(round(MM_ORDER_SIZE *
mult)))` rounds to 1 contract. The series gets promoted to LIVE with 1
contract on fake data.

B+D shifts the tradeoff:

- **P&L floor at promotion** rejects the all-zero regime outright.
  Cost: rejects some truly-edged series at n=5 due to sampling noise.
  They don't die — they stay SHADOW, accumulate more fills, and promote
  when their realized `pnl_per_fill` crosses 1¢. Delayed promotion, not
  missed promotion.
- **Bounded CANARY size** decouples "the gate said yes" from "uncapped
  Thompson-scaled exposure." Even a bad promotion burns at most 1
  contract × K-canary-rows before either kill-switch or graduation
  evaluation fires.
- **Paired live/shadow graduation** stops trusting the shadow model on
  its own word. A series has to prove that realized live P&L tracks
  predicted shadow P&L before it gets full Thompson sizing.

## Fixes shipped (2026-04-21)

| Change | File | Why |
|--------|------|-----|
| `_safe_cents` returns `Optional[int]`; zero → None | [bot/daemon/weather_quoter.py:993](bot/daemon/weather_quoter.py:993) | Propagate missing observations as NULL instead of 0. |
| `_parse_market` falls back to `_dollars` fields | [bot/daemon/weather_quoter.py:697](bot/daemon/weather_quoter.py:697) | Match Kalshi's actual `/markets` list response. |
| `match_shadow_fills` — zero-guard + no-observation-don't-lock | [bot/learning/mm_promotion.py:297](bot/learning/mm_promotion.py:297) | Treat 0 as missing; don't permanently mark a row "no_fill" if the lifetime window had no valid book data. |
| `evaluate_mm_promotion` — N-floor + P&L floor → LIVE_CANARY | [bot/learning/mm_promotion.py:528](bot/learning/mm_promotion.py:528) | Block promotion on non-positive shadow P&L. |
| `evaluate_mm_graduation` (new) — paired live/shadow ratio gate | [bot/learning/mm_promotion.py:569](bot/learning/mm_promotion.py:569) | CANARY → LIVE_FULL requires live realizing ≥50% of shadow-predicted P&L over ≥20 paired rows. |
| `get_mm_order_size_multiplier` — CANARY = 1/MM_ORDER_SIZE | [bot/learning/mm_promotion.py:163](bot/learning/mm_promotion.py:163) | Canary sizing is fixed 1 contract regardless of equity. |
| Three env-tunable thresholds added | [bot/config.py:164-178](bot/config.py:164) | `MM_CANARY_MIN_PNL_PER_FILL_CENTS=1.0`, `MM_GRADUATION_MIN_PAIRED_N=20`, `MM_GRADUATION_MIN_PNL_RATIO=0.5`. |
| 20,478 contaminated rows reset on VPS | VPS SQLite | Cleared fake `shadow_bid_filled=1` and `shadow_pnl_cents` so the hardened matcher can re-evaluate. |
| `_parse_market` `_dollars` test; 4 new tests covering the regression | [tests/test_weather_quoter.py](tests/test_weather_quoter.py) | Regression guard for the field-name fallback. |
| `_safe_cents` zero/None tests updated | [tests/test_weather_quoter.py](tests/test_weather_quoter.py) | Reflect Optional return type. |
| 10+ tests for B+D (P&L gate, CANARY multiplier, graduation, matcher zero-guard) | [tests/test_mm_promotion.py](tests/test_mm_promotion.py) | 990 total (up from 968). |

## Follow-ons (not shipped)

1. **Shared Kalshi market-book parser.** Both `trade.py` and the quoter
   hand-roll book extraction. One helper, one fallback list, one place
   to fix next time Kalshi renames a field.
2. **Per-series health-log assertion.** Daemon already emits a 300s
   health log; add "distinct non-null `market_yes_bid` values per
   series in the last hour > 1." This would have caught the bug within
   minutes of the 04-17 deploy.
3. **Canary-to-full graduation telemetry.** Log `evaluate_mm_graduation`
   metrics each sweep even when it returns False, so operators can
   watch the paired-row ratio trend up without querying the DB.
4. **Update T4 design doc.** Mark §3.1 / §2 superseded; point at this
   postmortem. Done in the companion change to
   [T4_CANARY_GATES_DESIGN_2026-04-20.md](reports/T4_CANARY_GATES_DESIGN_2026-04-20.md).

## Lessons

- **Silent defaults are the worst kind.** `int(None) → 0` feels
  defensive; it's offensive. Data-boundary code should fail loudly or
  propagate `Option`-style uncertainty.
- **"Thompson handles it" only handles magnitude, not integrity.**
  Any sampler applied to fake data samples from fake posteriors.
- **A rule that's correct in expectation can still be unsafe.** Our
  T4-design argument against a P&L floor was mathematically sound and
  operationally dangerous. The B+D floor is strictly worse in
  expectation — and strictly safer under the failure modes that
  actually occur.
- **Two bugs compose into one silent failure.** Neither bug 1 nor bug 2
  alone would have been this invisible. Ship both together and the
  signal cancels out.
