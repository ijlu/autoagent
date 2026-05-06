# T3 — Canonical fills ledger (scoping)

**Status.** Pre-implementation. This doc is for the separate session that
builds T3.1. Read this, react to the open questions at §7, then start
coding.

**Why it exists.** The kill-switch in `bot/learning/mm_promotion.py`
reads "realized P&L" derived from three independent write paths
(Kalshi API → `mm_processed_fills`, shadow row → `weather_mm_shadow`,
settlement → `settlements`). Three writers = three chances for drift.
At shadow scale this is a bug report; at live scale it's the class of
silent wrong number that loses real money. T3 collapses it to one
immutable write path that every reader derives from.

---

## 1. Goals (what "done" looks like)

1. **One append-only table** (`fills_ledger`) whose rows map 1:1 with
   Kalshi fill events, keyed by a primary key that Kalshi itself owns.
2. **One writer module** (`bot/daemon/fills_writer.py`) that is the only
   path by which new ledger rows are created. All `INSERT INTO
   fills_ledger` statements live here.
3. **Idempotent ingestion.** The writer can be called with the same
   Kalshi fill page N times and produces the same ledger state. Safe
   under systemd restart, Kalshi pagination retry, and deploy-mid-cycle.
4. **Dual-run validator.** A new module (`bot/learning/fills_validator.py`
   or inside the writer) replays the last N days through both the old
   path (`mm_processed_fills`) and the new ledger, reports row-by-row
   divergence. Must show zero divergence for 7 consecutive days before
   T3.3 migrates readers.
5. **Reader migration** (T3.3) — every consumer of fill data (kill-switch
   P&L, shadow row annotation, `record_settlements`, graduated exits)
   reads from the ledger, never from the intermediate tables.
6. **trade.py freeze** (T3.4) — no new fills-derived writers land in
   `trade.py`; the existing ones become read-through caches of the
   ledger.

## 2. Non-goals (explicitly out of scope for T3.1)

- Historical reconstruction. We do NOT back-fill ledger rows for Kalshi
  fills that predate the writer going live. The dual-run comparison is
  only meaningful on rows the writer captured forward. Pre-T3 data
  stays in `mm_processed_fills` as a read-only legacy table.
- Settlement reconciliation change. Settlements continue to live in
  `settlements`. T3 is about fills, not outcomes.
- Position-inventory recomputation. `mm_inventory` stays; it's derived
  from fills and will continue to be. But T3.3 will change its update
  path to read from `fills_ledger` rather than from the `fills_api`
  live call.

## 3. Kalshi fill-event shape

The `GET /v2/portfolio/fills` endpoint returns page-based JSON like:

```json
{
  "fills": [
    {
      "trade_id": "e9d2c…",       // Kalshi's unique trade/fill ID
      "order_id": "ord_…",        // our originating order
      "ticker": "KXHIGHNY-26APR20-B7476",
      "side": "yes",
      "action": "buy",             // buy | sell
      "count": 10,                 // contracts
      "yes_price": 47,             // cents (0-100)
      "no_price": 53,              // cents
      "is_taker": false,
      "created_time": "2026-04-20T18:23:11.402Z",
      "client_order_id": "mm_abc…" // our tag
    }
  ],
  "cursor": "…"                   // next-page token; empty when done
}
```

`trade_id` is globally unique and stable. This is our primary key.
`order_id` is a secondary index (we may want "all fills for this order").
`ticker` + `side` + `created_time` is the tuple most readers join on.

Fields `yes_price` and `no_price` always sum to 100. For a YES buy at
47¢, the row records `yes_price=47, no_price=53, side=yes, action=buy`.
The fee (maker vs taker) is implied by `is_taker`.

## 4. Proposed schema

