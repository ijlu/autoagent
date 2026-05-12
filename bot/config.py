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

# Cross-bracket portfolio live-trading gate (separate from MM).
# Default false — cross-bracket runs in shadow-log mode unless this
# env is true AND the per-family kv `cross_bracket_live:<family>` is
# also truthy. Both checks must pass; either alone keeps the family
# in shadow. Backtest validated 96-100% WR at TTE 4-6h pre-settle
# on 24 firings (n=24 is small — start with 1 family canary mode).
CROSS_BRACKET_LIVE = os.environ.get("CROSS_BRACKET_LIVE", "false").lower() in ("true", "1", "yes")

# Cross-bracket safety rails. Hard caps independent of phase config —
# even if the phase ramps to higher position size, cross-bracket
# specifically is bounded by these knobs while we accumulate live data.
CROSS_BRACKET_MAX_CONTRACTS_PER_LEG = int(
    os.environ.get("CROSS_BRACKET_MAX_CONTRACTS_PER_LEG", "1")
)
CROSS_BRACKET_MAX_LEGS_PER_PORTFOLIO = int(
    os.environ.get("CROSS_BRACKET_MAX_LEGS_PER_PORTFOLIO", "4")
)
# Daily exposure cap (cents) — sum of (contracts × price) across all
# cross-bracket fills today. Conservative $5/day to start; ramp once
# we see live alpha holds.
CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS = int(
    os.environ.get("CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS", "500")
)
# TTE backstop bounds. As of 2026-05-06 (Phase 3e cleanup) the primary
# entry filter is the per-city LST+stability gate
# (bot.learning.cross_bracket_lst_gate.is_post_peak_safe), NOT TTE.
# These values are belt-and-suspenders only:
#   - MIN 0.5h: don't post orders that arrive within 30min of settle
#     (Kalshi may stop accepting + settlement timing edge cases).
#   - MAX 24h: skip next-day-or-later tickers — but the LST gate
#     ``cur_lst_date == target_lst_date`` check already enforces this
#     more precisely. Wide ceiling avoids suppressing legitimate
#     marine-layer-city opportunities (e.g., LAX always_arm@LST 14 =
#     TTE ~9h, below the old 7h cap).
CROSS_BRACKET_MIN_TTE_HOURS = float(
    os.environ.get("CROSS_BRACKET_MIN_TTE_HOURS", "0.5")
)
CROSS_BRACKET_MAX_TTE_HOURS = float(
    os.environ.get("CROSS_BRACKET_MAX_TTE_HOURS", "24.0")
)
# Edge floor (we already gate at 0.07 in score_market_portfolio, but
# the live path can be tightened further). Higher than shadow's gate
# = more conservative live behavior.
CROSS_BRACKET_LIVE_MIN_EDGE = float(
    os.environ.get("CROSS_BRACKET_LIVE_MIN_EDGE", "0.10")
)
# Slippage tolerance: when posting a live cross-bracket order, the limit
# price is capped at ``best_ask + this``. With Kalshi's 1¢ tick, +2¢
# means we'll accept walking up at most 2 levels past best ask before
# the rest of the order rests as a limit (and is silently NOT filled if
# the book stays past that). Larger = more execution certainty, more
# slippage. Smaller = tighter pricing, more abandoned orders. 2¢ matches
# the existing trade.py slip tolerance.
CROSS_BRACKET_SLIP_TOLERANCE_CENTS = int(
    os.environ.get("CROSS_BRACKET_SLIP_TOLERANCE_CENTS", "2")
)
# Permanent per-family cross-bracket blocklist (env-overridable).
# Distinct from the kv-based ``cross_bracket_live:<family>`` toggle:
# the kv path is for canary rollout / temporary pauses, while this
# blocklist is for families with known structural problems that
# warrant a hard block until the underlying issue is fixed.
#
# 2026-05-12 audit: KXHIGHDEN's combined Gaussian σ is 1.4–4.2°F
# while actual day-to-day high RMSE was 11–12°F across 5 directional
# losses (≥3σ events on 40% of days). All 5 KXHIGHDEN directional
# bets resolved against us. Until σ inflation lands, block here.
# KXHIGHDEN is already in DIRECTIONAL_BLOCKLIST and MM_BLOCKED_SERIES
# for the same reason; the cross-bracket blocklist closes the loop.
_DEFAULT_CROSS_BRACKET_BLOCKLIST = "KXHIGHDEN"
CROSS_BRACKET_BLOCKLIST: frozenset[str] = frozenset(
    fam.strip().upper()
    for fam in os.environ.get(
        "CROSS_BRACKET_BLOCKLIST", _DEFAULT_CROSS_BRACKET_BLOCKLIST
    ).split(",")
    if fam.strip()
)
# Per-family σ floor used at cross-bracket scoring time. Sourced from
# the 2026-05-12 audit's RMS-of-residuals analysis
# (tools/sigma_residuals.py): the combined-Gaussian σ post-peak
# collapses to ~1°F (the physical floor) but empirical actual-vs-
# predicted RMSE across each family was wider:
#
#     LAX 1.34 °F   MIA 1.73 °F   NY 1.86 °F
#     AUS 1.95 °F   CHI 2.39 °F   DEN 6.55 °F (blocked)
#
# Setting σ floor = empirical RMSE (rounded up) makes ``p_yes`` for
# bracket-center brackets ~match the empirical hit rate. Without
# this, the model assigns near-0% to any bracket >1°F from μ and
# cross_bracket fires aggressive NO bets that systematically lose
# (Phase B finding: 0/29 directional NO bets resolved against us).
#
# Override per family via env: ``CROSS_BRACKET_SIGMA_FLOOR_KXHIGHNY=2.5``.
# Default to 1.0°F (the existing physical floor) for any family not
# in the table.
_DEFAULT_FAMILY_SIGMA_FLOORS = {
    "KXHIGHLAX": 1.5,
    "KXHIGHMIA": 2.0,
    "KXHIGHNY":  2.0,
    "KXHIGHAUS": 2.0,
    "KXHIGHCHI": 2.5,
    # KXHIGHDEN is hard-blocked above; floor would need to be 6.5
    # to be calibrated. Keep at default and rely on blocklist.
}


