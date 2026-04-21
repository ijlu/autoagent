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
from bot.daemon.shadow_integrity import run_shadow_integrity_check
from bot.daemon.weather_handler import WeatherChangeHandler
from bot.daemon.weather_quoter import WeatherQuoter
from bot.db import init_db, kv_cleanup
from bot.learning.fills_validator import compare_last_n_days, format_report
from bot.learning.mm_promotion import (
    match_shadow_fills,
    run_mm_promotion_sweep,
)
from bot.learning.shadow_promotion import run_promotion_sweep
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

    Never raises — the live trading loop must not crash on a fills
    fetch error. ``sync_since`` already returns 0 on API failure; this
    wrapper's try/except is belt-and-braces for unexpected DB errors
    (e.g. connection abruptly closed mid-shutdown).
    """
    try:
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
    except Exception as exc:  # pragma: no cover — defensive only
        logger.exception("[fills_sync] unexpected failure: %s", exc)


def _run_fills_validator(conn) -> None:
    """Daily ledger-vs-legacy divergence report. Silent on empty /
    informational, warns on meaningful divergence.

    - No divergence (clean + meaningful): info log only.
    - Non-meaningful (one side empty): info log, no alert — this is the
      T3.1 steady state until mm_processed_fills re-gains writers or we
      add a second reference source.
    - Meaningful + divergent: WARNING log + Telegram alert. This is the
      signal the T3.3 reader-migration gate cares about.
    """
    try:
        report = compare_last_n_days(
            conn, n_days=FILLS_VALIDATOR_WINDOW_DAYS,
        )
    except Exception as exc:  # pragma: no cover — defensive only
        logger.exception("[fills_validator] run failed: %s", exc)
        return

    text = format_report(report)
    if report.is_meaningful and not report.is_clean:
        logger.warning(text)
        send_alert(text, level="warning")
    else:
        logger.info(text)


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
        "mm_promotion_sweep",
        lambda: _run_mm_promotion_sweep(conn),
        interval_s=MM_PROMOTION_SWEEP_INTERVAL_S,
        initial_delay_s=900.0,  # run after directional promotion so logs group
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
