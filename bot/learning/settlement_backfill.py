"""Catalog-driven settlement back-fill for the learning loop.

Problem this solves
-------------------
`record_settlements()` in ``trade.py`` drives off ``/portfolio/settlements``,
which only returns markets where we held a bot-placed position. Any market we
*shadowed* but didn't trade (the steady state while
``WEATHER_MM_LIVE=false``) never flows through that loop, so:

  · ``alpha_backtest`` rows never get ``ts_settle_unix`` stamped →
    ``_alpha_populate_all`` can't cascade them into ``calibration``,
    ``timing_patterns``, ``edge_convergence``, or ``postmortems``.
  · ``weather_mm_shadow`` rows never get ``ticker_settled_yes`` /
    ``shadow_pnl_cents`` populated → ``evaluate_mm_promotion`` sees no
    settled data and the shadow-to-canary gate is dead on arrival.

The 2026-04-22 root-cause analysis established this is why the ensemble's
Platt correction has been starved: no settled rows in ``calibration`` →
no Platt fit → raw-and-miscalibrated probabilities in live scoring.
Brier audit result: our FV at 0.37–0.52 vs market mid at 0.03–0.06.

This module drives the back-fill off the market catalog
(``/markets?series_ticker=X&status=settled``) rather than the portfolio.
It's the permanent daemon-side replacement for the one-off
``tools/backtest_shadow_promotion.py``.

Writer boundaries
-----------------
Reuses ``fill_settlement_for_ticker`` (alpha_backtest) and
``annotate_shadow_pnl`` (weather_mm_shadow). Both are idempotent via
``WHERE ts_settle_unix IS NULL``, so racing with ``record_settlements``
at cycle boundaries is safe — the second writer's UPDATE finds zero rows.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Iterable, Optional

from bot.api import api_get
from bot.learning.alpha_log import fill_settlement_for_ticker
from bot.learning.mm_promotion import annotate_shadow_pnl

logger = logging.getLogger(__name__)


# Default per-series pagination cap for the scheduler-driven poller. One
# settled-catalog page holds 200 markets; a weather series rolls ~30 new
# settled markets per day, so 10 pages is a 66× steady-state headroom.
# The one-off backtest tool uses 50 for historical back-fills.
DEFAULT_MAX_PAGES = 10

# Politeness delay between catalog pages. Matches the one-off tool.
_PAGE_SLEEP_S = 0.25


def _parse_close_ts(close_time: str) -> Optional[float]:
    """Kalshi ISO8601 ("2026-04-21T20:00:00Z") → unix epoch float.

    Returns ``None`` on parse failure (caller drops the row).
    """
    if not close_time:
        return None
    try:
        dt = datetime.fromisoformat(close_time.rstrip("Z")).replace(
            tzinfo=timezone.utc
        )
        return dt.timestamp()
    except Exception:
        return None


def fetch_settled_markets(
    series: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, tuple[str, float]]:
    """Return ``{ticker: (result_lower, close_ts_unix)}`` for settled markets.

    Drops entries whose ``result`` is not ``"yes"`` or ``"no"`` (void /
    unresolved) and whose ``close_time`` is unparseable.
    Bounded by ``max_pages`` to keep poller-driven cost predictable.
    """
    out: dict[str, tuple[str, float]] = {}
    cursor: Optional[str] = None
    pages = 0
    while pages < max_pages:
        params: dict[str, str] = {
            "series_ticker": series,
            "status": "settled",
            "limit": "200",
        }
        if cursor:
            params["cursor"] = cursor
        path = "/markets?" + urllib.parse.urlencode(params)
        try:
            resp = api_get(path)
        except Exception as exc:
            logger.warning(
                "[settlement_backfill] %s API error page %d: %s",
                series, pages, exc,
            )
            break
        markets = resp.get("markets", [])
        if not markets:
            break
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue
            result = (m.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue
            close_time = m.get("close_time") or m.get("expiration_time") or ""
            ts = _parse_close_ts(close_time)
            if ts is None:
                continue
            out[ticker] = (result, ts)
        cursor = resp.get("cursor")
        pages += 1
        if not cursor:
            break
        time.sleep(_PAGE_SLEEP_S)
    return out


def _distinct_unsettled_series(conn: sqlite3.Connection) -> set[str]:
    """Union of series with unsettled rows in alpha_backtest OR
    weather_mm_shadow.

    Both tables store the series ticker identically (``KXHIGHNY``,
    ``KXFED``, …) — ``alpha_backtest.family`` from
    ``family_from_ticker`` and ``weather_mm_shadow.series`` from
    ``WeatherQuoter``.
    """
    out: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT family FROM alpha_backtest "
            "WHERE family IS NOT NULL AND ts_settle_unix IS NULL"
        ).fetchall()
        out.update(r[0] for r in rows if r[0])
    except Exception as exc:
        logger.warning(
            "[settlement_backfill] alpha_backtest series probe failed: %s", exc
        )
    try:
        rows = conn.execute(
            "SELECT DISTINCT series FROM weather_mm_shadow "
            "WHERE series IS NOT NULL AND ts_settle_unix IS NULL"
        ).fetchall()
        out.update(r[0] for r in rows if r[0])
    except Exception as exc:
        logger.warning(
            "[settlement_backfill] weather_mm_shadow series probe failed: %s",
            exc,
        )
    return {s.upper() for s in out}


def _unsettled_alpha_tickers(
    conn: sqlite3.Connection, family: str,
) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM alpha_backtest "
            "WHERE family = ? AND ts_settle_unix IS NULL",
            (family,),
        ).fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception as exc:
        logger.warning(
            "[settlement_backfill] alpha ticker probe %s failed: %s",
            family, exc,
        )
        return set()


def _unsettled_shadow_tickers(
    conn: sqlite3.Connection, series: str,
) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM weather_mm_shadow "
            "WHERE series = ? AND ts_settle_unix IS NULL",
            (series,),
        ).fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception as exc:
        logger.warning(
            "[settlement_backfill] shadow ticker probe %s failed: %s",
            series, exc,
        )
        return set()


def backfill_from_catalog(
    conn: sqlite3.Connection,
    *,
    series_list: Optional[Iterable[str]] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, int | list[str]]:
    """Main entrypoint. Called from the daemon scheduler every ~10 min.

    Flow:
      1. Discover distinct series with unsettled alpha/shadow rows
         (caller can override via ``series_list`` for a narrow pass).
      2. Per series, fetch ``/markets?series_ticker=X&status=settled``.
      3. Intersect catalog with our unsettled tickers, call both
         ``fill_settlement_for_ticker`` and ``annotate_shadow_pnl`` for
         each match.

    Returns stats dict — consumed by the scheduler wrapper for logging.
    """
    stats: dict[str, int | list[str]] = {
        "series_scanned": 0,
        "tickers_settled": 0,
        "alpha_rows_filled": 0,
        "shadow_rows_annotated": 0,
        "catalog_errors": 0,
        "series": [],
    }

    if series_list is None:
        series = sorted(_distinct_unsettled_series(conn))
    else:
        series = sorted({s.upper() for s in series_list if s})

    if not series:
        return stats

    for s in series:
        stats["series_scanned"] += 1
        assert isinstance(stats["series"], list)
        stats["series"].append(s)

        alpha_pending = _unsettled_alpha_tickers(conn, s)
        shadow_pending = _unsettled_shadow_tickers(conn, s)
        pending = alpha_pending | shadow_pending
        if not pending:
            continue

        try:
            settled = fetch_settled_markets(s, max_pages=max_pages)
        except Exception as exc:
            logger.warning(
                "[settlement_backfill] %s catalog fetch raised: %s", s, exc
            )
            stats["catalog_errors"] += 1
            continue

        matched = pending & set(settled.keys())
        if not matched:
            continue

        for ticker in sorted(matched):
            result, close_ts = settled[ticker]
            # Alpha back-fill
            if ticker in alpha_pending:
                try:
                    n = fill_settlement_for_ticker(
                        conn,
                        ticker=ticker,
                        settlement_result=result,
                        ts_settle_unix=float(close_ts),
                    )
                    stats["alpha_rows_filled"] += int(n or 0)
                except Exception as exc:
                    logger.warning(
                        "[settlement_backfill] alpha fill %s failed: %s",
                        ticker, exc,
                    )
            # Shadow annotation
            if ticker in shadow_pending:
                try:
                    n = annotate_shadow_pnl(
                        conn, ticker,
                        won_yes=(result == "yes"),
                        ts_settle_unix=float(close_ts),
                    )
                    stats["shadow_rows_annotated"] += int(n or 0)
                except Exception as exc:
                    logger.warning(
                        "[settlement_backfill] shadow annotate %s failed: %s",
                        ticker, exc,
                    )
            stats["tickers_settled"] += 1

    return stats