def _resolve_family_sigma_floor(family: str) -> float:
    env_key = f"CROSS_BRACKET_SIGMA_FLOOR_{family.upper()}"
    if env_key in os.environ:
        try:
            return float(os.environ[env_key])
        except ValueError:
            pass
    return _DEFAULT_FAMILY_SIGMA_FLOORS.get(family.upper(), 1.0)


CROSS_BRACKET_FAMILY_SIGMA_FLOORS: dict[str, float] = {
    fam: _resolve_family_sigma_floor(fam)
    for fam in (
        "KXHIGHLAX", "KXHIGHMIA", "KXHIGHNY",
        "KXHIGHAUS", "KXHIGHCHI", "KXHIGHDEN",
    )
}
# A6: route WeatherQuoter fair-value through `weather_ensemble_v2.predict_v2` instead
# of the v1 METAR-only logistic CDF. Shadow-first: flag toggles the FV path, live
# posting is still gated by WEATHER_MM_LIVE. Falls back to v1 on v2 errors / None.
WEATHER_ENSEMBLE_V2 = os.environ.get("WEATHER_ENSEMBLE_V2", "false").lower() in ("true", "1", "yes")

# Stage 1 (regime conditioning, 2026-04-28) — when true, METAR's residual σ
# lookup walks (station, hour, regime) → (station, regime) → (station, hour
# pooled) → schedule. Default false: snapshot rows still capture both the
# regime-σ-that-would-have-been-used and pooled-σ-that-was-used for offline
# Brier comparison, so we accumulate the longitudinal dataset Stage 2's
# promotion gate needs without changing live behavior.
WEATHER_REGIME_SIGMA = os.environ.get("WEATHER_REGIME_SIGMA", "false").lower() in ("true", "1", "yes")

# 2026-05-12 F.4: shadow-first toggle for the running-high-only μ path
# in metar_observations.get_metar_gaussian. When false (default), the
# live Gaussian is unchanged but a parallel ``metar_running_only`` row
# is emitted to weather_forecast_snapshots for offline comparison
# against the live ``metar`` row. When true, the live Gaussian uses
# μ = running_high directly (NWP-contamination removed from the METAR
# source channel). Flip to true only after shadow data shows the alt
# is better-calibrated; this single flag changes the bot's weather
# decision math everywhere combine_gaussian runs.
WEATHER_METAR_USE_RUNNING_HIGH_ONLY = os.environ.get(
    "WEATHER_METAR_USE_RUNNING_HIGH_ONLY", "false"
).lower() in ("true", "1", "yes")

