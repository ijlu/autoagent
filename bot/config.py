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
# A6: route WeatherQuoter fair-value through `weather_ensemble_v2.predict_v2` instead
# of the v1 METAR-only logistic CDF. Shadow-first: flag toggles the FV path, live
# posting is still gated by WEATHER_MM_LIVE. Falls back to v1 on v2 errors / None.
WEATHER_ENSEMBLE_V2 = os.environ.get("WEATHER_ENSEMBLE_V2", "false").lower() in ("true", "1", "yes")

# Directional families hard-blocked from trading regardless of
# per-family shadow-to-live flags. Anti-calibrated in Phase 0 backtests:
# KXBTC / KXETH (Brier 0.76–0.94) and KXHIGHDEN (0.316 vs 0.244 baseline).
# Env override: DIRECTIONAL_BLOCKLIST="KXBTC,KXETH,KXHIGHDEN,KXFOO" (comma-sep).
_DEFAULT_DIRECTIONAL_BLOCKLIST = "KXBTC,KXETH,KXHIGHDEN"
DIRECTIONAL_BLOCKED_FAMILIES: frozenset[str] = frozenset(
    f.strip().upper()
    for f in os.environ.get(
        "DIRECTIONAL_BLOCKLIST", _DEFAULT_DIRECTIONAL_BLOCKLIST
    ).split(",")
    if f.strip()
)

# Series the cycle scanner explicitly enumerates via `?series_ticker=<X>`.
# Why this exists: as of 2026-04-25 Kalshi's unfiltered `/markets?status=open`
# response is ~99% KXMVE parlay legs (50K rows), and the first 5K pages —
# all the scanner could reach — contained zero non-parlay markets. Switching
# to a per-series fetch deterministically pulls the markets we have ensemble
# signal for, in ~12 small API calls. Discovery of new tradeable series is
# handled separately by `bot/daemon/series_discovery.py` (alert-only, daily).
#
# Membership rule: only series with a registered family_router (weather via
# stations.py, KXJOB/GDP/CPI via family_routers.py) or that passed Phase 0
# gating (KXFED). Crypto stays in for shadow-mode evaluation despite the
# directional blocklist — the alpha_backtest log needs the rows so calibration
# can keep tracking the families we'd un-block once signal improves.
TRADE_SERIES_ALLOWLIST: tuple[str, ...] = (
    # Weather (one per WeatherStation in bot/daemon/stations.py)
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHAUS", "KXHIGHMIA", "KXHIGHDEN",
    # Macro
    "KXFED", "KXJOB", "KXGDP", "KXCPI",
    # Crypto (shadow-eval only via DIRECTIONAL_BLOCKED_FAMILIES)
    "KXBTC", "KXETH",
)
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

# Per-family cap as a fraction of total equity. Kalshi families (KXFED-26MAY,
# KXFED-26AUG, KXFED-26SEP) are correlated bets on the same underlying —
# Kelly-by-market treats them as independent, which is how the Apr 17 book
# ended up 95% KXFED. This cap limits aggregate correlated exposure without
# touching per-market Kelly sizing on uncorrelated bets.
MAX_FAMILY_EXPOSURE_RATIO = float(os.environ.get("MAX_FAMILY_EXPOSURE_RATIO", "0.25"))

# Per-settlement-event cap. Every KXFED-27APR-T* strike resolves on the
# same FOMC date (55 brackets, one risk). Family-cap pools KXFED-27APR
# with KXFED-26OCT — a 7.5% per-expiry cap separates them so one surprise
# decision can't compound. Apr 17 diagnostic: 27APR was at 9.1% equity.
MAX_EXPIRY_EXPOSURE_RATIO = float(os.environ.get("MAX_EXPIRY_EXPOSURE_RATIO", "0.075"))

# ══════════════════════════════════════════════════════════════════════════════
# Directional exit model (edge-decay anchor + backstops)
# ══════════════════════════════════════════════════════════════════════════════
# Exit when remaining_edge drops below entry_edge × this ratio. 0.33 holds
# positions with ≥⅓ of original edge. Lower = more patient; higher = exits
# sooner as edge erodes. See bot/core/exit_model.py for the evaluator.
EXIT_EDGE_DECAY_RATIO = float(os.environ.get("EXIT_EDGE_DECAY_RATIO", "0.33"))

# Near-expiry + no-conviction backstop. Window (hours) inside which we exit
# if |remaining_edge| is below the edge threshold. Handles "ensemble froze,
# signal never updated as info arrived."
EXIT_TIME_BACKSTOP_HOURS = float(os.environ.get("EXIT_TIME_BACKSTOP_HOURS", "0.25"))
EXIT_TIME_BACKSTOP_EDGE_ABS = float(os.environ.get("EXIT_TIME_BACKSTOP_EDGE_ABS", "0.02"))

# Stale-hold backstop. Hours held after which a flat-or-deteriorating
# position gets closed even if edge technically remains. Catches positions
# stuck in a frozen-ensemble regime.
EXIT_STALE_HOLD_HOURS = float(os.environ.get("EXIT_STALE_HOLD_HOURS", "24.0"))