```sql
CREATE TABLE IF NOT EXISTS fills_ledger (
    -- ═══ Identity (immutable, Kalshi-owned) ═══════════════════════════
    trade_id              TEXT    PRIMARY KEY,     -- Kalshi fill_id
    order_id              TEXT    NOT NULL,        -- Kalshi order_id
    client_order_id       TEXT,                    -- our tag (nullable: manual orders)
    ticker                TEXT    NOT NULL,
    series                TEXT    NOT NULL,        -- derived at write time
    family                TEXT    NOT NULL,        -- derived at write time

    -- ═══ Fill semantics ═══════════════════════════════════════════════
    side                  TEXT    NOT NULL,        -- 'yes' | 'no'
    action                TEXT    NOT NULL,        -- 'buy' | 'sell'
    contracts             INTEGER NOT NULL,
    yes_price_cents       INTEGER NOT NULL,        -- 0-100
    no_price_cents        INTEGER NOT NULL,        -- 0-100 (yes + no = 100)
    is_taker              INTEGER NOT NULL,        -- 0 | 1
    fee_cents             INTEGER NOT NULL,        -- computed at write, cached

    -- ═══ Time ═════════════════════════════════════════════════════════
    fill_ts_iso           TEXT    NOT NULL,        -- Kalshi's created_time
    fill_ts_unix          REAL    NOT NULL,        -- parsed to epoch
    ingested_ts_unix      REAL    NOT NULL,        -- when we saw it

    -- ═══ Write-time context (derived once, never updated) ═════════════
    live_mode             INTEGER NOT NULL,        -- 1 if WEATHER_MM_LIVE=true at fill time
    source                TEXT,                    -- 'mm_quote' | 'directional' | 'manual' | 'unknown'
    cycle_id              TEXT                     -- for joining to opportunity_log
);

CREATE INDEX IF NOT EXISTS idx_fills_ticker_ts ON fills_ledger(ticker, fill_ts_unix);
CREATE INDEX IF NOT EXISTS idx_fills_order_id  ON fills_ledger(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_series_ts ON fills_ledger(series, fill_ts_unix);
CREATE INDEX IF NOT EXISTS idx_fills_family_ts ON fills_ledger(family, fill_ts_unix);
```

**Why these indexes.** `ticker + ts` covers per-market reconciliation
and shadow-row join. `order_id` covers "all fills for this order" (used
by `match_shadow_fills` when a quote sees one bid fill then an ask fill
minutes apart). `series` and `family` cover aggregate kill-switch P&L
computation.

**Why `live_mode`, `source`, `cycle_id`.** Read-time joins against
`opportunity_log` and `weather_mm_shadow` cost a lot of index seeks.
Caching these three fields at write time pays for itself within the
first week — and they're immutable at the point of the fill, so
denormalizing doesn't create a consistency headache.

**Why `fee_cents` cached.** Fee math is `kalshi_maker_fee` vs
`kalshi_taker_fee` with price-dependent rounding. Writing it once means
every reader doesn't recompute. If the fee formula changes (it has
before — see CLAUDE.md §Money), we re-derive from stored fields and
backfill, but in steady-state the cache is always correct.

## 5. Writer design

```python
# bot/daemon/fills_writer.py

class FillsWriter:
    def __init__(self, conn, *, api, source_tagger):
        self.conn = conn
        self.api = api
        self.source_tagger = source_tagger  # callable: client_order_id -> 'mm_quote' | ...

    def ingest_page(self, fills: list[dict], *, live_mode: bool) -> int:
        """Ingest one Kalshi fill page. Returns number of new rows inserted.

        Idempotent: rows whose trade_id already exists are silently skipped
        via INSERT OR IGNORE. Caller can page through /fills multiple times
        and get identical ledger state.
        """
        ...

    def sync_since(self, since_unix: float, *, live_mode: bool) -> int:
        """Paginate through /v2/portfolio/fills with min_ts = since_unix.
        Ingest every page until cursor empty. Returns total rows inserted.

        Called from the scheduler once per cycle. since_unix comes from
        max(fill_ts_unix) in the ledger, or daemon-start if table empty.
        """
        ...
```

Design properties:

1. **Single-call idempotency.** `INSERT OR IGNORE` on `trade_id` PK.
   Never UPDATE. Fills don't change after they happen.
2. **No write lock held across API calls.** Fetch all pages first, then
   acquire `DB_WRITE_LOCK` for the insert batch. Holding the lock for
   HTTP round-trips would starve cycle writes.
3. **Source tagging is injectable.** The writer doesn't know how to
   classify a fill as `mm_quote` vs `directional`; it asks
   `source_tagger(client_order_id)`. Keeps strategy concerns out of
   the ledger writer.
4. **Never raises into the caller.** Logs a warning on API failure,
   returns 0. The ledger is auxiliary to the live trading loop — a
   broken fetch must not crash the cycle.

## 6. Dual-run validator design

```python
# bot/learning/fills_validator.py

def compare_last_n_days(conn, *, n_days: int = 7) -> ValidationReport:
    """Compute divergence between fills_ledger and mm_processed_fills
    over the last n days.

    Per-ticker:
      - sum(contracts_filled) per side
      - sum(cents_transacted) per side
      - sum(fee_cents)

    Returns structured diff. Zero divergence → empty report.
    """
    ...
```

Run nightly via `scheduler` task. If the report is non-empty, email via
`bot/observability/alerts.py` (already has Telegram notification
plumbing — reuse).

**Acceptance:** 7 consecutive days of empty reports → green light for
T3.3 reader migration.

## 7. Open questions (decide before writing code)

1. **What's the retention policy?** The ledger is append-only and grows
   linearly. At current fill rate (~10-50/day) it's trivial; at live
   scale with all five weather series and maybe directional, call it
   500-5000/day. Over a year: 2M rows worst case. Fine for SQLite
   but we should set an explicit retention decision now.
   - **Recommendation:** keep all rows indefinitely. A 2M-row SQLite
     table with the proposed indexes is ~400 MB and still performs
     well. Retention is premature optimization.