# Platt calibration application gate. Default false (2026-04-27 audit):
# the persisted Platt curve was fit overwhelmingly on weather rows from the
# broken v1 ensemble path (8509/8815 rows = 96.5%) and degenerated into a
# step function at p=0.28 — destroys signal when applied to non-weather
# predictions too. The fitter keeps running (data accumulates); the
# applier is gated until per-category fits land. Flip on when retrained
# per-category with ≥200 settled rows per family from the v2 path.
CALIBRATION_ENABLED = os.environ.get("CALIBRATION_ENABLED", "false").lower() in ("true", "1", "yes")

# Phase 2 item 2 (2026-05-10) — wire predict_v2 output through Platt at the
# ensemble.py integration boundary, so all sources (v2, family-router,
# generic ensemble) flow through one uniform calibration step. See
# memory/project_layer_separation_model_vs_trading.md.
#
# Default false: the persisted curve (current state: weather families
# uniformly A=0.5, B in [-1.13, -0.66]) aggressively shrinks weather
# predictions toward ~24% — a p=0.85 raw v2 prediction → ~0.43 after
# Platt. That shrinkage was correct for the overconfident v1 ensemble.
# Whether it remains appropriate after Phase 1's σ-inflation fix is
# unverified empirically. Refit calibration on v2-only data before
# flipping this flag (part of item 1 territory).
#
# When false (default), the v2 short-circuit returns raw clamped prob
# (current behavior, no regression). When true, output flows through
# apply_calibration_correction respecting CALIBRATION_ENABLED.
WEATHER_V2_PLATT_ENABLED = os.environ.get("WEATHER_V2_PLATT_ENABLED", "false").lower() in ("true", "1", "yes")

# 2026-05-04: weather-only mode. When true, the scan / score / log paths
# all skip non-weather families. Effects:
#   - TRADE_SERIES_ALLOWLIST narrows to KXHIGH* only (no KXFED, KXBTC, etc.)
#   - SC_ENABLED is forced off (Safe Compounder is non-weather)
#   - cross-bracket strategy is unaffected (already weather-only)
# Re-enable non-weather families by flipping this off and (separately)
# resolving the per-family signal issues that put them in the blocklist.
WEATHER_ONLY_MODE = os.environ.get("WEATHER_ONLY_MODE", "false").lower() in ("true", "1", "yes")

# Directional families hard-blocked from trading regardless of
# per-family shadow-to-live flags. KXBTC/KXETH have catastrophic raw Brier
# (0.76–0.94 in Phase 0). KXHIGHDEN was historically pathological but the
# 2026-05-04 station-mapping fix may have resolved this — we leave it in the
# blocklist for now and re-evaluate after the weather-only mode collects
# clean post-fix data. Env override: DIRECTIONAL_BLOCKLIST="..." (comma-sep).
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
_WEATHER_SERIES: tuple[str, ...] = (
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHAUS", "KXHIGHMIA", "KXHIGHDEN",
)
if WEATHER_ONLY_MODE:
    TRADE_SERIES_ALLOWLIST: tuple[str, ...] = _WEATHER_SERIES
else:
    TRADE_SERIES_ALLOWLIST = (
        *_WEATHER_SERIES,
        # Macro
        "KXFED", "KXJOB", "KXGDP", "KXCPI",
        # Crypto (shadow-eval only via DIRECTIONAL_BLOCKED_FAMILIES)
        "KXBTC", "KXETH",
    )

