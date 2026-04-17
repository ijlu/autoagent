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

from bot.daemon.cycle_runner import CycleRunner
from bot.daemon.forecast_cache import ForecastCache, FORECAST_REFRESH_INTERVAL_S
from bot.daemon.metar_poller import METARPoller
from bot.daemon.poller_base import Poller
from bot.daemon.scheduler import Scheduler
from bot.daemon.weather_handler import WeatherChangeHandler
from bot.daemon.weather_quoter import WeatherQuoter
from bot.db import init_db, kv_cleanup
from bot.learning.shadow_promotion import run_promotion_sweep

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


def _log_health(pollers: list[Poller], cycle_runner: CycleRunner,
                scheduler: Scheduler,
                weather_handler: Optional[WeatherChangeHandler] = None) -> None:
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
            "shadowed=%d quoted=%d skipped=%d errors=%d",
            "LIVE" if weather_handler.live else "SHADOW",
            s["changes_seen"], s["changes_throttled"], s["requotes_dispatched"],
            s["markets_shadowed"], s["markets_quoted"], s["markets_skipped"],
            s["errors"],
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
    weather_handler = WeatherChangeHandler(
        quoter=weather_quoter,
        forecast_cache=forecast_cache,
    )
    logger.info(
        "[daemon] weather handler: mode=%s",
        "LIVE" if weather_handler.live else "SHADOW",
    )

    # ── Pollers ────────────────────────────────────────────────────────
    # Phase 1: METAR → WeatherChangeHandler (shadow by default).
    # Phase 2 adds NWS, NBM, HRRR, MADIS, AFD. Phase 3 adds econ pollers.
    metar_poller = METARPoller(on_result=weather_handler)
    metar_poller.interval_s = METAR_POLL_INTERVAL_S
    pollers: list[Poller] = [metar_poller]

    # ── Cycle runner ───────────────────────────────────────────────────
    cycle_runner = CycleRunner(conn)

    # ── Scheduler ──────────────────────────────────────────────────────
    scheduler = Scheduler()

    scheduler.register("cycle", cycle_runner.run_once, interval_s=CYCLE_INTERVAL_S,
                       initial_delay_s=5.0)
    scheduler.register("kv_cleanup", lambda: kv_cleanup(conn),
                       interval_s=KV_CLEANUP_INTERVAL_S,
                       initial_delay_s=60.0)
    scheduler.register(
        "forecast_refresh",
        forecast_cache.refresh,
        interval_s=FORECAST_REFRESH_TASK_INTERVAL_S,
        initial_delay_s=2.0,  # prime cache early so first METAR events have data
    )
    scheduler.register(
        "promotion_sweep",
        lambda: _run_promotion_sweep(conn),
        interval_s=PROMOTION_SWEEP_INTERVAL_S,
        initial_delay_s=600.0,  # give the cycle a chance to write a sessions row first
    )
    scheduler.register(
        "health_log",
        lambda: _log_health(pollers, cycle_runner, scheduler, weather_handler),
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
            conn.close()
        except Exception:
            pass
        return 1

    uptime = time.time() - start_ts
    logger.info("[daemon] shutdown complete (uptime=%.1fs)", uptime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
