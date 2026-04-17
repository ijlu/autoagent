"""Shared dataclasses and type definitions for the trading bot."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Regime(str, Enum):
    TRENDING = "trending"
    RANGE_BOUND = "range_bound"
    VOLATILE = "volatile"
    QUIET = "quiet"
    UNKNOWN = "unknown"


@dataclass
class RunContext:
    """Shared context passed to all sub-agents during a single 2-minute cycle."""

    conn: sqlite3.Connection
    balance_cents: int = 0
    portfolio_cents: int = 0
    phase_num: int = 1
    phase_config: tuple = ()
    phase_stats: dict = field(default_factory=dict)
    dry_run: bool = True
    mm_dry_run: bool = True
    stress_level: float = 0.0
    regime: Regime = Regime.UNKNOWN
    active_feedback: dict = field(default_factory=dict)
    calibration_corrections: dict = field(default_factory=dict)
    adaptive_weights: dict = field(default_factory=dict)
    category_scores: dict = field(default_factory=dict)
    avoid_filters: dict = field(default_factory=dict)
    cycle_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FourFactorScore:
    """Four-factor scoring gate result for a trade candidate."""

    edge: float = 0.0
    liquidity: float = 0.0
    confidence: float = 0.0
    timing: float = 0.0

    # Thresholds
    EDGE_MIN: float = 0.1
    LIQUIDITY_MIN: float = 0.3
    CONFIDENCE_MIN: float = 0.4
    TIMING_MIN: float = 0.2

    @property
    def passes(self) -> bool:
        return (
            self.edge >= self.EDGE_MIN
            and self.liquidity >= self.LIQUIDITY_MIN
            and self.confidence >= self.CONFIDENCE_MIN
            and self.timing >= self.TIMING_MIN
        )

    @property
    def composite(self) -> float:
        return self.edge * self.liquidity * self.confidence * self.timing

    def to_dict(self) -> dict:
        return {
            "edge": round(self.edge, 4),
            "liquidity": round(self.liquidity, 4),
            "confidence": round(self.confidence, 4),
            "timing": round(self.timing, 4),
            "passes": self.passes,
            "composite": round(self.composite, 6),
        }


@dataclass
class TradeSignal:
    """A candidate trade produced by a strategy's detect() method."""

    ticker: str
    side: Side
    strategy: str
    ensemble_prob: float
    market_prob: float
    edge: float
    confidence: float
    n_sources: int
    source_desc: str
    regime: Regime = Regime.UNKNOWN
    four_factor: Optional[FourFactorScore] = None
    suggested_contracts: int = 0
    suggested_price_cents: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class SourceEstimate:
    """Result from a single data source."""

    probability: float
    weight: float
    source_name: str
    latency_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class TradeResult:
    """Outcome of an executed trade."""

    net_position: int
    avg_entry: float
    realized_pnl_cents: float
