"""Canonical fills-ledger writer (T3.1).

The single code path by which rows are inserted into ``fills_ledger``. All
``INSERT INTO fills_ledger`` statements live here — no other module is
allowed to write to the table. Every reader of realized fill P&L (kill
switch, shadow-P&L annotator, settlement reconciler, bandit reward
signal) derives from this table rather than from the legacy three-writer
mess in mm_processed_fills / weather_mm_shadow / settlements.

See reports/T3_FILLS_LEDGER_SCOPING.md for architecture, open-question
resolutions, and the dual-run plan.

Invariants this module enforces:

  * ``trade_id`` is the Kalshi-owned primary key. No synthetic IDs.
  * ``INSERT OR IGNORE`` on collision — fills are immutable; we never
    update a row after first write. This preserves ``ingested_ts_unix``
    across retries (Q6).
  * ``source`` is drawn from a closed set (Q4). ``mm_quote``,
    ``safe_compounder``, ``exit``, ``directional``, ``legacy``,
    ``manual``. Never ``unknown`` — if a tag doesn't match a known
    prefix AND is not empty, it falls back to ``legacy``; empty /
    missing client_order_id falls back to ``manual`` (meaning "not
    placed by this bot").
  * No write lock is held across outbound HTTP calls. ``sync_since``
    fetches all pages first, then acquires ``DB_WRITE_LOCK`` for the
    insert batch. This prevents the fills sync from starving the cycle.
  * The writer is defensive: API failures log a warning and return 0
    rather than raising. The ledger is auxiliary to the live trading
    loop — a broken ``/fills`` fetch must not crash the daemon.

This module is a skeleton — the ``ingest_page`` and ``sync_since``
methods have signatures but their bodies are filled in in Step 3 of the
T3.1 plan (scoping doc §8.3).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable, Optional

from bot.core.categorization import _get_series_prefix
from bot.core.money import kalshi_maker_fee, kalshi_taker_fee
from bot.db import db_write_ctx
from bot.learning.alpha_log import family_from_ticker

logger = logging.getLogger(__name__)

__all__ = [
    "FillsWriter",
    "default_source_tagger",
    "ALLOWED_SOURCES",
    "ROW_COUNT_WARNING_THRESHOLD",
    "record_posted_order",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Q1 (scoping doc §7): indefinite retention. When row count crosses this
# threshold, the writer logs a WARNING once per process so we're reminded
# to consider migration to a larger store. Current fill rate (~10-50/day)
# puts 2M rows >100 years out, so the trigger is practically
# aspirational — but it exists so we can't quietly balloon past it.
ROW_COUNT_WARNING_THRESHOLD = 2_000_000

# Q4 (scoping doc §7): closed-set source classification. Any value
# written to fills_ledger.source must be one of these. The writer fails
# closed if a classifier returns something else.
ALLOWED_SOURCES: frozenset[str] = frozenset({
    "mm_quote",            # weather MM two-sided quote (mm_wx_ prefix)
    "safe_compounder",     # safe-compounder entry (mm_sc_ prefix)
    "exit",                # manage_positions synthetic-sell exit (mm_exit_ prefix)
    "directional",         # directional buy (mm_dir_ prefix)
    "cross_bracket",       # cross-bracket arb entry (mm_xb_ prefix, not mm_xb_exit_)
    "cross_bracket_exit",  # cross-bracket arb hedge exit (mm_xb_exit_ prefix)
    "legacy",              # bot-placed but from pre-T3 code paths (mm_* with
                           # no recognized sub-prefix) — should decay to zero
                           # rows over time as legacy orders settle out
    "manual",              # external / human-placed (empty or missing
                           # client_order_id) — always present because Josh
                           # can place orders via the Kalshi UI
})


# ---------------------------------------------------------------------------
# Source tagger (Q4 prefix table)
# ---------------------------------------------------------------------------

def default_source_tagger(client_order_id: Optional[str]) -> str:
    """Classify a Kalshi fill by its ``client_order_id`` prefix.

    Returns one of ALLOWED_SOURCES. Never returns ``unknown`` — by
    construction every input maps to a real bucket.

    The prefix table is the T3.1 contract with every order-posting code
    path:

        mm_wx_         → mm_quote
        mm_sc_         → safe_compounder
        mm_exit_       → exit
        mm_dir_        → directional
        mm_xb_exit_    → cross_bracket_exit
        mm_xb_         → cross_bracket
        other mm_*     → legacy   (should be empty in steady state)
        else           → manual   (external, not placed by this bot)

    If a new order-posting path is added, it MUST use one of the above
    prefixes. The structural invariant test in
    tests/test_client_order_id_coverage.py fails CI if any
    ``api_post('/portfolio/orders', body)`` call ships without a
    ``client_order_id`` key.
    """
    if not client_order_id:
        return "manual"
    # Sub-prefix matches come first — the plain "mm_" fallback only
    # fires when nothing more specific matched. ``mm_xb_exit_`` must be
    # checked before ``mm_xb_`` because the latter is a prefix of the
    # former.
    if client_order_id.startswith("mm_wx_"):
        return "mm_quote"
    if client_order_id.startswith("mm_sc_"):
        return "safe_compounder"
    if client_order_id.startswith("mm_exit_"):
        return "exit"
    if client_order_id.startswith("mm_dir_"):
        return "directional"
    if client_order_id.startswith("mm_xb_exit_"):
        return "cross_bracket_exit"
    if client_order_id.startswith("mm_xb_"):
        return "cross_bracket"
    if client_order_id.startswith("mm_"):
        return "legacy"
    return "manual"


# ---------------------------------------------------------------------------
# Posted-orders ledger (Kalshi format-drift recovery)
# ---------------------------------------------------------------------------
#
# As of ~2026-05-10 Kalshi's /portfolio/fills response stopped echoing
# back ``client_order_id``. Confirmed live: the only identity fields in
# a fill payload are ``trade_id``, ``fill_id``, ``order_id``, ``ticker``.
# Without ``client_order_id``, ``default_source_tagger`` cannot route
# the fill to a strategy bucket — every fill silently becomes ``manual``,
# breaking every downstream attribution (weather_mm_shadow back-fill,
# mm_promotion graduation, backtest strategy slices).
#
# Recovery: every ``/portfolio/orders`` POST records its own
# ``(order_id, client_order_id, source_hint)`` here at post time.
# ``FillsWriter.ingest_page`` falls back to a lookup by ``order_id``
# when ``client_order_id`` is absent from the Kalshi payload.
#
# Companion to the 2026-05-03 dual-format parser fix in this same file.

def record_posted_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    client_order_id: str,
    ticker: str,
    side: str,
    action: str,
    count: int,
    price_cents: int,
    source_hint: str,
    live_mode: bool,
) -> None:
    """Record a successfully posted order for later fill attribution.

    Called by every ``/portfolio/orders`` POST site immediately after
    ``api_post`` returns a non-empty ``order_id``. Must succeed without
    raising — a write failure here would silently restart the
    attribution bug for the affected order. Logs and swallows on error.

    Idempotent via ``INSERT OR IGNORE``: a duplicate post on the same
    ``order_id`` (e.g., retry path) is a no-op.

    Holds ``DB_WRITE_LOCK`` via ``db_write_ctx`` per the daemon's
    write-discipline invariant (CLAUDE.md regression watchlist #14).
    """
    if not order_id or not client_order_id:
        # Both are required for attribution recovery; refusing to write
        # a partial row prevents a silently-broken lookup.
        logger.warning(
            "[posted_orders] refusing to record partial row: "
            "order_id=%r client_order_id=%r ticker=%s",
            order_id, client_order_id, ticker,
        )
        return
    try:
        with db_write_ctx(conn):
            conn.execute(
                """INSERT OR IGNORE INTO posted_orders
                   (order_id, client_order_id, ticker, side, action,
                    count, price_cents, posted_ts_unix, live_mode,
                    source_hint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, client_order_id, ticker, side, action,
                 int(count), int(price_cents), time.time(),
                 1 if live_mode else 0, source_hint),
            )
    except Exception as exc:
        logger.warning(
            "[posted_orders] insert failed for order_id=%s ticker=%s: %s",
            order_id, ticker, exc,
        )


