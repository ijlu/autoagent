"""Base strategy protocol — all trading strategies implement this interface."""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from bot.types import FourFactorScore, RunContext, TradeSignal


@runtime_checkable
class BaseStrategy(Protocol):
    """Every strategy must implement detect → evaluate → should_exit."""

    name: str

    def detect(self, ctx: RunContext, market: dict) -> Optional[TradeSignal]:
        """Scan a market for trading opportunities. Returns TradeSignal or None."""
        ...

    def evaluate(self, ctx: RunContext, signal: TradeSignal) -> FourFactorScore:
        """Score the signal through the four-factor gate."""
        ...

    def should_exit(self, ctx: RunContext, position: dict) -> Optional[str]:
        """Check if an existing position should be exited. Returns reason or None."""
        ...
