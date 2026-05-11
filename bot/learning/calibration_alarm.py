"""Daily |z| calibration alarm with TTE-bucketed thresholds.

Computes mean(|z|) per TTE bucket from settled weather forecasts and fires
Telegram alerts when calibration drifts. The <12h bucket is the most
sensitive — METAR is supposed to anchor the model in that window, so any
|z| spike there indicates a pipeline break (METAR not feeding predict_v2,
σ floor too low, μ stale), not an honest tail event.

Two alarms per bucket:
  - Drift:  3-day mean(|z|) > drift threshold  → slow degradation
  - Spike:  1-day mean(|z|) > spike threshold  → sudden break

Thresholds are deliberately loose v1; tighten after ~30 days of real
|z| distribution data. See memory/project_calibration_alarm_thresholds.md
for the planned tightening (especially <12h spike → 1.0).

Data path:
  - Predicted (μ, σ) per ticker per recorded_at: weather_forecast_snapshots
    where source='combined_v2'. Deduped to one snapshot per (ticker,
    integer hours_out) — last recording wins.
  - Actual high temp per (station, settle_date): weather_metar_hourly_backfill
    where daily_high_f IS NOT NULL.
  - z = (actual - μ) / σ, bucketed by hours_out, attributed to settle_date.

Schedule: daily at fixed UTC hour (registered in bot/daemon/main.py).

Decision: 2026-05-08, Phase 2 item 3.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.observability.alerts import send_alert

logger = logging.getLogger(__name__)


# v1 thresholds — see project_calibration_alarm_thresholds.md.
# Each bucket has its own "healthy" mean(|z|) baseline driven by what's
# anchoring the forecast at that horizon:
#   - >48h:   wide-σ ensemble; healthy mean(|z|) ~0.8
#   - 12-48h: tighter ensemble + early METAR; healthy ~0.6
#   - <12h:   METAR + persistence dominates; healthy ~0.4
THRESHOLDS: dict[str, dict[str, float]] = {
    ">48h":   {"drift": 1.5, "spike": 2.5},
    "12-48h": {"drift": 1.2, "spike": 2.0},
    "<12h":   {"drift": 0.8, "spike": 1.5},
}

# Skip alarms when a bucket has too few data points to be meaningful.
MIN_SETTLEMENTS = 10

# Drift looks back this many days of settlements (including the spike day).
DRIFT_LOOKBACK_DAYS = 3

_MONTHS_BY_NAME = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MONTHS_BY_NUM = {v: k for k, v in _MONTHS_BY_NAME.items()}

_TICKER_DATE_RE = re.compile(r"^KX[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-")


def _bucket_for(hours_out: int) -> str:
    if hours_out > 48:
        return ">48h"
    if hours_out >= 12:
        return "12-48h"
    return "<12h"


def _settle_date_from_ticker(ticker: str) -> Optional[str]:
    """'KXHIGHNY-26MAY08-T65.0' → '2026-05-08'. Returns None on parse fail."""
    m = _TICKER_DATE_RE.match(ticker.upper())
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2), m.group(3)
    if mon not in _MONTHS_BY_NAME:
        return None
    return f"20{yy}-{_MONTHS_BY_NAME[mon]:02d}-{int(dd):02d}"


def _date_to_ticker_pattern(d: datetime) -> str:
    """date(2026,5,8) → '%-26MAY08-%' for SQL LIKE matching."""
    yy = d.year % 100
    mon = _MONTHS_BY_NUM[d.month]
    return f"%-{yy:02d}{mon}{d.day:02d}-%"


def evaluate_calibration_z(
    conn: sqlite3.Connection,
    *,
    now_utc: Optional[datetime] = None,
) -> dict[str, dict]:
    """Compute per-bucket |z| metrics over the lookback window.

    Returns a dict keyed by bucket name with:
      n_snapshots:    deduped snapshot count
      n_settlements:  distinct ticker count
      daily_means:    {settle_date_str: mean(|z|)}
      drift_3d:       mean(|z|) across all snapshots in window (or None)
      spike_1d:       mean(|z|) for snapshots whose settle_date == spike_date
                      (or None if no settled tickers on that date)
      spike_date:     ISO date string used as the spike anchor

    Returns {} if no settled actuals are available yet.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Anchor on the most recent date with actual high observations.
    # Weather settles at local midnight, METAR backfill catches up shortly
    # after, so "yesterday" is the most recent fully-resolved settle date.
    row = conn.execute(
        "SELECT MAX(lst_date) FROM weather_metar_hourly_backfill "
        "WHERE daily_high_f IS NOT NULL"
    ).fetchone()
    if not row or not row[0]:
        return {}

    spike_date = datetime.strptime(row[0], "%Y-%m-%d").date()
    drift_dates = [
        spike_date - timedelta(days=i) for i in range(DRIFT_LOOKBACK_DAYS)
    ]
    ticker_patterns = [_date_to_ticker_pattern(
        datetime(d.year, d.month, d.day)) for d in drift_dates]

    pattern_clauses = " OR ".join(["ticker LIKE ?"] * len(ticker_patterns))

    # Pull the latest snapshot per (ticker, hours_out) for each lookback date.
    # σ > 0 guards against degenerate rows that would div-by-zero in z.
    cur = conn.execute(
        f"""
        WITH ranked AS (
          SELECT ticker, forecast_high_f AS mu, sigma_f AS sigma, hours_out,
                 ROW_NUMBER() OVER (
                   PARTITION BY ticker, hours_out
                   ORDER BY recorded_at DESC
                 ) AS rn
          FROM weather_forecast_snapshots
          WHERE source = 'combined_v2'
            AND sigma_f IS NOT NULL AND sigma_f > 0
            AND forecast_high_f IS NOT NULL
            AND hours_out IS NOT NULL
            AND ({pattern_clauses})
        )
        SELECT ticker, mu, sigma, hours_out FROM ranked WHERE rn = 1
        """,
        ticker_patterns,
    )
    snapshots = cur.fetchall()

    # Local import to avoid pulling station registry at module load.
    from bot.daemon.stations import station_for_ticker

    actual_cache: dict[tuple[str, str], Optional[float]] = {}

    def _actual_high(station: str, date_str: str) -> Optional[float]:
        key = (station, date_str)
        if key not in actual_cache:
            r = conn.execute(
                "SELECT daily_high_f FROM weather_metar_hourly_backfill "
                "WHERE station=? AND lst_date=? AND daily_high_f IS NOT NULL "
                "LIMIT 1",
                (station, date_str),
            ).fetchone()
            actual_cache[key] = r[0] if r else None
        return actual_cache[key]

    # Build per-bucket records: bucket -> list of (settle_date_str, ticker, |z|)
    records: dict[str, list[tuple[str, str, float]]] = {b: [] for b in THRESHOLDS}

    for ticker, mu, sigma, hours_out in snapshots:
        sd = _settle_date_from_ticker(ticker)
        if sd is None:
            continue
        st = station_for_ticker(ticker)
        if st is None:
            continue
        actual = _actual_high(st.icao, sd)
        if actual is None:
            continue
        z = abs(float(actual) - float(mu)) / float(sigma)
        bucket = _bucket_for(int(hours_out))
        records[bucket].append((sd, ticker, z))

    spike_date_str = spike_date.isoformat()
    result: dict[str, dict] = {}
    for bucket, rs in records.items():
        if not rs:
            result[bucket] = {
                "n_snapshots": 0, "n_settlements": 0,
                "daily_means": {}, "drift_3d": None, "spike_1d": None,
                "spike_date": spike_date_str,
            }
            continue
        daily_zs: dict[str, list[float]] = {}
        for sd, _ticker, z in rs:
            daily_zs.setdefault(sd, []).append(z)
        daily_means = {d: statistics.mean(zs) for d, zs in daily_zs.items()}
        drift_3d = statistics.mean(z for _, _, z in rs)
        spike_zs = [z for sd, _, z in rs if sd == spike_date_str]
        spike_1d = statistics.mean(spike_zs) if spike_zs else None
        result[bucket] = {
            "n_snapshots": len(rs),
            "n_settlements": len({t for _, t, _ in rs}),
            "daily_means": daily_means,
            "drift_3d": drift_3d,
            "spike_1d": spike_1d,
            "spike_date": spike_date_str,
        }
    return result


