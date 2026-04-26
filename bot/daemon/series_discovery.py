"""Daily discovery scan for new tradeable Kalshi series.

Why this exists
---------------
The cycle scanner (`trade.py`) reads from a hardcoded allowlist
(`bot.config.TRADE_SERIES_ALLOWLIST`) for two reasons: (1) Kalshi's
unfiltered `/markets?status=open` listing is dominated by ~50K KXMVE parlay
legs, drowning the few hundred markets we actually have signal for; (2)
explicit enumeration is deterministic and survives Kalshi catalog changes.

But that hardcoded list goes stale. Cities Kalshi removed (Houston,
Phoenix, SF — see stations.py comment) could come back. New families with
weather-like prefixes (e.g. KXLOW*, KXSNOW*) might launch. We want a daily
sweep that surfaces those without auto-trading them.

What this does
--------------
Once per day:

1. Paginate `/events?status=open` → collect distinct `series_ticker`s.
2. Filter to "potentially routable" prefixes — anything we have a family
   router for or could trivially extend the weather ensemble to cover.
3. Diff against `TRADE_SERIES_ALLOWLIST`. Unknowns get UPSERTed into
   `discovered_series` and trigger a single Telegram alert (idempotent —
   one alert per series_ticker, ever).

Auto-add is intentionally NOT performed. Adding a new weather city to
scanning without a corresponding `bot/daemon/stations.py` entry would
silently mis-route through the wrong METAR station. Alert + manual wire-up
is the safer pattern.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from typing import Iterable, Optional

from bot.api import api_get
from bot.config import TRADE_SERIES_ALLOWLIST
from bot.db import db_write_ctx
from bot.observability.alerts import send_alert

logger = logging.getLogger(__name__)


# Prefixes the ensemble can plausibly route. Three buckets:
#
# 1. Weather: anything in {KXHIGH, KXLOW, KXHMONTHRANGE, KXHURR, KXSNOW}.
#    The first six prefixes are daily-high; the rest are stretched horizons
#    or specialty bets. Adding any of these requires a station registry
#    entry (KXHIGH*) or a horizon-aware predict path.
#
# 2. Macro: KXFED, KXJOB, KXGDP, KXCPI — the four with family_routers.
#
# 3. Crypto: KXBTC, KXETH — directional-blocked but still useful to track
#    because shadow rows feed calibration.
ROUTABLE_PREFIXES: tuple[str, ...] = (
    "KXHIGH", "KXLOW", "KXHMONTHRANGE", "KXHURR", "KXSNOW",
    "KXFED", "KXJOB", "KXGDP", "KXCPI",
    "KXBTC", "KXETH",
)


def _routable_prefix(series_ticker: str) -> Optional[str]:
    """Return the longest matching ROUTABLE_PREFIXES prefix, or None."""
    if not series_ticker:
        return None
    upper = series_ticker.upper()
    # Longest-prefix-wins so KXHMONTHRANGE doesn't get caught by KXH* (none
    # exists today, but the rule is cheap to apply now and stable as the
    # prefix list grows).
    for prefix in sorted(ROUTABLE_PREFIXES, key=len, reverse=True):
        if upper.startswith(prefix):
            return prefix
    return None


def _enumerate_open_events() -> dict[str, dict]:
    """Page through `/events?status=open` and aggregate per-series stats.

    Returns a map of `series_ticker → {sample_event_ticker, market_count}`.
    `market_count` is approximate — we sum each event's market count
    (`event.markets` length when available, otherwise 1) to give the
    discovery alert some sense of breadth. Cheap heuristic, not authoritative.
    """
    aggregated: dict[str, dict] = {}
    cursor = None
    pages = 0
    MAX_PAGES = 50  # 50 × 200 = 10K events is plenty (saw ~6K live)
    while pages < MAX_PAGES:
        path = "/events?status=open&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            resp = api_get(path)
        except Exception as exc:
            logger.warning("[series_discovery] events fetch failed: %s", exc)
            break
        events = resp.get("events", [])
        if not events:
            break
        for ev in events:
            series = ev.get("series_ticker")
            if not series:
                continue
            entry = aggregated.setdefault(
                series,
                {"sample_event_ticker": ev.get("event_ticker"), "market_count": 0},
            )
            # `markets` may or may not be present on the events listing
            # depending on Kalshi's response shape — count it where we can,
            # otherwise treat the event itself as one market unit so the
            # discovery alert still reflects breadth.
            ms = ev.get("markets")
            entry["market_count"] += len(ms) if isinstance(ms, list) and ms else 1
        cursor = resp.get("cursor")
        pages += 1
        if not cursor:
            break
    return aggregated


def _existing_known_series(conn: sqlite3.Connection) -> set[str]:
    """Series we already know about — allowlist + previously-discovered."""
    known = set(TRADE_SERIES_ALLOWLIST)
    for (s,) in conn.execute(
        "SELECT series_ticker FROM discovered_series"
    ):
        known.add(s)
    return known


def run_discovery(conn: sqlite3.Connection) -> dict:
    """Full discovery pass. Returns a summary dict (counters)."""
    now = time.time()
    aggregated = _enumerate_open_events()
    known = _existing_known_series(conn)

    new_alerts: list[tuple[str, dict]] = []
    seen_routable = 0
    upserts = 0

    with db_write_ctx(conn):
        for series, info in aggregated.items():
            prefix = _routable_prefix(series)
            if not prefix:
                continue
            seen_routable += 1
            is_new = series not in known
            # Always upsert last_seen_unix so we can later prune entries that
            # haven't been seen in N days (markets that 410'd off the catalog).
            row = conn.execute(
                "SELECT alert_sent_unix FROM discovered_series WHERE series_ticker=?",
                (series,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO discovered_series
                       (series_ticker, first_seen_unix, last_seen_unix,
                        sample_event_ticker, sample_market_count, family_prefix,
                        alert_sent_unix)
                       VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                    (series, now, now,
                     info["sample_event_ticker"], info["market_count"],
                     prefix),
                )
                upserts += 1
            else:
                conn.execute(
                    """UPDATE discovered_series
                       SET last_seen_unix=?, sample_event_ticker=?,
                           sample_market_count=?, family_prefix=?
                       WHERE series_ticker=?""",
                    (now, info["sample_event_ticker"],
                     info["market_count"], prefix, series),
                )
            if is_new:
                # Defer the alert send until after the write commits — we
                # don't want a Telegram failure to roll back the upsert.
                new_alerts.append((series, info))

    # Group by prefix for a tidier alert when a launch hits multiple cities
    # (e.g. Kalshi enabling KXHIGHHOU + KXHIGHPHX + KXHIGHSF on the same day).
    if new_alerts:
        by_prefix: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for series, info in new_alerts:
            by_prefix[_routable_prefix(series) or "?"].append((series, info))
        lines = ["📡 Series discovery: NEW routable series detected"]
        for prefix in sorted(by_prefix):
            entries = by_prefix[prefix]
            lines.append(f"  {prefix}:")
            for series, info in sorted(entries):
                ev = info["sample_event_ticker"] or "?"
                mc = info["market_count"]
                lines.append(f"    • {series} (sample event {ev}, ~{mc} markets)")
        lines.append("")
        lines.append(
            "Add to bot.config.TRADE_SERIES_ALLOWLIST after wiring station / "
            "router registry as needed. Discovery does NOT auto-add."
        )
        try:
            send_alert("\n".join(lines), level="info")
            with db_write_ctx(conn):
                for series, _ in new_alerts:
                    conn.execute(
                        "UPDATE discovered_series SET alert_sent_unix=? "
                        "WHERE series_ticker=?",
                        (now, series),
                    )
        except Exception as exc:
            logger.warning("[series_discovery] alert send failed: %s", exc)

    return {
        "events_aggregated": len(aggregated),
        "routable_seen": seen_routable,
        "new_routable": len(new_alerts),
        "upserted": upserts,
    }