# ---------------------------------------------------------------------------
# Kalshi fill-payload parsers (dual-format)
# ---------------------------------------------------------------------------
#
# Kalshi's /fills endpoint emits payloads in TWO shapes; both must be
# parsed identically to avoid silent drops:
#
#   Legacy (cents int):
#     { "count": 1, "yes_price": 91, "no_price": 9, ... }
#
#   New (dollar string):
#     { "count_fp": "1.00", "yes_price_dollars": "0.9100",
#       "no_price_dollars": "0.0900", ... }
#
# 2026-05-04 postmortem: 18 cross-bracket fills were lost because
# fills_writer only knew the legacy keys. Today every cross-bracket
# fill on prod ships in the dollar-string format. Real money is in
# Kalshi's books that the local ledger never saw. Fixed by reading
# either shape, defensively normalizing to int cents.


def _parse_count(fill: dict) -> Optional[int]:
    """Return ``count`` as an integer, accepting either the legacy
    ``count`` (int) or the new ``count_fp`` (string in fixed-point
    contract units, e.g., ``"1.00"``). Returns None on missing/invalid.
    """
    raw = fill.get("count")
    if raw is None:
        raw = fill.get("count_fp")
    if raw is None:
        return None
    try:
        # round() not int() — "1.00" → 1, "1.99" → 2. Kalshi uses whole
        # contracts so partial values shouldn't occur, but rounding is
        # safer than truncating against future format drift.
        return round(float(raw))
    except (TypeError, ValueError):
        return None


