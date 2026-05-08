"""Cross-bracket portfolio scoring — Phase B.3.

Today's flow per cycle: score one (ticker, side) at a time. Each market
visit places at most one order. Wastes the fact that our μ/σ Gaussian
covers ALL brackets in the market simultaneously.

Cross-bracket flow: project the predicted Gaussian onto every bracket in
a market, decide per bracket (buy YES / buy NO / skip), return the full
portfolio of decisions in one shot. Each leg is independently EV+ —
no atomicity constraint, partial-fill is fine.

Why now: post-Phase-B.2 the ensemble has cleaner μ/σ inputs (ICON +
UKMO independent sources, σ ceiling honors learned σ, sanity gate
filters outliers). Cross-bracket benefits proportionally to combine
quality.

Design constraints:

  - Independent legs (NOT atomic batch). Each leg passes through existing
    gates (METAR-required, TTE ≥12h, sanity gate, family blocklist).
  - σ floor honored: if combined σ < _COMBINED_SIGMA_FLOOR_F, force it
    to the floor. Same logic as predict_v2 step 4d.
  - Penny floor: don't trade brackets where our predicted p_yes is in
    the [0.02, 0.05] tail OR market price is at the [1¢, 4¢] / [96¢, 99¢]
    extremes. Adverse selection on those.
  - Per-leg edge gate: each leg must clear ``min_edge`` independently.
    Prevents diluting the portfolio with marginal trades.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class BracketDecision:
    """One leg of a cross-bracket portfolio."""
    ticker: str
    is_bracket: bool
    bracket_lo: Optional[float]
    bracket_hi: Optional[float]
    threshold: Optional[float]  # for T-tickers
    is_above: bool

    p_yes: float                # our predicted P(YES)
    market_yes_bid: Optional[int]   # cents, YES side
    market_yes_ask: Optional[int]
    market_yes_mid: Optional[float]

    edge_yes: Optional[float]   # our p_yes - market_yes_ask/100
    edge_no: Optional[float]    # (1 - our_p_yes) - market_no_ask/100

    action: str                 # 'buy_yes' | 'buy_no' | 'skip'
    side: Optional[str]         # 'yes' | 'no' | None
    price_cents: Optional[int]
    skip_reason: Optional[str]


def _ncdf(x: float, mu: float, sigma: float) -> float:
    """Normal CDF P(X <= x). Matches predict_v2's projection convention."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def project_gaussian_to_bracket(
    mu: float, sigma: float, lo: float, hi: float,
) -> float:
    """P(lo <= X < hi) under N(mu, sigma). Clipped to [0.005, 0.995]."""
    p = _ncdf(hi, mu, sigma) - _ncdf(lo, mu, sigma)
    return max(0.005, min(0.995, p))


def project_gaussian_above(mu: float, sigma: float, threshold: float) -> float:
    """P(X > threshold) for T-tickers."""
    p = 1.0 - _ncdf(threshold, mu, sigma)
    return max(0.005, min(0.995, p))


# 2026-05-05 (Phase 3d): conviction gate — require model probability of
# the side we'd buy to exceed this threshold before firing. Without this,
# a bracket boundary case (e.g., NY high lands at 72.0°F = boundary of
# B72.5 [72, 74)) gives p_yes ≈ 0.45 which the strategy would interpret
# as "edge_no = 0.55 - market_no_price" and fire NO at high price. The
# market knows the high landed at 72 (it's seen METAR) and has priced
# YES at 86%. We have no edge — we have a coin flip with the market
# disagreeing about magnitude. Skip.
#
# 0.65 chosen so that NO leg fires only when model thinks NO has at
# least 65% probability — i.e., model has a clear directional view.
# Cross-bracket's value comes from confident disagreements with the
# market, not magnitude disputes on coin-flips.
_CONVICTION_THRESHOLD: float = 0.65