# ══════════════════════════════════════════════════════════════════════════════
# MM Thompson-sampled sizing (replaces SHADOW/CANARY/FULL step gate)
# ══════════════════════════════════════════════════════════════════════════════
# Per-fill P&L (cents, net of maker fees) that maps to the cap multiplier.
# A series realizing ≥ this much per fill in expectation gets sized at full
# `MM_ORDER_SIZE`. Phase 0 favorable markout was +4.7¢; we target 2¢ realized
# after fill-matching losses and maker fees — conservative relative to that
# ceiling. See bot/core/sizing.py for the evaluator.
MM_SIZING_TARGET_EDGE_CENTS = float(
    os.environ.get("MM_SIZING_TARGET_EDGE_CENTS", "2.0"))

# Upper clamp on the sizing multiplier. 1.0 = configured MM_ORDER_SIZE; values
# >1 would over-size relative to the equity-scaled baseline. Keep at 1.0 until
# we have evidence that a series reliably clears the target edge.
MM_SIZING_CAP_MULTIPLIER = float(
    os.environ.get("MM_SIZING_CAP_MULTIPLIER", "1.0"))

# Minimum settled filled rows required to sample from the posterior. Below
# this, multiplier = 0 (equivalent to SHADOW). 15 fills is enough to bound
# the canary blowup tail (~$40/family/day at 0.5× sizing) before the next
# decision interval, without stalling ramp-up — at typical weather-MM
# cadence ~5 days from cohort launch to first canary eval.
MM_SIZING_MIN_N = int(os.environ.get("MM_SIZING_MIN_N", "15"))

# Cache TTL for the sampled multiplier. Between sweeps the quoter reads a
# stable value; on cache miss the next call re-samples from the posterior.
# 300s keeps within-cycle quote bursts stable while letting posterior drift
# in at reasonable cadence.
MM_SIZING_CACHE_TTL_S = int(os.environ.get("MM_SIZING_CACHE_TTL_S", "300"))

# ── Graduated SHADOW → CANARY → FULL promotion gate (2026-04-21) ──────────────
# Plan B+D from the post-data-bug review: block promotion on unprofitable
# shadow P&L (step B), and require paired live-vs-shadow agreement during
# a canary phase before scaling to full Thompson sizing (step D).

# Minimum realized per-fill shadow P&L (cents, net of maker fees) required
# for SHADOW → CANARY. 2¢ matches the FULL-state target edge (see
# MM_SIZING_TARGET_EDGE_CENTS) — at canary 0.5× sizing on $982 equity
# (~10 contracts/fill), 2¢ covers the maker-fee-plus-slippage floor on a
# bad day. A lower floor (e.g. 1¢) would canary families that don't clear
# fees, burning the canary budget on negative-EV exposure. Setting this = 0
# would also let pre-fix spurious-fill bugs sneak through (the Apr-17
# _safe_cents bug generated 700+ fills with near-zero P&L).
MM_CANARY_MIN_PNL_PER_FILL_CENTS = float(
    os.environ.get("MM_CANARY_MIN_PNL_PER_FILL_CENTS", "2.0"))

# Minimum paired (live, shadow) row count accumulated since entering CANARY
# before graduation is evaluated. 30 paired rows gives ~5 settlement days at
# typical canary-cadence per family, enough to suppress one-lucky-run false
# positives while still letting a clean family graduate inside a week.
MM_GRADUATION_MIN_PAIRED_N = int(
    os.environ.get("MM_GRADUATION_MIN_PAIRED_N", "30"))

# CANARY → FULL requires sum(live_pnl) / sum(shadow_pnl) >= this ratio over
# the paired-row window. 0.5 = live captures at least half the P&L the
# shadow model predicted. Below that, the shadow model is over-predicting
# realizable edge and we should not scale up blindly.
MM_GRADUATION_MIN_PNL_RATIO = float(
    os.environ.get("MM_GRADUATION_MIN_PNL_RATIO", "0.5"))

# Per-series MM canary blocklist. Series in this set are pinned to SHADOW
# regardless of whether they pass the canary gate — used to stagger cohort
# rollouts (bring up uncorrelated regimes first, hold correlated ones).
# Default cohort-2 holdback: KXHIGHCHI + KXHIGHNY are the continental
# correlated pair (one heat-dome day affects both); KXHIGHDEN's signal Brier
# (0.316) is worse than baseline (0.244) on the Apr 17 backtest, so it stays
# blocked until the underlying source quality is re-investigated. Cohort 1
# (KXHIGHMIA, KXHIGHAUS, KXHIGHLAX) sits in distinct synoptic regimes —
# subtropical, marine layer, continental subtropical — so simultaneous canary
# launch is acceptable concurrent risk.
# Set MM_BLOCKED_SERIES="" to disable the blocklist entirely; comma-separated
# overrides are also accepted (e.g. "KXHIGHNY,KXHIGHDEN").
MM_BLOCKED_SERIES: frozenset[str] = frozenset(
    s.strip().upper()
    for s in os.environ.get(
        "MM_BLOCKED_SERIES", "KXHIGHCHI,KXHIGHNY,KXHIGHDEN"
    ).split(",")
    if s.strip()
)

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
