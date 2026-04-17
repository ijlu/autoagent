"""Centralized configuration for the trading bot.

All environment variables, constants, and phase config extracted from trade.py.
Other modules import from here — never read env vars directly.
"""

from __future__ import annotations

import os


# ══════════════════════════════════════════════════════════════════════════════
# Kalshi API
# ══════════════════════════════════════════════════════════════════════════════
KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
BASE_URL = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
HOST = BASE_URL.split("/trade-api")[0]

# ══════════════════════════════════════════════════════════════════════════════
# Core trading parameters
# ══════════════════════════════════════════════════════════════════════════════
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "0.10"))
MAX_DRAWDOWN = float(os.environ.get("MAX_DRAWDOWN_PCT", "0.15"))
KELLY_FRACTION = float(os.environ.get("KELLY_FRACTION", "0.10"))
MAX_CONTRACTS = int(os.environ.get("MAX_CONTRACTS", "500"))
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.02"))
DB_PATH = os.environ.get("DB_PATH", "/task/kalshi_trades.db")
MIN_WIN_RATE = float(os.environ.get("MIN_WIN_RATE", "0.45"))
MIN_SAMPLE_SIZE = int(os.environ.get("MIN_SAMPLE_SIZE", "5"))
ORDER_MAX_AGE_HOURS = float(os.environ.get("ORDER_MAX_AGE_HOURS", "2"))

# Position management
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.20"))
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0.15"))
MAX_HOLD_DAYS = int(os.environ.get("MAX_HOLD_DAYS", "7"))

# Information layer / edge thresholds
MIN_EDGE = float(os.environ.get("MIN_EDGE", "0.07"))
SINGLE_SOURCE_EDGE = float(os.environ.get("SINGLE_SOURCE_EDGE", "0.12"))
MAX_PER_CATEGORY = int(os.environ.get("MAX_PER_CATEGORY", "2"))
MAX_PORTFOLIO_PCT = float(os.environ.get("MAX_PORTFOLIO_PCT", "0.15"))

# ══════════════════════════════════════════════════════════════════════════════
# Market Making
# ══════════════════════════════════════════════════════════════════════════════
MM_ENABLED = os.environ.get("MM_ENABLED", "true").lower() in ("true", "1", "yes")
MM_DRY_RUN = os.environ.get("MM_DRY_RUN", "true").lower() in ("true", "1", "yes")
# Phase 1 shadow-to-live gate for weather MM. Default false — shadow mode only.
# Flipped to true only once the step-9 shadow backtest proves out.
WEATHER_MM_LIVE = os.environ.get("WEATHER_MM_LIVE", "false").lower() in ("true", "1", "yes")
MM_MIN_SPREAD = int(os.environ.get("MM_MIN_SPREAD_CENTS", "4"))
MM_HALF_SPREAD = int(os.environ.get("MM_HALF_SPREAD_CENTS", "2"))
MM_MAX_INVENTORY = int(os.environ.get("MM_MAX_INVENTORY", "50"))
MM_MAX_MARKETS = int(os.environ.get("MM_MAX_MARKETS", "10"))
MM_CAPITAL_PCT = float(os.environ.get("MM_CAPITAL_PCT", "0.10"))
MM_ORDER_SIZE = int(os.environ.get("MM_ORDER_SIZE", "10"))
MM_SKEW_PER_10 = int(os.environ.get("MM_SKEW_PER_10", "2"))
MM_MIN_VOLUME = int(os.environ.get("MM_MIN_VOLUME", "25"))
MM_ORDER_TAG = "mm_v1"
MM_PREFERRED_CATS = {"weather", "climate", "economics"}
MM_MAX_DAYS_TO_EXPIRY = int(os.environ.get("MM_MAX_DAYS_TO_EXPIRY", "30"))
MM_MAX_LOT_AGE_HOURS = int(os.environ.get("MM_MAX_LOT_AGE_HOURS", "48"))
MM_MAX_FAMILY_LOSS_PCT = float(os.environ.get("MM_MAX_FAMILY_LOSS_PCT", "0.10"))
MAX_QA_PER_RUN = int(os.environ.get("MAX_QA_PER_RUN", "10"))

# ══════════════════════════════════════════════════════════════════════════════
# Safe Compounder (separate from MM and directional)
# ══════════════════════════════════════════════════════════════════════════════
SC_ENABLED = os.environ.get("SC_ENABLED", "true").lower() in ("true", "1", "yes")
SC_DRY_RUN = os.environ.get("SC_DRY_RUN", "true").lower() in ("true", "1", "yes")

# ══════════════════════════════════════════════════════════════════════════════
# Risk limits
# ══════════════════════════════════════════════════════════════════════════════
MAX_PORTFOLIO_EXPOSURE_RATIO = float(os.environ.get("MAX_PORTFOLIO_EXPOSURE_RATIO", "0.50"))