def _decide_leg(
    p_yes: float,
    yes_bid: Optional[int], yes_ask: Optional[int],
    *,
    min_edge: float,
    min_price_cents: int,
    max_price_cents: int,
) -> tuple[str, Optional[str], Optional[int], Optional[str]]:
    """Return (action, side, price_cents, skip_reason) for one bracket.

    YES side: buy YES at yes_ask if (our_p_yes - yes_ask/100) >= min_edge
              AND p_yes >= _CONVICTION_THRESHOLD (strong YES conviction)
    NO side: buy NO at no_ask = 100 - yes_bid
              if ((1 - our_p_yes) - no_ask/100) >= min_edge
              AND (1 - p_yes) >= _CONVICTION_THRESHOLD (strong NO conviction)

    Cross-bracket is designed for confident disagreements with the
    market. The conviction gate prevents firing on bracket-boundary
    coin-flips where p_yes ≈ 0.5 — the market may know more than we
    do via real-time METAR, and we shouldn't bet against it without
    a clear directional view.
    """
    # Trading-time humility cap. Raw p_yes still recorded on
    # BracketDecision.p_yes by the caller; this cap only governs
    # downstream edge / conviction / sizing math.
    from bot.scoring.trading_caps import cap_trading_prob
    p_yes = cap_trading_prob(p_yes, source="decide_leg")

    edge_yes = None
    edge_no = None
    if yes_ask is not None:
        edge_yes = p_yes - (yes_ask / 100.0)
    if yes_bid is not None:
        no_ask = 100 - yes_bid  # NO ask = 100 - YES bid
        edge_no = (1.0 - p_yes) - (no_ask / 100.0)

    # Decide best side
    best_action = "skip"
    best_side = None
    best_price = None
    best_edge = 0.0
    skip_reason = None

    p_no = 1.0 - p_yes

    yes_passes_conviction = p_yes >= _CONVICTION_THRESHOLD
    no_passes_conviction = p_no >= _CONVICTION_THRESHOLD

    if (edge_yes is not None and edge_yes > best_edge
            and edge_yes >= min_edge and yes_passes_conviction):
        if min_price_cents <= yes_ask <= max_price_cents:
            best_action = "buy_yes"
            best_side = "yes"
            best_price = yes_ask
            best_edge = edge_yes
        else:
            skip_reason = f"yes_ask_{yes_ask}c_outside_band"
    if (edge_no is not None and edge_no > best_edge
            and edge_no >= min_edge and no_passes_conviction):
        no_ask_cents = 100 - yes_bid
        if min_price_cents <= no_ask_cents <= max_price_cents:
            best_action = "buy_no"
            best_side = "no"
            best_price = no_ask_cents
            best_edge = edge_no
        else:
            skip_reason = f"no_ask_{no_ask_cents}c_outside_band"

    if best_action == "skip" and skip_reason is None:
        # Both sides had insufficient edge or conviction. Annotate which.
        if edge_yes is None and edge_no is None:
            skip_reason = "no_market_quote"
        else:
            ey = f"{edge_yes:+.3f}" if edge_yes is not None else "n/a"
            en = f"{edge_no:+.3f}" if edge_no is not None else "n/a"
            # Add conviction context — distinguishes "edge too small" from
            # "no directional view" so post-mortems can tell them apart.
            conv_tag = ""
            if not yes_passes_conviction and not no_passes_conviction:
                conv_tag = (
                    f" conviction_fail(p_yes={p_yes:.2f}<{_CONVICTION_THRESHOLD}"
                    f" p_no={p_no:.2f}<{_CONVICTION_THRESHOLD})"
                )
            skip_reason = (
                f"edge_yes={ey} edge_no={en} min={min_edge:.3f}{conv_tag}"
            )

    return (best_action, best_side, best_price, skip_reason)


