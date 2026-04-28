"""Persistent daemon entrypoint — replaces the kalshi-bot.timer oneshot.

End-state architecture (Phase 1):

    main (systemd service)
     ├─ init_db() → WAL connection (shared across all threads)
     ├─ METARPoller thread (30s) → on_result → requote handler (future)
     ├─ Other poller threads (Phase 2+)
     └─ Scheduler (main thread)
         ├─ cycle task (60s) → CycleRunner.run_once → trade.main
         ├─ kv_cleanup task (3600s) → bot.db.kv_cleanup
         └─ health-log task (300s) → log poller + cycle health

Design notes:
- Single process, multiple threads. Shared DB connection under WAL with
  DB_WRITE_LOCK for writes.
- Pollers own their own threads (via Poller.start). Scheduler owns the
  main thread and runs periodic tasks.
- SIGTERM/SIGINT triggers scheduler.stop() which runs on_stop hooks to
  tell pollers to stop and close the DB.
- Unhandled exceptions inside cycles are caught by CycleRunner.
  Unhandled exceptions in the scheduler itself propagate out of
  run_forever, kill the process, and systemd restarts us.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

from bot.config import WEATHER_MM_LIVE
from bot.daemon.cycle_runner import CycleRunner
from bot.daemon.dispatcher import AsyncEventDispatcher
from bot.daemon.fills_writer import FillsWriter
from bot.daemon.forecast_cache import ForecastCache, FORECAST_REFRESH_INTERVAL_S
from bot.daemon.metar_poller import METARPoller
from bot.daemon.poller_base import Poller
from bot.daemon.requote_triggers import (
    ForecastChangeDriver,
    TimeDecayDriver,
)
from bot.daemon.scheduler import Scheduler
from bot.daemon.series_discovery import run_discovery as run_series_discovery
from bot.daemon.shadow_integrity import run_shadow_integrity_check
from bot.daemon.weather_handler import WeatherChangeHandler
from bot.daemon.weather_quoter import WeatherQuoter
from bot.db import init_db, kv_cleanup
from bot.learning.fills_validator import compare_last_n_days, format_report
from bot.learning.mm_promotion import (
    match_shadow_fills,
    run_mm_promotion_sweep,
)
from bot.learning.settlement_backfill import backfill_from_catalog
from bot.learning.shadow_calibration_bridge import bridge_shadow_to_calibration
from bot.learning.shadow_promotion import run_promotion_sweep
from bot.learning.weather_mos_materializer import (
    materialize_due as mos_materialize_due,
    fit_and_persist_mos_bias,
    fit_and_persist_skill_curves,
    fit_and_persist_group_correlation,
)
from bot.observability.alerts import send_alert

logger = logging.getLogger(__name__)


# Intervals (seconds). Keep in one place so they're easy to tune.
CYCLE_INTERVAL_S = 60
KV_CLEANUP_INTERVAL_S = 3600
HEALTH_LOG_INTERVAL_S = 300
METAR_POLL_INTERVAL_S = 30
FORECAST_REFRESH_TASK_INTERVAL_S = FORECAST_REFRESH_INTERVAL_S
# Promotion sweep runs daily. A per-family kv flag change is never urgent —
# a day's lag between meeting the gate and flipping live is fine, and daily
# avoids recomputing the same settled-row stats on every cycle.
PROMOTION_SWEEP_INTERVAL_S = 24 * 3600

# MM shadow fill matcher runs often — it's the input to every MM promotion
# decision, and the per-call cost is a bounded scan over unmatched rows.
MM_FILL_MATCH_INTERVAL_S = 300  # 5 minutes = quote lifetime window
MM_PROMOTION_SWEEP_INTERVAL_S = 24 * 3600

# T1.2 — time-decay driver cadence. The driver itself decides when to fire
# based on hours_left (45s in the last hour → 600s when >8h left). We tick
# it every 30 s so even the <1h/45s cadence has no more than ~45 s of lag
# beyond its declared target.
TIME_DECAY_DRIVER_INTERVAL_S = 30

# T3.1 — fills-ledger sync. One /portfolio/fills pagination per minute is
# plenty: fills are append-only Kalshi-side, the ledger PK dedups, and
# readers (kill-switch, settlement reconciler) are OK with a minute of
# lag. We over-request a 2-minute tail each tick so fills that arrive
# slightly out of order — Kalshi has been observed to backdate — still
# land. Overlap is free: INSERT OR IGNORE on trade_id makes re-requests
# no-ops.
FILLS_SYNC_INTERVAL_S = 60
FILLS_SYNC_OVERLAP_S = 120

# T3.1 — dual-run validator. Runs daily (24h is what the T3.3 migration
# gate counts in days). Window is 7 days; alerting fires only when
# is_meaningful (both sources populated) AND non-clean — empties are
# logged but silent so we don't spam Telegram during the bedding-in
# period when mm_processed_fills has no writers.
FILLS_VALIDATOR_INTERVAL_S = 24 * 3600
FILLS_VALIDATOR_WINDOW_DAYS = 7

# Shadow-data integrity monitor (post-mortem follow-on #2). Runs every
# 10 minutes over a 1-hour window. The window is deliberately wider than
# the cadence so a single slow METAR batch doesn't starve the signal —
# overlap is free, findings are idempotent.
#
# Why 10 min, not 5 min like the cycle health log: each check is O(rows-
# in-window), and a 1-hour window on six series grows to 10k+ rows.
# Re-running every 5 min would double DB load on the one query we most
# want to trust. 10 min still catches the Apr-17 regression within ~15
# minutes of first corrupt write, vs. the 4 days it took in reality.
SHADOW_INTEGRITY_INTERVAL_S = 600
SHADOW_INTEGRITY_WINDOW_S = 3600

# Settlement back-fill poller (2026-04-22). Drives off /markets?status=settled
# rather than /portfolio/settlements so alpha_backtest + weather_mm_shadow
# rows for *shadow-only* tickers (we never held a position) get
# ts_settle_unix stamped. Without this path, the Platt calibration feeding
# the ensemble had been starved for weeks — root cause of the 2026-04-22
# ensemble-calibration audit finding (Brier 0.37–0.52 vs market 0.03–0.06).
#
# 600s cadence: same as shadow_integrity. Settlement latency of 5–10 min
# feeding the learning loop is negligible vs the 24h mm_promotion_sweep
# downstream consumer.
SETTLEMENT_BACKFILL_INTERVAL_S = 600

# Weather MOS-bias materializer. Walks settled weather tickers in the past
# 14 days, fetches IEM observed daily-high once per (city, date), writes one
# row per canonical Gaussian source into weather_gaussian_snapshots_backfill.
# Feeds the same EWMA fitter (tools/backfill_weather_effective_n.fit_mos_bias)
# that the historical Open-Meteo backfill seeds. Covers nws_point + tomorrow
# (no historical archive available) and every other Gaussian source going
# forward.
#
# Hourly cadence: weather markets settle once per LST day, so per-tick
# new-row volume is small. The eligibility window is 14 days, so a single
# tick after a 13h outage backfills cleanly. One IEM fetch per (city, date)
# pair, ~6 cities × 1 date = ~6 HTTP calls per tick worst case.
MOS_MATERIALIZE_INTERVAL_S = 3600

# Daily series-discovery sweep. Looks at /events?status=open, finds
# series_tickers that match a routable prefix (weather + macro families)
# and aren't in TRADE_SERIES_ALLOWLIST, alerts on novel ones. Per Josh:
# "daily is fine since we'd need to backtest the ensemble anyway" — the
# minimum lead time before we'd act on a discovery is days, not hours.
SERIES_DISCOVERY_INTERVAL_S = 24 * 3600


def _configure_logging() -> None:
    """Log to stdout (captured by systemd's StandardOutput=append).
    Format includes thread name so poller vs cycle vs main are
    visually distinct in the daemon log."""
    level = os.environ.get("KALSHI_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _latest_equity_dollars(conn) -> float:
    """Best-effort current equity for the promotion sweep.

    Reads the most recent `sessions` row — populated each cycle by trade.py
    with balance_cents + portfolio_cents. Defaults to $1000 when no session
    has run yet (first-boot case — conservative under-estimate keeps the
    kill-switch trigger floors reasonable).
    """
    try:
        row = conn.execute(
            "SELECT balance_cents, portfolio_cents FROM sessions "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            balance = int(row[0] or 0)
            portfolio = int(row[1] or 0)
            return max(100.0, (balance + portfolio) / 100.0)
    except Exception:
        pass
    return 1000.0


def _run_promotion_sweep(conn) -> None:
    """Daily promotion+demotion sweep. Lightweight wrapper that pulls the
    current equity snapshot and delegates to shadow_promotion."""
    equity = _latest_equity_dollars(conn)
    summary = run_promotion_sweep(conn, equity_dollars=equity)
    if summary["promoted"] or summary["graduated"] or summary["demoted"]:
        logger.info(
            "[promotion] checked=%d promoted=%d graduated=%d demoted=%d",
            summary["checked"], len(summary["promoted"]),
            len(summary["graduated"]), len(summary["demoted"]),
        )
        for entry in summary["promoted"] + summary["graduated"]:
            logger.info("[promotion]   ↑ %s", entry)
        for entry in summary["demoted"]:
            logger.warning("[promotion]   ↓ %s", entry)
    else:
        logger.info(
            "[promotion] checked=%d unchanged=%d",
            summary["checked"], len(summary["unchanged"]),
        )


def _run_fills_sync(
    writer: FillsWriter, conn, daemon_start_unix: float,
) -> None:
    """Pull recent fills from Kalshi into ``fills_ledger``.

    Determines ``since_unix`` from ``max(fill_ts_unix)`` in the ledger,
    minus ``FILLS_SYNC_OVERLAP_S`` so backdated fills still land. When
    the ledger is empty (first boot after T3.1 deploy), we use daemon
    start time — per scoping doc §2, we deliberately do NOT back-fill
    historical mm_processed_fills rows.

    Exceptions propagate to the scheduler, which catches, logs, and
    increments ``error_count`` on the task. Swallowing them here would
    make the scheduler's health counter lie — this is the exact failure
    shape that hid the 2026-04-20 shadow corruption for four days.
    """
    row = conn.execute(
        "SELECT MAX(fill_ts_unix) FROM fills_ledger"
    ).fetchone()
    max_ts = row[0] if row and row[0] is not None else daemon_start_unix
    since = max(0.0, max_ts - FILLS_SYNC_OVERLAP_S)
    inserted = writer.sync_since(since, live_mode=WEATHER_MM_LIVE)
    if inserted:
        logger.info(
            "[fills_sync] inserted=%d new rows (since_unix=%.0f)",
            inserted, since,
        )


def _run_fills_validator(conn) -> None:
    """Daily ledger-vs-legacy divergence report. Silent on empty /
    informational, warns on meaningful divergence.

    - No divergence (clean + meaningful): info log only.
    - Non-meaningful (one side empty): info log, no alert — this is the
      T3.1 steady state until mm_processed_fills re-gains writers or we
      add a second reference source.
    - Meaningful + divergent: WARNING log + Telegram alert. This is the
      signal the T3.3 reader-migration gate cares about.

    Exceptions propagate to the scheduler. The wrapper's previous
    try/except swallowed them, which made the scheduler's per-task
    ``error_count`` report zero even when every run raised
    ``no such column: side`` against the legacy production schema
    (2026-04-22 audit finding).
    """
    report = compare_last_n_days(
        conn, n_days=FILLS_VALIDATOR_WINDOW_DAYS,
    )
    text = format_report(report)
    if report.is_meaningful and not report.is_clean:
        logger.warning(text)
        send_alert(text, level="warning")
    else:
        logger.info(text)


def _run_mos_materializer(conn) -> None:
    """Walks settled weather tickers and writes per-source forecast/observed
    pairs into the MOS-bias backfill table.

    Exceptions propagate to the scheduler — same rationale as fills_sync /
    settlement_backfill: silent error counters hid the April shadow corruption
    for four days. Loud failures, even at 3600s cadence, are the bar.
    """
    stats = mos_materialize_due(conn)
    if stats["rows_written"] or stats["iem_misses"] or stats["tickers_unresolved_city"]:
        logger.info(
            "[mos_materializer] eligible=%d city_dates=%d iem_calls=%d "
            "iem_misses=%d rows_written=%d unresolved_city=%d",
            stats["tickers_eligible"], stats["city_dates_eligible"],
            stats["iem_calls"], stats["iem_misses"],
            stats["rows_written"], stats["tickers_unresolved_city"],
        )
    else:
        logger.debug(
            "[mos_materializer] eligible=%d no new rows",
            stats["tickers_eligible"],
        )

    # Always re-fit bias after materialisation — even when no new rows were
    # written, EWMA weights shift daily so the fit should stay fresh.
    fit_stats = fit_and_persist_mos_bias(conn)
    if fit_stats["keys_written"] or fit_stats.get("error"):
        logger.info(
            "[mos_fitter] cells=%d keys_written=%d cells_thin=%d%s",
            fit_stats["cells_fitted"], fit_stats["keys_written"],
            fit_stats["cells_thin"],
            f" error={fit_stats['error']}" if fit_stats.get("error") else "",
        )

    # Skill σ and group ρ ride the same scheduler tick: they read from the
    # same backfill table the materializer just topped up, and the
    # combine consumes all three (bias / σ / ρ) on the next quote.
    skill_stats = fit_and_persist_skill_curves(conn)
    if skill_stats["keys_written"] or skill_stats.get("error"):
        logger.info(
            "[skill_fitter] buckets=%d keys_written=%d (city=%d) buckets_thin=%d%s",
            skill_stats["buckets_fitted"], skill_stats["keys_written"],
            skill_stats.get("city_keys_written", 0),
            skill_stats["buckets_thin"],
            f" error={skill_stats['error']}" if skill_stats.get("error") else "",
        )

    group_stats = fit_and_persist_group_correlation(conn)
    if group_stats["persisted"] or group_stats.get("error"):
        logger.info(
            "[group_rho_fitter] persisted=%s rho=%s n_eff=%s n_pairs=%s%s",
            group_stats["persisted"], group_stats["rho"],
            group_stats["n_eff"], group_stats["n_pairs"],
            f" error={group_stats['error']}" if group_stats.get("error") else "",
        )

    # METAR residual σ per (station, LST hour). Replaces the hardcoded
    # hours-remaining schedule with empirical std of
    # (eventual_daily_high − running_max_at_hour) from the hourly backfill
    # — making METAR's σ much tighter late in the day, which is when
    # running_high is most informative and forecasts are stalest.
    try:
        from bot.learning.weather_mos_materializer import (
            fit_and_persist_metar_residual_sigma,
        )
        residual_stats = fit_and_persist_metar_residual_sigma(conn)
        if residual_stats["keys_written"]:
            logger.info(
                "[metar_residual_fitter] cells=%d keys_written=%d cells_thin=%d",
                residual_stats["cells_fitted"], residual_stats["keys_written"],
                residual_stats["cells_thin"],
            )
    except Exception as exc:
        logger.warning("[metar_residual_fitter] failed: %s", exc)

    # Snapshots-based skill σ + MOS bias for sources missing from the
    # original Open-Meteo backfill (NWS Point, MADIS, etc.). Reads live
    # forecasts joined to observed daily highs so all firing sources get
    # per-city σ, not just HRRR/NBM/weather/open_meteo.
    try:
        from bot.learning.weather_mos_materializer import (
            fit_and_persist_skill_from_snapshots,
        )
        snap_stats = fit_and_persist_skill_from_snapshots(conn)
        if snap_stats["skill_keys_written"] or snap_stats["mos_keys_written"]:
            logger.info(
                "[snapshot_fitter] skill_written=%d skill_thin=%d  "
                "mos_written=%d mos_thin=%d",
                snap_stats["skill_keys_written"], snap_stats["skill_cells_thin"],
                snap_stats["mos_keys_written"], snap_stats["mos_cells_thin"],
            )
    except Exception as exc:
        logger.warning("[snapshot_fitter] failed: %s", exc)

    # Coverage audit fires after every fitter tick — surfaces any
    # (source, city) cell that's still on a wide pooled fallback so we
    # can fix the data instead of letting the under-weighting compound.
    try:
        _run_source_coverage_audit(conn)
    except Exception as exc:
        logger.warning("[source_coverage] audit failed: %s", exc)


def _run_source_coverage_audit(conn) -> None:
    """Per-source kv coverage check. Warns when a (source, city) cell has
    no learned skill σ or MOS bias, so we don't silently fall back to a
    wide pooled value (which under-weights the source in the combine).

    Discovery context (2026-04-27): NWS Point / MADIS / AFD had only n=12
    backfill rows total, so they got a pooled-only skill σ ≈ 3°F vs the
    canonical sources' per-city ≈ 1°F. NWS Point's correct +5°F warm
    forecast for Austin was being weighted at 4% of the precision pool —
    effectively excluded — and we never noticed. This audit makes the gap
    visible at every materializer tick.

    City convention (also 2026-04-27): three different city naming
    schemes exist in the codebase:
      * ``WeatherStation.city`` uses the raw human form: "los angeles",
        "nyc". ``STATION_BY_CITY`` is keyed by these AND their aliases
        ("new york", "la"), so its keys aren't unique-per-city.
      * The materializer normalizes when writing kv: ``"los angeles"``
        → ``"los_angeles"``, ``"nyc"`` → ``"nyc"``. KV keys end up as
        ``weather_skill_hrrr_los_angeles_6_24`` (underscore form).
      * ``predict_v2._city_for_ticker`` does the same normalization for
        reads, so live quotes hit the right kv keys.
    Earlier this audit iterated ``STATION_BY_CITY.keys()`` and its
    aliases ("new york", "la") got normalized to "new_york"/"la" — kv
    keys under those names don't exist, causing 30+ false-positive
    "missing" warnings that masked any real coverage gaps. Fixed by
    iterating the canonical 6-city set used by the materializer.
    """
    SKILL_SOURCES = ("hrrr", "nbm", "weather", "open_meteo", "nws_point",
                     "madis")
    MOS_SOURCES = ("hrrr", "nbm", "weather", "open_meteo", "nws_point",
                   "metar", "madis")
    # Canonical 6-city set. Must match
    # ``bot.learning.weather_mos_materializer._STATION_BY_CITY_KEY`` keys
    # — those are the only city-key strings the materializer ever writes.
    cities = ["nyc", "chicago", "miami", "los_angeles", "austin", "denver"]

    # Skill σ coverage: per-(source, city) check. ``cities`` is already
    # in kv-key form ("nyc", "los_angeles", etc.) — no normalization here
    # so the audit's idea of a city always matches the materializer's.
    skill_missing = []
    for src in SKILL_SOURCES:
        for city in cities:
            key = f"weather_skill_{src}_{city}_6_24"
            row = conn.execute(
                "SELECT 1 FROM kv_cache WHERE key=?", (key,),
            ).fetchone()
            if row is None:
                skill_missing.append((src, city))

    # MOS bias coverage: per-(source, city) check
    mos_missing = []
    for src in MOS_SOURCES:
        for city in cities:
            key = f"weather_mos_bias_{src}_{city}"
            row = conn.execute(
                "SELECT 1 FROM kv_cache WHERE key=?", (key,),
            ).fetchone()
            if row is None:
                mos_missing.append((src, city))

    if skill_missing:
        logger.warning(
            "[source_coverage] skill σ missing for %d (source, city) cells: %s",
            len(skill_missing),
            ", ".join(f"{s}/{c}" for s, c in skill_missing[:8])
            + (f" ... +{len(skill_missing)-8} more" if len(skill_missing) > 8 else ""),
        )
    if mos_missing:
        logger.warning(
            "[source_coverage] MOS bias missing for %d (source, city) cells: %s",
            len(mos_missing),
            ", ".join(f"{s}/{c}" for s, c in mos_missing[:8])
            + (f" ... +{len(mos_missing)-8} more" if len(mos_missing) > 8 else ""),
        )
    if not skill_missing and not mos_missing:
        logger.info("[source_coverage] all (source, city) cells have learned σ + bias")


def _run_settlement_backfill(conn) -> None:
    """Catalog-driven settlement back-fill.

    Fills ``alpha_backtest.ts_settle_unix`` and ``weather_mm_shadow``
    settlement columns for tickers we shadowed but didn't hold positions
    in — which ``record_settlements()``'s portfolio-driven loop can't
    reach. Unblocks the Platt calibration loop that feeds the ensemble.

    Exceptions propagate to the scheduler — we deliberately don't
    swallow them here for the same reason noted on ``_run_fills_sync``:
    silent error counters hid the April shadow corruption for four days.
    """
    summary = backfill_from_catalog(conn)
    if summary["tickers_settled"] or summary["catalog_errors"]:
        logger.info(
            "[settlement_backfill] series_scanned=%d tickers_settled=%d "
            "alpha_rows_filled=%d shadow_rows_annotated=%d catalog_errors=%d",
            summary["series_scanned"], summary["tickers_settled"],
            summary["alpha_rows_filled"], summary["shadow_rows_annotated"],
            summary["catalog_errors"],
        )
    else:
        logger.debug(
            "[settlement_backfill] series_scanned=%d no new settlements",
            summary["series_scanned"],
        )

    # Inline the shadow→calibration bridge here so newly-annotated
    # weather_mm_shadow rows flow into the Platt training set on the
    # same cadence as settlement back-fill. The bridge is a cheap
    # no-op when the watermark is caught up; chaining them here avoids
    # any race between the annotator writing ticker_settled_yes and a
    # separate scheduler task trying to read it.
    try:
        cal_summary = bridge_shadow_to_calibration(conn)
    except Exception as exc:
        logger.exception("[shadow_cal_bridge] bridge failed: %s", exc)
        return
    if cal_summary["rows_bridged"]:
        logger.info(
            "[shadow_cal_bridge] rows_bridged=%d tickers_touched=%d "
            "watermark=%d skipped_invalid=%d",
            cal_summary["rows_bridged"], cal_summary["tickers_touched"],
            cal_summary["watermark_after"], cal_summary["skipped_invalid"],
        )


def _run_mm_fill_match(conn) -> None:
    """Scan recent weather_mm_shadow rows and populate bid/ask fill flags."""
    try:
        summary = match_shadow_fills(conn)
    except Exception as exc:  # pragma: no cover
        logger.exception("[mm_fill] matcher failed: %s", exc)
        return
    if summary["bid_fills"] or summary["ask_fills"]:
        logger.info(
            "[mm_fill] checked=%d bid_fills=%d ask_fills=%d no_fill=%d",
            summary["checked"], summary["bid_fills"], summary["ask_fills"],
            summary["no_fill"],
        )
    else:
        logger.debug(
            "[mm_fill] checked=%d no_fill=%d",
            summary["checked"], summary["no_fill"],
        )


def _run_series_discovery(conn) -> None:
    """Daily routable-series discovery sweep.

    See `bot/daemon/series_discovery.py` for design. Cheap: paginates
    `/events?status=open` (~30 pages once) and writes a small
    `discovered_series` table. Exceptions propagate so a regression
    surfaces in `[scheduler] task series_discovery raised …` rather
    than silently dropping a daily heartbeat.
    """
    summary = run_series_discovery(conn)
    if summary["new_routable"]:
        logger.info(
            "[series_discovery] events=%d routable_seen=%d NEW=%d upserted=%d",
            summary["events_aggregated"], summary["routable_seen"],
            summary["new_routable"], summary["upserted"],
        )
    else:
        logger.debug(
            "[series_discovery] events=%d routable_seen=%d nothing new",
            summary["events_aggregated"], summary["routable_seen"],
        )


def _run_mm_promotion_sweep(conn) -> None:
    """Daily MM promotion/graduation/demotion sweep (Phase 1 step 10)."""
    equity = _latest_equity_dollars(conn)
    try:
        summary = run_mm_promotion_sweep(conn, equity_dollars=equity)
    except Exception as exc:  # pragma: no cover
        logger.exception("[mm_promotion] sweep failed: %s", exc)
        return
    if summary["promoted"] or summary["graduated"] or summary["demoted"]:
        logger.info(
            "[mm_promotion] checked=%d promoted=%d graduated=%d demoted=%d",
            summary["checked"], len(summary["promoted"]),
            len(summary["graduated"]), len(summary["demoted"]),
        )
        for entry in summary["promoted"] + summary["graduated"]:
            logger.info("[mm_promotion]   ↑ %s", entry)
        for entry in summary["demoted"]:
            logger.warning("[mm_promotion]   ↓ %s", entry)
    else:
        logger.info(
            "[mm_promotion] checked=%d unchanged=%d",
            summary["checked"], len(summary["unchanged"]),
        )


def _log_health(pollers: list[Poller], cycle_runner: CycleRunner,
                scheduler: Scheduler,
                weather_handler: Optional[WeatherChangeHandler] = None,
                weather_dispatcher: Optional[AsyncEventDispatcher] = None,
                time_decay_driver: Optional[TimeDecayDriver] = None,
                forecast_change_driver: Optional[ForecastChangeDriver] = None,
                ) -> None:
    """Periodic health summary. One line per subsystem."""
    for p in pollers:
        h = p.health()
        logger.info(
            "[health] poller=%s running=%s polls=%d errors=%d last_error=%s",
            h["name"], h["running"], h["poll_count"], h["error_count"],
            h["last_error"],
        )
    ch = cycle_runner.health()
    logger.info(
        "[health] cycle count=%d errors=%d last_success=%s last_duration=%.2fs",
        ch["cycle_count"], ch["error_count"], ch["last_cycle_success"],
        ch["last_cycle_duration_s"] or 0.0,
    )
    sh = scheduler.health()
    for name, stats in sh["tasks"].items():
        logger.info(
            "[health] task=%s runs=%d errors=%d last_duration=%.3fs",
            name, stats["run_count"], stats["error_count"],
            stats["last_run_duration_s"],
        )
    if weather_handler is not None:
        s = weather_handler.stats
        logger.info(
            "[health] wx_handler mode=%s seen=%d throttled=%d dispatched=%d "
            "shadowed=%d quoted=%d skipped=%d synth=%d synth_reject=%d "
            "errors=%d",
            "LIVE" if weather_handler.live else "SHADOW",
            s["changes_seen"], s["changes_throttled"], s["requotes_dispatched"],
            s["markets_shadowed"], s["markets_quoted"], s["markets_skipped"],
            s.get("synthetic_enqueued", 0),
            s.get("synthetic_rejected_no_state", 0)
                + s.get("synthetic_rejected_cooldown", 0),
            s["errors"],
        )
    if time_decay_driver is not None:
        td = time_decay_driver.stats
        logger.info(
            "[health] wx_time_decay ticks=%d fired=%d no_state=%d day_closed=%d "
            "cadence=%d rejected=%d",
            td["ticks"], td["fired"], td["skipped_no_state"],
            td["skipped_day_closed"], td["skipped_cadence"],
            td["skipped_enqueue_rejected"],
        )
    if forecast_change_driver is not None:
        fc = forecast_change_driver.stats
        logger.info(
            "[health] wx_forecast_change refreshes=%d fires=%d below_thresh=%d "
            "missing_prior=%d not_tradable=%d",
            fc["refreshes_seen"], fc["fires"], fc["skipped_below_threshold"],
            fc["skipped_missing_prior"], fc["skipped_not_tradable"],
        )
    if weather_dispatcher is not None:
        dh = weather_dispatcher.health()
        logger.info(
            "[health] wx_dispatch workers=%d dispatched=%d coalesced=%d",
            dh["worker_count"], dh["dispatched"], dh["coalesced"],
        )
        for w in dh["workers"]:
            if w["errors"] > 0 or not w["alive"]:
                logger.warning(
                    "[health] wx_dispatch worker=%s alive=%s processed=%d "
                    "coalesced=%d errors=%d last_error=%s",
                    w["key"], w["alive"], w["processed"], w["coalesced"],
                    w["errors"], w["last_error"],
                )


def main(argv: Optional[list[str]] = None) -> int:
    """Run the daemon. Blocks until SIGTERM/SIGINT.

    Returns an exit code (0 on clean shutdown).
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("Kalshi daemon starting (pid=%d)", os.getpid())
    logger.info("=" * 60)

    # ── Database ───────────────────────────────────────────────────────
    conn = init_db()
    logger.info("[daemon] DB initialized (WAL mode, shared connection)")

    # ── Weather MM event-driven path ───────────────────────────────────
    # Forecast cache + quoter + handler are created before the poller so
    # we can pass the handler in as METARPoller(on_result=...).
    forecast_cache = ForecastCache()
    weather_quoter = WeatherQuoter(conn)
    # Per-series dispatcher pushes requote work off the METAR poller
    # thread — a slow Kalshi round-trip for one city can't delay
    # observations for any other. Same-series work still serializes
    # (invariant required by the quoter's cancel-replace logic), and
    # stale pending events coalesce down to latest-state.
    weather_dispatcher = AsyncEventDispatcher(name="wx")

    # Poller is constructed first so we can wire it into the handler
    # (for enqueue_synthetic's current-state lookups) and into the
    # TimeDecayDriver. The handler needs on_fire wired to the driver,
    # which in turn needs the handler — resolved by late-binding the
    # attributes after construction.
    metar_poller = METARPoller(on_result=None)  # on_result set below
    metar_poller.interval_s = METAR_POLL_INTERVAL_S
    seeded = metar_poller.seed_running_high(conn)
    if seeded:
        logger.info(
            "[daemon] seeded running_high for %d station(s) from kv_cache: %s",
            len(seeded),
            ", ".join(f"{s}={v:.1f}°F" for s, v in seeded.items()),
        )

    weather_handler = WeatherChangeHandler(
        quoter=weather_quoter,
        forecast_cache=forecast_cache,
        dispatcher=weather_dispatcher,
        poller=metar_poller,
    )
    metar_poller._on_result = weather_handler  # now the handler exists
    logger.info(
        "[daemon] weather handler: mode=%s",
        "LIVE" if weather_handler.live else "SHADOW",
    )

    # ── T1.2 requote drivers ───────────────────────────────────────────
    # TimeDecayDriver fires synthetic requotes as sigma shrinks late in
    # the day; ForecastChangeDriver fires on material Open-Meteo refreshes.
    # Both reuse the same cooldown + dispatcher path via enqueue_synthetic.
    time_decay_driver = TimeDecayDriver(
        handler=weather_handler,
        poller=metar_poller,
    )
    forecast_change_driver = ForecastChangeDriver(handler=weather_handler)
    # Handler pokes the time-decay driver on every successful requote so
    # the cadence clock stays in sync with "any requote", not just
    # time-decay-driven ones — otherwise a METAR event at T + 0 would be
    # followed by a redundant time-decay fire at T + 45 s.
    weather_handler.on_fire = time_decay_driver.note_external_fire

    pollers: list[Poller] = [metar_poller]

    # Forecast refresh wrapper: captures a before/after snapshot and feeds
    # the delta to ForecastChangeDriver. Returns number of synthetic
    # requotes fired so scheduler stats stay informative.
    def _refresh_forecast_and_dispatch() -> None:
        pre = forecast_cache.snapshot()
        forecast_cache.refresh()
        post = forecast_cache.snapshot()
        try:
            fired = forecast_change_driver.on_refresh(pre, post)
        except Exception as exc:  # pragma: no cover
            logger.exception("[forecast_change] driver failed: %s", exc)
            return
        if fired:
            logger.info("[forecast_change] dispatched %d requote(s)", fired)

    # ── Cycle runner ───────────────────────────────────────────────────
    cycle_runner = CycleRunner(conn)

    # ── T3.1 fills-ledger writer ───────────────────────────────────────
    # Single owner of every ledger row write. Scheduler-driven sync each
    # minute pulls the latest /portfolio/fills pages and populates the
    # fills_ledger table forward from max(fill_ts_unix).
    fills_writer = FillsWriter(conn)
    daemon_start_unix = time.time()

    # ── Scheduler ──────────────────────────────────────────────────────
    scheduler = Scheduler()

    scheduler.register("cycle", cycle_runner.run_once, interval_s=CYCLE_INTERVAL_S,
                       initial_delay_s=5.0)
    scheduler.register("kv_cleanup", lambda: kv_cleanup(conn),
                       interval_s=KV_CLEANUP_INTERVAL_S,
                       initial_delay_s=60.0)
    scheduler.register(
        "forecast_refresh",
        _refresh_forecast_and_dispatch,
        interval_s=FORECAST_REFRESH_TASK_INTERVAL_S,
        initial_delay_s=2.0,  # prime cache early so first METAR events have data
    )
    scheduler.register(
        "wx_time_decay",
        time_decay_driver,
        interval_s=TIME_DECAY_DRIVER_INTERVAL_S,
        # Delay first tick: poller needs a read or two to fill last_temp_f,
        # and the forecast refresh should land first so the driver's
        # synthetic events have fresh context.
        initial_delay_s=90.0,
    )
    scheduler.register(
        "promotion_sweep",
        lambda: _run_promotion_sweep(conn),
        interval_s=PROMOTION_SWEEP_INTERVAL_S,
        initial_delay_s=600.0,  # give the cycle a chance to write a sessions row first
    )
    scheduler.register(
        "fills_sync",
        lambda: _run_fills_sync(fills_writer, conn, daemon_start_unix),
        interval_s=FILLS_SYNC_INTERVAL_S,
        # Start after cycle has had a chance to place its first orders.
        # First pass finds an empty ledger → uses daemon_start_unix as
        # since, minus overlap, which is fine — no risk of back-fetching
        # pre-T3.1 rows because those fills predate the start_unix clock.
        initial_delay_s=30.0,
    )
    scheduler.register(
        "fills_validator",
        lambda: _run_fills_validator(conn),
        interval_s=FILLS_VALIDATOR_INTERVAL_S,
        # Run 20 minutes after boot so the first fills_sync has had
        # multiple opportunities to pull pages. First run is a baseline
        # report; subsequent daily runs are the gating signal.
        initial_delay_s=1200.0,
    )
    scheduler.register(
        "mm_fill_match",
        lambda: _run_mm_fill_match(conn),
        interval_s=MM_FILL_MATCH_INTERVAL_S,
        initial_delay_s=120.0,  # first pass after a full quote-lifetime has elapsed
    )
    scheduler.register(
        "settlement_backfill",
        lambda: _run_settlement_backfill(conn),
        interval_s=SETTLEMENT_BACKFILL_INTERVAL_S,
        # 180s gives the first cycle time to populate alpha_backtest so
        # the series-discovery query has something to iterate. First tick
        # is typically a no-op; by tick 2 (+600s) real back-fill work
        # starts landing.
        initial_delay_s=180.0,
    )
    scheduler.register(
        "mos_materializer",
        lambda: _run_mos_materializer(conn),
        interval_s=MOS_MATERIALIZE_INTERVAL_S,
        # Run the fitter on startup with a small safety delay. The 30-min
        # delay this used to have was a defensive "wait for schema
        # migrations to settle" that didn't apply once init_db became
        # synchronous. The cost: post-restart, METAR's per-(station, hour)
        # residual σ kv was missing for ~30 min, so METAR's σ defaulted to
        # the wide hardcoded schedule and METAR contributed near-zero
        # weight to the combine. On hot days that meant we used cold-start
        # forecast values for the whole morning. Discovered 2026-04-28.
        initial_delay_s=60.0,
    )
    scheduler.register(
        "mm_promotion_sweep",
        lambda: _run_mm_promotion_sweep(conn),
        interval_s=MM_PROMOTION_SWEEP_INTERVAL_S,
        initial_delay_s=900.0,  # run after directional promotion so logs group
    )
    scheduler.register(
        "series_discovery",
        lambda: _run_series_discovery(conn),
        interval_s=SERIES_DISCOVERY_INTERVAL_S,
        # First run a few minutes after boot so any newly-launched series
        # gets surfaced quickly on a fresh deploy. Subsequent runs follow
        # the daily cadence.
        initial_delay_s=300.0,
    )
    scheduler.register(
        "shadow_integrity",
        lambda: run_shadow_integrity_check(
            conn, window_s=SHADOW_INTEGRITY_WINDOW_S,
        ),
        interval_s=SHADOW_INTEGRITY_INTERVAL_S,
        # Wait 15 min after boot so the window has a non-trivial sample
        # from the current process — alerts on pre-restart data would be
        # noisy and hard to action.
        initial_delay_s=900.0,
    )
    scheduler.register(
        "health_log",
        lambda: _log_health(
            pollers, cycle_runner, scheduler,
            weather_handler, weather_dispatcher,
            time_decay_driver, forecast_change_driver,
        ),
        interval_s=HEALTH_LOG_INTERVAL_S,
        initial_delay_s=30.0,
    )

    # Start pollers on scheduler start, stop them on scheduler stop.
    def start_pollers():
        for p in pollers:
            p.start()
    def stop_pollers():
        for p in pollers:
            p.stop(timeout=5.0)
        # Drain per-city requote workers after pollers stop so no new
        # events arrive while we're shutting down.
        try:
            weather_dispatcher.shutdown(timeout=5.0)
        except Exception as e:
            logger.warning("[daemon] error stopping dispatcher: %s", e)
        try:
            conn.close()
        except Exception as e:
            logger.warning("[daemon] error closing DB: %s", e)
    scheduler.on_start(start_pollers)
    scheduler.on_stop(stop_pollers)

    # ── Run ────────────────────────────────────────────────────────────
    start_ts = time.time()
    try:
        scheduler.run_forever()
    except Exception as exc:
        logger.exception("[daemon] scheduler crashed: %s", exc)
        # Try to close connection and stop pollers even in crash path
        try:
            for p in pollers:
                p.stop(timeout=2.0)
            weather_dispatcher.shutdown(timeout=2.0)
            conn.close()
        except Exception:
            pass
        return 1

    uptime = time.time() - start_ts
    logger.info("[daemon] shutdown complete (uptime=%.1fs)", uptime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
