"""T3.1 dual-run validator — fills_ledger vs mm_processed_fills.

Goal: catch drift between the canonical ledger and the legacy fills
table for N consecutive days before T3.3 migrates readers. Seven clean
reports in a row is the go/no-go gate for the reader migration.

Comparison strategy — per ``(ticker, side)`` aggregates over the last
``n_days``:

  * ``contracts``      — sum of contracts filled
  * ``cents_transacted`` — sum(contracts * price_cents), where the
    ledger uses the side-appropriate price column and mm_processed_fills
    stores a single price_cents column
  * ``fees``           — sum(fee_cents)

Key semantics:

  * ``ledger_rows == 0 and reference_rows == 0`` — inert. Window empty;
    no signal, no alert.
  * ``ledger_rows > 0 and reference_rows == 0`` — informational. This
    is the steady-state once mm_processed_fills is fully retired (its
    legacy writer was removed during the daemon refactor; the table
    survives only to hold historical rows). The validator reports this
    as ``is_meaningful=False`` so alerting skips it.
  * both sides populated — meaningful. Divergence on any aggregate is a
    finding that must be resolved before T3.3 reader migration.

The validator is intentionally pure — it computes a ``ValidationReport``
and returns it. Alert dispatch is the scheduler wrapper's concern
(``bot.daemon.main``), not the validator's.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "TickerSideStats",
    "Divergence",
    "ValidationReport",
    "compare_last_n_days",
    "format_report",
]

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class TickerSideStats:
    """Aggregate stats for a (ticker, side) bucket in a single source."""

    contracts: int = 0
    cents_transacted: int = 0
    fees: int = 0

    def matches(self, other: "TickerSideStats") -> bool:
        return (
            self.contracts == other.contracts
            and self.cents_transacted == other.cents_transacted
            and self.fees == other.fees
        )


@dataclass(frozen=True)
class Divergence:
    """One (ticker, side) bucket that differs between ledger and reference."""

    ticker: str
    side: str
    ledger: TickerSideStats
    reference: TickerSideStats

    @property
    def contracts_delta(self) -> int:
        return self.ledger.contracts - self.reference.contracts

    @property
    def cents_delta(self) -> int:
        return self.ledger.cents_transacted - self.reference.cents_transacted

    @property
    def fees_delta(self) -> int:
        return self.ledger.fees - self.reference.fees


@dataclass
class ValidationReport:
    """Structured divergence report for one comparison run."""

    n_days: int
    since_unix: float
    reference_name: str  # 'mm_processed_fills' today; may grow
    ledger_contracts: int
    reference_contracts: int
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.divergences

    @property
    def is_meaningful(self) -> bool:
        """True when both sides have data, so comparison is active.

        When one side is empty (typically the reference — its legacy
        writer is already gone), the report is informational rather
        than a gating signal. The T3.3 migration gate requires seven
        consecutive *meaningful* + *clean* reports, not seven empties.
        """
        return self.ledger_contracts > 0 and self.reference_contracts > 0


# ---------------------------------------------------------------------------
# Aggregators (one per source — trivial to add a third)
# ---------------------------------------------------------------------------

def _aggregate_ledger(
    conn: sqlite3.Connection, since_unix: float,
) -> dict[tuple[str, str], TickerSideStats]:
    rows = conn.execute(
        "SELECT ticker, side, "
        "       SUM(contracts), "
        "       SUM(contracts * (CASE WHEN side='yes' "
        "                              THEN yes_price_cents "
        "                              ELSE no_price_cents END)), "
        "       SUM(fee_cents) "
        "FROM fills_ledger "
        "WHERE fill_ts_unix >= ? "
        "GROUP BY ticker, side",
        (since_unix,),
    ).fetchall()
    return {
        (r[0], r[1]): TickerSideStats(
            contracts=int(r[2] or 0),
            cents_transacted=int(r[3] or 0),
            fees=int(r[4] or 0),
        )
        for r in rows
    }


def _aggregate_mm_processed_fills(
    conn: sqlite3.Connection, since_iso: str,
) -> dict[tuple[str, str], TickerSideStats]:
    rows = conn.execute(
        "SELECT ticker, side, "
        "       SUM(contracts), "
        "       SUM(contracts * price_cents), "
        "       SUM(fee_cents) "
        "FROM mm_processed_fills "
        "WHERE recorded_at >= ? "
        "GROUP BY ticker, side",
        (since_iso,),
    ).fetchall()
    return {
        (r[0], r[1]): TickerSideStats(
            contracts=int(r[2] or 0),
            cents_transacted=int(r[3] or 0),
            fees=int(r[4] or 0),
        )
        for r in rows
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compare_last_n_days(
    conn: sqlite3.Connection,
    *,
    n_days: int = 7,
    now_unix: Optional[float] = None,
) -> ValidationReport:
    """Compute divergence between ``fills_ledger`` and
    ``mm_processed_fills`` over the last ``n_days``.

    Args:
        conn: DB connection. Must have both tables (``init_db`` creates
            both).
        n_days: Window size (default 7 matches the T3.3 gating cadence).
        now_unix: Override current time (tests). Defaults to time.time().

    Returns:
        ValidationReport with:
          - counts of contracts seen in each source
          - divergence list (empty = clean)
          - ``is_meaningful`` flag — False when either source is empty,
            signalling the scheduler wrapper to skip the alert path.

    Reads only; never writes. No DB lock required.
    """
    if now_unix is None:
        now_unix = time.time()
    since_unix = now_unix - n_days * SECONDS_PER_DAY
    # recorded_at in mm_processed_fills is stored as ISO8601 text. SQLite
    # compares TEXT lexicographically — ISO8601 with fixed digits +
    # leading zeros is lexicographic = chronological, so this works.
    since_iso = datetime.fromtimestamp(
        since_unix, tz=timezone.utc,
    ).isoformat()

    ledger = _aggregate_ledger(conn, since_unix)
    reference = _aggregate_mm_processed_fills(conn, since_iso)

    divergences: list[Divergence] = []
    for key in sorted(set(ledger) | set(reference)):
        L = ledger.get(key, TickerSideStats())
        R = reference.get(key, TickerSideStats())
        if not L.matches(R):
            divergences.append(Divergence(
                ticker=key[0], side=key[1], ledger=L, reference=R,
            ))

    return ValidationReport(
        n_days=n_days,
        since_unix=since_unix,
        reference_name="mm_processed_fills",
        ledger_contracts=sum(v.contracts for v in ledger.values()),
        reference_contracts=sum(v.contracts for v in reference.values()),
        divergences=divergences,
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_report(report: ValidationReport, *, max_lines: int = 20) -> str:
    """Human-readable formatter. Used by the scheduler wrapper for log
    lines and by ``alert_on_divergence`` for Telegram payloads.

    Truncates divergence list at ``max_lines`` to keep alert bodies
    bounded — the first divergences carry enough signal; if we hit the
    cap we say so explicitly so the reader knows to consult the DB.
    """
    header = (
        f"[fills_validator] ledger vs {report.reference_name}, "
        f"last {report.n_days}d "
        f"(ledger={report.ledger_contracts} contracts, "
        f"{report.reference_name}={report.reference_contracts} contracts)"
    )
    if not report.is_meaningful:
        return header + " — INFORMATIONAL: one side empty, no comparison"
    if report.is_clean:
        return header + " — CLEAN: zero divergence"

    lines = [header, f"  {len(report.divergences)} diverging (ticker, side) bucket(s):"]
    for d in report.divergences[:max_lines]:
        lines.append(
            f"  - {d.ticker:30s} side={d.side:3s} "
            f"Δcontracts={d.contracts_delta:+d} "
            f"Δcents={d.cents_delta:+d} "
            f"Δfees={d.fees_delta:+d}"
        )
    if len(report.divergences) > max_lines:
        lines.append(
            f"  ... {len(report.divergences) - max_lines} more — "
            f"query fills_ledger + mm_processed_fills for full list"
        )
    return "\n".join(lines)
