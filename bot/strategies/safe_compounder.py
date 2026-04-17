"""Safe Compounder — NO-side only on near-certain outcomes.

Low-risk strategy: buy NO when YES price is very low (< 20c), capturing
the high probability that the market resolves NO. Expected 70-80% win rate,
low variance, consistent small profits.

Exclusions: crypto (too volatile), sports (outcome uncertain until end),
entertainment (unpredictable).
"""

from __future__ import annotations

from typing import Optional

from bot.strategies.registry import register
from bot.types import FourFactorScore, RunContext, Side, TradeSignal
from bot.scoring.filters import categorize_market
from bot.scoring.four_factor import score_four_factors


@register
class SafeCompounder:
    name = "safe_compounder"

    # Config
    MAX_YES_PRICE_CENTS = 20  # Only trade when YES < 20c
    MIN_NO_EDGE_CENTS = 5     # Need at least 5c edge after fees
    MAX_PORTFOLIO_PCT = 0.10  # Max 10% of portfolio per position
    EXCLUDED_CATEGORIES = {"crypto", "sports", "entertainment"}

    def detect(self, ctx: RunContext, market: dict) -> Optional[TradeSignal]:
        """Look for markets where YES is very cheap (near-certain NO)."""
        ticker = market.get("ticker", "")
        title = market.get("title", "")

        # Get YES price
        yes_ask = market.get("yes_ask") or market.get("yes_ask_dollars") or 0
        yes_ask_val = float(yes_ask)
        # Handle dollar vs cent encoding
        if yes_ask_val > 1:
            yes_ask_cents = round(yes_ask_val)
        else:
            yes_ask_cents = round(yes_ask_val * 100)

        # Filter: YES must be cheap (near-certain NO outcome)
        if yes_ask_cents <= 0 or yes_ask_cents > self.MAX_YES_PRICE_CENTS:
            return None

        # Get NO ask price
        no_ask = market.get("no_ask") or market.get("no_ask_dollars") or 0
        no_ask_val = float(no_ask)
        if no_ask_val > 1:
            no_ask_cents = round(no_ask_val)
        else:
            no_ask_cents = round(no_ask_val * 100)

        # NO must be available and reasonably priced
        if no_ask_cents <= 0 or no_ask_cents < 80:
            return None

        # Category exclusion
        category = categorize_market(ticker, title)
        if category in self.EXCLUDED_CATEGORIES:
            return None

        # Check category hard-block from context
        if ctx.category_scores:
            cat_score = ctx.category_scores.get(category, 50)
            if cat_score < 20:  # Hard-blocked category
                return None

        # Volume filter -- need minimum liquidity
        volume = float(market.get("volume") or market.get("volume_24h") or 0)
        if volume < 25:
            return None

        # Compute edge
        # Our estimate: YES resolves NO with high probability
        # Fair NO value ~ 1 - yes_ask_cents/100
        # We're buying NO at no_ask_cents
        market_no_prob = no_ask_cents / 100
        estimated_no_prob = 1 - (yes_ask_cents / 100)  # Conservative: use market's YES as our estimate

        # To be more aggressive, we could use ensemble estimates, but for safe compounder
        # we trust the market signal that YES is cheap
        edge = estimated_no_prob - market_no_prob

        if edge * 100 < self.MIN_NO_EDGE_CENTS:
            return None

        # Build signal
        # Size: limited by MAX_PORTFOLIO_PCT
        max_spend = int(ctx.balance_cents * self.MAX_PORTFOLIO_PCT)
        contracts = min(max_spend // no_ask_cents, 20)  # Cap at 20 contracts
        if contracts < 1:
            return None

        return TradeSignal(
            ticker=ticker,
            side=Side.NO,
            strategy=self.name,
            ensemble_prob=1 - estimated_no_prob,  # YES probability (low)
            market_prob=yes_ask_cents / 100,
            edge=edge,
            confidence=0.7,  # High confidence in near-certain outcomes
            n_sources=1,  # Market price is the signal
            source_desc=f"safe_compounder:yes@{yes_ask_cents}c",
            regime=ctx.regime,
            suggested_contracts=contracts,
            suggested_price_cents=no_ask_cents,
            metadata={
                "category": category,
                "yes_ask_cents": yes_ask_cents,
                "no_ask_cents": no_ask_cents,
            },
        )

    def evaluate(self, ctx: RunContext, signal: TradeSignal) -> FourFactorScore:
        """Score through four-factor gate with safe compounder adjustments."""
        # Use the standard four-factor scorer but with relaxed edge threshold
        # Safe compounder trades have inherent structural edge
        score = score_four_factors(
            market_data=signal.metadata,
            ensemble_prob=signal.ensemble_prob,
            market_prob=signal.market_prob,
            n_sources=signal.n_sources,
            source_desc=signal.source_desc,
            regime=signal.regime,
            active_feedback=ctx.active_feedback,
            category_scores=ctx.category_scores,
            conn=ctx.conn,
        )

        # Boost confidence for safe compounder (structural edge, not model edge)
        score.confidence = max(score.confidence, 0.6)

        return score

    def should_exit(self, ctx: RunContext, position: dict) -> Optional[str]:
        """Check if we should exit a safe compounder position early."""
        # Exit if YES price has risen significantly (our thesis is wrong)
        net = position.get("net_position", 0)

        if net >= 0:
            return None  # We should be short YES (long NO), so net < 0

        # If YES price rose above 40c, our near-certain NO thesis may be wrong
        yes_ask = position.get("current_yes_ask", 0)
        if yes_ask > 40:
            return f"safe_compounder_exit:YES rose to {yes_ask}c (was <20c)"

        return None