def _format_drift_alert(bucket: str, m: dict, threshold: float) -> str:
    daily = m["daily_means"]
    daily_str = ", ".join(
        f"{d}={daily[d]:.2f}"
        for d in sorted(daily.keys())
    )
    return (
        f"Calibration drift: {bucket} bucket\n"
        f"3-day mean |z| = {m['drift_3d']:.2f} > {threshold:.2f} threshold\n"
        f"N={m['n_snapshots']} snapshots across {m['n_settlements']} settlements\n"
        f"Daily means: {daily_str}"
    )


def _format_spike_alert(bucket: str, m: dict, threshold: float) -> str:
    return (
        f"Calibration spike: {bucket} bucket\n"
        f"{m['spike_date']} mean |z| = {m['spike_1d']:.2f} > {threshold:.2f} threshold\n"
        f"N={m['n_snapshots']} snapshots, {m['n_settlements']} settlements in 3-day window"
    )


def run_calibration_alarm(conn: sqlite3.Connection) -> dict:
    """Scheduler entry point. Compute per-bucket metrics, fire Telegram for
    any threshold breach. Returns the metrics dict for log/diagnostic use."""
    metrics = evaluate_calibration_z(conn)
    if not metrics:
        logger.info("[calibration_alarm] no actuals yet, skipping")
        return {}

    fired = []
    for bucket, m in metrics.items():
        thresh = THRESHOLDS[bucket]
        if m["n_settlements"] < MIN_SETTLEMENTS:
            logger.info(
                "[calibration_alarm] %s skipped (n_settlements=%d < %d)",
                bucket, m["n_settlements"], MIN_SETTLEMENTS,
            )
            continue

        if m["drift_3d"] is not None and m["drift_3d"] > thresh["drift"]:
            msg = _format_drift_alert(bucket, m, thresh["drift"])
            send_alert(msg, "warning")
            fired.append(("drift", bucket, m["drift_3d"]))
            logger.warning("[calibration_alarm] DRIFT fired: %s drift_3d=%.2f",
                           bucket, m["drift_3d"])

        if m["spike_1d"] is not None and m["spike_1d"] > thresh["spike"]:
            msg = _format_spike_alert(bucket, m, thresh["spike"])
            send_alert(msg, "warning")
            fired.append(("spike", bucket, m["spike_1d"]))
            logger.warning("[calibration_alarm] SPIKE fired: %s spike_1d=%.2f",
                           bucket, m["spike_1d"])

    if not fired:
        def _fmt(v: Optional[float]) -> str:
            return f"{v:.2f}" if v is not None else "na"

        logger.info(
            "[calibration_alarm] all clear: %s",
            " ".join(
                f"{b}(drift={_fmt(m['drift_3d'])},"
                f"spike={_fmt(m['spike_1d'])},"
                f"n={m['n_settlements']})"
                for b, m in metrics.items()
            ),
        )

    return metrics
