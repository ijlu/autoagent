"""Strategy registry — auto-discovers strategies via @register decorator."""

from __future__ import annotations

from typing import Type

from bot.strategies.base import BaseStrategy
from bot.types import RunContext

_STRATEGIES: dict[str, Type[BaseStrategy]] = {}


def register(cls):
    """Decorator: @register makes a strategy available to the orchestrator.

    Usage:
        @register
        class SafeCompounder:
            name = "safe_compounder"
            def detect(self, ctx, market): ...
            def evaluate(self, ctx, signal): ...
            def should_exit(self, ctx, position): ...
    """
    _STRATEGIES[cls.name] = cls
    return cls


def get_all_strategies() -> dict[str, Type[BaseStrategy]]:
    """Return all registered strategy classes."""
    return dict(_STRATEGIES)


def get_enabled_strategies(ctx: RunContext) -> list[BaseStrategy]:
    """Return instantiated strategies not blocked by active feedback or category scoring.

    Checks ctx.active_feedback for disabled strategies and ctx.category_scores
    for hard-blocked categories.
    """
    enabled = []
    disabled = set()

    # Check active feedback for disabled strategies
    if ctx.active_feedback:
        avoid_strats = ctx.active_feedback.get("avoid_strategies", [])
        if isinstance(avoid_strats, list):
            disabled.update(s.lower() for s in avoid_strats)

    for name, cls in _STRATEGIES.items():
        if name.lower() in disabled:
            print(f"[registry] Strategy '{name}' disabled by active feedback")
            continue
        try:
            enabled.append(cls())
        except Exception as e:
            print(f"[registry] Failed to instantiate '{name}': {e}")

    return enabled