def score_market_portfolio(
    related_markets: list[dict],
    combined_mu: float,
    combined_sigma: float,
    *,
    min_edge: float = 0.07,
    min_price_cents: int = 5,
    max_price_cents: int = 95,
    sigma_floor: float = 1.0,
) -> list[BracketDecision]:
    """For each bracket in ``related_markets`` (e.g., all
    KXHIGHNY-26APR30-* brackets), project the predicted Gaussian onto
    that bracket's (lo, hi) and decide buy YES / buy NO / skip.

    Args:
      related_markets: list of Kalshi market_data dicts, all settling on
                       the same date for the same family
      combined_mu: predicted daily-high (°F) — from production
                   ``predict_v2`` combined Gaussian
      combined_sigma: predicted σ
      min_edge: minimum (our_p - market_price/100) for a leg to fire
      min/max_price_cents: penny-floor on market price
      sigma_floor: enforce minimum σ matching ``_COMBINED_SIGMA_FLOOR_F``

    Returns: per-bracket decisions. Caller iterates non-skip entries
    through gating (METAR-required, TTE, exposure caps) and posts each.
    """
    if combined_sigma < sigma_floor:
        combined_sigma = sigma_floor

    decisions: list[BracketDecision] = []
    # Late import to avoid pulling weather_ensemble_v2 at import time
    from bot.signals.weather_ensemble_v2 import _parse_market_for_projection
    from bot.learning.alpha_log import _parse_kalshi_cents

    for market in related_markets:
        ticker = market.get("ticker", "")
        proj = _parse_market_for_projection(ticker, market)
        if proj is None:
            continue
        is_bracket, threshold, is_above, bracket_lo, bracket_hi = proj

        # Compute our P(YES) for this bracket / threshold
        if is_bracket and bracket_lo is not None and bracket_hi is not None:
            p_yes = project_gaussian_to_bracket(
                combined_mu, combined_sigma, bracket_lo, bracket_hi)
        elif is_above:
            p_yes = project_gaussian_above(
                combined_mu, combined_sigma, threshold)
        else:
            # "below" market — P(X <= threshold)
            p_yes = _ncdf(threshold, combined_mu, combined_sigma)
            p_yes = max(0.005, min(0.995, p_yes))

        # Market quotes (cents)
        yes_bid = _parse_kalshi_cents(
            market.get("yes_bid_dollars") or market.get("yes_bid"))
        yes_ask = _parse_kalshi_cents(
            market.get("yes_ask_dollars") or market.get("yes_ask"))
        yes_mid = (
            (yes_bid + yes_ask) / 2.0
            if yes_bid is not None and yes_ask is not None
            else None
        )

        action, side, price_cents, skip_reason = _decide_leg(
            p_yes, yes_bid, yes_ask,
            min_edge=min_edge,
            min_price_cents=min_price_cents,
            max_price_cents=max_price_cents,
        )

        edge_yes = (p_yes - yes_ask / 100.0) if yes_ask is not None else None
        edge_no = (
            (1.0 - p_yes) - ((100 - yes_bid) / 100.0)
            if yes_bid is not None else None
        )

        decisions.append(BracketDecision(
            ticker=ticker,
            is_bracket=is_bracket,
            bracket_lo=bracket_lo, bracket_hi=bracket_hi,
            threshold=threshold, is_above=bool(is_above),
            p_yes=p_yes,
            market_yes_bid=yes_bid, market_yes_ask=yes_ask,
            market_yes_mid=yes_mid,
            edge_yes=edge_yes, edge_no=edge_no,
            action=action, side=side, price_cents=price_cents,
            skip_reason=skip_reason,
        ))

    return decisions


def group_markets_by_settlement(markets: list[dict]) -> dict[str, list[dict]]:
    """Group a flat market list by (family, settle_date) so cross-bracket
    can score them together. Settlement key is the ticker prefix up to
    the bracket suffix, e.g. ``KXHIGHNY-26APR30``.

    Returns dict mapping settlement_key → list of market_data dicts.
    """
    grouped: dict[str, list[dict]] = {}
    for m in markets:
        ticker = (m.get("ticker") or "").upper()
        if not ticker:
            continue
        # Settlement key = everything before the bracket / threshold suffix
        # e.g. "KXHIGHNY-26APR30-B68.5" → "KXHIGHNY-26APR30"
        for sep in ("-B", "-T"):
            idx = ticker.rfind(sep)
            if idx > 0:
                key = ticker[:idx]
                break
        else:
            # No bracket / threshold suffix — single-market series
            key = ticker
        grouped.setdefault(key, []).append(m)
    return grouped