# ══════════════════════════════════════════════════════════════════════════════
# Source horizon compatibility (max forecast horizon in days per source)
# ══════════════════════════════════════════════════════════════════════════════
SOURCE_MAX_HORIZON_DAYS = {
    "weather": 7,
    "tomorrow": 7,
    "noaa": 5,
    "metar": 1,           # real-time observations only
    "nws_point": 7,       # NWS hourly forecast horizon
    "nbm": 7,             # NBM blended forecast
    "hrrr": 2,            # HRRR high-res 18-48h only
    "madis": 1,           # mesonet observations today only
    "afd": 2,             # Area Forecast Discussion ~48h
    "weather_ensemble": 7,  # combined weather router
    "zq_futures": 365,    # futures curve extends ~1 year
    "fedwatch": 365,      # same underlying as ZQ
    "fred": 90,
    "bls": 90,
    "clevfed": 90,
    "adp_nfp": 30,        # monthly NFP release cadence
    "gdpnow": 90,         # quarterly GDP, nowcast spans ~3mo
    "commodity": 60,      # CPI monthly release cadence
    "crypto": 30,
    "sports": 14,
    "odds": 14,
    "series": 365,        # structural — always applicable
    "polymarket": 365,    # prediction market — always applicable
    "metaculus": 365,     # prediction market — always applicable
    "momentum": 30,
    "company_kpi": 90,
    "sensortower": 90,
    "finnhub": 30,
    "llm": 365,           # LLM can reason about any horizon
}

