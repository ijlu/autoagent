"""Per-family scenario caps for threshold ladder markets (e.g., KXFED).

KXFED threshold markets are additive in scenario space — all thresholds above
the terminal rate pay out YES. The bot was treating them as independent positions,
leading to massive correlated exposure (e.g., 141 contracts on KXFED-27APR alone).

This module computes worst-case P&L by terminal rate scenario across all positions
in a family+expiry group. If worst-case loss exceeds the cap, new entries are blocked.

Example: If terminal rate = 425bp, then:
  - KXFED-27APR-T4.375 (threshold 437.5) → YES wins (rate > threshold? NO, 425 < 437.5)
  - KXFED-27APR-T4.250 (threshold 425.0) → YES wins (rate >= threshold? YES)
  - KXFED-27APR-T4.125 (threshold 412.5) → YES wins
  etc.

For each scenario, sum P&L across all positions in that family+expiry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class FamilyExposure:
    """Worst-case scenario analysis for a family+expiry group."""
    family: str                    # e.g., "KXFED"
    expiry: str                    # e.g., "27APR"
    positions: list                # list of (ticker, net, avg_entry)
    worst_scenario: str            # e.g., "425bp"
    worst_pnl_cents: int           # worst-case P&L in cents (negative = loss)
    total_contracts: int           # total absolute contracts
    blocked: bool                  # whether new entries should be blocked


def _parse_threshold_bp(ticker: str) -> float | None:
    """Extract threshold in basis points from a KXFED ticker.

    Examples:
        KXFED-27APR-T4.375 → 437.5 bp
        KXFED-27APR-T4.250 → 425.0 bp
        KXFED-26JUL-T3.750 → 375.0 bp
    """
    match = re.search(r'-T(\d+\.?\d*)', ticker)
    if match:
        return float(match.group(1)) * 100  # convert rate to bp
    return None


def _parse_family_expiry(ticker: str) -> tuple[str, str] | None:
    """Extract (family, expiry) from a ticker.

    Example: KXFED-27APR-T4.375 → ("KXFED", "27APR")
    """
    parts = ticker.split("-")
    if len(parts) >= 2:
        return (parts[0], parts[1])
    return None


def _compute_scenario_range(
    positions: list[tuple[str, int, float]],
) -> list[float]:
    """Compute scenario sweep range covering all held thresholds plus margin.

    The old hardcoded range (300-600bp) missed positions at low thresholds
    like T0.50 (50bp) or T2.00 (200bp). This generates a dynamic range
    centered on the actual held strikes.

    Returns:
        Sorted list of scenario rates in basis points.
    """
    thresholds = []
    for ticker, net, _ in positions:
        t = _parse_threshold_bp(ticker)
        if t is not None:
            thresholds.append(t)

    if not thresholds:
        # Fallback: standard range if no parseable positions
        return list(range(300, 601, 25))

    # Cover from 100bp below min threshold to 100bp above max, in 25bp steps
    lo = max(0, min(thresholds) - 100)  # floor at 0bp
    hi = max(thresholds) + 100
    # Also include extreme floor/ceiling for tail risk
    lo = min(lo, 25)     # always test near-zero rates
    hi = max(hi, 600)    # always test up to 6%
    # Round to nearest 25bp increment
    lo = int(round(lo / 25.0)) * 25
    hi = int(round(hi / 25.0)) * 25
    return list(range(lo, hi + 1, 25))


def compute_scenario_pnl(
    positions: list[tuple[str, int, float]],
    terminal_rate_bp: float,
    total_fees_cents: int = 0,
) -> int:
    """Compute aggregate P&L in cents for a set of threshold positions given a terminal rate.

    Args:
        positions: list of (ticker, net_position, avg_entry_cents)
            net > 0 = long YES, net < 0 = short YES (long NO)
        terminal_rate_bp: hypothetical terminal rate in basis points (e.g., 425)
        total_fees_cents: total entry fees paid (subtracted from every scenario)

    Returns:
        Total P&L in cents (negative = loss)
    """
    total_pnl = 0
    for ticker, net, avg_entry in positions:
        threshold = _parse_threshold_bp(ticker)
        if threshold is None or net == 0:
            continue

        # Determine outcome: YES if terminal rate >= threshold, NO if below
        # (KXFED markets ask "Will fed funds rate be at or above X?")
        result = "yes" if terminal_rate_bp >= threshold else "no"

        # Settlement math (YES-equivalent convention):
        if result == "yes":
            pnl = net * (100.0 - avg_entry)
        else:
            pnl = -net * avg_entry

        total_pnl += round(pnl)

    return total_pnl - total_fees_cents


def check_family_caps(
    conn,
    total_equity_cents: int,
    max_family_loss_pct: float = 0.10,
) -> dict[str, FamilyExposure]:
    """Compute worst-case scenario analysis for all threshold families.

    Args:
        conn: SQLite connection
        total_equity_cents: Total equity (balance + portfolio) in cents
        max_family_loss_pct: Max loss as fraction of equity before blocking (default 10%)

    Returns:
        Dict mapping "FAMILY-EXPIRY" → FamilyExposure
    """
    max_loss_cents = int(total_equity_cents * max_family_loss_pct)

    # Fetch all threshold positions
    rows = conn.execute(
        "SELECT ticker, net_position, avg_entry_cents FROM mm_inventory WHERE net_position != 0"
    ).fetchall()

    # Group by family+expiry
    groups: dict[str, list[tuple[str, int, float]]] = {}
    for ticker, net, avg_entry in rows:
        parsed = _parse_family_expiry(ticker)
        if parsed is None:
            continue
        family, expiry = parsed
        # Only process threshold markets (have -T in ticker)
        if _parse_threshold_bp(ticker) is None:
            continue
        key = f"{family}-{expiry}"
        groups.setdefault(key, []).append((ticker, net, avg_entry))

    results = {}
    for group_key, positions in groups.items():
        worst_pnl = 0
        worst_scenario = ""
        total_contracts = sum(abs(n) for _, n, _ in positions)

        # Dynamic scenario range based on actual held thresholds
        scenarios = _compute_scenario_range(positions)

        # Include fees from fills for this group's tickers
        total_fees_cents = 0
        tickers_in_group = [t for t, _, _ in positions]
        if tickers_in_group:
            try:
                placeholders = ",".join("?" * len(tickers_in_group))
                fee_row = conn.execute(
                    f"SELECT COALESCE(SUM(fee_cents), 0) FROM mm_processed_fills "
                    f"WHERE ticker IN ({placeholders})",
                    tickers_in_group
                ).fetchone()
                total_fees_cents = int(fee_row[0]) if fee_row else 0
            except Exception:
                pass  # mm_processed_fills may not exist or have fee_cents

        for rate_bp in scenarios:
            pnl = compute_scenario_pnl(positions, rate_bp, total_fees_cents)
            if pnl < worst_pnl:
                worst_pnl = pnl
                worst_scenario = f"{rate_bp}bp"

        family, expiry = group_key.split("-", 1)
        blocked = worst_pnl < -max_loss_cents

        results[group_key] = FamilyExposure(
            family=family,
            expiry=expiry,
            positions=positions,
            worst_scenario=worst_scenario,
            worst_pnl_cents=worst_pnl,
            total_contracts=total_contracts,
            blocked=blocked,
        )

        if blocked:
            print(f"[mm-family] ⛔ {group_key}: BLOCKED — worst case {worst_scenario} "
                  f"→ {worst_pnl/100:.2f}$ loss ({total_contracts} contracts, "
                  f"cap={max_loss_cents/100:.2f}$)")

    return results


def is_family_blocked(
    family_exposures: dict[str, FamilyExposure],
    ticker: str,
) -> bool:
    """Check if a ticker's family+expiry group is blocked from new entries."""
    parsed = _parse_family_expiry(ticker)
    if parsed is None:
        return False
    key = f"{parsed[0]}-{parsed[1]}"
    exposure = family_exposures.get(key)
    if exposure is None:
        return False
    return exposure.blocked
