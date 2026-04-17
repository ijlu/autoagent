"""In-process cycle runner for the persistent daemon.

The oneshot era spawned a fresh Python process every 2 minutes via a
systemd timer. Each invocation called `trade.main()` which opened a
DB connection, did everything, and closed the connection.

The daemon calls this module's `run_cycle()` instead of forking. It
reuses the long-lived DB connection (set up once at daemon start),
catches cycle-level exceptions so one bad cycle doesn't take down the
daemon, and returns a lightweight report suitable for logging.

Design notes:
- This module is a thin wrapper around `trade.main()`. The bulk of the
  trading logic stays in trade.py — we're NOT extracting it. Keep the
  daemon/oneshot blast radius small for Phase 1.
- `run_cycle()` is the callable registered with the scheduler. Invoked
  once per minute from the scheduler thread. Any exception inside
  `trade.main()` is caught here and logged; the scheduler keeps ticking.
- Legacy MM phases are already deleted from `trade.main()`. Safe
  Compounder (Phase 4sc) is gated by the `SC_ENABLED` env var which
  defaults to off.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """Summary of one cycle for daemon logging."""
    started_at: float = 0.0
    duration_s: float = 0.0
    success: bool = False
    error: Optional[str] = None
    # Flattened counts from trade.main()'s result dict. All optional so
    # failure cases can return an empty report.
    markets_scanned: int = 0
    opportunities: int = 0
    orders_placed: int = 0
    positions_managed: int = 0
    settlements_recorded: int = 0
    halted: bool = False
    halt_reason: str = ""

    @classmethod
    def from_result(cls, result: dict, started_at: float, duration_s: float) -> "CycleReport":
        return cls(
            started_at=started_at,
            duration_s=duration_s,
            success=True,
            markets_scanned=result.get("markets_scanned", 0),
            opportunities=len(result.get("opportunities", [])),
            orders_placed=len(result.get("orders_placed", [])),
            positions_managed=result.get("positions_managed", 0),
            settlements_recorded=result.get("settlements_recorded", 0),
            halted=result.get("halted", False),
            halt_reason=result.get("halt_reason", ""),
        )


class CycleRunner:
    """Holds the daemon's persistent DB connection and invokes trade.main().

    Construct once at daemon startup (after init_db), register
    `run_once` as a scheduler task at 60s interval.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._last_report: Optional[CycleReport] = None
        self._cycle_count = 0
        self._error_count = 0

    def run_once(self) -> CycleReport:
        """Run one full cycle. Catches all exceptions — returns a report
        with success=False if trade.main() raises. Never raises itself.
        """
        # Import here, not at module top, so cycle_runner can be imported
        # by tests without pulling in the full trading stack.
        import trade as trade_mod

        started = time.time()
        try:
            result = trade_mod.main(
                conn=self._conn,
                close_conn=False,          # daemon owns the connection
                write_json_report=False,   # no /task/trades.json spam
            )
            duration = time.time() - started
            if not isinstance(result, dict):
                # trade.main() can return {"error": "..."} on portfolio failure
                result = result or {}
            report = CycleReport.from_result(result, started, duration)
            self._cycle_count += 1
            self._last_report = report
            logger.info(
                "[cycle %d] ok duration=%.2fs markets=%d opps=%d orders=%d "
                "positions=%d settlements=%d halted=%s",
                self._cycle_count, duration, report.markets_scanned,
                report.opportunities, report.orders_placed,
                report.positions_managed, report.settlements_recorded,
                report.halted,
            )
            return report
        except Exception as exc:
            self._error_count += 1
            duration = time.time() - started
            err = f"{type(exc).__name__}: {exc}"
            logger.exception("[cycle] failed after %.2fs: %s", duration, err)
            report = CycleReport(
                started_at=started, duration_s=duration,
                success=False, error=err,
            )
            self._last_report = report
            return report

    # ------------------------------------------------------------------
    # Health / stats
    # ------------------------------------------------------------------

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def health(self) -> dict:
        last = self._last_report
        return {
            "cycle_count": self._cycle_count,
            "error_count": self._error_count,
            "last_cycle_success": last.success if last else None,
            "last_cycle_duration_s": last.duration_s if last else None,
            "last_cycle_age_s": (
                time.time() - last.started_at if last and last.started_at else None
            ),
            "last_error": last.error if last else None,
        }
