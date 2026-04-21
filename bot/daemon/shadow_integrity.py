"""Per-series shadow-data integrity monitor.

The Apr-17 → Apr-21 data corruption (20,478 fake-filled rows on fabricated
zero-priced book data) took four days to surface because nothing asserted
that `weather_mm_shadow.market_yes_bid` had the shape Kalshi actually
returns. The bug was: upstream parser read a wrong field name, `_safe_cents`
silently turned the resulting `None` into `0`, and `match_shadow_fills`
then "filled" every quote against the fabricated zero-ask.

Post B+D, `_safe_cents` returns `Optional[int]` and never fabricates zero.
But we were only one typo away from the same class of failure, so this
module adds a live invariant check:

1.  **Zero-price invariant.** Kalshi's minimum quoted price is 1¢. Any
    row with `market_yes_bid = 0` or `market_yes_ask = 0` means a writer
    is back to synthesizing zero from None — critical data-integrity
    failure. Fires a Telegram alert.

2.  **Blind-quote watchdog.** If we've emitted `gate_should_quote=1` rows
    for a series over the last hour but every one of them has
    `market_yes_bid IS NULL`, something upstream is not returning book
    data — parser regression, endpoint change, or upstream outage.
    Fires a warning.

3.  **Stuck-book watchdog.** For series with >=20 posting rows in the last
    hour, the count of DISTINCT non-null `market_yes_bid` values should
    be >1. A frozen book over an hour of quoting is either a broken
    cache or a market that isn't there. Fires an info-level flag so we
    can spot it in the daemon log without alert fatigue.

Hooked into the daemon's scheduler as its own task, 10-minute cadence.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Literal, Optional

logger = logging.getLogger(__name__)

# Level + kind stay typed so a fat-finger typo (e.g. "criticl") is caught
# at static-analysis time instead of silently dropping alerts.
FindingLevel = Literal["critical", "warning", "info"]
FindingKind = Literal["zero_price", "blind_quote", "stuck_book"]


# Minimum rows before the stuck-book / blind-quote signals become
# statistically meaningful. <20 rows in an hour is normal for a sparsely
# traded series and we don't want alert noise on quiet markets.
MIN_ROWS_FOR_SIGNAL = 20

# Default analysis window. 3600s matches "distinct values per hour per
# series" — the check recommended in the post-mortem follow-ons list.
DEFAULT_WINDOW_S = 3600


@dataclass(frozen=True)
class IntegrityFinding:
    """One row-group-level anomaly flagged by the monitor.

    `level`:
      - "critical": invariant broken. Page someone.
      - "warning":  suspicious pattern that warrants attention.
      - "info":     soft signal — log but don't alert.

    `series` may be None for aggregate findings that span all series.
    `metric` stores the numeric value that triggered the finding so the
    log line has a quick-glance number without re-running the query.
    """
    level: FindingLevel
    series: Optional[str]
    kind: FindingKind
    message: str
    metric: float


def check_shadow_data_integrity(
    conn: sqlite3.Connection,
    window_s: int = DEFAULT_WINDOW_S,
) -> List[IntegrityFinding]:
    """Scan the last `window_s` of weather_mm_shadow and return anomalies.

    Returns an empty list when all invariants hold.
    """
    findings: List[IntegrityFinding] = []

    # Rule 1 — zero-price invariant. One query, all series, counts any
    # fabricated-zero row in the window. We count rather than merely
    # detect so the alert text carries the severity magnitude.
    zero_rows = conn.execute(
        """
        SELECT series, COUNT(*) AS n
        FROM weather_mm_shadow
        WHERE ts_unix >= (strftime('%s','now') - ?)
          AND (market_yes_bid = 0 OR market_yes_ask = 0)
        GROUP BY series
        """,
        (int(window_s),),
    ).fetchall()
    for series, n in zero_rows:
        findings.append(IntegrityFinding(
            level="critical",
            series=series,
            kind="zero_price",
            message=(
                f"{series}: {n} row(s) with market_yes_bid=0 or "
                f"market_yes_ask=0 in the last {window_s}s — Kalshi never "
                f"returns 0. This is the Apr-17 bug signature (upstream "
                f"parser + _safe_cents returning 0 on None)."
            ),
            metric=float(n),
        ))

    # Rules 2 + 3 — per-series shape check. Single pass over the window
    # bucketing by series so every signal comes out of one aggregation.
    per_series = conn.execute(
        """
        SELECT
          series,
          COUNT(*) AS n_rows,
          SUM(CASE WHEN gate_should_quote=1 THEN 1 ELSE 0 END) AS n_post,
          SUM(CASE WHEN market_yes_bid IS NOT NULL THEN 1 ELSE 0 END) AS n_book,
          COUNT(DISTINCT market_yes_bid) AS n_distinct_bid
        FROM weather_mm_shadow
        WHERE ts_unix >= (strftime('%s','now') - ?)
        GROUP BY series
        """,
        (int(window_s),),
    ).fetchall()

    for series, n_rows, n_post, n_book, n_distinct_bid in per_series:
        n_rows = int(n_rows or 0)
        n_post = int(n_post or 0)
        n_book = int(n_book or 0)
        # COUNT(DISTINCT market_yes_bid) counts non-NULL values natively.
        n_distinct_bid = int(n_distinct_bid or 0)

        if n_post >= MIN_ROWS_FOR_SIGNAL and n_book == 0:
            # Blind-quote watchdog: we posted but never saw a book.
            findings.append(IntegrityFinding(
                level="warning",
                series=series,
                kind="blind_quote",
                message=(
                    f"{series}: {n_post} would-post rows in the last "
                    f"{window_s}s but 0 have market_yes_bid set. Upstream "
                    f"parser may be returning None for every market."
                ),
                metric=float(n_post),
            ))

        if n_book >= MIN_ROWS_FOR_SIGNAL and n_distinct_bid <= 1:
            # Stuck-book watchdog: many observations, all the same value.
            # Info-level: a real but tight market could match this (e.g.
            # 99c book for a near-certain outcome). We want the signal
            # in the log, not a Telegram buzz.
            findings.append(IntegrityFinding(
                level="info",
                series=series,
                kind="stuck_book",
                message=(
                    f"{series}: {n_book} rows with book data in the last "
                    f"{window_s}s but only {n_distinct_bid} distinct "
                    f"market_yes_bid value(s). Cache stale or market frozen?"
                ),
                metric=float(n_distinct_bid),
            ))

    return findings


def run_shadow_integrity_check(
    conn: sqlite3.Connection,
    window_s: int = DEFAULT_WINDOW_S,
    alert_fn=None,
) -> List[IntegrityFinding]:
    """Run the integrity check and emit logs + alerts.

    `alert_fn` defaults to `bot.observability.alerts.send_alert`; tests
    inject a mock here. Returns the findings list so the scheduler can
    record stats if it wants.

    Never raises. A check failure must not kill the daemon — the whole
    point of this monitor is to be boringly reliable background noise.
    """
    if alert_fn is None:
        # Late import so tests can monkey-patch send_alert before the
        # module is loaded into the scheduler.
        from bot.observability.alerts import send_alert as alert_fn_default
        alert_fn = alert_fn_default

    try:
        findings = check_shadow_data_integrity(conn, window_s=window_s)
    except Exception as exc:  # pragma: no cover — defensive only
        logger.exception("[shadow_integrity] check failed: %s", exc)
        return []

    if not findings:
        logger.info(
            "[shadow_integrity] OK (window=%ds, 0 findings)", window_s,
        )
        return findings

    # Group by level so the log line counts match telemetry expectations.
    critical = [f for f in findings if f.level == "critical"]
    warnings = [f for f in findings if f.level == "warning"]
    infos = [f for f in findings if f.level == "info"]

    for f in critical:
        logger.critical("[shadow_integrity] %s", f.message)
        try:
            alert_fn(f.message, level="critical")
        except Exception as exc:  # pragma: no cover
            logger.exception("[shadow_integrity] alert failed: %s", exc)
    for f in warnings:
        logger.warning("[shadow_integrity] %s", f.message)
    for f in infos:
        logger.info("[shadow_integrity] %s", f.message)

    logger.info(
        "[shadow_integrity] summary: critical=%d warning=%d info=%d",
        len(critical), len(warnings), len(infos),
    )
    return findings
