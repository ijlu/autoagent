"""Kalshi market snapshotter — Layer 2 prereq for city expansion.

Polls ``/markets?status=open&series_ticker=<S>`` once per minute for every
series in ``bot.config.WEATHER_SNAPSHOT_SERIES`` and persists snapshots to
``kalshi_market_snapshots``.

Why this exists
---------------
The city-expansion framework (reports/SESSION_HANDOFF_CITY_EXPANSION_2026-05-06.md
§5 Layer 2) requires per-city historical bid/ask + liquidity data to evaluate
market capacity *before* a city is promoted. We can't reconstruct it after
the fact — Kalshi has no historical depth/quote API — so we have to start
collecting now.

Why standalone (not piggybacking the trading scan)
--------------------------------------------------
The cycle's per-series ``/markets`` enumeration is gated to
``TRADE_SERIES_ALLOWLIST`` (6 traded weather + macro/crypto). Candidate
cities (PHX/SEA/HOU/...) are not in that allowlist and won't be until they
graduate. Piggybacking gives us nothing for the cities we actually need
data on. Standalone keeps snapshotter health independent of cycle halts.

Write rules — see ``_decide_write`` below.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from bot.api import api_get
from bot.config import WEATHER_SNAPSHOT_SERIES
from bot.core.categorization import categorize_market
from bot.daemon.poller_base import Poller
from bot.db import db_write_ctx, get_connection

logger = logging.getLogger(__name__)


# Heartbeat: write a row at least every N seconds even if quotes unchanged.
# Distinguishes "snapshotter alive, market quiet" from "snapshotter down" —
# reader can alert on absence of any row for >2× heartbeat.
HEARTBEAT_INTERVAL_S = 300

# Payload sampling: include the full /markets response JSON every N seconds.
# Schema-evolution safety net. Otherwise NULL ~99% of rows.
PAYLOAD_SAMPLE_INTERVAL_S = 3600

# Pagination bound — same as trade.py's series scan.
PAGE_LIMIT = 200

# Inserted row column order. Kept in one place so the INSERT statement and
# the row tuple stay in lockstep.
_COLUMNS = (
    "ticker", "ts",
    "event_ticker", "series_ticker", "market_type", "status",
    "strike_type", "floor_strike", "cap_strike", "custom_strike",
    "yes_bid", "yes_ask", "no_bid", "no_ask",
    "last_price", "previous_yes_bid", "previous_yes_ask",
    "previous_price", "yes_bid_size", "yes_ask_size",
    "volume", "volume_24h", "liquidity", "open_interest",
    "notional_value", "tick_size", "risk_limit_cents",
    "open_time", "close_time", "expected_expiration_time", "expiration_time",
    "settlement_value", "result",
    "payload",
)
_INSERT_SQL = (
    f"INSERT OR REPLACE INTO kalshi_market_snapshots "
    f"({', '.join(_COLUMNS)}) VALUES ({', '.join(['?'] * len(_COLUMNS))})"
)


@dataclass
class _LastSnapshot:
    """In-memory cache of the most recent stored row per ticker.

    Used for change-detection + heartbeat-clock + payload-clock decisions.
    Reset on daemon restart — first poll after restart legitimately writes
    a new row per active ticker (which counts as a "first observation"
    from the snapshotter's perspective).
    """
    ts: int
    yes_bid: Optional[int]
    yes_ask: Optional[int]
    no_bid: Optional[int]
    no_ask: Optional[int]
    last_price: Optional[int]
    volume: Optional[int]
    status: str
    last_payload_ts: int


def _coerce_int(value: Any) -> Optional[int]:
    """Round-to-int coercion. Per CLAUDE.md known bug pattern #5 we use
    round(float(...)) not int(float(...)) — the latter truncates and
    causes off-by-one drift on fixed-point string parses."""
    if value is None:
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_to_cents(value: Any) -> Optional[int]:
    """Kalshi prices arrive as ``yes_bid_dollars="0.0200"`` (dollar string)
    in the live /markets response and as ``yes_bid=2`` (int cents) in many
    test fixtures and legacy callers. Accept either; mirrors the
    ``_safe_cents`` heuristic in bot/daemon/weather_quoter.py.

    Empty / zero → None: a 0¢ value is indistinguishable from "no resting
    book" in the /markets response, and silently coercing the latter to
    "price is 0¢" poisons downstream fill-match logic. Kalshi's minimum
    quoted price is 1¢, so any 0 here is genuinely missing.
    """
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    cents = round(v * 100) if 0 < v <= 1.0 else round(v)
    return cents if cents > 0 else None


def _dollars_to_cents(value: Any) -> Optional[int]:
    """Convert a dollar-denominated amount (liquidity, notional) to integer
    cents. Always multiplies by 100; values can exceed $1 (e.g. liquidity
    score = $5000.00). Distinct from ``_price_to_cents`` because the smart
    0-1 vs >1 heuristic is only safe for the 1¢-99¢ price domain."""
    if value is None or value == "":
        return None
    try:
        cents = round(float(value) * 100)
    except (TypeError, ValueError):
        return None
    return cents if cents > 0 else None


def _parse_fp(value: Any) -> Optional[int]:
    """Kalshi *_fp fields (volume_fp, open_interest_fp, *_size_fp) arrive
    as decimal strings like ``"7717.41"``. Round to int."""
    if value is None or value == "":
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _iso_to_unix(value: Any) -> Optional[int]:
    """Parse Kalshi ISO timestamps (``"2026-05-07T05:59:00Z"``) to unix
    seconds. Returns None on missing or unparseable input."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        s = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError, AttributeError):
        return None


def _series_from_event(event_ticker: Any) -> Optional[str]:
    """Kalshi /markets doesn't return ``series_ticker`` on individual rows.
    Derive it from ``event_ticker`` — the prefix before the first ``-``.

    Examples:
      KXHIGHAUS-26MAY06   → KXHIGHAUS
      KXFED-27APR         → KXFED
    """
    if not event_ticker:
        return None
    s = str(event_ticker).split("-", 1)
    return s[0] if s and s[0] else None


def _market_to_signature(market: dict) -> tuple:
    """Extract the change-detection signature of a market response.

    Returns the tuple of fields that, if any differ from the previously
    stored row, force a new write. Order matches ``_LastSnapshot``."""
    return (
        _price_to_cents(market.get("yes_bid_dollars") or market.get("yes_bid")),
        _price_to_cents(market.get("yes_ask_dollars") or market.get("yes_ask")),
        _price_to_cents(market.get("no_bid_dollars") or market.get("no_bid")),
        _price_to_cents(market.get("no_ask_dollars") or market.get("no_ask")),
        _price_to_cents(
            market.get("last_price_dollars") or market.get("last_price")
        ),
        _parse_fp(market.get("volume_fp") or market.get("volume")),
        str(market.get("status") or ""),
    )


def _read_dollar_amount(market: dict, dollars_key: str, fallback_key: str) -> Optional[int]:
    """For dollar-amount fields (liquidity, notional). Prefer the live API's
    ``*_dollars`` string form (multiply by 100) and fall back to the bare
    name as already-cents integer. The fallback exists so test fixtures
    with cents-int inputs keep working without an explicit string."""
    v = market.get(dollars_key)
    if v is not None and v != "":
        return _dollars_to_cents(v)
    return _coerce_int(market.get(fallback_key))


def _decide_write(
    market: dict,
    cache: Optional[_LastSnapshot],
    now_ts: int,
    *,
    heartbeat_interval_s: int = HEARTBEAT_INTERVAL_S,
    payload_sample_interval_s: int = PAYLOAD_SAMPLE_INTERVAL_S,
) -> tuple[bool, bool]:
    """Apply the write-decision rules to a market dict.

    Returns (write, include_payload).

    Write triggers (any one is sufficient):
      1. First observation of this ticker (no cache entry)
      2. Quote/status change vs cache
      3. ≥ heartbeat_interval_s since last write

    Payload triggers (only set if write=True):
      a. First observation
      b. Status transition
      c. ≥ payload_sample_interval_s since last payload write
    """
    sig = _market_to_signature(market)
    yes_bid, yes_ask, no_bid, no_ask, last_price, volume, status = sig

    if cache is None:
        return True, True  # first observation — capture full payload

    cached_sig = (
        cache.yes_bid, cache.yes_ask, cache.no_bid, cache.no_ask,
        cache.last_price, cache.volume, cache.status,
    )
    quote_changed = sig != cached_sig
    heartbeat_due = (now_ts - cache.ts) >= heartbeat_interval_s
    status_changed = status != cache.status

    write = quote_changed or heartbeat_due
    if not write:
        return False, False

    payload = (
        status_changed
        or (now_ts - cache.last_payload_ts) >= payload_sample_interval_s
    )
    return True, payload


def _build_row(market: dict, now_ts: int, include_payload: bool) -> tuple:
    """Build the row tuple for INSERT. Order MUST match ``_COLUMNS``.

    ``include_payload=False`` writes NULL for the payload column.

    Field-name fallbacks accommodate both the live Kalshi /markets shape
    (``*_dollars`` strings, ``*_fp`` decimal strings, ISO timestamps) and
    the integer/cents/unix shape used by some test fixtures. The fallback
    chain is: native-name first, then ``_dollars`` / ``_fp`` variants.
    """
    payload_json = json.dumps(market, default=str) if include_payload else None
    event_ticker = market.get("event_ticker")
    return (
        market.get("ticker"),
        now_ts,
        event_ticker,
        market.get("series_ticker") or _series_from_event(event_ticker),
        market.get("market_type"),
        market.get("status"),
        market.get("strike_type"),
        _coerce_float(market.get("floor_strike")),
        _coerce_float(market.get("cap_strike")),
        (
            json.dumps(market.get("custom_strike"), default=str)
            if market.get("custom_strike") is not None else None
        ),
        _price_to_cents(market.get("yes_bid_dollars") or market.get("yes_bid")),
        _price_to_cents(market.get("yes_ask_dollars") or market.get("yes_ask")),
        _price_to_cents(market.get("no_bid_dollars") or market.get("no_bid")),
        _price_to_cents(market.get("no_ask_dollars") or market.get("no_ask")),
        _price_to_cents(
            market.get("last_price_dollars") or market.get("last_price")
        ),
        _price_to_cents(
            market.get("previous_yes_bid_dollars")
            or market.get("previous_yes_bid")
        ),
        _price_to_cents(
            market.get("previous_yes_ask_dollars")
            or market.get("previous_yes_ask")
        ),
        _price_to_cents(
            market.get("previous_price_dollars") or market.get("previous_price")
        ),
        _parse_fp(market.get("yes_bid_size_fp") or market.get("yes_bid_size")),
        _parse_fp(market.get("yes_ask_size_fp") or market.get("yes_ask_size")),
        _parse_fp(market.get("volume_fp") or market.get("volume")),
        _parse_fp(market.get("volume_24h_fp") or market.get("volume_24h")),
        _read_dollar_amount(market, "liquidity_dollars", "liquidity"),
        _parse_fp(market.get("open_interest_fp") or market.get("open_interest")),
        _read_dollar_amount(market, "notional_value_dollars", "notional_value"),
        _coerce_int(market.get("tick_size")),
        _coerce_int(market.get("risk_limit_cents")),
        _iso_to_unix(market.get("open_time")),
        _iso_to_unix(market.get("close_time")),
        _iso_to_unix(market.get("expected_expiration_time")),
        _iso_to_unix(market.get("expiration_time")),
        _coerce_int(market.get("settlement_value")),
        market.get("result") or None,
        payload_json,
    )


def cleanup_old_snapshots(
    conn: sqlite3.Connection,
    ttl_days: Optional[int],
    *,
    now_ts: Optional[int] = None,
    batch_rows: int = 10_000,
) -> int:
    """Delete snapshot rows older than ``ttl_days``. Returns rows deleted.

    ``ttl_days=None`` is the no-op default — wired into the scheduler so the
    daemon has the cleanup loop in place but inert until the kv_cache key
    ``market_snapshots_ttl_days`` is set. Caller is responsible for reading
    that key and passing it in.
    """
    if ttl_days is None or ttl_days <= 0:
        return 0
    cutoff = (now_ts or int(time.time())) - ttl_days * 86400
    total_deleted = 0
    # WITHOUT ROWID table → can't use rowid in LIMIT subquery. Bound
    # batches by (ticker, ts) tuple instead, walking the PK in order.
    while True:
        with db_write_ctx(conn):
            stale_pks = conn.execute(
                "SELECT ticker, ts FROM kalshi_market_snapshots "
                "WHERE ts < ? LIMIT ?",
                (cutoff, batch_rows),
            ).fetchall()
            if not stale_pks:
                break
            conn.executemany(
                "DELETE FROM kalshi_market_snapshots "
                "WHERE ticker = ? AND ts = ?",
                stale_pks,
            )
            n = len(stale_pks)
        total_deleted += n
        if n < batch_rows:
            break
    return total_deleted


class MarketSnapshotPoller(Poller):
    """Polls Kalshi /markets per weather series, writes change-detected snapshots.

    Cadence: ``interval_s = 60`` — same effective resolution as the trading
    cycle's market scan.

    On cold start, the in-memory cache is empty, so the first poll writes a
    "first observation" row per active ticker. This is intentional and
    auditable — each restart leaves a marker in the snapshot stream.
    """

    name = "market_snapshotter"
    interval_s = 60.0

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        *,
        series: Optional[Iterable[str]] = None,
        api_get_fn: Callable[[str], dict] = api_get,
        on_result: Optional[Callable[[Any], None]] = None,
        heartbeat_interval_s: int = HEARTBEAT_INTERVAL_S,
        payload_sample_interval_s: int = PAYLOAD_SAMPLE_INTERVAL_S,
    ) -> None:
        super().__init__(on_result=on_result)
        self._conn = conn
        self._series: tuple[str, ...] = tuple(series) if series is not None else WEATHER_SNAPSHOT_SERIES
        self._api_get = api_get_fn
        self._heartbeat_s = heartbeat_interval_s
        self._payload_s = payload_sample_interval_s
        self._cache: dict[str, _LastSnapshot] = {}
        # Stats — reported through health() in addition to base counters.
        self._rows_written = 0
        self._rows_with_payload = 0
        self._markets_seen = 0
        self._non_weather_skipped = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        return self._conn if self._conn is not None else get_connection()

    def _fetch_series(self, series: str) -> list[dict]:
        """Paginated fetch of all open markets for one series."""
        markets: list[dict] = []
        cursor: Optional[str] = None
        while True:
            url = f"/markets?limit={PAGE_LIMIT}&status=open&series_ticker={series}"
            if cursor:
                url += f"&cursor={cursor}"
            resp = self._api_get(url)
            batch = resp.get("markets", [])
            markets.extend(batch)
            cursor = resp.get("cursor")
            if not cursor or len(batch) < PAGE_LIMIT:
                break
        return markets

    def _filter_weather(self, markets: list[dict]) -> list[dict]:
        """Drop any market that doesn't classify as weather.

        Defensive — guards against accidental non-weather entries in
        WEATHER_SNAPSHOT_SERIES. categorize_market is the canonical
        classifier (bot/core/categorization.py).
        """
        out: list[dict] = []
        for m in markets:
            ticker = m.get("ticker") or ""
            title = m.get("title") or ""
            if categorize_market(ticker, title) == "weather":
                out.append(m)
            else:
                self._non_weather_skipped += 1
        return out

    def _ingest(self, markets: list[dict], now_ts: int) -> tuple[list[tuple], int]:
        """Apply the write-decision rules across one batch.

        Returns (rows_to_insert, payload_count). Pure aside from the
        in-memory cache update — does not touch the DB."""
        rows: list[tuple] = []
        payload_count = 0
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue
            cache = self._cache.get(ticker)
            write, include_payload = _decide_write(
                m, cache, now_ts,
                heartbeat_interval_s=self._heartbeat_s,
                payload_sample_interval_s=self._payload_s,
            )
            if not write:
                continue
            row = _build_row(m, now_ts, include_payload)
            rows.append(row)
            sig = _market_to_signature(m)
            yb, ya, nb, na, lp, vol, st = sig
            self._cache[ticker] = _LastSnapshot(
                ts=now_ts,
                yes_bid=yb, yes_ask=ya, no_bid=nb, no_ask=na,
                last_price=lp, volume=vol, status=st,
                last_payload_ts=now_ts if include_payload else (
                    cache.last_payload_ts if cache is not None else now_ts
                ),
            )
            if include_payload:
                payload_count += 1
        return rows, payload_count

    def _persist(self, rows: list[tuple]) -> None:
        if not rows:
            return
        conn = self._get_conn()
        with db_write_ctx(conn):
            conn.executemany(_INSERT_SQL, rows)

    # ------------------------------------------------------------------
    # Poller hook
    # ------------------------------------------------------------------

    def _poll_once(self) -> dict:
        now_ts = int(time.time())
        all_rows: list[tuple] = []
        total_seen = 0
        payload_total = 0
        for series in self._series:
            try:
                markets = self._fetch_series(series)
            except Exception as exc:
                # One bad series shouldn't kill the whole cycle. Log and
                # continue — base class would otherwise count this against
                # error budget for the entire poll.
                logger.warning("[%s] fetch %s failed: %s", self.name, series, exc)
                continue
            total_seen += len(markets)
            weather_only = self._filter_weather(markets)
            rows, payload_count = self._ingest(weather_only, now_ts)
            all_rows.extend(rows)
            payload_total += payload_count

        try:
            self._persist(all_rows)
        except Exception:
            # Logging + error budget; do NOT update stats so the failed
            # batch isn't counted as written.
            logger.exception("[%s] persist failed for %d rows", self.name, len(all_rows))
            raise

        self._markets_seen += total_seen
        self._rows_written += len(all_rows)
        self._rows_with_payload += payload_total

        return {
            "markets_seen": total_seen,
            "rows_written": len(all_rows),
            "rows_with_payload": payload_total,
            "ts": now_ts,
        }

    # ------------------------------------------------------------------
    # Health stats
    # ------------------------------------------------------------------

    def health(self) -> dict:
        base = super().health()
        base.update({
            "rows_written_total": self._rows_written,
            "rows_with_payload_total": self._rows_with_payload,
            "markets_seen_total": self._markets_seen,
            "non_weather_skipped_total": self._non_weather_skipped,
            "cached_tickers": len(self._cache),
            "series_count": len(self._series),
        })
        return base
