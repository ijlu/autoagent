"""Shadow→calibration bridge for weather MM families.

Problem this solves
-------------------
``calibration`` is populated from two paths inside ``record_settlements``:

  1. Directional trades — ``est_prob`` + ``won`` on the settled ticker
  2. MM trades — ``AVG(fair_value_cents)`` + ``won`` on the settled ticker

Both paths require a bot-placed position to reach the settlement loop.
Weather MM is fully blocked (phase-0 gate) and directional is DRY_RUN,
so neither path has been firing. Meanwhile ``weather_mm_shadow`` holds
27K+ rows with both ``fair_value_cents`` *and* ``ticker_settled_yes`` —
a dense training set sitting one JOIN away from the Platt fitter.

This bridge converts those settled shadow rows into ``calibration`` rows
so the fitter has per-family training signal today, instead of waiting
for weeks of fresh ``alpha_backtest`` accumulation.

Safety: not a feedback loop
---------------------------
``WeatherQuoter._compute_fair_value`` uses a direct logistic CDF on
temperature trajectory. It does **not** call
``get_independent_estimate`` and does **not** call
``apply_calibration_correction`` — so ``weather_mm_shadow.fair_value_cents``
is the raw ensemble output, pre-Platt.

Feeding these into ``calibration`` therefore creates a correct training
signal: the fit will learn the systematic bias in WeatherQuoter's raw
FV vs realized outcomes, and downstream directional scoring (which runs
through ``get_independent_estimate``) will inherit the per-family Platt
segments on those same weather families.

Watermark idempotence
---------------------
``kv_cache['shadow_cal_bridge_watermark']`` stores ``max(id)`` of shadow
rows already bridged. Subsequent invocations process only rows with
``id > watermark``, so this is safe to schedule at 600s cadence on the
daemon without double-counting.

The watermark is stored with ``expires_at = NULL`` (never expires).
Losing it would cause duplicate rows, not incorrect ones — the Platt
fitter still converges — but it would over-weight early shadow data
during the duplicated run.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from bot.db import db_write_ctx

logger = logging.getLogger(__name__)

# kv_cache key — must stay stable; clients depend on it for dedup.
WATERMARK_KEY = "shadow_cal_bridge_watermark"

# source_desc stamped on inserted rows. Distinguishes bridge-sourced
# rows from trade-sourced rows inside the same calibration table.
SOURCE_DESC = "weather_mm_shadow"


def _prob_bucket(p: float) -> str:
    """Match the 0.1-wide bucket labels used by ``trade.py._prob_bucket``."""
    p = max(0.0, min(0.999999, p))
    lo = int(p * 10) / 10
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def _load_watermark(conn: sqlite3.Connection) -> int:
    """Return max shadow.id already bridged, or 0 if none."""
    try:
        row = conn.execute(
            "SELECT value FROM kv_cache WHERE key=?", (WATERMARK_KEY,)
        ).fetchone()
    except Exception as exc:
        logger.warning(
            "[shadow_cal_bridge] watermark load failed: %s", exc
        )
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        logger.warning(
            "[shadow_cal_bridge] watermark value unparseable: %r — resetting to 0",
            row[0],
        )
        return 0


def _save_watermark(conn: sqlite3.Connection, max_id: int) -> None:
    """Persist watermark with no TTL (``expires_at IS NULL``)."""
    conn.execute(
        "INSERT INTO kv_cache(key, value, expires_at) VALUES(?, ?, NULL) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=NULL",
        (WATERMARK_KEY, str(int(max_id))),
    )


def bridge_shadow_to_calibration(
    conn: sqlite3.Connection,
    *,
    batch_limit: Optional[int] = None,
) -> dict:
    """Insert ``calibration`` rows for newly-settled ``weather_mm_shadow`` rows.

    Flow:
      1. Read watermark (max shadow.id already bridged from kv_cache).
      2. ``SELECT`` settled shadow rows with id > watermark.
      3. ``INSERT`` one calibration row per shadow row (same transaction).
      4. Advance watermark to ``max(id)`` processed.

    Every shadow row contributes one row; the fitter handles the
    multiple-rows-per-ticker case by treating each as an independent
    estimate taken at a different moment in the lifecycle. That is
    genuinely additional calibration signal — morning vs afternoon FVs
    on the same ticker differ substantially as the temperature
    trajectory resolves.

    Args:
      conn: shared DB connection (must be daemon-compatible).
      batch_limit: cap per invocation. Useful for the one-off initial
        backfill where we want to chunk 27K+ rows into progress-reportable
        slices. None = process everything pending.

    Returns stats dict::

        {
          "rows_bridged": int,         # calibration rows inserted
          "tickers_touched": int,      # distinct tickers represented
          "watermark_before": int,     # shadow.id floor at entry
          "watermark_after": int,      # shadow.id floor at exit
          "skipped_invalid": int,      # rows dropped for bad data
        }
    """
    stats: dict = {
        "rows_bridged": 0,
        "tickers_touched": 0,
        "watermark_before": 0,
        "watermark_after": 0,
        "skipped_invalid": 0,
    }
    watermark = _load_watermark(conn)
    stats["watermark_before"] = watermark
    stats["watermark_after"] = watermark

    sql = (
        "SELECT id, ts_unix, ticker, fair_value_cents, ticker_settled_yes "
        "FROM weather_mm_shadow "
        "WHERE id > ? "
        "  AND ts_settle_unix IS NOT NULL "
        "  AND ticker_settled_yes IS NOT NULL "
        "  AND fair_value_cents IS NOT NULL "
        "ORDER BY id ASC"
    )
    params: tuple = (watermark,)
    if batch_limit:
        sql += " LIMIT ?"
        params = (watermark, int(batch_limit))

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        logger.warning("[shadow_cal_bridge] shadow query failed: %s", exc)
        return stats

    if not rows:
        return stats

    tickers: set[str] = set()
    max_id = watermark
    to_insert: list[tuple] = []
    for rid, ts_unix, ticker, fv, settled_yes in rows:
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            stats["skipped_invalid"] += 1
            continue
        max_id = max(max_id, rid_int)
        if ticker is None or fv is None or settled_yes is None:
            stats["skipped_invalid"] += 1
            continue
        try:
            est_prob = float(fv) / 100.0
            actual = int(settled_yes)
        except (TypeError, ValueError):
            stats["skipped_invalid"] += 1
            continue
        if actual not in (0, 1) or est_prob < 0.0 or est_prob > 1.0:
            stats["skipped_invalid"] += 1
            continue
        try:
            recorded_at = datetime.fromtimestamp(
                float(ts_unix), tz=timezone.utc
            ).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            recorded_at = datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds"
            )
        bucket = _prob_bucket(est_prob)
        to_insert.append(
            (recorded_at, ticker, est_prob, actual, SOURCE_DESC, None, bucket)
        )
        tickers.add(ticker)

    # Even if every row was invalid, advance the watermark — we've seen
    # those ids and don't want to rescan them. Only write inside the lock.
    with db_write_ctx(conn):
        if to_insert:
            conn.executemany(
                "INSERT INTO calibration "
                "(recorded_at, ticker, estimated_prob, actual_outcome, "
                " source_desc, n_sources, bucket) VALUES (?,?,?,?,?,?,?)",
                to_insert,
            )
        if max_id > watermark:
            _save_watermark(conn, max_id)

    stats["rows_bridged"] = len(to_insert)
    stats["tickers_touched"] = len(tickers)
    stats["watermark_after"] = max_id
    return stats