# ══════════════════════════════════════════════════════════════════════════════
# External API keys
# ══════════════════════════════════════════════════════════════════════════════
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
BLS_API_KEY = os.environ.get("BLS_API_KEY", "")
BEA_API_KEY = os.environ.get("BEA_API_KEY", "")
CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY", "")
EIA_API_KEY = os.environ.get("EIA_API_KEY", "")
TOMORROW_API_KEY = os.environ.get("TOMORROW_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
SENSORTOWER_TOKEN = os.environ.get("SENSORTOWER_API_TOKEN", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
METACULUS_API_TOKEN = os.environ.get("METACULUS_API_TOKEN", "")

# ══════════════════════════════════════════════════════════════════════════════
# Source weights (default priors — adaptive learning updates these)
# ══════════════════════════════════════════════════════════════════════════════
SOURCE_WEIGHTS = {
    "polymarket": 0.75, "odds": 0.85, "weather": 0.80, "tomorrow": 0.82,
    "noaa": 0.75, "metar": 0.90, "series": 0.75, "metaculus": 0.70,
    "clevfed": 0.72, "fedwatch": 0.80, "zq_futures": 0.85,
    "crypto": 0.65, "company_kpi": 0.65, "sensortower": 0.55,
    "bls": 0.50, "fred": 0.50, "finnhub": 0.30, "llm": 0.15,
    "momentum": 0.15,
    # Phase 2 weather expansion
    "nws_point": 0.78,         # NWS authoritative hourly
    "nbm": 0.80,               # NOAA blended model
    "hrrr": 0.85,              # high-res short-range (tightest sigma)
    "madis": 0.60,             # mesonet/citizen stations, noisier
    "afd": 0.50,               # forecaster discussion text
    "weather_ensemble": 0.95,  # combined router output; dominates when present
    # Phase 3 economics expansion
    "adp_nfp": 0.80,           # ADP 2-day lead on BLS NFP
    "gdpnow": 0.78,            # Atlanta Fed GDPNow nowcast
    "commodity": 0.55,         # commodity futures → CPI transmission
}

# Correlated source groups (count as ~1 effective source, not N)
CORRELATED_GROUPS = {
    # All weather sources — when weather_ensemble fires it collapses the 9-source
    # group into a single effective source. Keeps the edge-threshold scaling honest.
    "weather": {
        "weather", "tomorrow", "metar", "noaa",
        "nws_point", "nbm", "hrrr", "madis", "afd",
        "weather_ensemble",
    },
    "cpi": {"fred", "bls", "commodity"},
    "prediction_market": {"polymarket", "metaculus"},
    "fed": {"fred", "clevfed", "fedwatch", "zq_futures"},
    "nfp": {"adp_nfp", "fred", "bls"},
    "gdp": {"gdpnow", "fred"},
}

# ══════════════════════════════════════════════════════════════════════════════
# Phased sizing — auto-ramp based on track record
# ══════════════════════════════════════════════════════════════════════════════
FORCE_PHASE = os.environ.get("FORCE_PHASE", "")

# phase: (min_settled, min_win_rate, max_position_pct, max_portfolio_pct,
#         max_contracts, kelly_mult, min_edge_mult, description)
PHASE_CONFIG = {
    1: (0, 0.00, 0.000, 0.000, 0, 0.0, 1.0,
        "Paper trading — DRY_RUN forced on, zero risk"),
    2: (50, 0.50, 0.035, 0.15, 10, 0.25, 1.5,
        "Micro live — ~$20 max position, 10 contracts max, 1.5x edge required"),
    3: (150, 0.52, 0.050, 0.05, 50, 0.50, 1.25,
        "Small live — $500 max position, $5k max portfolio, 1.25x edge required"),
    4: (300, 0.53, 0.010, 0.10, 200, 0.75, 1.1,
        "Medium live — $1k max position, $10k max portfolio"),
    5: (500, 0.54, 0.020, 0.15, 500, 1.00, 1.0,
        "Full deployment — standard parameters"),
}

# ══════════════════════════════════════════════════════════════════════════════
# Rate limiting
# ══════════════════════════════════════════════════════════════════════════════
RATE_LIMITS = {
    "kalshi": (0.25, 8),
    "polymarket": (0.5, 4),
    "open-meteo": (1.0, 3),
    "coingecko": (1.5, 2),
    "fred.stlouisfed": (1.0, 3),
    "the-odds-api": (2.0, 2),
    "metaculus": (1.0, 3),
    "finnhub": (1.0, 3),
    "deribit": (1.0, 3),
    "noaa": (1.0, 3),
    "clevelandfed": (2.0, 2),
    "openai": (0.5, 5),
    "manifold": (0.5, 4),
    "bls.gov": (2.0, 3),
    "tomorrow.io": (2.0, 3),
}

# ══════════════════════════════════════════════════════════════════════════════
# Personal trade prefixes (skip in settlement processing)
# ══════════════════════════════════════════════════════════════════════════════
PERSONAL_PREFIXES = ("KXNBA", "KXNCAA", "KXMVE", "KXNFL", "KXMARMAD")

# ══════════════════════════════════════════════════════════════════════════════
# Self-improvement guardrails
# ══════════════════════════════════════════════════════════════════════════════
def compute_dynamic_sizing(total_equity_cents, conn=None):
    """Scale MM and position sizing based on total equity + confidence gate.

    Starts at conservative baseline (10/50), ramps to full dynamic sizing
    as expectancy (avg P&L per settlement) improves. Uses expectancy not raw
    win rate — a bot that wins 15% but wins big has positive expectancy.
    """
    equity_dollars = max(50, total_equity_cents / 100)

    BASELINE_ORDER_SIZE = 10
    BASELINE_MAX_INVENTORY = 50

    raw_order_size = int(equity_dollars * 0.01 / 0.50)
    target_order_size = max(3, min(500, raw_order_size))
    target_max_inventory = max(15, min(2500, target_order_size * 5))

    confidence = 0.0
    recent_wr = 0.0
    recent_n = 0
    avg_pnl = 0.0
    if conn is not None:
        try:
            row = conn.execute("""
                SELECT COUNT(*) as n, COALESCE(SUM(won), 0) as wins,
                       COALESCE(SUM(profit_cents), 0) as total_pnl
                FROM settlements
                WHERE recorded_at > datetime('now', '-14 days')
            """).fetchone()
            recent_n = row[0] or 0
            recent_wins = row[1] or 0
            total_pnl = row[2] or 0
            if recent_n >= 20:
                recent_wr = recent_wins / recent_n
                avg_pnl = total_pnl / recent_n
                confidence = max(0.0, min(1.0, avg_pnl / 200.0))
        except Exception as e:
            print(f"[sizing] confidence calc failed: {e}")

    dyn_mm_order_size = max(3, int(BASELINE_ORDER_SIZE + confidence * (target_order_size - BASELINE_ORDER_SIZE)))
    dyn_mm_max_inventory = max(15, int(BASELINE_MAX_INVENTORY + confidence * (target_max_inventory - BASELINE_MAX_INVENTORY)))
    dyn_trim_threshold = max(3, int(equity_dollars * 0.003))
    dyn_major_threshold = max(5, int(equity_dollars * 0.005))

    return {
        "mm_order_size": dyn_mm_order_size,
        "mm_max_inventory": dyn_mm_max_inventory,
        "trim_threshold": dyn_trim_threshold,
        "major_threshold": dyn_major_threshold,
        "confidence": round(confidence, 3),
        "recent_wr": round(recent_wr, 3),
        "recent_n": recent_n,
        "avg_pnl_cents": round(avg_pnl, 1),
        "target_order_size": target_order_size,
        "target_max_inventory": target_max_inventory,
    }


GUARDRAILS = {
    "max_config_change_pct": 0.50,
    "require_min_samples": 20,
    "cooldown_hours": 24,
    "max_daily_modifications": 3,
    "stress_gate_threshold": 0.8,
    "auto_revert_after_n_losses": 10,
    "protected_params": {
        "DAILY_LOSS_LIMIT", "MAX_DRAWDOWN", "MM_MAX_INVENTORY",
        "MAX_CONTRACTS", "PHASE_CONFIG",
    },
    "require_human_review_for": {
        "new_strategy", "new_source", "risk_limit_increase",
    },
}