# Series the market_snapshotter poller persists to kalshi_market_snapshots.
# Superset of _WEATHER_SERIES — includes city-expansion candidates from
# reports/SESSION_HANDOFF_CITY_EXPANSION_2026-05-06.md §6 so we accumulate
# bid/ask + capacity history *before* a city is promoted to live trading.
# Layer 2 of the city-expansion framework requires ≥14 days of this data
# per candidate before the per-city scorecard runs.
#
# Maintenance: when bot/daemon/series_discovery.py alerts on a new weather
# series, add it here. Auto-add deferred per existing "alert-only" intent.
WEATHER_SNAPSHOT_SERIES: tuple[str, ...] = (
    *_WEATHER_SERIES,
    # Candidates (handoff §6 recommended order)
    "KXHIGHPHX", "KXHIGHDFW", "KXHIGHPHL", "KXHIGHDTW",
    "KXHIGHHOU", "KXHIGHATL",
    "KXHIGHSEA", "KXHIGHBOS", "KXHIGHSFO", "KXHIGHLAS",
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
_SC_ENV = os.environ.get("SC_ENABLED", "true").lower() in ("true", "1", "yes")
# Weather-only mode forces SC off — Safe Compounder targets non-weather YES<20¢
# markets and shouldn't trade while we're focused on weather.
SC_ENABLED = _SC_ENV and not WEATHER_ONLY_MODE
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
# for SHADOW → CANARY. 2026-04-26: relaxed 2.0¢ → 0.5¢ to compress the
# 4-week go-live ETA. 0.5¢ is still strictly above the maker-fee floor
# (~0.44¢ at typical fill price), so a family that earns this is at least
# fee-positive in shadow — we are NOT canarying negative-EV exposure, just
# tolerating a thinner edge buffer. The downstream CANARY → FULL gate
# (MM_GRADUATION_MIN_PNL_RATIO=0.5 over MM_GRADUATION_MIN_PAIRED_N pairs)
# is the real safety net here. Setting this = 0 would still let pre-fix
# spurious-fill bugs sneak through (the Apr-17 _safe_cents bug generated
# 700+ fills with near-zero P&L), so keep the strict-positive floor.
MM_CANARY_MIN_PNL_PER_FILL_CENTS = float(
    os.environ.get("MM_CANARY_MIN_PNL_PER_FILL_CENTS", "0.5"))

# Minimum paired (live, shadow) row count accumulated since entering CANARY
# before graduation is evaluated. 2026-04-26: relaxed 30 → 8 to compress
# the canary observation window from ~5 settlement days to ~1.5 days at
# typical canary cadence per family. With cohort-1 (MIA/AUS/LAX) running
# concurrently in distinct synoptic regimes, 8 paired rows per family
# still suppresses pure-luck false positives via the joint MIN_PNL_RATIO
# check — graduation requires sum(live)/sum(shadow) >= 0.5, which a
# one-day fluke can't fake across 8 fills.
MM_GRADUATION_MIN_PAIRED_N = int(
    os.environ.get("MM_GRADUATION_MIN_PAIRED_N", "8"))

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
# Open-Meteo commercial API key. When set, all Open-Meteo callers
# route through the commercial endpoint (customer-api.open-meteo.com)
# and include the key as `?apikey=...`. When empty, callers fall
# through to the free non-commercial endpoint (api.open-meteo.com)
# which has 10K calls/day, 5K/hour, 600/min, 300K/month limits.
#
# Trading is technically commercial use of Open-Meteo; the free tier
# is non-commercial only. Once we go live we should be on the paid
# tier for both compliance and to eliminate the 429 throttle that
# affects 7 of our 10 ensemble sources at peak load.
#
# Sign-up: https://open-meteo.com/en/pricing → Standard or Professional.
# Set the key in `.env` on the VPS (chmod 600), restart the daemon.
OPENMETEO_API_KEY = os.environ.get("OPENMETEO_API_KEY", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
BLS_API_KEY = os.environ.get("BLS_API_KEY", "")
BEA_API_KEY = os.environ.get("BEA_API_KEY", "")
CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY", "")
EIA_API_KEY = os.environ.get("EIA_API_KEY", "")
# 2026-04-26: Tomorrow.io dropped from the ensemble. Their TOS forbids storing
# the unaltered Datafeed beyond the contract Term and contains a broad
# competitive-product clause; their "Historical" endpoint returns reanalysis,
# not as-issued forecasts (so it can't backfill calibration anyway). Force
# the API key to empty regardless of env so every code path that reads it
# (weather.py:get_tomorrow_forecast, trade.py:get_tomorrow_forecast) returns
# None and never makes a live call. Re-enable would be a deliberate code
# change here, not just an env flip.
TOMORROW_API_KEY = ""  # noqa: was os.environ.get("TOMORROW_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
SENSORTOWER_TOKEN = os.environ.get("SENSORTOWER_API_TOKEN", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
METACULUS_API_TOKEN = os.environ.get("METACULUS_API_TOKEN", "")

# ══════════════════════════════════════════════════════════════════════════════
# Source weights (default priors — adaptive learning updates these)
# ══════════════════════════════════════════════════════════════════════════════
SOURCE_WEIGHTS = {
    "polymarket": 0.75, "odds": 0.85, "weather": 0.80,
    "tomorrow": 0.0,  # 2026-04-26: dropped (see TOMORROW_API_KEY note); weight=0 belt+suspenders

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