2. **Do we version the ledger schema?**
   If we add a column later (e.g. `queue_position` once we instrument
   it), we should be able to migrate without losing rows.
   - **Recommendation:** use the existing `bot/db.py::_migrations` list
     mechanism. Same pattern as `weather_mm_shadow.trigger_reason`
     (added in T1.2). Default-null new columns, backfill lazily.

3. **How do we handle fills that predate the writer going live?**
   There will be historical rows in `mm_processed_fills` when we
   deploy T3.1. Two options:
   - (a) Leave them. Only new fills go into the ledger. Dual-run
     comparison is bounded to post-deploy rows.
   - (b) One-time backfill from `mm_processed_fills` + Kalshi API.
   - **Recommendation:** (a). The point of the ledger is forward
     consistency for the live-trading decision path. Historical rows
     are read-only history; the kill-switch doesn't care about them.
     Backfilling invents synthetic `trade_id`s for rows that don't
     have one, which violates the "Kalshi owns the PK" invariant.

4. **Source-tagging: how confident are we about the
   `client_order_id` → `mm_quote / directional` mapping?**
   MM orders use `mm_` prefix; directional uses... let's verify before
   writing the tagger.
   - **Action:** `grep -n "client_order_id" trade.py | head -20`
     during T3.1 session. Probably a simple prefix table; if not,
     tag as `unknown` and leave the classification for T3.3 readers
     to resolve.

5. **What about the dual-run ALSO catching `weather_mm_shadow`
   annotation drift?**
   `annotate_shadow_pnl` joins settlements × fills to compute
   `shadow_pnl_cents`. If the ledger says fill was at 47¢ and
   shadow table says 48¢, that's the exact drift we built T3 to catch.
   - **Recommendation:** yes. The validator compares THREE paths:
     `fills_ledger`, `mm_processed_fills`, and
     `weather_mm_shadow.shadow_pnl_cents` (re-derived). Divergence in
     any pair is a finding.

6. **Where does `ingested_ts_unix` come from on a retry?**
   If we ingest a fill, crash, and re-ingest, the second call should
   no-op (INSERT OR IGNORE). But if `ingested_ts_unix` were updated,
   it'd drift. Keeping it column-default at first-write only is
   correct — INSERT OR IGNORE semantics preserve the original.

7. **How do we cycle-id a fill?**
   `cycle_id` is the ID the cycle wrote into `opportunity_log` when
   it posted the originating order. We learn it via `order_id`
   lookup against `mm_orders` (legacy) or... wait, `mm_orders` was
   deleted. Today there's no cycle_id → order_id index anywhere.
   - **Recommendation:** nullable column; populate when
     `opportunity_log` has a row keyed by `client_order_id`. Otherwise
     leave NULL and don't sweat it. The cycle_id is a convenience for
     joining, not load-bearing.

## 8. Suggested session plan

If we do this in one multi-hour session:

1. **15 min** — walk through open questions §7 with Josh, lock decisions.
2. **30 min** — schema migration + writer module skeleton.
3. **60 min** — `FillsWriter.ingest_page` + `sync_since` + tests.
4. **45 min** — `FillsWriter` + scheduler wiring in `bot/daemon/main.py`.
5. **45 min** — validator module + tests.
6. **15 min** — deploy-script import-check additions.
7. **30 min** — smoke-test locally against a fixture-Kalshi response,
   document rollout procedure, update CLAUDE.md.

Total: ~4 hours of focused work. T3.1 is one session. T3.2 dual-run
sits on the VPS for a week; T3.3/T3.4 are another 2-3 hour session
after observation clears.

## 9. What to do right after T3.1 deploys

- **Smoke-check** from the VPS:
  ```bash
  ssh root@45.55.79.193 "sudo -u kalshi sqlite3 /home/kalshi/autoagent/kalshi_trades.db \
    'SELECT COUNT(*), MIN(fill_ts_unix), MAX(fill_ts_unix) FROM fills_ledger'"
  ```
  First daemon cycle fires `fills_sync` at +30s. Expect rows to start
  accumulating; `MAX(fill_ts_unix)` should track wall-clock within 60s.
- Check `idx_fills_ticker_ts` query plan: `EXPLAIN QUERY PLAN SELECT …
  WHERE ticker = ? AND fill_ts_unix > ?`. Should use the index.
- Baseline the validator report on day one. Because the legacy
  `mm_processed_fills` writer was removed during the daemon refactor,
  the reference side is empty and the report will log as
  `INFORMATIONAL: one side empty, no comparison`. This is expected —
  the validator is future-proofing for when a second fill consumer
  comes online; for T3.1 verification, inspect `fills_ledger` directly.
