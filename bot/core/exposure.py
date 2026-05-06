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


def expiry_from_ticker(ticker: str) -> str:
    """Extract the settlement-event key (family + expiry date, no strike).

    Kalshi tickers: ``{FAMILY}-{EXPIRY}-{STRIKE}`` where STRIKE starts with
    ``T`` (threshold) or ``B`` (bracket). Examples:
        KXFED-27APR-T2.00        → KXFED-27APR
        KXHIGHMIA-26APR18-B75    → KXHIGHMIA-26APR18
        KXETH-26APR0917-B2150    → KXETH-26APR0917

    Two strikes sharing this key settle on the same real-world event and
    are near-perfectly correlated (one Fed decision moves all KXFED-27APR
    strikes together). Family-level caps miss this because KXFED-27APR
    and KXFED-26OCT are different bets, but family-cap treats them as one.

    Returns ``""`` for empty input. Returns the full ticker if no
    T/B-prefixed segment is found (degrades to family-level granularity).
    """
    if not ticker:
        return ""
    parts = ticker.split("-")
    if len(parts) < 2:
        return ticker
    for i in range(1, len(parts)):
        p = parts[i]
        if p and p[0] in ("T", "B") and len(p) > 1 and (p[1].isdigit() or p[1] == "."):
            return "-".join(parts[:i])
    return ticker


def compute_expiry_exposures(positions: Iterable[dict]) -> dict[str, int]:
    """Bucket a list of Kalshi position dicts into {expiry_key: exposure_cents}.

    Parallel to compute_family_exposures but keyed on settlement event.
    Only non-zero positions contribute. Returns an empty dict on empty input.
    """
    out: dict[str, int] = {}
    for pos in positions or []:
        ticker = pos.get("ticker", "") or ""
        if not ticker:
            continue
        key = expiry_from_ticker(ticker)
        if not key:
            continue
        expo = _position_exposure_cents(pos)
        if expo <= 0:
            continue
        out[key] = out.get(key, 0) + expo
    return out


def expiry_headroom_cents(
    *,
    expiry_key: str,
    current_expiry_exposure_cents: int,
    total_equity_cents: int,
    max_expiry_ratio: float,
) -> int:
    """How many more cents we can put on this settlement event before cap.

    Returns 0 if already at or over the cap. Negative-ratio or zero-equity
    inputs clamp to 0 (safe-closed).
    """
    if max_expiry_ratio <= 0 or total_equity_cents <= 0:
        return 0
    cap = int(total_equity_cents * max_expiry_ratio)
    return max(0, cap - max(0, current_expiry_exposure_cents))


def size_trade_against_expiry_cap(
    *,
    ticker: str,
    proposed_contracts: int,
    price_cents: int,
    expiry_exposures: dict[str, int],
    total_equity_cents: int,
    max_expiry_ratio: float,
) -> tuple[int, Optional[str]]:
    """Apply the per-settlement-event cap to a proposed trade.

    Mirror of size_trade_against_family_cap, keyed on expiry instead of
    family. Both caps compose: caller runs family cap first, then expiry
    cap, and whichever binds tighter wins.

    Returns ``(allowed_contracts, skip_reason)``:
        - ``(proposed_contracts, None)`` when the trade fits
        - ``(reduced_contracts, None)`` when scaled down with some fit
        - ``(0, "expiry_cap_exhausted:<key>")`` when no contracts fit
    """
    if proposed_contracts <= 0 or price_cents <= 0:
        return 0, "invalid_input"

    key = expiry_from_ticker(ticker)
    if not key:
        return proposed_contracts, None

    headroom = expiry_headroom_cents(
        expiry_key=key,
        current_expiry_exposure_cents=expiry_exposures.get(key, 0),
        total_equity_cents=total_equity_cents,
        max_expiry_ratio=max_expiry_ratio,
    )
    if headroom <= 0:
        return 0, f"expiry_cap_exhausted:{key}"

    order_cost = proposed_contracts * price_cents
    if order_cost <= headroom:
        return proposed_contracts, None

    fitted = max(1, headroom // price_cents)
    if fitted < 1:
        return 0, f"expiry_cap_exhausted:{key}"
    return fitted, None


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