def _parse_price_cents(fill: dict, side: str) -> Optional[int]:
    """Return ``side`` ('yes' or 'no') price as integer cents.

    Accepts either ``{side}_price`` (int cents) or ``{side}_price_dollars``
    (string in dollars, e.g., ``"0.0900"`` for 9¢). Returns None on
    missing/invalid.
    """
    raw = fill.get(f"{side}_price")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    raw_dollars = fill.get(f"{side}_price_dollars")
    if raw_dollars is None:
        return None
    try:
        # round() to handle floating-point noise: "0.0900" → 9, not 8.
        return round(float(raw_dollars) * 100)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FillsWriter
# ---------------------------------------------------------------------------

class FillsWriter:
    """Owns every insert into ``fills_ledger``.

    Typical wiring (built out in T3.1 step 4, scoping doc §8.4):

        writer = FillsWriter(conn)
        # Scheduler calls once per cycle:
        writer.sync_since(last_ts, live_mode=WEATHER_MM_LIVE)

    Args:
        conn: daemon-shared sqlite3.Connection. Must be the same
            connection returned by ``init_db()`` so WAL/busy_timeout/
            DB_WRITE_LOCK discipline is consistent.
        api_get: callable ``(path: str) -> dict`` for ``GET
            /v2/portfolio/fills`` pagination. Defaults to
            ``bot.api.api_get`` when None; parameterized for tests.
        source_tagger: callable ``(client_order_id) -> str`` returning
            a member of ALLOWED_SOURCES. Defaults to
            ``default_source_tagger``. Override for tests or future
            source-classification refinement.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        api_get: Optional[Callable[[str], dict]] = None,
        source_tagger: Callable[[Optional[str]], str] = default_source_tagger,
    ) -> None:
        self.conn = conn
        self._api_get = api_get  # resolved lazily to avoid import cycle at
                                 # module load time; see _get_api().
        self.source_tagger = source_tagger
        self._row_count_warned = False  # one-shot warning latch (Q1)

    def _get_api(self) -> Callable[[str], dict]:
        """Resolve the api_get callable, preferring the constructor arg
        and falling back to bot.api. Import is lazy because this module
        is imported by bot.daemon.main at startup, before bot.api has
        set up its rate limiter.
        """
        if self._api_get is not None:
            return self._api_get
        from bot.api import api_get as real_api_get
        self._api_get = real_api_get
        return real_api_get

    # -----------------------------------------------------------------
    # Row count warning (Q1)
    # -----------------------------------------------------------------

    def _check_row_count(self) -> None:
        """Log a WARNING once if the ledger has crossed the indefinite-
        retention threshold. Idempotent within a process via
        ``_row_count_warned``.

        Called from ``sync_since``. We avoid calling it on every
        ``ingest_page`` because COUNT(*) on a ~2M-row indexed table is
        non-trivial and once-per-cycle is plenty of resolution for a
        soft reminder.
        """
        if self._row_count_warned:
            return
        try:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM fills_ledger"
            ).fetchone()[0]
        except sqlite3.OperationalError as exc:
            # Table missing → writer is running against an uninitialized
            # DB. That's a caller bug, not a warning condition. Surface
            # and return.
            logger.error("[fills] row-count check failed: %s", exc)
            return
        if n >= ROW_COUNT_WARNING_THRESHOLD:
            logger.warning(
                "[fills] ledger has %d rows (threshold %d). "
                "Consider migrating to a larger store or revisiting "
                "retention policy. See reports/T3_FILLS_LEDGER_SCOPING.md "
                "§7 Q1 for the original decision.",
                n, ROW_COUNT_WARNING_THRESHOLD,
            )
            self._row_count_warned = True

    # -----------------------------------------------------------------
    # Drift alert
    # -----------------------------------------------------------------

    # How far back to look for a matching posted decision. 10 min
    # comfortably covers the latency between an api_post call and
    # Kalshi's /portfolio/fills page advancing — typically seconds
    # but allows for retry delays. Larger windows risk false alarms
    # if the bot legitimately posts and later sees a separate Josh
    # fill on the same ticker.
    _DRIFT_ALERT_WINDOW_SECONDS = 600

    def _maybe_log_drift_alert(
        self, ticker: str, side: str, created_time: Optional[str],
    ) -> None:
        """Log a loud warning when a fill tags ``manual`` despite a
        matching bot decision having been posted in the last
        ``_DRIFT_ALERT_WINDOW_SECONDS`` seconds. This is the canary
        for Kalshi response-shape drift — same class of bug as the
        2026-05-03 (count_fp / *_price_dollars) and 2026-05-10
        (client_order_id removed) field renames. Catching it here
        means a single broken fill triggers a visible alert in
        daemon.log instead of silently mis-attributing every fill
        for days.

        Best-effort: any exception in the lookup is swallowed (a
        broken alert must not break the writer).
        """
        if not created_time:
            return
        try:
            from datetime import datetime
            fill_ts_unix = datetime.fromisoformat(
                created_time.replace("Z", "+00:00")
            ).timestamp()
            row = self.conn.execute(
                """SELECT ts_decision, decision_type
                     FROM alpha_backtest
                    WHERE ticker = ?
                      AND side = ?
                      AND decision_outcome = 'posted'
                      AND ts_decision_unix BETWEEN ? AND ?
                    ORDER BY ts_decision_unix DESC LIMIT 1""",
                (ticker, side,
                 fill_ts_unix - self._DRIFT_ALERT_WINDOW_SECONDS,
                 fill_ts_unix + 60),  # accept up to 60s clock skew
            ).fetchone()
            if row is None:
                return
            decision_ts, decision_type = row
            logger.warning(
                "[fills] DRIFT ALERT: ticker=%s side=%s tagged 'manual' "
                "but %s posted decision exists at %s (within %ds). "
                "Likely Kalshi response-shape drift — check that fills "
                "payload includes client_order_id and that posted_orders "
                "is being written. See 2026-05-12 audit Phase A.",
                ticker, side, decision_type, decision_ts,
                self._DRIFT_ALERT_WINDOW_SECONDS,
            )
        except Exception as exc:
            logger.debug("[fills] drift-alert lookup failed: %s", exc)

    # -----------------------------------------------------------------
    # Row construction (pure helper — no I/O)
    # -----------------------------------------------------------------

    def _fill_to_row(self, fill: dict, *, live_mode: bool) -> Optional[dict]:
        """Convert a Kalshi /fills page entry into a row dict ready for
        insertion. Returns None if the fill is malformed (logged and
        skipped; caller continues).

        The derivation logic (series, family, fee, source) is isolated
        here so it can be unit-tested without touching the database.

        2026-05-04: handles BOTH Kalshi response shapes — the legacy
        cents-int format (``count``, ``yes_price``, ``no_price``) and
        the dollar-string format (``count_fp``, ``yes_price_dollars``,
        ``no_price_dollars``). Discovered after the cross-bracket canary:
        18 fills were silently dropped because Kalshi switched to the
        dollar-string format and our parser only knew the legacy keys.
        """
        trade_id = fill.get("trade_id")
        order_id = fill.get("order_id")
        ticker = fill.get("ticker")
        side = fill.get("side")
        action = fill.get("action")
        # Read with fallback to the dollar-string format keys.
        count = _parse_count(fill)
        yes_price = _parse_price_cents(fill, "yes")
        no_price = _parse_price_cents(fill, "no")
        is_taker = fill.get("is_taker")
        created_time = fill.get("created_time")

        if not all([
            trade_id, order_id, ticker, side, action,
            count is not None, yes_price is not None, no_price is not None,
            is_taker is not None, created_time,
        ]):
            logger.warning(
                "[fills] malformed fill (missing required field): %r",
                {
                    "trade_id": trade_id, "order_id": order_id,
                    "ticker": ticker, "side": side, "action": action,
                    "count": count, "yes_price": yes_price,
                    "no_price": no_price, "is_taker": is_taker,
                    "created_time": created_time,
                    "raw_keys": sorted(fill.keys()),
                },
            )
            return None

        # Fee: price_cents depends on which side of the trade WE are on.
        # For a YES buy, our price is yes_price; for a NO buy it's
        # no_price. The maker/taker distinction comes from is_taker.
        our_price_cents = int(yes_price) if side == "yes" else int(no_price)
        fee_fn = kalshi_taker_fee if is_taker else kalshi_maker_fee
        fee_cents = fee_fn(int(count), our_price_cents)

        series, _is_bracket = _get_series_prefix(ticker)
        family = family_from_ticker(ticker)
        client_order_id = fill.get("client_order_id")
        # Kalshi format drift (2026-05-10+): /portfolio/fills no longer
        # echoes back client_order_id. Recover via order_id → posted_orders
        # lookup, which every /portfolio/orders POST site populates at
        # post time. Falls through to ``manual`` only for genuinely
        # external (Josh-placed via Kalshi UI) fills.
        if not client_order_id and order_id:
            try:
                row = self.conn.execute(
                    "SELECT client_order_id FROM posted_orders "
                    "WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if row and row[0]:
                    client_order_id = row[0]
            except sqlite3.Error as exc:
                # Treat as a soft failure — better to tag manual than
                # to crash the fills sync. The warning surfaces drift
                # in the recovery path.
                logger.warning(
                    "[fills] posted_orders lookup failed for "
                    "order_id=%s: %s", order_id, exc,
                )
        source = self.source_tagger(client_order_id)
        if source not in ALLOWED_SOURCES:
            # source_tagger contract violation — fail closed. Logging
            # loudly so a mis-implemented override is noticed.
            logger.error(
                "[fills] source_tagger returned invalid source %r for "
                "client_order_id=%r; falling back to 'manual'",
                source, client_order_id,
            )
            source = "manual"

        # Drift alert: a ``manual`` fill that lands on a ticker we just
        # decided to post on is almost certainly bot activity with
        # broken attribution (Kalshi /portfolio/fills field-drift
        # pattern that has bitten us twice — count_fp/*_price_dollars
        # on 2026-05-03, client_order_id on 2026-05-10). Surface this
        # loudly so the gap doesn't go unnoticed for days again.
        if source == "manual":
            self._maybe_log_drift_alert(ticker, side, fill.get("created_time"))

        # Parse ISO timestamp → unix. Kalshi sends "2026-04-20T18:23:11.402Z".
        # datetime.fromisoformat handles the "Z" suffix on 3.11+.
        from datetime import datetime
        fill_ts_iso = created_time
        fill_ts_unix = datetime.fromisoformat(
            created_time.replace("Z", "+00:00")
        ).timestamp()

        return {
            "trade_id": trade_id,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "ticker": ticker,
            "series": series,
            "family": family,
            "side": side,
            "action": action,
            "contracts": int(count),
            "yes_price_cents": int(yes_price),
            "no_price_cents": int(no_price),
            "is_taker": 1 if is_taker else 0,
            "fee_cents": int(fee_cents),
            "fill_ts_iso": fill_ts_iso,
            "fill_ts_unix": fill_ts_unix,
            "ingested_ts_unix": time.time(),
            "live_mode": 1 if live_mode else 0,
            "source": source,
        }

    # -----------------------------------------------------------------
    # Write paths (skeleton — implemented in Step 3)
    # -----------------------------------------------------------------

    # Columns in the exact order used by the INSERT statement. Keeping the
    # tuple near the method makes it obvious when a schema change touches
    # the writer — any mismatch between _COLUMNS and _fill_to_row keys is
    # a straight KeyError at insert time, which the tests catch.
    _COLUMNS: tuple[str, ...] = (
        "trade_id", "order_id", "client_order_id",
        "ticker", "series", "family",
        "side", "action", "contracts",
        "yes_price_cents", "no_price_cents",
        "is_taker", "fee_cents",
        "fill_ts_iso", "fill_ts_unix", "ingested_ts_unix",
        "live_mode", "source",
    )

    def ingest_page(
        self,
        fills: list[dict],
        *,
        live_mode: bool,
    ) -> int:
        """Ingest one Kalshi ``/v2/portfolio/fills`` page.

        Idempotent: rows whose ``trade_id`` already exists silently
        skip via ``INSERT OR IGNORE``. Safe to call with the same page
        repeatedly.

        Returns the number of NEW rows inserted (i.e. excluding
        already-present trade_ids).

        Acquires ``DB_WRITE_LOCK`` for the insert batch. Malformed
        fills are logged and skipped, never raised.
        """
        if not fills:
            return 0

        # Pure transform first — no DB work while we're deriving rows.
        # Malformed fills have already been logged inside _fill_to_row.
        rows: list[dict] = []
        for fill in fills:
            row = self._fill_to_row(fill, live_mode=live_mode)
            if row is not None:
                rows.append(row)
        if not rows:
            return 0

        placeholders = ", ".join(f":{c}" for c in self._COLUMNS)
        sql = (
            f"INSERT OR IGNORE INTO fills_ledger "
            f"({', '.join(self._COLUMNS)}) VALUES ({placeholders})"
        )

        # db_write_ctx holds DB_WRITE_LOCK for the whole execute→commit
        # region (T0.2 discipline). Readers proceed under WAL. No HTTP
        # happens inside this block.
        before = self.conn.total_changes
        with db_write_ctx(self.conn):
            self.conn.executemany(sql, rows)
        inserted = self.conn.total_changes - before
        return inserted

    def sync_since(
        self,
        since_unix: float,
        *,
        live_mode: bool,
    ) -> int:
        """Paginate through ``/v2/portfolio/fills`` and ingest every
        page whose fill timestamp is ≥ ``since_unix``.

        Strategy:
          1. Fetch all pages into memory (no DB lock held).
          2. Call ``_check_row_count`` for the Q1 threshold warning.
          3. Hand the accumulated fill list to ``ingest_page`` under a
             single ``DB_WRITE_LOCK`` acquisition.

        API failures log a warning and return 0 — the ledger is
        auxiliary; cycle trading must not crash on a /fills fetch
        error.

        Returns the total number of new rows inserted across all
        pages.
        """
        api_get = self._get_api()
        accumulated: list[dict] = []
        cursor: Optional[str] = None
        # Bound the loop defensively. At 1000 fills/page this is 100k
        # fills per call — orders of magnitude above steady state.
        # Without a bound a mis-behaving cursor response could pin us in
        # a fetch loop forever.
        MAX_PAGES = 100
        min_ts = int(since_unix)

        for page_idx in range(MAX_PAGES):
            path = f"/portfolio/fills?limit=1000&min_ts={min_ts}"
            if cursor:
                path += f"&cursor={cursor}"
            try:
                resp = api_get(path)
            except Exception as exc:
                logger.warning(
                    "[fills] /portfolio/fills fetch failed on page %d "
                    "(since_unix=%s): %s. Aborting this sync cycle; "
                    "will retry next tick.",
                    page_idx, since_unix, exc,
                )
                return 0

            page_fills = resp.get("fills") or []
            accumulated.extend(page_fills)
            cursor = resp.get("cursor") or None
            if not cursor:
                break
        else:
            logger.warning(
                "[fills] hit MAX_PAGES=%d without exhausting cursor; "
                "truncating sync at %d fills. since_unix=%s",
                MAX_PAGES, len(accumulated), since_unix,
            )

        self._check_row_count()
        if not accumulated:
            return 0
        return self.ingest_page(accumulated, live_mode=live_mode)