- **Rollout order** (deploy/04_redeploy.sh handles all of this):
  1. Pre-flight: full local test suite (`pytest tests/` — expect 968/968).
  2. `init_db` is idempotent; it creates `fills_ledger` on first restart.
  3. Daemon restart triggers `fills_sync` +30s later, then every 60s.
  4. `fills_validator` first runs at +1200s (20 min) then every 24h.
- **Rollback**: if fills_sync misbehaves, set `FILLS_SYNC_INTERVAL_S`
  very large in `bot/daemon/main.py`, redeploy. The ledger is
  append-only + idempotent, so no data cleanup is needed — just stop
  writing. Consumers don't read from it yet (T3.3 work).

## 10. T3.1 completion log (2026-04-21)

- ✅ `fills_ledger` schema + `FillsWriter` (`ingest_page`/`sync_since`).
- ✅ `T0.4` writer-ownership registry updated: `fills_ledger` → single
  owner `bot/daemon/fills_writer.py`.
- ✅ `fills_sync` (60s) + `fills_validator` (24h) scheduler tasks wired
  in `bot/daemon/main.py`.
- ✅ `bot/learning/fills_validator.py` with `is_meaningful` semantics
  for the empty-reference steady state.
- ✅ `deploy/04_redeploy.sh` import-check battery carries T3.1 asserts.
- ✅ All writes route through `db_write_ctx()` (T0.2 discipline).

## 11. T3.3 reader migration (2026-04-21)

T3.3 originally scoped as a "mechanical swap" of the remaining readers of
`mm_processed_fills` to `fills_ledger`. It turned out to be two pieces:

**Reader swap (the mechanical part).**

- ✅ [bot/signals/regime.py](bot/signals/regime.py) — `detect_regime`
  now reads recent MM fills from `fills_ledger` using the canonical
  per-side price (`CASE WHEN side='yes' THEN yes_price_cents ELSE
  no_price_cents END`). Fill-rate calculation reads fills from the
  ledger (ordered by `fill_ts_unix`). Denominator still reads
  `mm_orders` — that legacy order log no longer has a writer but
  carries a tail of pre-daemon rows; once it drains to zero fill_rate
  will pin to 0.0 until a replacement posted-orders table lands.
- ✅ [backtest_comprehensive.py](backtest_comprehensive.py) Fee Impact
  section — primary path reads `fills_ledger`; legacy
  `mm_processed_fills` path kept as a fallback so historical DBs still
  report fee totals.

**Live P&L wiring (the non-obvious part).**

`evaluate_mm_graduation` filters paired rows with `live_pnl_cents IS NOT
NULL`. Before T3.3 nothing populated that column, so the gate could
never fire — CANARY was effectively terminal.

- ✅ [bot/learning/mm_promotion.py](bot/learning/mm_promotion.py)
  `_attribute_live_fills_to_shadow_rows` — joins `fills_ledger`
  entries (`source='mm_quote'`, `live_mode=1`) to live-mode shadow
  rows by nearest-preceding `ts_unix`, bounded by the 300s quote
  lifetime. 1:1 fill→row attribution, deterministic.
- ✅ `annotate_shadow_pnl` — at settlement, stamps
  `live_pnl_cents = sum(attributed fill P&L net of fee)` on every
  live-mode row (0 when no fills were attributed — that's the drift
  case graduation is designed to catch; shadow-only rows leave the
  column NULL so the gate's filter correctly skips them).
- ✅ Eight new tests in `TestAnnotateShadowPnlLivePaired` cover: the
  shadow-only `NULL` invariant, the live-but-no-fill `0` invariant
  (drift case), realized P&L math on bid-side and ask-side fills,
  out-of-lifetime orphan handling, nearest-preceding attribution
  between overlapping shadow rows, and exclusion of non-`mm_quote` +
  non-`live_mode=1` fills.

Test count 990 → 997. Deploy import-check battery updated to assert
`_attribute_live_fills_to_shadow_rows` and `detect_regime` import
cleanly.

**T3.4 remaining.** `trade.py` still contains the legacy fills-write
path (pre-daemon directional fills-sync). Freezing it requires the
daemon's `FillsWriter` to be the only live source; the T3.2 validator
needs to confirm no gaps first.

- Next milestone: **T3.2** — 7 consecutive clean+meaningful validator
  reports (requires a second fills writer to reappear) or 30 days of
  ledger-only operation with zero gaps vs Kalshi API spot-checks,
  whichever comes first. **T3.3 landed ahead of T3.2 observation**
  because the live_pnl_cents dependency was blocking graduation — the
  reader side is the cheap half.

---

**Entry point for the T3.1 session.** Open this doc, read §7, pick my
recommendations or override them in one message, then start at §8 step 2.
