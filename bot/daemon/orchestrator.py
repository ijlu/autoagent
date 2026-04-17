"""Weather daemon orchestrator — persistent process for sub-minute requoting.

Replaces the 2-minute oneshot cycle for weather markets with a persistent
daemon that polls METAR every 30 seconds and requotes within seconds of
temperature changes.

Architecture:
  - METARPoller runs every 30s, detects temperature changes
  - On change: WeatherQuoter requotes all open markets for affected city
  - Smart gates filter which markets to quote and adjust spreads
  - Forecast cache updated every 15 minutes from Open-Meteo
  - All state is in-memory (daemon's speed advantage over oneshot)

The daemon runs alongside the existing 2-minute oneshot cycle:
  - Daemon handles: weather markets (KXHIGH*, KXHMONTHRANGE, KXHURR)
  - Oneshot handles: everything else (KXFED, KXGDP, KXCPI, etc.)

Usage:
    python -m bot.daemon.orchestrator [--dry-run] [--poll-interval 30]
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bot.daemon.metar_poller import METARPoller, TemperatureChange
from bot.daemon.stations import STATIONS, SERIES_TO_STATION
from bot.daemon.smart_gates import evaluate_all_gates, compute_smart_spread
from bot.daemon.weather_quoter import WeatherQuoter

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_POLL_INTERVAL = 30  # seconds between METAR polls
FORECAST_REFRESH_INTERVAL = 900  # 15 minutes — refresh forecast cache
HEALTH_LOG_INTERVAL = 300  # 5 minutes — log health status
MAX_CONSECUTIVE_ERRORS = 20  # shutdown if too many errors in a row
REQUOTE_COOLDOWN = 10  # minimum seconds between requotes for same city

# Logging
LOG_FORMAT = "%(asctime)s [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("wx-daemon")


# ══════════════════════════════════════════════════════════════════════════════
# Forecast cache — refreshed periodically from Open-Meteo
# ══════════════════════════════════════════════════════════════════════════════

class ForecastCache:
    """Periodically refreshes and caches daily forecast highs for all stations.

    Uses Open-Meteo (free, no auth) to get today's forecast high temperature
    for each city. This feeds into the probability model alongside METAR obs.
    """

    def __init__(self):
        self._forecasts: dict[str, float] = {}  # station -> forecast high °F
        self._last_refresh: float = 0

    def get(self, station: str) -> float | None:
        """Get forecast high for a station. Returns None if no forecast."""
        return self._forecasts.get(station)

    def refresh_if_stale(self) -> None:
        """Refresh forecasts if older than FORECAST_REFRESH_INTERVAL."""
        if time.time() - self._last_refresh < FORECAST_REFRESH_INTERVAL:
            return
        self.refresh()

    def refresh(self) -> None:
        """Fetch fresh forecasts for all stations from Open-Meteo."""
        import requests

        refreshed = 0
        for station_id, cfg in STATIONS.items():
            lat, lon = cfg["lat"], cfg["lon"]
            url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={lat}&longitude={lon}"
                f"&daily=temperature_2m_max"
                f"&temperature_unit=fahrenheit"
                f"&timezone=auto"
                f"&forecast_days=1"
            )
            try:
                r = requests.get(url, timeout=8)
                if r.status_code == 200:
                    data = r.json()
                    temps = data.get("daily", {}).get("temperature_2m_max", [])
                    if temps:
                        self._forecasts[station_id] = float(temps[0])
                        refreshed += 1
                else:
                    logger.warning(
                        "Open-Meteo HTTP %d for %s", r.status_code, station_id
                    )
            except Exception as exc:
                logger.warning("Open-Meteo error for %s: %s", station_id, exc)

        self._last_refresh = time.time()
        logger.info("Forecast refresh: %d/%d stations updated", refreshed, len(STATIONS))


# ══════════════════════════════════════════════════════════════════════════════
# Main daemon
# ══════════════════════════════════════════════════════════════════════════════

class WeatherDaemon:
    """Persistent weather market making daemon.

    Polls METAR every `poll_interval` seconds. On temperature change,
    requotes all affected weather markets through smart gates.
    """

    def __init__(
        self,
        db_path: str | None = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        dry_run: bool = False,
    ):
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self._running = False
        self._consecutive_errors = 0

        # Components
        self.poller = METARPoller()
        self.forecasts = ForecastCache()
        self.quoter: WeatherQuoter | None = None  # initialized in run()

        # DB path
        self._db_path = db_path or os.environ.get(
            "KALSHI_DB_PATH",
            str(Path.home() / "autoagent" / "kalshi_trades.db"),
        )

        # Requote cooldown tracking — prevents hammering the same city
        self._last_requote: dict[str, float] = {}

        # Stats
        self.stats = {
            "polls": 0,
            "changes_detected": 0,
            "requotes_triggered": 0,
            "markets_quoted": 0,
            "markets_skipped": 0,
            "errors": 0,
            "started_at": None,
        }

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _handle_signal(self, signum, frame):
        """Graceful shutdown on SIGTERM/SIGINT."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down gracefully", sig_name)
        self._running = False

    # ------------------------------------------------------------------
    # Smart gate wrapper
    # ------------------------------------------------------------------

    def _smart_gate_fn(
        self,
        station: str,
        bracket_floor: float | None,
        bracket_cap: float | None,
        running_high: float,
        forecast_high: float,
        hours_left: float,
        trajectory: float,
    ) -> tuple[bool, str, float]:
        """Smart gate wrapper that passes through to evaluate_all_gates.

        Handles None bracket values for threshold markets by synthesizing
        bracket bounds from the threshold.
        """
        # For threshold markets, smart gates still need bracket-like bounds
        b_floor = bracket_floor if bracket_floor is not None else 0.0
        b_cap = bracket_cap if bracket_cap is not None else 200.0

        return evaluate_all_gates(
            station=station,
            bracket_floor=b_floor,
            bracket_cap=b_cap,
            running_high=running_high,
            forecast_high=forecast_high,
            hours_left=hours_left,
            trajectory_f_per_hr=trajectory,
        )

    # ------------------------------------------------------------------
    # Handle a set of METAR changes
    # ------------------------------------------------------------------

    def _handle_changes(self, changes: list[TemperatureChange]) -> None:
        """Process temperature changes — requote affected markets."""
        if self.quoter is None:
            return

        for change in changes:
            station = change.station
            series = change.series
            city = change.city

            # Cooldown: don't requote the same city too frequently
            now = time.time()
            last = self._last_requote.get(series, 0)
            if now - last < REQUOTE_COOLDOWN:
                logger.debug(
                    "Skipping %s requote — cooldown (%.0fs since last)",
                    series,
                    now - last,
                )
                continue

            # Get forecast high
            forecast_high = self.forecasts.get(station)
            if forecast_high is None:
                # Fallback: running high + 5°F
                forecast_high = change.running_high_f + 5.0
                logger.warning(
                    "No forecast for %s, using fallback %.0f°F",
                    station,
                    forecast_high,
                )

            logger.info(
                "🌡️  %s (%s): %.0f°F → %.0f°F  high=%.0f°F  forecast=%.0f°F  "
                "trajectory=%+.1f°F/hr  hrs_left=%.1f",
                series,
                station,
                change.old_temp_f or 0,
                change.new_temp_f,
                change.running_high_f,
                forecast_high,
                change.trajectory_f_per_hr,
                change.hours_left,
            )

            # Requote all markets for this city
            t0 = time.time()
            try:
                results = self.quoter.requote_city(
                    series=series,
                    station=station,
                    running_high_f=change.running_high_f,
                    forecast_high_f=forecast_high,
                    hours_left=change.hours_left,
                    trajectory_f_per_hr=change.trajectory_f_per_hr,
                    smart_gates=self._smart_gate_fn,
                )

                quoted = sum(1 for r in results if not r.skipped)
                skipped = sum(1 for r in results if r.skipped)
                total_orders = sum(r.orders_posted for r in results)
                elapsed_ms = (time.time() - t0) * 1000

                self.stats["requotes_triggered"] += 1
                self.stats["markets_quoted"] += quoted
                self.stats["markets_skipped"] += skipped
                self._last_requote[series] = time.time()
                self._consecutive_errors = 0

                logger.info(
                    "  → %s: %d quoted, %d skipped, %d orders in %.0fms",
                    series,
                    quoted,
                    skipped,
                    total_orders,
                    elapsed_ms,
                )

                # Log skip reasons for debugging
                for r in results:
                    if r.skipped and r.skip_reason:
                        logger.debug("    SKIP %s: %s", r.ticker, r.skip_reason)

            except Exception as exc:
                self.stats["errors"] += 1
                self._consecutive_errors += 1
                logger.error("Requote error for %s: %s", series, exc, exc_info=True)

    # ------------------------------------------------------------------
    # Health logging
    # ------------------------------------------------------------------

    def _log_health(self) -> None:
        """Periodic health status log."""
        uptime = time.time() - (self.stats["started_at"] or time.time())
        states = self.poller.get_all_states()

        station_summary = []
        for sid, state in sorted(states.items()):
            if state.last_temp_f is not None:
                station_summary.append(
                    f"{sid}={state.last_temp_f:.0f}°F(high={state.running_high_f:.0f})"
                )
            else:
                station_summary.append(f"{sid}=?")

        logger.info(
            "Health: uptime=%.0fs polls=%d changes=%d requotes=%d "
            "quoted=%d skipped=%d errors=%d | %s",
            uptime,
            self.stats["polls"],
            self.stats["changes_detected"],
            self.stats["requotes_triggered"],
            self.stats["markets_quoted"],
            self.stats["markets_skipped"],
            self.stats["errors"],
            " ".join(station_summary),
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main daemon loop. Runs until SIGTERM/SIGINT or fatal error."""

        # Set up dry run override
        if self.dry_run:
            os.environ["MM_DRY_RUN"] = "true"
            logger.info("DRY RUN mode — no real orders will be placed")

        # Initialize DB connection
        try:
            conn = sqlite3.connect(self._db_path)
            logger.info("Connected to DB: %s", self._db_path)
        except Exception as exc:
            logger.error("Cannot connect to DB: %s", exc)
            sys.exit(1)

        self.quoter = WeatherQuoter(conn)

        # Signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self._running = True
        self.stats["started_at"] = time.time()
        last_health_log = 0

        logger.info(
            "Weather daemon starting — poll_interval=%ds, stations=%d, dry_run=%s",
            self.poll_interval,
            len(STATIONS),
            self.dry_run,
        )

        # Initial forecast fetch
        logger.info("Fetching initial forecasts...")
        self.forecasts.refresh()

        # Initial METAR poll to seed state
        logger.info("Seeding METAR state with initial poll...")
        initial_changes = self.poller.poll()
        if initial_changes:
            logger.info("Initial poll: %d stations reporting", len(initial_changes))
        else:
            logger.warning("Initial poll returned no data — stations may be offline")

        logger.info("Daemon ready. Entering main loop.")

        while self._running:
            loop_start = time.time()

            try:
                # Refresh forecasts if stale
                self.forecasts.refresh_if_stale()

                # Poll METAR
                changes = self.poller.poll()
                self.stats["polls"] += 1

                if changes:
                    self.stats["changes_detected"] += len(changes)
                    self._handle_changes(changes)

                # Periodic health log
                if time.time() - last_health_log > HEALTH_LOG_INTERVAL:
                    self._log_health()
                    last_health_log = time.time()

                # Check for too many consecutive errors
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error(
                        "Too many consecutive errors (%d) — shutting down",
                        self._consecutive_errors,
                    )
                    self._running = False
                    break

                self._consecutive_errors = 0

            except Exception as exc:
                self.stats["errors"] += 1
                self._consecutive_errors += 1
                logger.error("Main loop error: %s", exc, exc_info=True)

            # Sleep until next poll (account for processing time)
            elapsed = time.time() - loop_start
            sleep_time = max(1.0, self.poll_interval - elapsed)
            time.sleep(sleep_time)

        # Graceful shutdown
        logger.info("Daemon stopping. Final stats:")
        self._log_health()
        try:
            conn.close()
        except Exception:
            pass
        logger.info("Goodbye.")


# ══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Weather market making daemon with sub-minute METAR updates"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Don't place real orders (log what would happen)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between METAR polls (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite database (default: ~/autoagent/kalshi_trades.db)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )
    # Suppress noisy HTTP connection pool logging
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Load .env if available
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        logger.info("Loading environment from %s", env_path)
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    daemon = WeatherDaemon(
        db_path=args.db_path,
        poll_interval=args.poll_interval,
        dry_run=args.dry_run,
    )
    daemon.run()


if __name__ == "__main__":
    main()
