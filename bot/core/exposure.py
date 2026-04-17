"""Per-family exposure accounting.

Kalshi families like KXFED-26JUN / KXFED-26JUL / KXFED-26AUG are correlated
bets on the same underlying (Fed rate path). Kelly sizing assumes independence,
so left to its own devices it can compound a single thesis into a 95%
concentrated book — exactly what happened on 2026-04-16 before Phase 1. A
per-family cap bounds aggregate correlated exposure without constraining
per-market Kelly on genuinely independent bets.

This module is I/O-free: callers pass in already-fetched Kalshi positions
plus a family key for each candidate trade. Keeps the risk logic testable
without live API dependency.
"""
from __future__ import annotations

from typing import Iterable, Optional


def family_from_ticker(ticker: str) -> str:
    """Extract the family prefix (everything before the first hyphen).

    Kalshi tickers: KXFED-26MAY-T425 → KXFED, KXHIGHMIA-26APR18-T75 → KXHIGHMIA.
    Duplicated from bot.learning.alpha_log.family_from_ticker to avoid a
    learning→core dependency. Must stay identical in behavior.
    """
    if not ticker:
        return ""
    idx = ticker.find("-")
    return ticker if idx == -1 else ticker[:idx]


def _position_exposure_cents(pos: dict) -> int:
    """Cost basis for one Kalshi position row, in cents.

    Kalshi's ``market_exposure`` is the authoritative live-position cost
    figure. We fall back to ``abs(position) * average_price_paid`` so this
    works for tests and for older API response shapes that omit the field.
    """
    exposure = pos.get("market_exposure")
    if exposure is not None:
        try:
            return max(0, int(round(float(exposure))))
        except (TypeError, ValueError):
            pass

    pos_raw = pos.get("position_fp") or pos.get("position", 0)
    avg_raw = pos.get("average_price_paid") or pos.get("avg_price_cents", 0)
    try:
        contracts = abs(round(float(pos_raw)))
        avg_price = float(avg_raw)
    except (TypeError, ValueError):
        return 0
    if contracts <= 0 or avg_price <= 0:
        return 0
    return int(contracts * avg_price)


def compute_family_exposures(positions: Iterable[dict]) -> dict[str, int]:
    """Bucket a list of Kalshi position dicts into {family: exposure_cents}.

    Only non-zero positions contribute (Kalshi keeps settled-zero rows around
    in the ``market_positions`` list). Returns an empty dict on empty input.
    """
    out: dict[str, int] = {}
    for pos in positions or []:
        ticker = pos.get("ticker", "") or ""
        if not ticker:
            continue
        fam = family_from_ticker(ticker)
        if not fam:
            continue
        expo = _position_exposure_cents(pos)
        if expo <= 0:
            continue
        out[fam] = out.get(fam, 0) + expo
    return out


def family_headroom_cents(
    *,
    family: str,
    current_family_exposure_cents: int,
    total_equity_cents: int,
    max_family_ratio: float,
) -> int:
    """How many more cents we can put on this family before hitting the cap.

    Returns 0 if already at or over the cap. Negative-ratio or zero-equity
    inputs clamp to 0 (safe-closed).
    """
    if max_family_ratio <= 0 or total_equity_cents <= 0:
        return 0
    cap = int(total_equity_cents * max_family_ratio)
    return max(0, cap - max(0, current_family_exposure_cents))


def size_trade_against_family_cap(
    *,
    ticker: str,
    proposed_contracts: int,
    price_cents: int,
    family_exposures: dict[str, int],
    total_equity_cents: int,
    max_family_ratio: float,
) -> tuple[int, Optional[str]]:
    """Apply the family cap to a proposed trade.

    Returns ``(allowed_contracts, skip_reason)``:
        - ``(proposed_contracts, None)`` when the trade fits
        - ``(reduced_contracts, None)`` when the trade was scaled down but
          some contracts still fit
        - ``(0, "family_cap_exhausted:<fam>")`` when no contracts fit

    Pass ``family_exposures`` mutated across the cycle so within-cycle
    accumulation is counted. Caller is responsible for updating the dict
    after a successful post.
    """
    if proposed_contracts <= 0 or price_cents <= 0:
        return 0, "invalid_input"

    fam = family_from_ticker(ticker)
    if not fam:
        return proposed_contracts, None

    headroom = family_headroom_cents(
        family=fam,
        current_family_exposure_cents=family_exposures.get(fam, 0),
        total_equity_cents=total_equity_cents,
        max_family_ratio=max_family_ratio,
    )
    if headroom <= 0:
        return 0, f"family_cap_exhausted:{fam}"

    order_cost = proposed_contracts * price_cents
    if order_cost <= headroom:
        return proposed_contracts, None

    # Scale down — at minimum one contract if any headroom exists
    fitted = max(1, headroom // price_cents)
    if fitted < 1:
        return 0, f"family_cap_exhausted:{fam}"
    return fitted, None
