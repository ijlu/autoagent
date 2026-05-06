"""Regression guard for CLAUDE.md Known Bug Pattern #12.

``trade.py._CACHE`` historically was a plain ``dict`` — the daemon runs for
days, the cache grows unboundedly, and we'd OOM. ``bot/api.py`` already uses
a ``cachetools.TTLCache`` (covered by ``tests/bot/test_api_concurrency.py``);
this test enforces the same shape on the trade.py side so neither path can
silently regress to an unbounded dict.

Cross-module key-overlap (the original "stale read" half of the bug pattern)
isn't statically testable — once both caches are bounded TTLCaches with sane
TTLs, the worst-case is a stale read that expires within seconds, not an
unbounded leak.
"""

from __future__ import annotations

import importlib

import pytest
from cachetools import TTLCache

trade = importlib.import_module("trade")


def test_trade_cache_is_bounded_ttl_cache() -> None:
    cache = getattr(trade, "_CACHE", None)
    assert cache is not None, "trade._CACHE must exist"
    assert isinstance(cache, TTLCache), (
        f"trade._CACHE must be a cachetools.TTLCache, got {type(cache).__name__}. "
        f"A plain dict grows unboundedly under a long-running daemon "
        f"(CLAUDE.md Known Bug Pattern #12)."
    )
    assert cache.maxsize > 0
    assert cache.ttl > 0


def test_trade_cache_evicts_when_full() -> None:
    """Sanity: cache enforces its maxsize bound."""
    cache = trade._CACHE
    cache.clear()
    cap = cache.maxsize
    for i in range(cap + 100):
        cache[f"k_{i}"] = i
    assert len(cache) <= cap, (
        f"trade._CACHE accepted {len(cache)} entries with maxsize={cap}"
    )
